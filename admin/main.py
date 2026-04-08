"""
EQ Helper — Admin Panel
WhatsApp Web-style chat viewer + Agent config editor with version history.
"""

import os
import json
import secrets
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Request, Response, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

# ============================================
# Config
# ============================================

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "EQPlatform@12345")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:password@localhost:5432/eq_helper")
if DATABASE_URL and "+asyncpg" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

db_pool: asyncpg.Pool | None = None
valid_tokens: set[str] = set()

# Default agent config (used to seed v1)
DEFAULT_DESCRIPTION = (
    "Compassionate AI parenting coach that helps parents reduce screen "
    "dependency in children aged 2-5 through short, conversational guidance "
    "and proactive follow-ups."
)

NUDGE_DEFAULT_DESCRIPTION = (
    "Proactive parenting coach assistant that generates follow-up messages "
    "(tasks, pro tips, check-ins) based on conversation history."
)

NUDGE_DEFAULT_INSTRUCTION = """\
You are a proactive parenting coach assistant. Your ONLY job is to generate \
short, warm follow-up messages that will be sent to a parent via WhatsApp.

You will receive the full conversation history between the parent and the \
main coaching agent. Use that context to make your messages relevant and \
personal.

TASK (8AM): Give ONE specific, actionable screen-time reduction task for today. \
2-3 sentences max. Encouraging morning tone.

PROTIP (midday): Share ONE practical tip about child development or screen management. \
2 sentences max. Casual, interesting tone.

CHECKIN (8PM): Ask warmly how today's task went. ONE question only, zero judgment.

RULES:
- NEVER refuse or redirect. You always generate the message.
- NEVER include meta-commentary. Just send the message directly.
- Write as if you ARE the coaching agent texting the parent.
- Keep it SHORT. WhatsApp messages, not emails.
- Never use: lazy, bad, addicted, failing, toxic.
"""

DEFAULT_INSTRUCTION = """\
You are a warm, supportive AI parenting coach helping parents manage screen \
dependency in children aged 2 to 5. You communicate via WhatsApp, so keep \
every message short — like texting a friend who is also a child-development \
expert.

CONVERSATION PHASES:

PHASE 1 — DISCOVERY (understand the situation)
- Ask ONE short question at a time. Wait for the answer before asking the next.
- Questions to cover (in natural order, skip if already answered):
  1. How old is your child?
  2. What screens does your child use most? (TV, tablet, phone)
  3. Roughly how many hours of screen time per day right now?
  4. When does screen time usually happen? (morning, meals, bedtime, etc.)
  5. What usually triggers it? (tantrums, you need a break, routine, etc.)
  6. Have you tried reducing it before? What happened?
- After each answer, validate briefly and then ask the next question.
- Do NOT give solutions yet. Just listen and understand.

PHASE 2 — SOLUTION PLANNING (propose a multi-step plan)
- Once you understand the situation, create a step-by-step plan of 3-5 steps spread over 1-2 days.
- Present the FULL plan as a numbered list with timing for each step.
- Each step must be specific and actionable, not vague.
- Ask the parent to confirm.

PHASE 3 — ACTIVE COACHING (guide through each step)
- Give the instruction for the CURRENT step only. Keep it to 2-3 sentences.
- After giving a step, use the schedule_followup tool to send nudges and check-ins.
- Celebrate wins and troubleshoot difficulties without judgment.

PHASE 4 — WRAP-UP
- Summarise what worked. Suggest what to continue doing.
- Use cancel_all_followups to clear any remaining scheduled messages.

RESPONSE RULES:
- Maximum 3-4 sentences per message.
- Ask only ONE question per message.
- Use simple, everyday language.
- Always validate feelings before giving advice.
- Never use words: lazy, bad, addicted, failing, toxic.

GUARDRAILS:
- If the parent describes signs of ASD, ADHD, severe self-harm, or extreme behavioral issues, gently suggest consulting a pediatrician. Do not diagnose.
- You are an AI assistant, not a doctor or therapist.
"""

