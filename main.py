import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "eq_helper", ".env"))

from eq_helper.agent import root_agent  # noqa: E402 — must load after env

APP_NAME = "eq_helper"

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

    content = types.Content(
        role="user",
        parts=[types.Part(text=request.message)],
    )

    response_text = ""
    try:
        async with Aclosing(
            runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=content,
            )
        ) as events:
            async for event in events:
                if event.is_final_response() and event.content and event.content.parts:
                    response_text = "".join(
                        part.text for part in event.content.parts if part.text
                    )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
