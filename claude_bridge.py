"""
Invokes Claude via the `claude -p` CLI subprocess.

Three entry points, all backed by the same `claude -p` binary:
  - ask_claude()        sync, returns response text. iMessage + DM fallback.
  - ask_claude_json()   sync, returns response + session_id + cost. HTTP API.
  - ask_claude_stream() async, streams progress + returns session_id + cost.
                        Telegram topics, with per-topic --session-id/--resume.

Tool permissions are governed by each project's `allowed_tools` in config.yaml,
passed through as --allowedTools. There is no interactive permission prompt in
print mode — tools outside the allowlist are denied automatically.
"""

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "LS"]

# stderr substrings that indicate a --resume target no longer exists, so the
# caller can transparently retry with a fresh session instead of erroring out.
_MISSING_SESSION_MARKERS = ("no conversation found", "session not found", "no session")


def ask_claude(prompt: str, project_path: str, allowed_tools: list[str], timeout: int = 120) -> str:
    """
    Run claude -p with the given prompt in the project directory.
    Returns the response text, or an error message if it fails.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return "Error: claude CLI not found in PATH."

    project_dir = Path(project_path).resolve()
    if not project_dir.is_dir():
        return f"Error: project directory not found: {project_path}"

    tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
    tools_arg = ",".join(tools)

    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "text",
        "--allowedTools", tools_arg,
    ]

    logger.info("Invoking claude in %s (tools: %s)", project_dir, tools_arg)
    logger.debug("Prompt: %s", prompt[:200])

    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            err = result.stderr.strip() or "Unknown error"
            logger.error("Claude exited with code %d: %s", result.returncode, err)
            return f"Claude error (code {result.returncode}): {err[:500]}"

        output = result.stdout.strip()
        if not output:
            return "Claude returned an empty response."
        return output

    except subprocess.TimeoutExpired:
        logger.error("Claude timed out after %ds", timeout)
        return f"Request timed out after {timeout} seconds."
    except Exception as e:
        logger.error("Unexpected error calling claude: %s", e)
        return f"Unexpected error: {e}"


def _build_cmd(
    claude_bin: str,
    prompt: str,
    tools: list[str],
    output_format: str,
    session_id: str | None,
    resume: bool,
) -> list[str]:
    """Assemble a `claude -p` argv shared by the json and streaming paths."""
    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", output_format,
        "--allowedTools", ",".join(tools),
    ]
    if output_format == "stream-json":
        cmd.append("--verbose")  # required by the CLI for stream-json print mode
    if session_id:
        # --resume continues an existing conversation; --session-id pins the id
        # of a brand-new one so we can resume it deterministically next time.
        cmd += (["--resume", session_id] if resume else ["--session-id", session_id])
    return cmd


def ask_claude_json(
    prompt: str,
    project_path: str,
    allowed_tools: list[str],
    timeout: int = 600,
    session_id: str | None = None,
    resume: bool = False,
) -> dict:
    """
    Run `claude -p --output-format json` and return a structured result:
      {response, session_id, cost_usd, num_turns, ok}
    Synchronous — used by the HTTP API server. Pass session_id + resume=True to
    continue a conversation, or session_id + resume=False to pin a new one.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return {"response": "Error: claude CLI not found in PATH.", "session_id": session_id, "ok": False}

    project_dir = Path(project_path).resolve()
    if not project_dir.is_dir():
        return {"response": f"Error: project directory not found: {project_path}", "session_id": session_id, "ok": False}

    tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
    cmd = _build_cmd(claude_bin, prompt, tools, "json", session_id, resume)

    logger.info("Invoking claude (json) in %s (resume=%s)", project_dir, resume)
    try:
        result = subprocess.run(
            cmd, cwd=str(project_dir), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"response": f"Request timed out after {timeout} seconds.", "session_id": session_id, "ok": False}
    except Exception as e:
        return {"response": f"Unexpected error: {e}", "session_id": session_id, "ok": False}

    if result.returncode != 0:
        err = result.stderr.strip() or "Unknown error"
        logger.error("Claude (json) exited %d: %s", result.returncode, err)
        return {"response": f"Claude error: {err[:500]}", "session_id": session_id, "ok": False}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"response": result.stdout.strip() or "Claude returned no output.", "session_id": session_id, "ok": False}

    is_error = bool(data.get("is_error", False))
    response = data.get("result", "")
    if not response:
        errors_text = " ".join(data.get("errors", []) or [])
        response = (
            f"Claude error: {errors_text[:500] or data.get('subtype', 'unknown error')}"
            if is_error else "Claude returned an empty response."
        )
    return {
        "response": response,
        "session_id": data.get("session_id") or session_id,
        "cost_usd": float(data.get("total_cost_usd", 0.0) or 0.0),
        "num_turns": int(data.get("num_turns", 0) or 0),
        "ok": not is_error,
    }


