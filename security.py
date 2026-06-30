"""
Security: sender validation, input sanitization, rate limiting.
"""

import asyncio
import re
import time
import logging
from collections import defaultdict, deque
from pathlib import Path

logger = logging.getLogger(__name__)

_APPLESCRIPT_ESCAPE_RE = re.compile(r'([\\"])')

# Patterns redacted from log output to prevent credential leaks
_SECRET_PATTERNS = [
    (re.compile(r'(sk-ant-[A-Za-z0-9\-_]{10})[A-Za-z0-9\-_]+'), r'\1…'),
    (re.compile(r'(AKIA[A-Z0-9]{4})[A-Z0-9]{12}'), r'\1…'),
    (re.compile(r'(?i)(authorization:\s*bearer\s+\S{6})\S+'), r'\1…'),
    (re.compile(r'(?i)(bot_token["\s:=]+\S{6})\S+'), r'\1…'),
    (re.compile(r'(?i)(api[_-]?key["\s:=]+\S{6})\S+'), r'\1…'),
    (re.compile(r'(?i)(secret["\s:=]+\S{6})\S+'), r'\1…'),
]


def redact_secrets(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_for_applescript(text: str) -> str:
    return _APPLESCRIPT_ESCAPE_RE.sub(r"\\\1", text)


def sanitize_prompt(text: str) -> str:
    text = text.replace("\x00", "")
    return text[:8000]


def validate_sender(chat_identifier: str, allowed_sender: str) -> bool:
    def norm(s: str) -> str:
        return s.strip().lower().replace(" ", "")
    return norm(chat_identifier) == norm(allowed_sender)


def validate_tool_path(path_str: str, project_path: str) -> bool:
    """
    Return True if path_str resolves to a location inside project_path.
    Prevents a can_use_tool auto-allow from silently permitting cross-project reads.
    """
    try:
        target = Path(path_str).resolve()
        root = Path(project_path).resolve()
        target.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


class RateLimiter:
    """
    Sliding window rate limiter (single shared bucket, used for iMessage).
    """
    def __init__(self, max_count: int, window_seconds: int):
        self.max_count = max_count
        self.window = window_seconds
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.time()
        while self._timestamps and self._timestamps[0] < now - self.window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_count:
            logger.warning("Rate limit exceeded: %d events in %ds window", self.max_count, self.window)
            return False
        self._timestamps.append(now)
        return True


class PerUserRateLimiter:
    """
    Token bucket rate limiter with per-user buckets and automatic cleanup.
    Each user gets their own bucket; stale buckets are evicted after idle_ttl seconds.
    """

    def __init__(self, rate: float, burst: int, idle_ttl: int = 300):
        """
        rate:     tokens refilled per second
        burst:    maximum bucket capacity (allows short bursts)
        idle_ttl: seconds of inactivity before a user's bucket is evicted
        """
        self.rate = rate
        self.burst = burst
        self.idle_ttl = idle_ttl
        self._buckets: dict[int, dict] = {}
        self._lock = asyncio.Lock()

    def _refill(self, bucket: dict, now: float) -> None:
        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = min(self.burst, bucket["tokens"] + elapsed * self.rate)
        bucket["last_refill"] = now

    async def allow(self, user_id: int) -> bool:
        async with self._lock:
            now = time.monotonic()
            if user_id not in self._buckets:
                self._buckets[user_id] = {"tokens": float(self.burst), "last_refill": now, "last_used": now}
            bucket = self._buckets[user_id]
            self._refill(bucket, now)
            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                bucket["last_used"] = now
                return True
            logger.warning("Per-user rate limit exceeded for user %s", user_id)
            return False

    async def cleanup(self) -> None:
        """Evict buckets idle longer than idle_ttl. Call periodically."""
        async with self._lock:
            cutoff = time.monotonic() - self.idle_ttl
            stale = [uid for uid, b in self._buckets.items() if b["last_used"] < cutoff]
            for uid in stale:
                del self._buckets[uid]
