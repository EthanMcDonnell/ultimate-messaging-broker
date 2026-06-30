"""
Lightweight HTTP API server for posting jobs to Telegram from external projects.

POST /telegram/send
  Body: {
    "content": "text to send",
    "topic_id": 123,                        // optional; Telegram topic thread ID
    "buttons": ["Delete", "Keep"],          // optional; strings or {"label":..,"action":..}
    "on_response": {                        // optional; what to do when a button is pressed
      "type": "webhook",                    //   "webhook" | "script" | "claude"
      "url": "http://localhost:5001/cb"    //   webhook: POST with {job_id, action, content}
    }
    // script:  {"type":"script","path":"/abs/path/handler.py"}  — job JSON via stdin
    // claude:  {"type":"claude","project":"name",
    //           "prompt_template":"User said {action} about: {content}",
    //           "result_webhook":"http://..."}  — optional result_webhook for Claude's reply
  }
  Returns: {"job_id": "abc12345"}

GET /telegram/jobs/<id>
  Returns the full job record including status ("pending" | "responded") and action.

POST /claude/ask
  Body: {
    "prompt": "explain this codebase",
    "directory": "/abs/path/to/project",
    "allowed_tools": ["Read", "Edit", "Bash"],  // optional; defaults to read-only tools
    "timeout": 300,                              // optional; seconds (default 600)
    "session_id": "8f3c…",                       // optional; resume a specific session
    "new_session": false                         // optional; true = force fresh session
  }
  Returns: {"response": "...", "session_id": "<uuid>"}
  Backed by `claude -p`. Pass a session_id (the UUID returned by a prior call)
  to continue that conversation; omit it, or set new_session, to start fresh.
"""

import json
import logging
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from claude_bridge import ask_claude_json

logger = logging.getLogger(__name__)


def _send_telegram_message(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
    message_thread_id: int | None = None,
) -> int:
    """Send a message via the Telegram Bot API. Returns the message_id."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    data = json.dumps(payload).encode()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["result"]["message_id"]


from tg.utils import build_inline_keyboard as _build_inline_keyboard


def _resolve_topic_id(name: str, projects: list[dict]) -> int | None:
    """Look up a topic_id by project name."""
    for p in projects:
        if p["name"] == name:
            return p.get("telegram_topic_id")
    return None


def _make_handler(config: dict, job_store):
    tg_cfg = config.get("telegram", {})
    bot_token = tg_cfg["bot_token"]
    dm_chat_id = int(tg_cfg["allowed_user_id"])
    group_chat_id = int(tg_cfg["telegram_group_id"]) if tg_cfg.get("telegram_group_id") else None
    projects = config.get("projects", [])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.debug("API %s", fmt % args)

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

        def do_GET(self):
            if self.path.startswith("/telegram/jobs/"):
                job_id = self.path[len("/telegram/jobs/"):]
                job = job_store.get(job_id)
                self._json(200 if job else 404, job or {"error": "not found"})
            elif self.path == "/claude/sessions":
                # `claude -p` sessions live in claude's own ~/.claude store with
                # no broker-side registry to enumerate. Kept for compatibility.
                self._json(200, {"sessions": [], "note": "session listing unavailable with the claude -p backend"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            try:
                body = self._read_body()
            except Exception:
                self._json(400, {"error": "invalid JSON"})
                return

            if self.path == "/claude/ask":
                self._handle_ask(body)
            elif self.path == "/telegram/send":
                self._handle_send(body, bot_token, dm_chat_id, group_chat_id, projects, job_store)
            else:
                self._json(404, {"error": "not found"})

        def _handle_ask(self, body: dict) -> None:
            prompt = (body.get("prompt") or "").strip()
            directory = body.get("directory")
            if not prompt or not directory:
                self._json(400, {"error": "prompt and directory are required"})
                return
            timeout = int(body.get("timeout", 600))
            allowed_tools = body.get("allowed_tools") or []

            # Resume the supplied session, or pin a fresh UUID for a new one.
            session_id = body.get("session_id")
            new_session = bool(body.get("new_session"))
            resume = bool(session_id) and not new_session
            if not session_id or new_session:
                session_id = str(uuid.uuid4())

            result = ask_claude_json(
                prompt, directory, allowed_tools, timeout=timeout,
                session_id=session_id, resume=resume,
            )
            self._json(200 if result.get("ok") else 500, {
                "response": result.get("response", ""),
                "session_id": result.get("session_id", session_id),
            })

        def _handle_send(self, body: dict, bot_token, dm_chat_id, group_chat_id, projects, job_store) -> None:
            content = body.get("content", "").strip()
            if not content:
                self._json(400, {"error": "content is required"})
                return

            # Resolve topic: accept name ("topic": "articles") or raw ID ("topic_id": 123)
            topic_name = body.get("topic")
            topic_id = body.get("topic_id")
            if topic_name:
                topic_id = _resolve_topic_id(topic_name, projects)
                if topic_id is None:
                    self._json(400, {"error": f"unknown topic name: {topic_name!r}"})
                    return

            buttons = body.get("buttons")
            on_response = body.get("on_response")
            metadata = body.get("metadata")

            # Topic posts go to the group; plain posts go to DM
            target_chat_id = group_chat_id if topic_id and group_chat_id else dm_chat_id

            job_id = job_store.create(content, buttons, on_response, topic_id=topic_id, metadata=metadata)

            try:
                keyboard = _build_inline_keyboard(job_id, buttons) if buttons else None
                tg_msg_id = _send_telegram_message(
                    bot_token, target_chat_id, content, keyboard,
                    message_thread_id=topic_id,
                )
                job_store.set_tg_msg_id(job_id, tg_msg_id)
            except Exception as e:
                logger.error("Failed to send Telegram message: %s", e)
                self._json(500, {"error": str(e)})
                return

            self._json(200, {"job_id": job_id})

    return Handler


def start(config: dict, job_store, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = _make_handler(config, job_store)
    server = HTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("API server listening on http://%s:%d", host, port)
