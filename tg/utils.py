"""Shared Telegram utilities used by both the bot and the HTTP API server."""


def build_inline_keyboard(job_id: str, buttons: list) -> dict:
    """Build a raw Telegram inline_keyboard dict. Suitable for urllib and python-telegram-bot."""
    rows = []
    for btn in buttons:
        if isinstance(btn, str):
            label, action = btn, btn.lower()
        else:
            label, action = btn["label"], btn["action"]
        rows.append([{"text": label, "callback_data": f"job:{job_id}:{action}"}])
    return {"inline_keyboard": rows}
