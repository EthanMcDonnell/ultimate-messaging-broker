# claude-through-messaging-platform

Use Claude Code from your phone via iMessage or Telegram. Send a message, get a response — with full project-switching, intent routing, and rate limiting. Runs as a background daemon on your Mac.

## How it works

A single `main.py` entry point handles both platforms. When a message arrives it is routed through shared logic:

1. **Intent detection** — is this "list projects", "switch to X", or a question for Claude?
2. **Project resolution** — each project maps to a directory on disk with its own allowed tools
3. **Claude invocation** — Telegram topics use the Agent SDK (streaming, session persistence, tool permission hooks); iMessage and Telegram DMs use the CLI subprocess
4. **Reply** — sends the response back through the platform's transport layer

```
main.py                  ← entry point + shared process_message()
├── claude_bridge.py     ← CLI subprocess (iMessage) + Agent SDK async (Telegram topics)
├── router.py            ← intent detection (switch / list / ask)
├── security.py          ← rate limiting, input sanitization
├── state.py             ← persists current project across restarts
├── sessions.py          ← persists Claude session IDs per Telegram topic (24h TTL)
├── jobs.py              ← SQLite store for async API jobs and permission prompts
├── dispatcher.py        ← routes job responses (webhook / script / claude)
├── api_server.py        ← HTTP API server (Telegram platform only)
│
├── imessage/
│   ├── watcher.py       ← polls ~/Library/Messages/chat.db
│   ├── responder.py     ← sends replies via AppleScript
│   └── message_parser.py ← parses iMessage attributedBody blobs
│
└── tg/
    ├── bot.py           ← Telegram bot, topic routing, job callbacks, SDK integration
    ├── ask.py           ← standalone blocking inline-keyboard question utility
    └── utils.py         ← shared Telegram helpers (inline keyboard builder)
```

---

## Prerequisites

- macOS (iMessage platform requires this; Telegram works on any OS)
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (`claude` on PATH)
- Python 3.11+
- For iMessage: Full Disk Access granted to Terminal (so it can read `chat.db`)

---

## Installation

```bash
git clone https://github.com/yourusername/claude-through-messaging-platform
cd claude-through-messaging-platform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Copy the example config and fill in your details:

```bash
cp config.example.yaml config.yaml
```

The config has three sections: `imessage` (sender number, poll interval), `telegram` (bot token, user ID), and shared settings (`claude`, `rate_limits`, `projects`). See `config.example.yaml` for the full annotated reference.

### Finding your iMessage phone number

```bash
sqlite3 ~/Library/Messages/chat.db \
  "SELECT DISTINCT chat_identifier FROM chat;"
```

Use the value that matches your phone number for both `allowed_sender` and `self_chat_id`.

### Getting a Telegram bot token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the token into `config.yaml`

### Finding your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot) — it replies with your user ID. Put that in `allowed_user_id`.

### Projects

Each project maps a name to a directory on disk with a set of allowed tools. Claude runs inside that directory using only those tools. `allowed_tools` is passed directly to `claude --allowedTools` — use standard tool names (`Read`, `Edit`, `Bash`, etc.) or MCP tools in the form `mcp__servername__toolname`. See `config.example.yaml` for examples.

---

## Running

### Selecting a platform

Set `platform:` in `config.yaml` (persisted default):

```yaml
platform: telegram   # or imessage
```

Or pass it at runtime to override:

```bash
python main.py --platform telegram
python main.py --platform imessage
```

One of the two must be set — the process will exit with an error if neither is specified.

### iMessage

```bash
.venv/bin/python main.py --platform imessage
```

Send a message **to yourself** in iMessage. The bridge monitors your self-chat.

### Telegram

```bash
.venv/bin/python main.py --platform telegram
```

Open your bot in Telegram and start sending messages.

### Other options

```
--config      path to config.yaml  (default: config.yaml in repo root)
--dry-run     log responses without sending or calling Claude
--log-level   DEBUG | INFO | WARNING | ERROR
```

---

## Messaging commands

These work on both platforms and are matched with fuzzy intent detection (designed for Siri voice dictation on iMessage):

| Say | What happens |
|-----|-------------|
| `list projects` | Shows all configured projects |
| `current project` / `where am I` | Shows the active project and path |
| `switch to [name]` | Switches to that project |
| `use [name]` | Also switches project |
| Anything else | Sent to Claude in the current project |

Project names are fuzzy-matched, so Siri-dictated phrases like "hey switch to the bridge project" will resolve correctly.

---

## Telegram bot commands

| Command | What it does |
|---|---|
| `/start` | Check the bot is connected; shows current project |
| `/status` | Shows platform, projects, and whether the Claude CLI is found |
| `/new` | Start a fresh Claude session in the current topic (clears conversation history) |
| `/stop` `/cancel` `/esc` | Stop the currently running Claude call in this topic |
| `/killall` `/stopall` | Cancel all running Claude calls across all topics |

### Sessions

When you message a configured topic, Claude remembers the conversation context for 24 hours. Each topic has its own independent session. Use `/new` to reset a topic to a blank slate.

### Tool permission prompts

When Claude wants to use a tool (read a file, run a command, etc.), the bot sends an inline keyboard with **Allow** and **Deny** buttons. The call is blocked until you respond. If you don't respond within 60 seconds, the tool use is automatically denied and Claude is told why.

---

## HTTP API (Telegram platform)

When running with `--platform telegram`, an HTTP server starts automatically on `http://localhost:8765`. Other projects on your machine can post content to Telegram and receive responses back.

### POST /send

Post content to Telegram, optionally with buttons and a response handler.

**Request body:**

```json
{
  "content": "Text to send to Telegram",
  "topic": "articles",
  "buttons": ["Delete", "Keep"],
  "on_response": {
    "type": "webhook",
    "url": "http://localhost:5001/callback"
  }
}
```

