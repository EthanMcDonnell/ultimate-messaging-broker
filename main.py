#!/usr/bin/env python3
"""
Claude Messaging Bridge
Single entry point for all messaging platforms (iMessage, Telegram).
Shared logic: routing, project management, Claude invocation.
Platform-specific I/O lives in imessage/ and telegram/.
"""

import argparse
import atexit
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import yaml

from claude_bridge import ask_claude
from router import Intent, detect_intent
from security import RateLimiter, sanitize_prompt, validate_sender
from state import State

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


# ─── Single-instance lock ────────────────────────────────────────────────────
# Two broker processes on the same platform would both poll Telegram / chat.db
# and race against each other (especially fatal in tmux mode, where they'd
# fight over `send-keys` to the same panes). A PID-file lock per platform is
# enough — same-machine, same-user.

def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _acquire_singleton_lock(platform: str) -> None:
    pid_file = Path(tempfile.gettempdir()) / f"claude-messaging-broker-{platform}.pid"
    if pid_file.exists():
        try:
            existing = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            existing = None
        if existing and _is_alive(existing):
            logger.error(
                "Another %s broker is already running (PID %d). "
                "If that's wrong, remove %s and retry.",
                platform, existing, pid_file,
            )
            sys.exit(1)
        logger.info("Stale pid file at %s — taking over", pid_file)
    pid_file.write_text(str(os.getpid()))

    def _cleanup() -> None:
        try:
            if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass

    atexit.register(_cleanup)


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        config = yaml.safe_load(f)
    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    """Fail fast on startup if project paths are missing or unsafe."""
    for project in config.get("projects", []):
        path = Path(project.get("path", "")).resolve()
        if not path.is_dir():
            raise ValueError(f"Project '{project['name']}' path does not exist: {path}")
        home = Path.home().resolve()
        try:
            path.relative_to(home)
        except ValueError:
            raise ValueError(
                f"Project '{project['name']}' path is outside home directory: {path}\n"
                "Only paths under your home directory are allowed."
            )


# ─── Shared message processing ────────────────────────────────────────────────

def get_project(config: dict, name: str) -> dict | None:
    for p in config.get("projects", []):
        if p["name"] == name:
            return p
    return None


def filter_projects_for_platform(config: dict, platform: str) -> dict:
    """Return a copy of config with only projects available on the given platform."""
    filtered = [
        p for p in config.get("projects", [])
        if platform in p.get("platforms", ["imessage", "telegram"])
    ]
    return {**config, "projects": filtered}


def format_project_list(projects: list[dict], current: str | None) -> str:
    lines = ["Available projects:"]
    for p in projects:
        marker = " ← current" if p["name"] == current else ""
        lines.append(f"  • {p['name']}{marker}")
    lines.append('\nSay "switch to [name]" to change.')
    return "\n".join(lines)


def process_message(text: str, config: dict, state: State, dry_run: bool = False) -> str:
    """
    Route an incoming message and return the response text.
    Platform-agnostic: used by both iMessage and Telegram.
    """
    projects = config.get("projects", [])
    result = detect_intent(text, projects)

    if result.intent == Intent.LIST_PROJECTS:
        return format_project_list(projects, state.current_project)

    if result.intent == Intent.CURRENT_STATUS:
        proj = get_project(config, state.current_project) if state.current_project else None
        if proj:
            return f"Current project: {proj['name']}\nPath: {proj['path']}"
        return "No project selected."

    if result.intent == Intent.SWITCH_PROJECT:
        if result.ambiguous_matches:
            opts = " or ".join(f'"{m}"' for m in result.ambiguous_matches)
            return f"Ambiguous — did you mean {opts}?"
        target = get_project(config, result.project_name)
        if not target:
            return f'Project "{result.project_name}" not found. Say "list projects" to see options.'
        state.set_project(target["name"])
        return f"Switched to: {target['name']}\n({target['path']})"

    # ASK_CLAUDE — default
    current = get_project(config, state.current_project)
    if not current:
        return 'No project selected. Say "list projects" to see options, then "switch to [name]".'

    prompt = sanitize_prompt(text)
    max_len = config.get("claude", {}).get("max_response_length", 16000)
    timeout = config.get("claude", {}).get("timeout", 120)

    if dry_run:
        return f"[DRY RUN] Would ask Claude in {current['path']}:\n{prompt[:200]}"

    response = ask_claude(prompt, current["path"], current.get("allowed_tools", []), timeout)

    if len(response) > max_len:
        response = response[:max_len] + f"\n\n[Response truncated at {max_len} chars]"

    return response


# ─── iMessage platform ────────────────────────────────────────────────────────

