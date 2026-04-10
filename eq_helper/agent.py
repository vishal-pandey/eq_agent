import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from google.adk.agents.llm_agent import Agent
from google.adk.agents.context import Context
from temporalio.client import Client

from temporal.models import ScheduledHttpTask

# ---------------------------------------------------------------------------
# Temporal config
# ---------------------------------------------------------------------------

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "scheduled-http-tasks"
N8N_FOLLOWUP_WEBHOOK_URL = os.environ.get("N8N_FOLLOWUP_WEBHOOK_URL", "")

_temporal_client: Client | None = None


async def _get_temporal_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await Client.connect(TEMPORAL_HOST)
    return _temporal_client


# ---------------------------------------------------------------------------
# Tool: schedule_followup
# ---------------------------------------------------------------------------

async def schedule_followup(
    message: str,
    delay_minutes: int,
    followup_type: str,
    tool_context: Context,
) -> str:
    """Schedule a proactive follow-up message to send to the parent later via WhatsApp.

    Use this tool whenever you want to check in, send a nudge, or remind the
    parent about something after the current conversation pauses.
    The message will be delivered automatically after the specified delay.

    Args:
        message: The exact message text to send to the parent later.
        delay_minutes: How many minutes from now to send the follow-up.
        followup_type: One of "nudge", "reminder", "checkin", or "encouragement".
    """
    if not N8N_FOLLOWUP_WEBHOOK_URL:
        return "Error: Follow-up webhook URL is not configured. Cannot schedule."

    session_id = tool_context.state.get("_session_id", "")
    user_id = tool_context.state.get("_user_id", "default_user")

    scheduled_at = (
        datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    ).isoformat()

    payload = json.dumps({
        "session_id": session_id,
        "user_id": user_id,
        "message": message,
        "type": followup_type,
    })

    workflow_id = f"followup-{uuid.uuid4().hex[:8]}"

    task = ScheduledHttpTask(
        url=N8N_FOLLOWUP_WEBHOOK_URL,
        method="POST",
        scheduled_at=scheduled_at,
        headers={"Content-Type": "application/json"},
        body=payload,
        timeout_seconds=30,
    )

    try:
        client = await _get_temporal_client()
        await client.start_workflow(
            "ScheduledHttpTaskWorkflow",
            task,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    except Exception as exc:
        return f"Failed to schedule follow-up: {exc}"

    # Track in session state so we can list / cancel later
    scheduled = tool_context.state.get("_scheduled_workflows", [])
    scheduled.append({
        "workflow_id": workflow_id,
        "message": message,
        "type": followup_type,
        "delay_minutes": delay_minutes,
    })
    tool_context.state["_scheduled_workflows"] = scheduled

    return (
        f"Follow-up scheduled (ID: {workflow_id}). "
        f"A '{followup_type}' message will be sent in {delay_minutes} minutes."
    )


# ---------------------------------------------------------------------------
# Tool: cancel_followup
# ---------------------------------------------------------------------------

async def cancel_followup(
    workflow_id: str,
    tool_context: Context,
) -> str:
    """Cancel a previously scheduled follow-up message by its workflow ID.

    Use this when a scheduled follow-up no longer makes sense — for example
    the parent said the problem is resolved, or the situation changed.

    Args:
        workflow_id: The workflow ID returned when the follow-up was scheduled.
    """
    try:
        client = await _get_temporal_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.cancel()
    except Exception as exc:
        return f"Could not cancel {workflow_id}: {exc}"

    # Remove from tracked list
    scheduled = tool_context.state.get("_scheduled_workflows", [])
    scheduled = [s for s in scheduled if s["workflow_id"] != workflow_id]
    tool_context.state["_scheduled_workflows"] = scheduled

    return f"Follow-up {workflow_id} cancelled successfully."


# ---------------------------------------------------------------------------
# Tool: cancel_all_followups
# ---------------------------------------------------------------------------

async def cancel_all_followups(
    tool_context: Context,
) -> str:
    """Cancel ALL pending scheduled follow-up messages.

    Use this when the parent says the problem is fully resolved, or you need
    to clear the slate before creating a new plan.
    """
    scheduled = tool_context.state.get("_scheduled_workflows", [])
    if not scheduled:
        return "No follow-ups to cancel."

    cancelled = []
    failed = []
    try:
        client = await _get_temporal_client()
        for entry in scheduled:
            wid = entry["workflow_id"]
            try:
                handle = client.get_workflow_handle(wid)
                await handle.cancel()
                cancelled.append(wid)
            except Exception:
                failed.append(wid)
    except Exception as exc:
        return f"Could not connect to scheduler: {exc}"

    tool_context.state["_scheduled_workflows"] = []

    parts = [f"Cancelled {len(cancelled)} follow-up(s)."]
    if failed:
        parts.append(
            f"{len(failed)} could not be cancelled (may have already been sent)."
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

DEFAULT_DESCRIPTION = (
    "Compassionate AI parenting coach that helps parents reduce screen "
    "dependency in children aged 2-5 through short, conversational guidance "
    "and proactive follow-ups."
)

DEFAULT_INSTRUCTION = """\
You are a warm, supportive AI parenting coach helping parents manage screen \
dependency in children aged 2 to 5. You communicate via WhatsApp, so keep \
every message short — like texting a friend who is also a child-development \
expert.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONVERSATION PHASES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 1 — DISCOVERY (understand the situation)
• Ask ONE short question at a time. Wait for the answer before asking the next.
• Questions to cover (in natural order, skip if already answered):
  1. How old is your child?
  2. What screens does your child use most? (TV, tablet, phone)
  3. Roughly how many hours of screen time per day right now?
  4. When does screen time usually happen? (morning, meals, bedtime, etc.)
  5. What usually triggers it? (tantrums, you need a break, routine, etc.)
  6. Have you tried reducing it before? What happened?
• After each answer, validate briefly ("That makes sense", "Many parents feel \
the same") and then ask the next question.
• Do NOT give solutions yet. Just listen and understand.

PHASE 2 — SOLUTION PLANNING (propose a multi-step plan)
• Once you understand the situation, create a step-by-step plan of 3-5 steps \
spread over 1-2 days.
• Present the FULL plan as a numbered list with timing for each step, e.g.:
  "Here is what I suggest we try together:
   Step 1 (Today evening): …
   Step 2 (Tomorrow morning): …
   Step 3 (Tomorrow afternoon): …"
• Each step must be specific and actionable, not vague.
• Ask the parent to confirm: "Does this feel doable? Want to adjust anything?"
• Store the agreed plan in your memory for the conversation.

PHASE 3 — ACTIVE COACHING (guide through each step)
• Give the instruction for the CURRENT step only. Keep it to 2-3 sentences.
• After giving a step, use the schedule_followup tool to:
  - Send a "nudge" 15-30 min before a step is due.
  - Send a "checkin" 30-60 min after a step to ask how it went.
  - Send "encouragement" if a difficult moment is expected.
• When the parent reports back, celebrate wins ("That is great progress!") \
and troubleshoot difficulties without judgment.
• Then move to the next step.

PHASE 4 — WRAP-UP
• After all steps are done, summarise what worked.
• Suggest what to continue doing.
• Offer to create a new plan if they want to keep going.
• Use cancel_all_followups to clear any remaining scheduled messages.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Maximum 3-4 sentences per message. Parents are busy.
• Never send walls of text. If you have multiple points, split across messages \
using follow-ups.
• Ask only ONE question per message.
• Use simple, everyday language. Avoid jargon.
• Always validate feelings before giving advice.
• Never use words: lazy, bad, addicted, failing, toxic.
  Use instead: screen reliance, habituation, high-stimulation, overwhelmed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOLLOW-UP TOOLS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCHEDULING (schedule_followup):
You MUST schedule follow-ups during Phase 3. Typical patterns:
• After giving a step → schedule a "checkin" in 60-120 min.
• Before a tricky moment (e.g., dinner screen time) → "nudge" 15 min before.
• After a win → "encouragement" in 30 min to reinforce.
• If parent goes quiet after agreeing to a plan → "checkin" in 180 min.
The message should read naturally, as if you are texting them.
The tool returns a workflow ID — remember it in case you need to cancel later.

CANCELLING (cancel_followup / cancel_all_followups):
• If the parent says the problem is resolved, or the situation has changed \
so that a scheduled follow-up no longer makes sense, cancel it.
• Use cancel_followup with a specific workflow ID to cancel one message.
• Use cancel_all_followups to clear ALL pending messages at once.
• Always cancel outdated follow-ups before scheduling replacements to avoid \
duplicate or contradictory messages.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KNOWLEDGE BASE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• AAP/WHO recommend max 1 hour/day of high-quality, co-viewed content for \
ages 2-5.
• Extinction burst: tantrums get worse before better when removing screens. \
This is normal, not bad parenting.
• Replacement > removal: swap screens with sensory/gross-motor activities \
(water play, building blocks, safe kitchen help, jumping).
• Visual timers and transition warnings reduce meltdowns.
• Slow-paced educational content is better than fast-paced high-dopamine videos.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GUARDRAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• If the parent describes signs of ASD, ADHD, severe self-harm, or extreme \
behavioral issues, gently suggest consulting a pediatrician. Do not diagnose.
• You are an AI assistant, not a doctor or therapist. Be transparent about this \
if asked.
"""


# ---------------------------------------------------------------------------
# Dynamic instruction provider — reloads from DB every 60s
# ---------------------------------------------------------------------------
import time as _time
import asyncpg as _apg_mod

_config_cache: dict = {"instruction": DEFAULT_INSTRUCTION, "description": DEFAULT_DESCRIPTION, "ts": 0, "last_error": None}
_CACHE_TTL = 60  # seconds


async def _refresh_config_cache():
    """Fetch active config from DB and update cache."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        _config_cache["ts"] = _time.time()
        _config_cache["last_error"] = "DATABASE_URL not set"
        return
    if "+asyncpg" in db_url:
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await _apg_mod.connect(db_url)
        row = await conn.fetchrow(
            "SELECT description, instruction FROM agent_config "
            "WHERE is_active = true ORDER BY version DESC LIMIT 1"
        )
        await conn.close()
        if row:
            _config_cache["instruction"] = row["instruction"]
            _config_cache["description"] = row["description"]
            _config_cache["ts"] = _time.time()
            _config_cache["last_error"] = None
            print(f"✅ Refreshed agent config from DB (len={len(row['instruction'])})")
        else:
            _config_cache["last_error"] = "No active agent_config row found"
            print("⚠️  No active agent_config row found in DB")
    except Exception as exc:
        _config_cache["last_error"] = str(exc)
        print(f"⚠️  Failed to refresh agent config: {exc}")


async def _dynamic_instruction(ctx) -> str:
    """ADK InstructionProvider — returns latest instruction from DB."""
    if _time.time() - _config_cache["ts"] > _CACHE_TTL:
        await _refresh_config_cache()
    desc = _config_cache["description"]
    instr = _config_cache["instruction"]
    now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y %H:%M UTC")
    return f"Current date and time: {now}\n\nYou are: {desc}\n\n{instr}"


# Initial cache uses hardcoded defaults; first request triggers async DB refresh
_config_cache["ts"] = 0  # force refresh on first request

root_agent = Agent(
    model='gemini-3.1-pro-preview',
    name='root_agent',
    description="AI assistant powered by dynamic configuration.",
    instruction=_dynamic_instruction,
    tools=[],
)
