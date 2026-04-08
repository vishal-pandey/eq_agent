import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel
from temporalio.client import Client as TemporalClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "eq_helper", ".env"))

from eq_helper.agent import root_agent  # noqa: E402 — must load after env
from temporal.models import FollowupCycleInput  # noqa: E402

APP_NAME = "eq_helper"
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
N8N_FOLLOWUP_WEBHOOK_URL = os.environ.get("N8N_FOLLOWUP_WEBHOOK_URL", "")
TASK_QUEUE = "scheduled-http-tasks"

_temporal_client: TemporalClient | None = None


async def _get_temporal_client() -> TemporalClient:
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await TemporalClient.connect(TEMPORAL_HOST)
    return _temporal_client

# ---------------------------------------------------------------------------
# Session storage: PostgreSQL if DATABASE_URL is set, else in-memory
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # Ensure the URL uses the asyncpg driver so SQLAlchemy picks the right dialect
    _db_url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "postgresql+asyncpg+asyncpg" in _db_url:
        _db_url = DATABASE_URL  # was already correct, undo double-replace

    from google.adk.sessions import DatabaseSessionService
    print("🗄️  Using DatabaseSessionService (PostgreSQL)")
    session_service = DatabaseSessionService(db_url=_db_url)
else:
    from google.adk.sessions import InMemorySessionService
    print("⚠️  Using InMemorySessionService (sessions lost on restart)")
    session_service = InMemorySessionService()