| Field | Required | Description |
|---|---|---|
| `content` | yes | Text to send |
| `topic` | no | Project name from `config.yaml` — resolves to its `telegram_topic_id` automatically |
| `topic_id` | no | Raw Telegram topic thread ID — use `topic` instead unless you need to override |
| `buttons` | no | Array of button labels (strings), or objects `{"label": "...", "action": "..."}`. Action defaults to lowercased label if not specified. |
| `on_response` | no | What to do when a button is pressed — see below |

**`on_response` types:**

```json
{ "type": "webhook", "url": "http://localhost:5001/cb" }
```
POSTs `{"job_id", "action", "content"}` to the URL when a button is pressed.

```json
{ "type": "script", "path": "/abs/path/to/handler.py" }
```
Runs the script with the job payload as JSON via stdin.

```json
{
  "type": "claude",
  "project": "my-project",
  "prompt_template": "User selected \"{action}\" for: {content}",
  "result_webhook": "http://localhost:5001/result"
}
```
Runs Claude in the named project with the formatted prompt. Sends Claude's response back to Telegram. Optionally also POSTs `{"job_id", "action", "content", "claude_response"}` to `result_webhook`.

**Response:**

```json
{ "job_id": "a3f2c1b4" }
```

**Example (curl):**

```bash
curl -X POST http://localhost:8765/send \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Article: Why distributed systems are hard",
    "topic": "articles",
    "buttons": ["Delete", "Keep"],
    "on_response": {"type": "webhook", "url": "http://localhost:5001/cb"}
  }'
```

**Example (Python):**

```python
import requests

r = requests.post("http://localhost:8765/send", json={
    "content": "Video idea: 5 Python patterns that kill performance",
    "topic": "video-ideas",
    "buttons": ["Develop", "Skip"],
    "on_response": {
        "type": "claude",
        "project": "content",
        "prompt_template": "Develop this video idea into a full outline: {content}",
        "result_webhook": "http://localhost:5001/outline-result"
    }
})
job_id = r.json()["job_id"]
```

---

### GET /jobs/{id}

Check the status of a posted job.

```bash
curl http://127.0.0.1:8765/jobs/a3f2c1b4
```

**Response:**

```json
{
  "id": "a3f2c1b4",
  "content": "Article: ...",
  "status": "responded",
  "action": "keep",
  "topic_id": 123,
  "created_at": "2026-04-26T10:00:00",
  "responded_at": "2026-04-26T14:32:11"
}
```

`status` is `"pending"` until a button is pressed, then `"responded"`. `action` is the button's action value.

---

### Telegram group topics setup

To post into topics, you need a Telegram supergroup with Topics enabled:

1. Create a private supergroup and enable Topics (**Group Settings → Topics**)
2. Add your bot as admin with permission to post messages
3. Get the group ID — forward any message from the group to [@userinfobot](https://t.me/userinfobot), or check the URL in Telegram Web (`/c/<group_id>/...`)
4. Get topic IDs from the URL when viewing a topic: `/c/<group_id>/<topic_id>/<message_id>`
5. Add to `config.yaml`:

```yaml
telegram:
  telegram_group_id: -100123456789

projects:
  - name: articles
    path: /Users/you/Documents/articles
    platforms: [telegram]
    telegram_topic_id: 3
```

---

## Running as a background service (iMessage, macOS)

A launchd plist is included at `imessage/launchd/com.ethan.claude-imessage.plist`. It starts the bridge on login and restarts it if it crashes.

**1. Edit the plist** — update the Python path and working directory to match your setup:

```xml
<string>/Users/YOU/Documents/claude-through-messaging-platform/.venv/bin/python3</string>
<string>/Users/YOU/Documents/claude-through-messaging-platform/main.py</string>
...
<string>/Users/YOU/Documents/claude-through-messaging-platform</string>
```

**2. Install it:**

```bash
cp imessage/launchd/com.ethan.claude-imessage.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ethan.claude-imessage.plist
```

**3. Check logs:**

```bash
tail -f /tmp/claude-imessage.log
tail -f /tmp/claude-imessage-error.log
```

**4. Stop / restart:**

```bash
launchctl unload ~/Library/LaunchAgents/com.ethan.claude-imessage.plist
launchctl load   ~/Library/LaunchAgents/com.ethan.claude-imessage.plist
```

---

## iMessage setup notes

### Full Disk Access

The bridge reads `~/Library/Messages/chat.db`. On macOS Ventura+ this requires Full Disk Access:

**System Settings → Privacy & Security → Full Disk Access** — add Terminal (or your IDE / Python binary if running via launchd).

### How the self-chat works

Send messages to yourself in iMessage. The bridge only reads messages where `is_from_me = 1` in your self-chat, so your normal conversations are never touched.

Replies are prefixed with `✦claude✦` so the bridge can recognise and skip its own outgoing messages on the next poll.

---

## Tests

```bash
.venv/bin/python -m pytest imessage/tests/
```

Tests cover intent routing, input sanitization, and message parsing. They import from the shared root modules automatically.

---

## Security

- **Single-user only** — both platforms reject any sender/user that isn't the configured one
- **Rate limiting** — configurable cap on messages per minute; exceeding it exits the process (iMessage) or sends an error reply (Telegram)
- **Input sanitization** — null bytes stripped, prompts truncated at 8000 chars before being passed to Claude
- **AppleScript escaping** — iMessage responses escape backslashes and double-quotes before interpolation
- **Path validation** — project paths must exist and must be inside your home directory; symlinks that escape are rejected at startup
- **Subprocess, not shell** — Claude is invoked with `subprocess.run(..., shell=False)`, preventing shell injection
