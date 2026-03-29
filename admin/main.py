"""
EQ Helper — Admin Chat Viewer
WhatsApp Web-style interface to view all parent conversations.
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

AGENT_AUTHORS = ("user", "root_agent")

db_pool: asyncpg.Pool | None = None

# In-memory session tokens (good enough for a single-admin panel)
valid_tokens: set[str] = set()

# ============================================
# App
# ============================================

app = FastAPI(title="EQ Helper Admin", version="1.0.0")

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


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


# ============================================
# Auth helpers
# ============================================

def _check_auth(token: str | None) -> bool:
    return token is not None and token in valid_tokens


# ============================================
# Login endpoint
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
# Content helpers
# ============================================

def extract_text(content: Any) -> str:
    if not content:
        return ""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return content[:200]
    if isinstance(content, dict):
        if "parts" in content:
            parts = content["parts"]
            if isinstance(parts, list) and parts:
                p = parts[0]
                if isinstance(p, dict) and "text" in p:
                    return p["text"]
                if isinstance(p, str):
                    return p
        if "text" in content:
            return content["text"]
    return str(content)[:200]


# ============================================
# API
# ============================================

@app.get("/api/conversations")
async def get_conversations(token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                session_id,
                user_id,
                app_name,
                COUNT(*) as message_count,
                MIN(timestamp) as first_ts,
                MAX(timestamp) as last_ts
            FROM events
            WHERE event_data->>'author' IN ('user', 'root_agent')
            GROUP BY session_id, user_id, app_name
            ORDER BY MAX(timestamp) DESC
        """)
        # Grab first user message per session for preview
        previews = {}
        if rows:
            session_ids = [r["session_id"] for r in rows]
            preview_rows = await conn.fetch("""
                SELECT DISTINCT ON (session_id)
                    session_id,
                    event_data->'content'->'parts'->0->>'text' as preview_text
                FROM events
                WHERE session_id = ANY($1) AND event_data->>'author' = 'user'
                ORDER BY session_id, timestamp ASC
            """, session_ids)
            for pr in preview_rows:
                previews[pr["session_id"]] = pr["preview_text"] or ""

        stats = await conn.fetchrow("""
            SELECT
                COUNT(DISTINCT session_id) as total_sessions,
                COUNT(*) as total_messages,
                COUNT(DISTINCT user_id) as unique_users
            FROM events
            WHERE event_data->>'author' IN ('user', 'root_agent')
        """)

    return {
        "total_sessions": stats["total_sessions"],
        "total_messages": stats["total_messages"],
        "unique_users": stats["unique_users"],
        "conversations": [
            {
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "app_name": r["app_name"],
                "message_count": r["message_count"],
                "first_ts": r["first_ts"].isoformat(),
                "last_ts": r["last_ts"].isoformat(),
                "preview": previews.get(r["session_id"], ""),
            }
            for r in rows
        ],
    }


