from dataclasses import dataclass
from typing import Optional


@dataclass
class FollowupCycleInput:
    """Input for starting a recurring follow-up cycle for a user session."""

    session_id: str
    user_id: str
    webhook_url: str  # n8n webhook URL


@dataclass
class ScheduledHttpTask:
    """Input for scheduling an HTTP request at a future time."""

    url: str
    method: str  # GET, POST, PUT, DELETE, etc.
    scheduled_at: str  # ISO 8601 datetime string
    headers: Optional[dict[str, str]] = None
    body: Optional[str] = None
    timeout_seconds: int = 30


@dataclass
class HttpResponse:
    """Result of the HTTP request execution."""

    status_code: int
    body: str
    headers: dict[str, str]
