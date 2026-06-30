#!/usr/bin/env python3
"""
ask.py — Send an inline keyboard question to Telegram and return the user's choice.

Usage (CLI):
    python3 ask.py "Question?" "Option A" "Option B" "Option C"
    python3 ask.py --message-prefix "Context header" "Question?" "A" "B" "C"
    python3 ask.py --timeout 3600 "Question?" "A" "B"
    python3 ask.py --config /path/to/config.yaml "Question?" "A" "B"
    python3 ask.py --multi "Pick all that apply?" "A" "B" "C"

Stdout:
    Single-select: 0-based index of the selected option (e.g. "0", "1", "2")
    Multi-select:  space-separated indices of selected options (e.g. "0 2")
    "skip" if the user tapped Skip or the timeout elapsed

Exit codes:
    0  — success (including skip/timeout)
    1  — Telegram API error
    2  — configuration error

Library usage:
    from tg.ask import ask_user
    import asyncio
    index = asyncio.run(ask_user(
        question="Pick one?",
        options=["A", "B", "C"],
        bot_token="...",
        chat_id=123456789,
        message_prefix="Optional header",
        timeout_seconds=86400,
    ))
    # index is int (0-based) or None (skip/timeout)

Design note:
    Creates a job in jobs.db and sends the question via sendMessage (no getUpdates call).
    The main bot daemon handles button callbacks and marks the job responded.
    This avoids the Telegram API conflict that arises when two processes both call
    getUpdates on the same bot token.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import yaml


def load_config(config_path: Optional[Path] = None) -> dict:
    """Load config.yaml. Defaults to <script_dir>/../config.yaml."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(2)
    with open(config_path) as f:
        return yaml.safe_load(f)


def _format_message_text(
    question: str,
    options: list[str],
    message_prefix: Optional[str],
) -> str:
    parts = []
    if message_prefix:
        parts.append(message_prefix)
        parts.append("")
    parts.append(question)
    parts.append("")
    for i, option in enumerate(options, 1):
        parts.append(f"{i}. {option}")
    return "\n".join(parts)


def _build_ss_keyboard(job_id: str, options: list[str]):
    """Single-select keyboard: one numbered button per option, plus Skip."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [
        [InlineKeyboardButton(str(i + 1), callback_data=f"job:{job_id}:{i}")]
        for i in range(len(options))
    ]
    rows.append([InlineKeyboardButton("Skip ↩", callback_data=f"job:{job_id}:-1")])
    return InlineKeyboardMarkup(rows)


def _build_ms_keyboard(job_id: str, options: list[str], selected: set):
    """Multi-select keyboard: toggle buttons, Confirm, Skip."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [
        [InlineKeyboardButton(
            ("✓ " if i in selected else "") + str(i + 1),
            callback_data=f"job:{job_id}:t{i}",
        )]
        for i in range(len(options))
    ]
    rows.append([
        InlineKeyboardButton("✅ Confirm", callback_data=f"job:{job_id}:confirm"),
        InlineKeyboardButton("Skip ↩", callback_data=f"job:{job_id}:-1"),
    ])
    return InlineKeyboardMarkup(rows)


async def ask_user(
    question: str,
    options: list[str],
    bot_token: str,
    chat_id: int,
    message_prefix: Optional[str] = None,
    timeout_seconds: int = 86400,
    multi_select: bool = False,
) -> "Optional[int] | Optional[list[int]]":
    """
    Send an inline keyboard question and block until the user responds.

    Requires the main bot daemon to be running — it handles button callbacks and
    marks the job responded in jobs.db.  This function never calls getUpdates.

    Single-select (default): returns 0-based index, or None for skip/timeout.
    Multi-select: returns sorted list of selected indices, or None for skip/timeout.
    """
    from telegram import Bot

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from jobs import JobStore

    job_store = JobStore()
    text = _format_message_text(question, options, message_prefix)

    if multi_select:
        metadata = {"type": "multi_select", "options": options, "selected": []}
        job_id = job_store.create(text, buttons=None, on_response=None, metadata=metadata)
        keyboard = _build_ms_keyboard(job_id, options, set())
    else:
        job_id = job_store.create(text, buttons=None, on_response=None)
        keyboard = _build_ss_keyboard(job_id, options)

    async with Bot(token=bot_token) as bot:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = job_store.get(job_id)
        if job and job["status"] == "responded":
            action = job["action"]
            if action in ("-1", "skip"):
                return None
            if multi_select:
                return json.loads(action)
            idx = int(action)
            return idx if idx >= 0 else None
        await asyncio.sleep(0.5)

    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send an inline keyboard question to Telegram and return the user's choice."
    )
    parser.add_argument("question", help="The question to ask")
    parser.add_argument("options", nargs="+", help="The options to present as buttons")
    parser.add_argument(
        "--message-prefix",
        default=None,
        help="Optional header line shown above the question",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=86400,
        help="Seconds to wait for a response (default: 86400 = 24h)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: <script_dir>/../config.yaml)",
    )
    parser.add_argument(
        "--multi",
        action="store_true",
        default=False,
        help="Allow selecting multiple options; prints space-separated indices or 'skip'",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    tg = config.get("telegram", {})
    bot_token = tg.get("bot_token", "")
    chat_id = tg.get("allowed_user_id")

    if not bot_token:
        print("Error: telegram.bot_token not set in config.yaml", file=sys.stderr)
        sys.exit(2)
    if not chat_id:
        print("Error: telegram.allowed_user_id not set in config.yaml", file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(
            ask_user(
                question=args.question,
                options=args.options,
                bot_token=bot_token,
                chat_id=int(chat_id),
                message_prefix=args.message_prefix,
                timeout_seconds=args.timeout,
                multi_select=args.multi,
            )
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if result is None:
        print("skip")
    elif isinstance(result, list):
        print(" ".join(str(i) for i in result))
    else:
        print(result)


if __name__ == "__main__":
    main()