@app.get("/api/conversations/{session_id}")
async def get_conversation(session_id: str, token: str | None = Cookie(None)):
    if not _check_auth(token):
        raise HTTPException(status_code=401)
    async with db_pool.acquire() as conn:
        msgs = await conn.fetch("""
            SELECT
                event_data->>'author' as author,
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
        "messages": [
            {
                "author": m["author"],
                "text": m["text"] or "",
                "timestamp": m["timestamp"].isoformat(),
            }
            for m in msgs
        ],
    }


# ============================================
# UI
# ============================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE



HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EQ Helper — Admin</title>
<style>
:root {
  --wa-green: #00a884;
  --wa-dark: #111b21;
  --wa-panel: #202c33;
  --wa-sidebar: #111b21;
  --wa-chat-bg: #0b141a;
  --wa-hover: #2a3942;
  --wa-border: #2a3942;
  --wa-text: #e9edef;
  --wa-text-secondary: #8696a0;
  --wa-incoming: #202c33;
  --wa-outgoing: #005c4b;
  --wa-header: #202c33;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: 'Segoe UI', Helvetica, Arial, sans-serif; background: var(--wa-dark); color: var(--wa-text); height:100vh; overflow:hidden; }

/* ---- Login ---- */
.login-overlay {
  position:fixed; inset:0; background:var(--wa-dark);
  display:flex; align-items:center; justify-content:center; z-index:999;
}
.login-overlay.hidden { display:none; }
.login-box {
  background:var(--wa-panel); padding:40px; border-radius:12px;
  width:360px; text-align:center;
}
.login-box h2 { color:var(--wa-green); margin-bottom:8px; font-size:1.5em; }
.login-box p { color:var(--wa-text-secondary); margin-bottom:24px; font-size:0.9em; }
.login-box input {
  width:100%; padding:12px 16px; margin-bottom:12px;
  background:var(--wa-chat-bg); border:1px solid var(--wa-border);
  border-radius:8px; color:var(--wa-text); font-size:1em; outline:none;
}
.login-box input:focus { border-color:var(--wa-green); }
.login-box button {
  width:100%; padding:12px; background:var(--wa-green); color:#fff;
  border:none; border-radius:8px; font-size:1em; cursor:pointer; margin-top:4px;
}
.login-box button:hover { opacity:0.9; }
.login-error { color:#ef4444; font-size:0.85em; margin-top:8px; min-height:20px; }

/* ---- Main layout ---- */
.app { display:flex; height:100vh; }

/* Sidebar */
.sidebar {
  width:380px; min-width:380px; background:var(--wa-sidebar);
  border-right:1px solid var(--wa-border); display:flex; flex-direction:column;
}
.sidebar-header {
  padding:16px 20px; background:var(--wa-header);
  display:flex; justify-content:space-between; align-items:center;
  border-bottom:1px solid var(--wa-border);
}
.sidebar-header h3 { color:var(--wa-text); font-size:1em; }
.sidebar-header .stats { color:var(--wa-text-secondary); font-size:0.75em; }
.logout-btn {
  background:none; border:1px solid var(--wa-border); color:var(--wa-text-secondary);
  padding:6px 14px; border-radius:6px; cursor:pointer; font-size:0.8em;
}
.logout-btn:hover { color:var(--wa-text); border-color:var(--wa-text-secondary); }
.search-box {
  padding:8px 12px; background:var(--wa-panel); border-bottom:1px solid var(--wa-border);
}
.search-box input {
  width:100%; padding:8px 14px; background:var(--wa-chat-bg);
  border:none; border-radius:8px; color:var(--wa-text); font-size:0.9em; outline:none;
}
.conv-list { flex:1; overflow-y:auto; }
.conv-item {
  display:flex; padding:14px 20px; cursor:pointer;
  border-bottom:1px solid var(--wa-border); transition:background 0.15s;
}
.conv-item:hover { background:var(--wa-hover); }
.conv-item.active { background:var(--wa-hover); }
.conv-avatar {
  width:48px; height:48px; border-radius:50%; background:var(--wa-green);
  display:flex; align-items:center; justify-content:center;
  font-size:1.2em; color:#fff; font-weight:bold; flex-shrink:0;
}
.conv-info { margin-left:14px; flex:1; min-width:0; }
.conv-top { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
.conv-name { font-size:0.95em; color:var(--wa-text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.conv-time { font-size:0.72em; color:var(--wa-text-secondary); flex-shrink:0; margin-left:8px; }
.conv-bottom { display:flex; justify-content:space-between; align-items:center; }
.conv-preview { font-size:0.82em; color:var(--wa-text-secondary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex:1; }
.conv-badge {
  background:var(--wa-green); color:#fff; font-size:0.7em; font-weight:bold;
  padding:2px 7px; border-radius:10px; margin-left:8px; flex-shrink:0;
}

/* Chat area */
.chat-area { flex:1; display:flex; flex-direction:column; background:var(--wa-chat-bg); }
.chat-header {
  padding:14px 24px; background:var(--wa-header);
  border-bottom:1px solid var(--wa-border); display:flex; align-items:center;
}
.chat-header .conv-avatar { width:40px; height:40px; font-size:1em; }
.chat-header-info { margin-left:14px; }
.chat-header-name { font-size:0.95em; }
.chat-header-sub { font-size:0.78em; color:var(--wa-text-secondary); }
.chat-messages {
  flex:1; overflow-y:auto; padding:20px 60px;
  background: url("data:image/svg+xml,%3Csvg width='200' height='200' xmlns='http://www.w3.org/2000/svg'%3E%3Cdefs%3E%3Cpattern id='p' width='40' height='40' patternUnits='userSpaceOnUse'%3E%3Ccircle cx='20' cy='20' r='1' fill='%23ffffff08'/%3E%3C/pattern%3E%3C/defs%3E%3Crect width='200' height='200' fill='%230b141a'/%3E%3Crect width='200' height='200' fill='url(%23p)'/%3E%3C/svg%3E");
}
.empty-chat {
  flex:1; display:flex; align-items:center; justify-content:center;
  flex-direction:column; color:var(--wa-text-secondary);
}
.empty-chat svg { width:300px; opacity:0.15; margin-bottom:20px; }
.empty-chat p { font-size:1.1em; }

/* Messages */
.msg {
  max-width:65%; padding:8px 12px; margin-bottom:4px;
  border-radius:8px; position:relative; line-height:1.45; font-size:0.9em;
  word-wrap:break-word; white-space:pre-wrap;
}
.msg.user { background:var(--wa-outgoing); margin-left:auto; border-top-right-radius:0; }
.msg.root_agent { background:var(--wa-incoming); margin-right:auto; border-top-left-radius:0; }
.msg-time { font-size:0.68em; color:var(--wa-text-secondary); text-align:right; margin-top:4px; }
.msg-date-sep {
  text-align:center; margin:16px 0 12px;
}
.msg-date-sep span {
  background:var(--wa-panel); color:var(--wa-text-secondary);
  padding:5px 14px; border-radius:8px; font-size:0.78em;
}

/* Scrollbar */
::-webkit-scrollbar { width:6px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--wa-border); border-radius:3px; }
</style>
</head>
<body>

<!-- Login -->
<div class="login-overlay" id="loginOverlay">
  <div class="login-box">
    <h2>EQ Helper</h2>
    <p>Admin Panel — Sign in to view conversations</p>
    <input type="text" id="loginUser" placeholder="Username" autocomplete="username">
    <input type="password" id="loginPass" placeholder="Password" autocomplete="current-password">
    <button onclick="doLogin()">Sign In</button>
    <div class="login-error" id="loginError"></div>
  </div>
</div>

<!-- Main app -->
<div class="app" id="mainApp" style="display:none">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-header">
      <div>
        <h3>EQ Helper Admin</h3>
        <div class="stats" id="statsLine">Loading…</div>
      </div>
      <button class="logout-btn" onclick="doLogout()">Logout</button>
    </div>
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="Search conversations…">
    </div>
    <div class="conv-list" id="convList"></div>
  </div>

  <!-- Chat -->
  <div class="chat-area" id="chatArea">
    <div class="empty-chat" id="emptyChat">
      <svg viewBox="0 0 303 172"><path fill="#8696a0" d="M229.565 160.229c32.647-20.565 45.727-64.346 22.07-95.105-4.286-5.572-9.627-10.539-15.762-14.578C198.303 26.677 144.07 35.83 120.2 73.4c-8.684 13.674-12.233 29.282-10.86 44.677 1.373 15.396 7.96 30.168 19.66 42.152l-7.498 26.041 27.26-8.003c10.58 4.98 22.039 7.702 33.604 7.702 16.424 0 32.793-5.544 46.199-15.74z"/></svg>
      <p>Select a conversation to view messages</p>
    </div>
  </div>
</div>

<script>
let conversations = [];
let activeSession = null;

async function doLogin() {
  const u = document.getElementById('loginUser').value;
  const p = document.getElementById('loginPass').value;
  const err = document.getElementById('loginError');
  err.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username:u, password:p})
    });
    if (!r.ok) { err.textContent = 'Invalid credentials'; return; }
    document.getElementById('loginOverlay').classList.add('hidden');
    document.getElementById('mainApp').style.display = 'flex';
    loadConversations();
  } catch(e) { err.textContent = 'Connection error'; }
}

async function doLogout() {
  await fetch('/api/logout', {method:'POST'});
  location.reload();
}

// Enter key on login
document.getElementById('loginPass').addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });
document.getElementById('loginUser').addEventListener('keydown', e => { if(e.key==='Enter') document.getElementById('loginPass').focus(); });

async function loadConversations() {
  try {
    const r = await fetch('/api/conversations');
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    conversations = data.conversations;
    document.getElementById('statsLine').textContent =
      `${data.unique_users} users · ${data.total_sessions} sessions · ${data.total_messages} messages`;
    renderList(conversations);
  } catch(e) { console.error(e); }
}

function renderList(list) {
  const el = document.getElementById('convList');
  if (!list.length) { el.innerHTML = '<div style="padding:40px;text-align:center;color:var(--wa-text-secondary)">No conversations yet</div>'; return; }
  el.innerHTML = list.map(c => {
    const initials = (c.user_id || '?').substring(0,2).toUpperCase();
    const active = c.session_id === activeSession ? ' active' : '';
    return `<div class="conv-item${active}" onclick="openChat('${c.session_id}')">
      <div class="conv-avatar">${initials}</div>
      <div class="conv-info">
        <div class="conv-top">
          <span class="conv-name">${esc(c.user_id)}</span>
          <span class="conv-time">${relTime(c.last_ts)}</span>
        </div>
        <div class="conv-bottom">
          <span class="conv-preview">${esc(c.preview || 'No messages')}</span>
          <span class="conv-badge">${c.message_count}</span>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function openChat(sessionId) {
  activeSession = sessionId;
  renderList(filterConversations());
  const conv = conversations.find(c => c.session_id === sessionId);
  const area = document.getElementById('chatArea');

  area.innerHTML = `
    <div class="chat-header">
      <div class="conv-avatar">${(conv?.user_id||'?').substring(0,2).toUpperCase()}</div>
      <div class="chat-header-info">
        <div class="chat-header-name">${esc(conv?.user_id||sessionId)}</div>
        <div class="chat-header-sub">Session: ${sessionId}</div>
      </div>
    </div>
    <div class="chat-messages" id="chatMessages"><div style="padding:40px;text-align:center;color:var(--wa-text-secondary)">Loading…</div></div>`;

  try {
    const r = await fetch(`/api/conversations/${sessionId}`);
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    const msgEl = document.getElementById('chatMessages');
    let html = '';
    let lastDate = '';
    data.messages.forEach(m => {
      const d = new Date(m.timestamp);
      const dateStr = d.toLocaleDateString(undefined, {year:'numeric',month:'long',day:'numeric'});
      if (dateStr !== lastDate) {
        html += `<div class="msg-date-sep"><span>${dateStr}</span></div>`;
        lastDate = dateStr;
      }
      const timeStr = d.toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit'});
      const cls = m.author === 'user' ? 'user' : 'root_agent';
      html += `<div class="msg ${cls}">${esc(m.text)}<div class="msg-time">${timeStr}</div></div>`;
    });
    msgEl.innerHTML = html || '<div style="padding:40px;text-align:center;color:var(--wa-text-secondary)">No messages</div>';
    msgEl.scrollTop = msgEl.scrollHeight;
  } catch(e) { console.error(e); }
}

function filterConversations() {
  const q = document.getElementById('searchInput').value.toLowerCase();
  if (!q) return conversations;
  return conversations.filter(c =>
    c.user_id.toLowerCase().includes(q) ||
    c.session_id.toLowerCase().includes(q) ||
    (c.preview||'').toLowerCase().includes(q)
  );
}

document.getElementById('searchInput').addEventListener('input', () => renderList(filterConversations()));

function esc(s) { const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }

function relTime(iso) {
  const d = new Date(iso), now = new Date(), diff = now - d;
  const mins = Math.floor(diff/60000);
  if (mins < 1) return 'now';
  if (mins < 60) return mins + 'm';
  const hrs = Math.floor(mins/60);
  if (hrs < 24) return hrs + 'h';
  const days = Math.floor(hrs/24);
  if (days < 7) return days + 'd';
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
}

// Auto-refresh sidebar every 30s
setInterval(loadConversations, 30000);

// On page load, check if already logged in
(async () => {
  try {
    const r = await fetch('/api/me');
    if (r.ok) {
      document.getElementById('loginOverlay').classList.add('hidden');
      document.getElementById('mainApp').style.display = 'flex';
      loadConversations();
    }
  } catch(e) {}
})();
</script>
</body>
</html>"""