runner = Runner(
    app_name=APP_NAME,
    agent=root_agent,
    session_service=session_service,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await runner.close()


app = FastAPI(
    title="EQ Helper — Parenting Coach API",
    description="Text-in / text-out interface to the EQ Helper parenting coach agent.",
    version="0.3.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_id: Optional[str] = "default_user"


class ChatResponse(BaseModel):
    response: str
    session_id: str


class InjectRequest(BaseModel):
    session_id: str
    user_id: Optional[str] = "default_user"
    message: str
    role: Optional[str] = "model"


class InjectResponse(BaseModel):
    status: str
    session_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Send a text message and receive a coaching response.

    The agent may internally schedule follow-up messages via Temporal
    (nudges, check-ins, reminders) which will arrive later through WhatsApp.
    """
    user_id = request.user_id or "default_user"
    session_id = request.session_id or str(uuid.uuid4())

    # Ensure session exists with metadata the tools need
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if not session:
        session = await session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state={"_session_id": session_id, "_user_id": user_id},
        )
        # Kick off the fixed-time follow-up cycle for this new session
        if N8N_FOLLOWUP_WEBHOOK_URL:
            try:
                client = await _get_temporal_client()
                await client.start_workflow(
                    "FollowupCycleWorkflow",
                    FollowupCycleInput(
                        session_id=session_id,
                        user_id=user_id,
                        webhook_url=N8N_FOLLOWUP_WEBHOOK_URL,
                    ),
                    id=f"cycle-{session_id}",
                    task_queue=TASK_QUEUE,
                )
            except Exception:
                pass  # Don't fail the chat request if cycle scheduling fails

    content = types.Content(
        role="user",
        parts=[types.Part(text=request.message)],
    )

    response_parts: list[str] = []
    try:
        async with Aclosing(
            runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=content,
            )
        ) as events:
            async for event in events:
                # Skip partial streaming chunks
                if getattr(event, "partial", False):
                    continue
                # Skip events without content
                if not event.content or not event.content.parts:
                    continue
                # Skip user-authored events
                if event.content.role == "user":
                    continue
                # Extract text parts (ignore function_call / function_response parts)
                text = "".join(
                    part.text for part in event.content.parts if part.text
                )
                if text.strip():
                    response_parts.append(text.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response_text = "\n\n".join(response_parts)

    return ChatResponse(
        response=response_text,
        session_id=session_id,
    )


@app.post("/inject", response_model=InjectResponse)
async def inject(request: InjectRequest) -> InjectResponse:
    """Record a message in the agent's session history.

    Call this when your orchestrator sends a scheduled follow-up (nudge /
    reminder) to the user via WhatsApp. This ensures the agent knows what
    it said so the conversation stays coherent when the user replies.

    - **session_id**: The session to inject into.
    - **message**: The text that was sent.
    - **role**: "model" (agent-sent, default) or "user" (user-sent).
    """
    user_id = request.user_id or "default_user"
    try:
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=request.session_id,
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        event = Event(
            invocation_id=str(uuid.uuid4()),
            author="root_agent" if request.role == "model" else "user",
            content=types.Content(
                role=request.role or "model",
                parts=[types.Part(text=request.message)],
            ),
        )
        await session_service.append_event(session=session, event=event)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return InjectResponse(status="ok", session_id=request.session_id)


class GenerateRequest(BaseModel):
    session_id: str
    user_id: Optional[str] = "default_user"
    category: Literal["task", "protip", "checkin"]


class GenerateResponse(BaseModel):
    response: str
    session_id: str


class ActivityResponse(BaseModel):
    session_id: str
    active: bool  # True if user messaged in last 2.5 hours


# ---------------------------------------------------------------------------
# /start-cycle — launch the recurring follow-up cycle for a session
# ---------------------------------------------------------------------------

@app.post("/start-cycle")
async def start_cycle(session_id: str, user_id: Optional[str] = "default_user") -> dict:
    """Start the fixed-time follow-up cycle (8AM task, 3-hourly protips, 8PM checkin IST).

    Call this once when a new conversation is created. Safe to call again —
    if a cycle is already running for this session it will be a no-op.
    """
    if not N8N_FOLLOWUP_WEBHOOK_URL:
        raise HTTPException(status_code=500, detail="N8N_FOLLOWUP_WEBHOOK_URL not configured")

    workflow_id = f"cycle-{session_id}"
    try:
        client = await _get_temporal_client()
        await client.start_workflow(
            "FollowupCycleWorkflow",
            FollowupCycleInput(
                session_id=session_id,
                user_id=user_id or "default_user",
                webhook_url=N8N_FOLLOWUP_WEBHOOK_URL,
            ),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    except Exception as exc:
        # AlreadyExistsError means cycle is already running — that's fine
        if "already exists" in str(exc).lower():
            return {"status": "already_running", "workflow_id": workflow_id}
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "started", "workflow_id": workflow_id}


# ---------------------------------------------------------------------------
# /activity/{session_id} — check if user was active in last 2.5 hours
# ---------------------------------------------------------------------------

@app.get("/activity/{session_id}", response_model=ActivityResponse)
async def check_activity(session_id: str, user_id: Optional[str] = "default_user") -> ActivityResponse:
    """Return whether the user sent a message in the last 2.5 hours.

    n8n calls this before sending a protip nudge — if active=true, skip the nudge.
    """
    uid = user_id or "default_user"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)

    try:
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=uid, session_id=session_id
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        active = False
        for event in reversed(session.events or []):
            ts = getattr(event, "timestamp", None)
            if ts is None:
                continue
            # timestamp may be a float (unix) or datetime
            if isinstance(ts, (int, float)):
                event_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                event_dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

            if event_dt < cutoff:
                break  # events are ordered; no need to go further back

            # Check if this is a user-authored event with text
            if (
                event.content
                and event.content.role == "user"
                and event.content.parts
                and any(p.text for p in event.content.parts if p.text)
            ):
                active = True
                break

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ActivityResponse(session_id=session_id, active=active)


# ---------------------------------------------------------------------------
# /generate — ask the agent to produce a follow-up message for a session
# ---------------------------------------------------------------------------

_GENERATE_PROMPTS = {
    "task": (
        "It is 8AM. Based on what you know about this parent's situation, "
        "generate today's task. Keep it short, specific, "
        "and actionable. Do not ask questions — just give the task warmly."
    ),
    "protip": (
        "Share one quick, practical pro tip about the problem or child "
        "development that's relevant to this parent's situation. "
        "2 sentences max. Friendly text tone, not a lecture."
    ),
    "checkin": (
        "It is 8PM. Check in warmly about today's task — ask if they managed to "
        "try it and how it went. One question only."
    ),
}


@app.post("/generate", response_model=GenerateResponse)
async def generate(request: GenerateRequest) -> GenerateResponse:
    """Generate a follow-up message using the session's conversation history.

    Runs the agent against a temporary session that mirrors the real session's
    history, so the nudge prompt is never persisted to the actual conversation.
    n8n should call /inject after sending the response to keep history coherent.
    """
    from google.adk.sessions import InMemorySessionService
    import copy

    uid = request.user_id or "default_user"
    nudge = _GENERATE_PROMPTS[request.category]

    # Load the real session to get conversation history + state
    real_session = await session_service.get_session(
        app_name=APP_NAME, user_id=uid, session_id=request.session_id
    )
    if not real_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Create a throw-away in-memory session with the same history and state
    temp_service = InMemorySessionService()
    temp_session_id = f"tmp-{uuid.uuid4().hex}"
    temp_session = await temp_service.create_session(
        app_name=APP_NAME,
        user_id=uid,
        session_id=temp_session_id,
        state=copy.deepcopy(dict(real_session.state or {})),
    )
    # Copy real conversation events into the temp session
    for event in (real_session.events or []):
        await temp_service.append_event(session=temp_session, event=event)

    temp_runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=temp_service,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text=nudge)],
    )

    response_parts: list[str] = []
    try:
        async with Aclosing(
            temp_runner.run_async(
                user_id=uid,
                session_id=temp_session_id,
                new_message=content,
            )
        ) as events:
            async for event in events:
                if getattr(event, "partial", False):
                    continue
                if not event.content or not event.content.parts:
                    continue
                if event.content.role == "user":
                    continue
                text = "".join(p.text for p in event.content.parts if p.text)
                if text.strip():
                    response_parts.append(text.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await temp_runner.close()

    response_text = "\n\n".join(response_parts)

    # Inject the generated response into the real session so history stays coherent
    if response_text.strip():
        inject_event = Event(
            invocation_id=str(uuid.uuid4()),
            author="root_agent",
            content=types.Content(
                role="model",
                parts=[types.Part(text=response_text)],
            ),
        )
        await session_service.append_event(session=real_session, event=inject_event)

    return GenerateResponse(
        response=response_text,
        session_id=request.session_id,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
