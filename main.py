import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "eq_helper", ".env"))

from eq_helper.agent import root_agent  # noqa: E402 — must load after env

APP_NAME = "eq_helper"

session_service = InMemorySessionService()
runner = Runner(
    app_name=APP_NAME,
    agent=root_agent,
    session_service=session_service,
    auto_create_session=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await runner.close()


app = FastAPI(
    title="EQ Helper — Parenting Coach API",
    description="Text-in / text-out interface to the EQ Helper parenting coach agent.",
    version="0.1.0",
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


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Send a text message and receive a text coaching response.

    - **message**: The parent's question or situation.
    - **session_id**: Pass the `session_id` from a previous response to
      continue the same conversation. Omit to start a fresh session.
    - **user_id**: Optional stable identifier for the user.
    """
    user_id = request.user_id or "default_user"
    session_id = request.session_id or str(uuid.uuid4())

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

    return ChatResponse(response=response_text, session_id=session_id)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