async def _terminate(proc: "asyncio.subprocess.Process") -> None:
    """Terminate a subprocess, escalating to kill if it doesn't exit promptly."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def ask_claude_stream(
    prompt: str,
    project_path: str,
    allowed_tools: list[str],
    session_id: str,
    resume: bool,
    timeout: int = 600,
    on_progress=None,
) -> dict:
    """
    Run `claude -p --output-format stream-json` and stream progress.

    Calls the async `on_progress(snapshot_text)` callback as assistant text and
    tool-use events arrive, then returns:
      {response, session_id, cost_usd, num_turns, ok, error, resume_failed}

    Cancelling the awaiting task terminates the underlying subprocess (this is
    how /kill and /stop interrupt a running turn).
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return {"response": "Error: claude CLI not found in PATH.", "session_id": session_id, "ok": False}

    project_dir = Path(project_path).resolve()
    if not project_dir.is_dir():
        return {"response": f"Error: project directory not found: {project_path}", "session_id": session_id, "ok": False}

    tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
    cmd = _build_cmd(claude_bin, prompt, tools, "stream-json", session_id, resume)

    logger.info("Invoking claude (stream) in %s (resume=%s)", project_dir, resume)
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    final: dict | None = None
    captured_session = session_id
    transcript: list[str] = []

    async def _consume() -> None:
        nonlocal final, captured_session
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if event.get("session_id"):
                captured_session = event["session_id"]
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        transcript.append(block["text"])
                    elif btype == "tool_use":
                        transcript.append(f"🔧 {block.get('name', 'tool')}…")
                if on_progress:
                    snapshot = "\n".join(transcript).strip()
                    if snapshot:
                        await on_progress(snapshot)
            elif etype == "result":
                final = event

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        await _terminate(proc)
        return {"response": f"Request timed out after {timeout} seconds.", "session_id": captured_session, "ok": False}
    except asyncio.CancelledError:
        await _terminate(proc)
        raise

    if final is not None:
        is_error = bool(final.get("is_error", False))
        errors_text = " ".join(final.get("errors", []) or [])
        resume_failed = resume and any(m in errors_text.lower() for m in _MISSING_SESSION_MARKERS)
        response = final.get("result") or "\n".join(transcript).strip()
        if not response:
            response = (
                f"Claude error: {errors_text[:500] or final.get('subtype', 'unknown error')}"
                if is_error else "Claude returned an empty response."
            )
        return {
            "response": response,
            "session_id": final.get("session_id") or captured_session,
            "cost_usd": float(final.get("total_cost_usd", 0.0) or 0.0),
            "num_turns": int(final.get("num_turns", 0) or 0),
            "ok": not is_error,
            "resume_failed": resume_failed,
        }

    # No result event — read stderr to report the failure, and flag the case
    # where --resume failed because the session is gone so the caller can retry.
    stderr = ""
    if proc.stderr is not None:
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
    resume_failed = resume and any(m in stderr.lower() for m in _MISSING_SESSION_MARKERS)
    logger.error("Claude (stream) produced no result. stderr: %s", stderr[:500])
    return {
        "response": "\n".join(transcript).strip() or f"Claude error: {stderr[:500] or 'no output'}",
        "session_id": captured_session,
        "cost_usd": 0.0,
        "num_turns": 0,
        "ok": False,
        "error": stderr,
        "resume_failed": resume_failed,
    }
