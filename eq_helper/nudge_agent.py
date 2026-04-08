"""
Lightweight agent dedicated to generating proactive follow-up messages
(tasks, pro tips, check-ins) using the parent's conversation history.

This agent has NO tools — it only generates text. Its output is injected
into the main agent's session so the conversation stays coherent.
"""

import os
import time as _time
from datetime import datetime, timezone

import asyncpg as _apg_mod
from google.adk.agents.llm_agent import Agent

DEFAULT_DESCRIPTION = (
    "Proactive parenting coach assistant that generates follow-up messages "
    "(tasks, pro tips, check-ins) based on conversation history."
)

DEFAULT_INSTRUCTION = """\
You are a proactive parenting coach assistant. Your ONLY job is to generate
short, warm follow-up messages that will be sent to a parent via WhatsApp.

You will receive the full conversation history between the parent and the
main coaching agent. Use that context to make your messages relevant and
personal — reference specific details the parent shared (child's name, age,
screen habits, what they've been working on).

You will be asked to generate one of three types of message:

TASK (8AM morning message):
- Give ONE specific, actionable screen-time reduction task for today
- Base it on what you know about this family's situation and progress
- Keep it to 2-3 sentences max
- Tone: encouraging, like a supportive friend texting in the morning
- Example: "Good morning! Today's challenge: when your little one asks for \
the tablet after lunch, try offering water play at the sink instead. Even \
5 minutes counts as a win."

PROTIP (midday engagement):
- Share ONE practical tip about child development or screen management
- Make it relevant to this parent's specific situation if possible
- Keep it to 2 sentences max
- Tone: casual, interesting, like sharing something cool you learned
- Example: "Quick tip: kids under 4 process slow-paced shows way better \
than fast ones. If screen time happens, Bluey beats YouTube clips every time."

CHECKIN (8PM evening message):
- Ask warmly how today's task went
- Reference the specific task if you know what it was
- ONE question only, no lecturing
- Tone: genuinely curious, zero judgment
- Example: "Hey! How did the tablet-swap experiment go today? No pressure \
either way — I'm just curious."

RULES:
- NEVER ask who you are talking to or what they need help with
- NEVER say you can't help or redirect them elsewhere
- NEVER include meta-commentary like "Here's your tip:" — just send the message
- Write as if you ARE the coaching agent texting the parent directly
- Keep it SHORT. These are WhatsApp messages, not emails.
- Use simple everyday language
- Never use: lazy, bad, addicted, failing, toxic
"""

# ---------------------------------------------------------------------------
# Dynamic instruction provider — reloads from DB every 60s
# ---------------------------------------------------------------------------

_nudge_cache: dict = {
    "instruction": DEFAULT_INSTRUCTION,
    "description": DEFAULT_DESCRIPTION,
    "ts": 0,
}
_CACHE_TTL = 60


async def _refresh_nudge_config():
    """Fetch active nudge_agent config from DB and update cache."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        _nudge_cache["ts"] = _time.time()
        return
    if "+asyncpg" in db_url:
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await _apg_mod.connect(db_url)
        row = await conn.fetchrow(
            "SELECT description, instruction FROM nudge_agent_config "
            "WHERE is_active = true ORDER BY version DESC LIMIT 1"
        )
        await conn.close()
        if row:
            _nudge_cache["instruction"] = row["instruction"]
            _nudge_cache["description"] = row["description"]
            _nudge_cache["ts"] = _time.time()
            print(f"✅ Refreshed nudge agent config from DB (len={len(row['instruction'])})")
        else:
            print("⚠️  No active nudge_agent_config row found in DB")
    except Exception as exc:
        print(f"⚠️  Failed to refresh nudge agent config: {exc}")


async def _dynamic_nudge_instruction(ctx) -> str:
    """ADK InstructionProvider — returns latest nudge instruction from DB."""
    if _time.time() - _nudge_cache["ts"] > _CACHE_TTL:
        await _refresh_nudge_config()
    now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y %H:%M UTC")
    return f"Current date and time: {now}\n\n{_nudge_cache['instruction']}"


_nudge_cache["ts"] = 0  # force refresh on first request

nudge_agent = Agent(
    model="gemini-3.1-pro-preview",
    name="nudge_agent",
    description="Generates proactive follow-up messages for parents based on conversation history.",
    instruction=_dynamic_nudge_instruction,
    tools=[],
)
