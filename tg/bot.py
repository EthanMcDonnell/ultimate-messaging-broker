"""
Telegram platform adapter.
Handles Telegram-specific I/O; delegates all message routing and Claude
invocation to the process_message function injected from main.py.

Job callbacks (inline keyboard buttons posted via the HTTP API or permission
prompts from the SDK hook) are handled here via handle_job_callback.
Callback data format: "job:<id>:<action>".
"""

import asyncio
import logging
import time
import uuid
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from claude_bridge import ask_claude_stream
from security import PerUserRateLimiter, redact_secrets, sanitize_prompt
from state import State

logger = logging.getLogger(__name__)


_TABLE_CHARS = frozenset("│├┌└┐┘┤┬┴┼")


def _format_for_telegram(text: str) -> str:
    """Wrap ASCII table sections in code fences for monospace rendering in Telegram."""
    lines = text.split("\n")
    out = []
    in_table = False
    for line in lines:
        is_table = any(c in line for c in _TABLE_CHARS)
        if is_table and not in_table:
            out.append("```")
            in_table = True
        elif not is_table and in_table:
            out.append("```")
            in_table = False
        out.append(line)
    if in_table:
        out.append("```")
    return "\n".join(out)


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """Split a long response into Telegram-sized chunks."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def run_telegram_bot(
    config: dict,
    state: State,
    process_fn: Callable[[str, dict, State], str],
    dry_run: bool = False,
    job_store=None,
    session_store=None,
) -> None:
    """
    Start the Telegram bot.

    config:       shared config dict
    state:        shared project state
    process_fn:   process_message(text, config, state) → str  (DM / non-topic fallback)
    dry_run:      if True, log replies instead of sending them
    job_store:    JobStore for permission callbacks and external API jobs
    session_store: SessionStore for Claude session persistence per topic
    """
    tg_cfg = config.get("telegram", {})
    bot_token = tg_cfg["bot_token"]
    allowed_user_id = int(tg_cfg["allowed_user_id"])

    topic_map: dict[int, dict] = {
        p["telegram_topic_id"]: p
        for p in config.get("projects", [])
        if p.get("telegram_topic_id")
    }

    rate_limits = config.get("rate_limits", {})
    # Token bucket: burst of up to `messages_per_minute` tokens, refill at that rate/60 per second
    msgs_per_min = rate_limits.get("messages_per_minute", 10)
    msg_limiter = PerUserRateLimiter(rate=msgs_per_min / 60.0, burst=msgs_per_min)

    def _is_authorized(update: Update) -> bool:
        if update.effective_user.id == allowed_user_id:
            return True
        logger.warning("Unauthorized Telegram access from user %s", update.effective_user.id)
        return False

    async def cmd_start(update: Update, context) -> None:
        if not _is_authorized(update):
            return
        await update.message.reply_text(
            f"Connected. Current project: {state.current_project or '(none)'}.\n"
            'Send a message, or say "list projects" / "switch to [name]".'
        )

    async def cmd_help(update: Update, context) -> None:
        if not _is_authorized(update):
            return
        await update.message.reply_text(
            "Commands:\n"
            "  /start — connection check\n"
            "  /status — platform + project info\n"
            "  /help — this message\n"
            "  /new — start a fresh Claude session for this topic\n"
            "  /kill — stop the current task in this topic\n"
            "  /killall, /stopall — cancel every running task\n\n"
            "If commands are ignored in a group: open @BotFather, send /setprivacy, "
            "pick this bot, choose Disable, then restart the bot."
        )

    async def cmd_status(update: Update, context) -> None:
        if not _is_authorized(update):
            return
        import shutil as _shutil
        claude_found = _shutil.which("claude") is not None
        projects = config.get("projects", [])
        proj_lines = []
        for p in projects:
            tid = p.get("telegram_topic_id")
            stats = session_store.get_stats(tid) if (session_store and tid) else None
            suffix = ""
            if stats:
                suffix = f" — {stats['turns']} turn(s), ${stats['cost_usd']:.4f}"
            marker = "→" if p["name"] == state.current_project else "•"
            proj_lines.append(f"  {marker} {p['name']}{suffix}")
        proj_list = "\n".join(proj_lines)
        await update.message.reply_text(
            f"Platform: telegram\n"
            f"Current project: {state.current_project or '(none)'}\n"
            f"Claude CLI: {'found' if claude_found else 'NOT FOUND'}\n"
            f"Projects:\n{proj_list or '  (none configured)'}"
        )

    # task_key → asyncio.Task running the current `claude -p` turn for that topic.
    # Cancelling the task terminates the underlying subprocess (see ask_claude_stream).
    running_tasks: dict[int, asyncio.Task] = {}

    async def cmd_new(update: Update, context) -> None:
        if not _is_authorized(update):
            return
        thread_id = update.message.message_thread_id
        if session_store and thread_id is not None:
            session_store.delete(thread_id)
            await update.message.reply_text("Starting a fresh Claude session.")
        else:
            await update.message.reply_text("No active session to clear.")

    async def cmd_kill(update: Update, context) -> None:
        if not _is_authorized(update):
            return
        thread_id = update.message.message_thread_id
        key = thread_id if thread_id is not None else update.effective_chat.id
        logger.info("cmd_kill: key=%r running_tasks keys=%s", key, list(running_tasks.keys()))
        task = running_tasks.get(key)
        if task and not task.done():
            task.cancel()
            await update.message.reply_text("⏹ Stopping…")
            return
        await update.message.reply_text("Nothing running.")

    async def cmd_killall(update: Update, context) -> None:
        if not _is_authorized(update):
            return
        count = sum(1 for t in running_tasks.values() if not t.done())
        for task in list(running_tasks.values()):
            task.cancel()
        running_tasks.clear()
        if count:
            await update.message.reply_text(f"⏹ Cancelled {count} task(s).")
        else:
            await update.message.reply_text("Nothing running.")

    # Slash-command fallback: if Telegram delivered "/cmd" but PTB's CommandHandler
    # didn't fire (privacy mode, weird formatting, etc.), we still dispatch it here.
    slash_dispatch: dict[str, Callable] = {}

    async def handle_message(update: Update, context) -> None:
        if not _is_authorized(update):
            return

        text = update.message.text
        if not text:
            return

        if text.startswith("/"):
            first = text.split(maxsplit=1)[0]
            cmd = first[1:].split("@", 1)[0].lower()
            handler = slash_dispatch.get(cmd)
            if handler:
                logger.info("Slash fallback dispatching /%s", cmd)
                await handler(update, context)
                return
            # Unknown slash — let it fall through as a regular message so Claude
            # Code slash commands (/review, /init, /compact, etc.) reach the TUI.

        user_id = update.effective_user.id
        if not await msg_limiter.allow(user_id):
            await update.message.reply_text("Rate limit exceeded. Please slow down.")
            return

        thread_id = update.message.message_thread_id
        topic_project = topic_map.get(thread_id) if thread_id else None
        task_key = thread_id if thread_id is not None else update.effective_chat.id

        if topic_project and not topic_project.get("claude_enabled", True):
            return

        if dry_run:
            label = f"topic:{thread_id} ({topic_project['name']})" if topic_project else "DM/general"
            reply = f"[DRY RUN] [{label}] Would process: {text[:100]}"
            logger.info("[DRY RUN] %s", reply)
            await update.message.reply_text(reply)
            return

        # Reject (don't queue) a new message in a topic that's still working.
        # Queueing was deliberately removed: we want /stop and /kill to actually
        # stop, not silently advance to the next queued prompt.
        busy = running_tasks.get(task_key)
        if busy and not busy.done():
            await update.message.reply_text(
                "⚠️ Still working on the previous message in this topic. "
                "Send /stop to interrupt it, then resend."
            )
            return

        placeholder = await update.message.reply_text("…")

        if topic_project and job_store:
            # Per-topic session continuity: resume the topic's Claude session if
            # one is still active, otherwise pin a fresh UUID we can resume later.
            stored_sid = session_store.get_active(thread_id) if (session_store and thread_id) else None
            if session_store and thread_id and stored_sid is None:
                session_store.pop_if_stale(thread_id)  # drop any expired row
            resume = stored_sid is not None
            session_id = stored_sid or str(uuid.uuid4())
            allowed_tools = topic_project.get("allowed_tools", [])
            timeout = config.get("claude", {}).get("timeout", 600)
            prompt = sanitize_prompt(text)
            project_path = topic_project["path"]

            # Throttled live-progress: edit the placeholder as Claude streams,
            # but no more than once every 2s to stay under Telegram edit limits.
            last_edit = 0.0

            async def _on_progress(snapshot: str) -> None:
                nonlocal last_edit
                now = time.monotonic()
                if now - last_edit < 2.0:
                    return
                last_edit = now
                try:
                    await placeholder.edit_text(snapshot[-4096:])
                except Exception:
                    pass

            async def _run_claude() -> dict:
                res = await ask_claude_stream(
                    prompt, project_path, allowed_tools,
                    session_id=session_id, resume=resume, timeout=timeout,
                    on_progress=_on_progress,
                )
                # If the resume target vanished (e.g. ~/.claude was cleared),
                # transparently retry once as a brand-new session.
                if res.get("resume_failed"):
                    logger.info("Resume failed for topic %s — starting fresh session", thread_id)
                    res = await ask_claude_stream(
                        prompt, project_path, allowed_tools,
                        session_id=str(uuid.uuid4()), resume=False, timeout=timeout,
                        on_progress=_on_progress,
                    )
                return res

            task = asyncio.create_task(_run_claude())
            running_tasks[task_key] = task
            try:
                res = await task
                reply = res.get("response") or "(no response)"
                new_sid = res.get("session_id") or session_id
                if res.get("ok") and thread_id and session_store:
                    session_store.upsert(
                        thread_id, topic_project["name"], new_sid,
                        turns=res.get("num_turns", 0), cost_usd=res.get("cost_usd", 0.0),
                    )
            except asyncio.CancelledError:
                logger.info("Claude task cancelled for topic %s", thread_id)
                await placeholder.edit_text("⏹ Stopped.")
                return
            except Exception as e:
                logger.exception("Claude error for topic %s", thread_id)
                reply = f"Error: {e}"
            finally:
                running_tasks.pop(task_key, None)
        else:
            reply = process_fn(text, config, state)

        state.save()
        logger.info("Reply (%d chars): %s...", len(redact_secrets(reply)), redact_secrets(reply[:60]))

        async def _send_with_retry(coro_fn, retries: int = 2):
            for attempt in range(retries + 1):
                try:
                    return await coro_fn()
                except RetryAfter as e:
                    if attempt < retries:
                        await asyncio.sleep(e.retry_after + 1)
                    else:
                        raise

        formatted = _format_for_telegram(reply)
        chunks = _split_message(formatted)
        md = "Markdown"
        try:
            await _send_with_retry(lambda: placeholder.edit_text(chunks[0], parse_mode=md))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                await _send_with_retry(lambda: update.message.reply_text(chunks[0], parse_mode=md))
        except (RetryAfter, Exception):
            await _send_with_retry(lambda: update.message.reply_text(chunks[0], parse_mode=md))
        for chunk in chunks[1:]:
            await _send_with_retry(lambda: update.message.reply_text(chunk, parse_mode=md))

    async def handle_job_callback(update: Update, context) -> None:
        try:
            cq = update.callback_query
            logger.debug("Callback query received: data=%r", cq.data if cq else None)

            if not cq or not cq.data or not cq.data.startswith("job:"):
                return

            parts = cq.data.split(":", 2)
            if len(parts) != 3:
                logger.warning("Malformed callback data: %r", cq.data)
                await cq.answer()
                return

            _, job_id, action = parts
            logger.info("Job callback: job_id=%s action=%s", job_id, action)

            if not job_store:
                await cq.answer(text="No job store available.")
                return

            job = job_store.get(job_id)
            if not job:
                logger.warning("Job not found: %s", job_id)
                await cq.answer(text="Job not found.")
                return

            if job["status"] == "responded":
                logger.info("Job %s already responded", job_id)
                await cq.answer(text="Already responded.")
                return

            # Multi-select toggle: update state without finalizing the job
            if action.startswith("t") and action[1:].isdigit():
                meta = job.get("metadata") or {}
                if meta.get("type") == "multi_select":
                    selected = set(meta.get("selected", []))
                    selected ^= {int(action[1:])}
                    meta["selected"] = sorted(selected)
                    job_store.update_metadata(job_id, meta)
                    keyboard = InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton(
                                ("✓ " if i in selected else "") + str(i + 1),
                                callback_data=f"job:{job_id}:t{i}",
                            )]
                            for i in range(len(meta["options"]))
                        ] + [[
                            InlineKeyboardButton("✅ Confirm", callback_data=f"job:{job_id}:confirm"),
                            InlineKeyboardButton("Skip ↩", callback_data=f"job:{job_id}:-1"),
                        ]]
                    )
                    await cq.answer()
                    await cq.edit_message_reply_markup(reply_markup=keyboard)
                    return

            # Multi-select confirm: finalize with the selected-indices JSON
            if action == "confirm":
                import json as _json
                meta = job.get("metadata") or {}
                if meta.get("type") == "multi_select":
                    action = _json.dumps(meta.get("selected", []))

            job_store.respond(job_id, action)
            await cq.answer()
            await cq.edit_message_reply_markup(reply_markup=None)

            chat_id = update.effective_chat.id
            thread_id = cq.message.message_thread_id if cq.message else None
            logger.info("Dispatching job %s: action=%s chat_id=%s thread_id=%s", job_id, action, chat_id, thread_id)

            from dispatcher import dispatch
            await dispatch(job, action, config, bot=context.bot, chat_id=chat_id, thread_id=thread_id)
            logger.info("Job %s dispatched successfully", job_id)

        except Exception:
            logger.exception("Error handling job callback for update: %s", update)

    # Populate the slash-fallback table the handle_message helper consults.
    # Keys are bare command names without the leading slash.
    slash_dispatch.update({
        "start": cmd_start,
        "status": cmd_status,
        "help": cmd_help,
        "new": cmd_new,
        "kill": cmd_kill,
        "killall": cmd_killall, "stopall": cmd_killall,
    })

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("kill", cmd_kill, block=False))
    app.add_handler(CommandHandler(["killall", "stopall"], cmd_killall, block=False))
    # Filter intentionally includes commands so the slash-fallback inside
    # handle_message can catch any /cmd that PTB's CommandHandler missed.
    app.add_handler(MessageHandler(filters.TEXT, handle_message, block=False))
    if job_store:
        app.add_handler(CallbackQueryHandler(handle_job_callback, pattern=r"^job:"))

    from telegram.error import Conflict, NetworkError

    async def _error_handler(update, context) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.warning("Telegram conflict (another getUpdates caller): %s", err)
        elif isinstance(err, (RetryAfter, NetworkError)):
            logger.warning("Telegram network issue (auto-retry): %s", err)
        else:
            logger.exception("Unhandled PTB error", exc_info=err)

    app.add_error_handler(_error_handler)

    async def _post_init(application):
        # Brief pause to let any previous instance's Telegram long-poll connection
        # fully close before we start polling — avoids Conflict on rapid restarts.
        await asyncio.sleep(4)
        try:
            await application.bot.set_my_commands([
                ("start",   "Connection check"),
                ("status",  "Platform + project info"),
                ("help",    "List commands"),
                ("new",     "Clear this topic's session"),
                ("kill",    "Stop the current task in this topic"),
                ("killall", "Cancel every running task"),
            ])
        except Exception:
            logger.exception("set_my_commands failed (non-fatal)")
    app.post_init = _post_init

    topic_count = len(topic_map)
    if topic_count:
        logger.info("Telegram bot started. %d topic(s) configured.", topic_count)
    else:
        logger.info("Telegram bot started. Project: %s", state.current_project)
    if dry_run:
        logger.info("DRY RUN MODE — messages will not be sent")
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)