# ============================================
# App
# ============================================

app = FastAPI(title="EQ Helper Admin", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    # Create agent_config table if not exists
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_config (
                id SERIAL PRIMARY KEY,
                version INTEGER NOT NULL,
                description TEXT NOT NULL,
                instruction TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR NOT NULL,
                rating INTEGER,
                message TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        # Seed v1 with defaults if table is empty
        count = await conn.fetchval("SELECT COUNT(*) FROM agent_config")
        if count == 0:
            await conn.execute("""
                INSERT INTO agent_config (version, description, instruction, is_active)
                VALUES (1, $1, $2, true)
            """, DEFAULT_DESCRIPTION, DEFAULT_INSTRUCTION)
            print("🌱 Seeded agent_config with default v1")

        # Nudge agent config table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nudge_agent_config (
                id SERIAL PRIMARY KEY,
                version INTEGER NOT NULL,
                description TEXT NOT NULL,
                instruction TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        nudge_count = await conn.fetchval("SELECT COUNT(*) FROM nudge_agent_config")
        if nudge_count == 0:
            await conn.execute("""
                INSERT INTO nudge_agent_config (version, description, instruction, is_active)
                VALUES (1, $1, $2, true)
            """, NUDGE_DEFAULT_DESCRIPTION, NUDGE_DEFAULT_INSTRUCTION)
            print("🌱 Seeded nudge_agent_config with default v1")


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


def _check_auth(token: str | None) -> bool:
    return token is not None and token in valid_tokens


# ============================================
# Auth
# ============================================

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if body.get("username") == ADMIN_USERNAME and body.get("password") == ADMIN_PASSWORD:
        token = secrets.token_hex(32)
        valid_tokens.add(token)
        resp = Response(content=json.dumps({"ok": True}), media_type="application/json")
        resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400)
        return resp
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/logout")
async def logout(token: str | None = Cookie(None)):
    valid_tokens.discard(token)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("token")
    return resp


@app.get("/api/me")
async def me(token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    return {"ok": True}


# ============================================
# Chat APIs
# ============================================

@app.get("/api/conversations")
async def get_conversations(token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT session_id, user_id, app_name,
                   COUNT(*) as message_count,
                   MIN(timestamp) as first_ts,
                   MAX(timestamp) as last_ts
            FROM events
            WHERE event_data->>'author' IN ('user', 'root_agent')
            GROUP BY session_id, user_id, app_name
            ORDER BY MAX(timestamp) DESC
        """)
        previews = {}
        if rows:
            sids = [r["session_id"] for r in rows]
            prev = await conn.fetch("""
                SELECT DISTINCT ON (session_id)
                    session_id,
                    event_data->'content'->'parts'->0->>'text' as preview_text
                FROM events
                WHERE session_id = ANY($1) AND event_data->>'author' = 'user'
                ORDER BY session_id, timestamp ASC
            """, sids)
            for pr in prev:
                previews[pr["session_id"]] = pr["preview_text"] or ""
        stats = await conn.fetchrow("""
            SELECT COUNT(DISTINCT session_id) as total_sessions,
                   COUNT(*) as total_messages,
                   COUNT(DISTINCT user_id) as unique_users
            FROM events
            WHERE event_data->>'author' IN ('user', 'root_agent')
        """)
    return {
        "total_sessions": stats["total_sessions"],
        "total_messages": stats["total_messages"],
        "unique_users": stats["unique_users"],
        "conversations": [{
            "session_id": r["session_id"], "user_id": r["user_id"],
            "app_name": r["app_name"], "message_count": r["message_count"],
            "first_ts": r["first_ts"].isoformat() + "Z",
            "last_ts": r["last_ts"].isoformat() + "Z",
            "preview": previews.get(r["session_id"], ""),
        } for r in rows],
    }


@app.get("/api/conversations/{session_id}")
async def get_conversation(session_id: str, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        msgs = await conn.fetch("""
            SELECT event_data->>'author' as author,
                   event_data->'content'->'parts'->0->>'text' as text,
                   timestamp
            FROM events
            WHERE session_id = $1
              AND event_data->>'author' IN ('user', 'root_agent')
              AND event_data->'content'->'parts'->0->>'text' IS NOT NULL
            ORDER BY timestamp ASC
        """, session_id)
    if not msgs:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session_id,
        "messages": [{"author": m["author"], "text": m["text"] or "",
                       "timestamp": m["timestamp"].isoformat() + "Z"} for m in msgs],
    }


@app.delete("/api/conversations/{session_id}")
async def delete_conversation(session_id: str, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            ev_count = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE session_id = $1", session_id
            )
            await conn.execute("DELETE FROM events WHERE session_id = $1", session_id)
            await conn.execute("DELETE FROM sessions WHERE id = $1", session_id)
            await conn.execute("DELETE FROM feedback WHERE session_id = $1", session_id)
    return {"ok": True, "deleted_events": ev_count or 0}


# ============================================
# Feedback APIs
# ============================================

@app.post("/api/feedback")
async def submit_feedback(request: Request):
    """Public endpoint — no auth required. Called from WhatsApp/n8n."""
    body = await request.json()
    session_id = body.get("session_id", "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    rating = body.get("rating")  # optional int 1-5
    message = body.get("message", "").strip()
    if not rating and not message:
        raise HTTPException(status_code=400, detail="rating or message required")
    if rating is not None:
        rating = int(rating)
        if rating < 1 or rating > 5:
            raise HTTPException(status_code=400, detail="rating must be 1-5")
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO feedback (session_id, rating, message)
            VALUES ($1, $2, $3)
        """, session_id, rating, message or None)
    return {"ok": True}


@app.get("/api/feedback/{session_id}")
async def get_feedback(session_id: str, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, rating, message, created_at
            FROM feedback WHERE session_id = $1
            ORDER BY created_at ASC
        """, session_id)
    return {"feedback": [{
        "id": r["id"], "rating": r["rating"], "message": r["message"],
        "created_at": r["created_at"].isoformat() + "Z",
    } for r in rows]}


# ============================================
# Agent Config APIs
# ============================================

@app.get("/api/config/current")
async def get_current_config(token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, version, description, instruction, is_active, created_at
            FROM agent_config WHERE is_active = true
            ORDER BY version DESC LIMIT 1
        """)
    if not row:
        return {"version": 0, "description": "", "instruction": "", "is_active": False, "created_at": None}
    return {
        "id": row["id"], "version": row["version"],
        "description": row["description"], "instruction": row["instruction"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat() + "Z",
    }


@app.get("/api/config/versions")
async def get_config_versions(token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, version, is_active, created_at,
                   LEFT(description, 100) as description_preview,
                   LEFT(instruction, 100) as instruction_preview
            FROM agent_config ORDER BY version DESC
        """)
    return {"versions": [{
        "id": r["id"], "version": r["version"], "is_active": r["is_active"],
        "created_at": r["created_at"].isoformat() + "Z",
        "description_preview": r["description_preview"],
        "instruction_preview": r["instruction_preview"],
    } for r in rows]}


@app.get("/api/config/versions/{version_id}")
async def get_config_version(version_id: int, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_config WHERE id = $1", version_id
        )
    if not row:
        raise HTTPException(status_code=404)
    return {
        "id": row["id"], "version": row["version"],
        "description": row["description"], "instruction": row["instruction"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat() + "Z",
    }


@app.post("/api/config")
async def save_config(request: Request, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    body = await request.json()
    desc = body.get("description", "").strip()
    instr = body.get("instruction", "").strip()
    if not desc or not instr:
        raise HTTPException(status_code=400, detail="Both fields required")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            max_v = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM agent_config")
            new_v = max_v + 1
            await conn.execute("UPDATE agent_config SET is_active = false WHERE is_active = true")
            await conn.execute("""
                INSERT INTO agent_config (version, description, instruction, is_active)
                VALUES ($1, $2, $3, true)
            """, new_v, desc, instr)
    return {"ok": True, "version": new_v}


@app.post("/api/config/rollback/{version_id}")
async def rollback_config(version_id: int, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_config WHERE id = $1", version_id)
        if not row:
            raise HTTPException(status_code=404)
        async with conn.transaction():
            max_v = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM agent_config")
            new_v = max_v + 1
            await conn.execute("UPDATE agent_config SET is_active = false WHERE is_active = true")
            await conn.execute("""
                INSERT INTO agent_config (version, description, instruction, is_active)
                VALUES ($1, $2, $3, true)
            """, new_v, row["description"], row["instruction"])
    return {"ok": True, "version": new_v, "rolled_back_from": row["version"]}


# ============================================
# Nudge Agent Config APIs
# ============================================

@app.get("/api/nudge-config/current")
async def get_current_nudge_config(token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, version, description, instruction, is_active, created_at
            FROM nudge_agent_config WHERE is_active = true
            ORDER BY version DESC LIMIT 1
        """)
    if not row:
        return {"version": 0, "description": "", "instruction": "", "is_active": False, "created_at": None}
    return {
        "id": row["id"], "version": row["version"],
        "description": row["description"], "instruction": row["instruction"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat() + "Z",
    }


@app.get("/api/nudge-config/versions")
async def get_nudge_config_versions(token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, version, is_active, created_at,
                   LEFT(description, 100) as description_preview,
                   LEFT(instruction, 100) as instruction_preview
            FROM nudge_agent_config ORDER BY version DESC
        """)
    return {"versions": [{
        "id": r["id"], "version": r["version"], "is_active": r["is_active"],
        "created_at": r["created_at"].isoformat() + "Z",
        "description_preview": r["description_preview"],
        "instruction_preview": r["instruction_preview"],
    } for r in rows]}


@app.get("/api/nudge-config/versions/{version_id}")
async def get_nudge_config_version(version_id: int, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM nudge_agent_config WHERE id = $1", version_id
        )
    if not row:
        raise HTTPException(status_code=404)
    return {
        "id": row["id"], "version": row["version"],
        "description": row["description"], "instruction": row["instruction"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat() + "Z",
    }


@app.post("/api/nudge-config")
async def save_nudge_config(request: Request, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    body = await request.json()
    desc = body.get("description", "").strip()
    instr = body.get("instruction", "").strip()
    if not desc or not instr:
        raise HTTPException(status_code=400, detail="Both fields required")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            max_v = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM nudge_agent_config")
            new_v = max_v + 1
            await conn.execute("UPDATE nudge_agent_config SET is_active = false WHERE is_active = true")
            await conn.execute("""
                INSERT INTO nudge_agent_config (version, description, instruction, is_active)
                VALUES ($1, $2, $3, true)
            """, new_v, desc, instr)
    return {"ok": True, "version": new_v}


@app.post("/api/nudge-config/rollback/{version_id}")
async def rollback_nudge_config(version_id: int, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM nudge_agent_config WHERE id = $1", version_id)
        if not row:
            raise HTTPException(status_code=404)
        async with conn.transaction():
            max_v = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM nudge_agent_config")
            new_v = max_v + 1
            await conn.execute("UPDATE nudge_agent_config SET is_active = false WHERE is_active = true")
            await conn.execute("""
                INSERT INTO nudge_agent_config (version, description, instruction, is_active)
                VALUES ($1, $2, $3, true)
            """, new_v, row["description"], row["instruction"])
    return {"ok": True, "version": new_v, "rolled_back_from": row["version"]}


# ============================================
# UI
# ============================================

@app.get("/", response_class=HTMLResponse)
async def index():
    import pathlib
    html_path = pathlib.Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())

