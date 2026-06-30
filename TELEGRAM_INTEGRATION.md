# Telegram Integration Design

## Repo

`claude-messaging-broker` — extend this, do not merge into ai-mission-control.

---

## What already exists

| File | What it does |
|---|---|
| `tg/bot.py` | Telegram bot, topic routing (`thread_id → project`), message splitting, rate limiting, auth |
| `tg/ask.py` | Interactive inline keyboard questions — standalone blocking tool, not used for permissions (see below) |
| `api_server.py` | HTTP API on `localhost:8765` — used by external projects (e.g. Influencer) to post jobs to Telegram topics. Already handles `message_thread_id` correctly. |
| `jobs.py` + `jobs.db` | Job store (SQLite) — create job, set response, poll status. Used by `api_server.py` and `tg/bot.py` callback handler. |
| `dispatcher.py` | Routes job responses to webhook / script / claude |
| `claude_bridge.py` | CLI subprocess (`claude -p`), per-project cwd, error handling |
| `config.yaml` | Per-project `telegram_topic_id` + `allowed_tools` |

---

## What needs to be built

### 1. Replace `claude_bridge.py` with Agent SDK

Swap CLI subprocess for `claude_agent_sdk.query()` with CLI credentials (no API key needed — SDK picks up `~/.claude/` auth automatically).

```python
from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher

async for message in query(
    prompt=text,
    options=ClaudeAgentOptions(
        resume=session_id,       # None for new session
        cwd=project["path"],
        allowed_tools=project.get("allowed_tools"),
        hooks={
            "PreToolUse": [HookMatcher(matcher=".*", hooks=[permission_hook])]
        },
    ),
):
    # stream output, capture session_id
```

### 2. Session management

Add a new `sessions.py` module (alongside `jobs.py`) that owns the session table in `jobs.db`:

```python
# sessions.py
class SessionStore:
    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telegram_sessions (
                    topic_id   INTEGER PRIMARY KEY,
                    project    TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)

    def get(self, topic_id: int) -> dict | None: ...
    def upsert(self, topic_id: int, project: str, session_id: str) -> None: ...
    def delete(self, topic_id: int) -> None: ...
```

Flow per incoming message:
- Look up `topic_id` → get `session_id` + `updated_at`
- If exists and `updated_at` < 24h → `options.resume = session_id`
- If expired or missing → no resume, capture new session ID from SDK `init` event
- `/new` command in `tg/bot.py` calls `session_store.delete(topic_id)` then replies "Starting new session."

Capture session ID from SDK:
```python
if message.type == "system" and message.subtype == "init":
    session_id = message.data["session_id"]
    session_store.upsert(topic_id, project["name"], session_id)
```

### 3. Permission approval via job store (internal — no HTTP)

`PreToolUse` hook as an async callback. The Agent SDK runs inside the broker process, so the hook calls `job_store` and the async `bot` instance directly — no loopback HTTP call to the API server needed. The API is the external boundary for other projects (Influencer etc.); inside the broker we call internals directly.

Note: do NOT use `_send_telegram_message` from `api_server.py` here — it uses `urllib.request` (synchronous) and will block the event loop. Use `await bot.send_message(...)` instead.

```python
def make_permission_hook(job_store, bot, chat_id: int, thread_id: int | None):
    async def hook(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})
        content = _format_tool(tool_name, tool_input)

        job_id = job_store.create(content, buttons=["Allow", "Deny"], on_response=None, topic_id=thread_id)
        keyboard = _build_inline_keyboard(job_id, ["Allow", "Deny"])
        await bot.send_message(chat_id=chat_id, text=content, reply_markup=keyboard, message_thread_id=thread_id)

        # poll job_store directly — no HTTP
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            job = job_store.get(job_id)
            if job["status"] == "responded":
                return {} if job["action"] == "allow" else {"decision": "block", "reason": "Denied via Telegram"}

        await bot.send_message(chat_id=chat_id, text="⏱ Permission timed out — denied.", message_thread_id=thread_id)
        return {"decision": "block", "reason": "Timed out — denied"}
    return hook
```

`_build_inline_keyboard` already exists in `api_server.py` — move it to a shared `tg/utils.py` so both can import it.

### 4. Async architecture shift in `tg/bot.py`

Currently `handle_message` calls `process_for_project_fn(text, project)` synchronously. With the SDK, Claude invocation becomes async. The handler is already `async def` so this is a straight swap — replace the sync call with `await ask_claude_sdk(text, project, ...)`.

`process_message_for_project` in `main.py` also needs an async variant for the Telegram path. The iMessage path stays synchronous and unchanged.

### 5. Streaming output

Replace the current fire-and-wait pattern with an edit-in-place stream:

```python
placeholder = await bot.send_message(chat_id=chat_id, text="…", message_thread_id=thread_id)
buffer = ""

async for message in query(...):
    if message.type == "system" and message.subtype == "init":
        session_store.upsert(topic_id, project["name"], message.session_id)
    if hasattr(message, "text"):
        buffer += message.text
        await placeholder.edit_text(buffer[-4096:])

# Start new message if buffer exceeded 4096 during stream
```

### 5. Failure surfacing

All errors sent to Telegram — nothing silent:

| Failure | Behaviour |
|---|---|
| SDK auth failure | Send error + "run `claude auth login`" |
| Session resume failure | Retry as new session + notify user |
| Permission timeout (60s) | Auto-deny + send "Timed out — denied" |
| Tool blocked | Send Claude's stated reason |
| Claude process error | Send error text to topic |

---

## Files to change / add

| File | Change |
|---|---|
| `claude_bridge.py` | Replace CLI subprocess with async Agent SDK, add streaming, session capture |
| `sessions.py` | New — `SessionStore` class, owns `telegram_sessions` table in `jobs.db` |
| `tg/bot.py` | `handle_message` awaits async Claude call; add `/new` command handler; pass `bot` + `session_store` + `job_store` to permission hook |
| `tg/utils.py` | New — move `_build_inline_keyboard` here from `api_server.py` so both can import it |
| `main.py` | Pass `session_store` into Telegram path; add async variant of `process_message_for_project` |
| `requirements.txt` | Add `claude-agent-sdk` |

`tg/ask.py` — **no changes needed**, not used for permissions.
`api_server.py` — **no changes needed**, import `_build_inline_keyboard` from `tg/utils.py` instead.

---

## Config

No schema changes to `config.yaml`. Sessions are runtime state stored in SQLite, not config.

---

## Out of scope

- Multi-user support
- Webhook mode
- Voice / file uploads
- iMessage parity (keep existing path unchanged)
- AI Mission Control web UI integration (separate concern)
