# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Bridges Claude Code CLI to iMessage and Telegram. Messages arrive via a platform transport layer, pass through shared intent routing and security, then invoke `claude -p` as a subprocess in a configured project directory. The reply is sent back through the platform.

## Python

Always use `.venv/bin/python` — never the system `python` or `python3`.

```bash
.venv/bin/python main.py --platform telegram
.venv/bin/python main.py --platform imessage
.venv/bin/python main.py --dry-run --log-level DEBUG   # no Claude calls, no sends
```

Requires `config.yaml` (copy from `config.example.yaml`). The `platform:` key can also be set in the config instead of passing `--platform`.

## Tests

```bash
.venv/bin/python -m pytest imessage/tests/
```

## Architecture

All message handling flows through `process_message()` in `main.py` — this is the platform-agnostic core. Platform modules only handle I/O:

- **`router.py`** — fuzzy keyword intent detection (LIST_PROJECTS / CURRENT_STATUS / SWITCH_PROJECT / ASK_CLAUDE). Uses `difflib` with score thresholds; designed to tolerate Siri voice dictation.
- **`claude_bridge.py`** — every Claude call goes through `claude -p` here. Three entry points: `ask_claude()` (sync, text — iMessage + Telegram DM/non-topic fallback), `ask_claude_json()` (sync, `--output-format json` — the HTTP API), and `ask_claude_stream()` (async, `--output-format stream-json` — Telegram topics, streams progress to the placeholder and returns session_id/cost). Session continuity uses `--session-id <uuid>` to pin a new conversation and `--resume <uuid>` to continue it. Tool access is governed entirely by each project's `allowed_tools` (`--allowedTools`); print mode has no interactive permission prompt, so tools outside the allowlist are denied automatically.
- **`sessions.py`** — persists per-topic Claude session UUIDs in `jobs.db` (resumable via `--resume`). 24h TTL plus a cumulative cost cap (`SESSION_COST_CAP_USD`); cost/turns come from each result's `total_cost_usd`/`num_turns`. `/new` clears the row. `pop_if_stale()` cleans up sessions whose TTL/cost expired before the next inbound message.
- **`security.py`** — sender validation, rate limiting, prompt sanitization (null-byte strip, 8000-char truncation).
- **`state.py`** — persists current project and seen message GUIDs across restarts.
- **`jobs.py`** — SQLite store for async jobs (external API) and permission prompts (inline keyboard Allow/Deny).
- **`dispatcher.py`** — routes job responses: webhook POST, Python script, or Claude invocation.
- **`imessage/`** — polls `~/Library/Messages/chat.db` (requires Full Disk Access), sends replies via AppleScript. Replies are prefixed `✦claude✦` so the bridge skips its own outgoing messages.
- **`tg/bot.py`** — async Telegram bot. Topic messages go through `claude_bridge.ask_claude_stream` as a per-topic `claude -p` subprocess, resuming the topic's stored session UUID and editing a placeholder message with streamed progress. DM/non-topic messages go through `process_message()` (intent routing → `claude_bridge.ask_claude`). Each topic tracks its running task; `/kill` and `/killall` cancel it, which terminates the subprocess. A new message in a topic that's still working is rejected (not queued) so `/kill` always stops the task you meant. Commands: `/start`, `/status`, `/help`, `/new`, `/kill`, `/killall`, `/stopall`. A slash-command fallback inside `handle_message` re-dispatches commands that PTB's `CommandHandler` somehow missed.
- **`tg/ask.py`** — standalone utility (CLI or library) for sending inline keyboard questions and blocking on user response. Uses raw `get_updates()` polling so it coexists with the main bot daemon on the same token.
- **`tg/utils.py`** — shared inline keyboard builder used by both `tg/bot.py` and `api_server.py`.

## Config structure

```yaml
platform: telegram | imessage
imessage:
  allowed_sender, self_chat_id, poll_interval, max_chunk_size
telegram:
  bot_token, allowed_user_id
claude:
  timeout, max_response_length
rate_limits:
  messages_per_minute
projects:
  - name, path, aliases, allowed_tools   # allowed_tools → --allowedTools
default_project: <name>
```

Project paths must exist and must be inside the home directory — validated at startup. `allowed_tools` accepts standard Claude tool names (`Read`, `Edit`, `Bash`, etc.) and MCP tools (`mcp__servername__toolname`).

## launchd (background service, iMessage)

Install/manage via `imessage/launchd/com.ethan.claude-imessage.plist`. Logs go to `/tmp/claude-imessage.log` and `/tmp/claude-imessage-error.log`.