def run_imessage(config: dict, state: State, dry_run: bool = False) -> None:
    config = filter_projects_for_platform(config, "imessage")
    from imessage.watcher import current_db_timestamp, fetch_new_messages, get_db_connection
    from imessage.responder import send_message

    imessage_cfg = config.get("imessage", {})
    poll_interval = imessage_cfg.get("poll_interval", 3)
    allowed_sender = imessage_cfg.get("allowed_sender", "")
    self_chat_id = imessage_cfg.get("self_chat_id", allowed_sender)

    rate_limits = config.get("rate_limits", {})
    msg_limiter = RateLimiter(
        max_count=rate_limits.get("messages_per_minute", 10),
        window_seconds=60,
    )

    logger.info("Starting iMessage bridge. Project: %s | Chat: %s", state.current_project, self_chat_id)
    if dry_run:
        logger.info("DRY RUN MODE — messages will not be sent")

    conn = get_db_connection()
    since = current_db_timestamp(conn)
    logger.info("Polling from timestamp %.0f", since)

    def _shutdown(sig, frame):
        logger.info("Shutting down...")
        state.save()
        conn.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        new_messages = list(fetch_new_messages(conn, since, self_chat_id))

        for msg in new_messages:
            guid = msg["guid"]
            text = msg["text"]

            if state.is_seen(guid):
                continue

            state.mark_seen(guid)

            imessage_ns = msg["date"]
            unix_ts = (imessage_ns / 1e9) + 978307200
            state.update_timestamp(unix_ts)
            since = state.last_message_time

            logger.info("New message [%s]: %s", guid[:8], text[:80])

            if not validate_sender(msg["chat_identifier"], allowed_sender):
                logger.warning("Rejected message from unexpected sender: %s", msg["chat_identifier"])
                continue

            if not msg_limiter.allow():
                logger.error("Rate limit exceeded — too many messages. Exiting.")
                sys.exit(1)

            reply = process_message(text, config, state, dry_run=dry_run)

            if not dry_run:
                logger.info("Reply (%d chars): %s...", len(reply), reply[:60])
                send_message(allowed_sender, reply, project_name=state.current_project)
            else:
                logger.info("[DRY RUN] Reply: %s", reply)

            state.save()

        time.sleep(poll_interval)


# ─── Telegram platform ────────────────────────────────────────────────────────

def run_telegram(config: dict, state: State, dry_run: bool = False) -> None:
    config = filter_projects_for_platform(config, "telegram")
    from tg.bot import run_telegram_bot
    from jobs import JobStore
    from sessions import SessionStore
    import api_server

    job_store = JobStore()
    session_store = SessionStore()

    api_cfg = config.get("api_server", {})
    host = api_cfg.get("host", "127.0.0.1")
    port = api_cfg.get("port", 8765)
    api_server.start(config, job_store, host=host, port=port)

    run_telegram_bot(
        config, state, process_message,
        dry_run=dry_run,
        job_store=job_store,
        session_store=session_store,
    )


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Claude Messaging Bridge")
    parser.add_argument(
        "--platform",
        choices=["imessage", "telegram"],
        help="Messaging platform to run (overrides config)",
    )
    parser.add_argument(
        "--config",
        default=Path(__file__).parent / "config.yaml",
        type=Path,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Process messages but don't send replies or call Claude",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    for _noisy in ("httpcore", "httpx", "urllib3", "telegram.vendor.ptb_urllib3",
                   "telegram.ext", "claude_code_sdk"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    config = load_config(args.config)

    platform = args.platform or config.get("platform")
    if not platform:
        parser.error("No platform specified. Set 'platform:' in config.yaml or pass --platform imessage|telegram.")

    state = State()
    # Clear saved project if it doesn't belong to the current platform
    if state.current_project:
        saved_proj = next((p for p in config.get("projects", []) if p["name"] == state.current_project), None)
        if saved_proj and platform not in saved_proj.get("platforms", ["imessage", "telegram"]):
            logger.info("Clearing saved project '%s' (not available on %s)", state.current_project, platform)
            state.current_project = None
    if not state.current_project:
        default = config.get("default_project")
        topics_configured = any(p.get("telegram_topic_id") for p in config.get("projects", []))
        if default and not (platform == "telegram" and topics_configured):
            state.set_project(default)

    logger.info("Platform: %s", platform)

    _acquire_singleton_lock(platform)

    if platform == "imessage":
        run_imessage(config, state, dry_run=args.dry_run)
    elif platform == "telegram":
        run_telegram(config, state, dry_run=args.dry_run)
    else:
        logger.error("Unknown platform: %s", platform)
        sys.exit(1)


if __name__ == "__main__":
    main()
