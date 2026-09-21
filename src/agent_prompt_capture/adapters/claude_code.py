"""Claude Code CLI hooks (``UserPromptSubmit`` and ``Stop``).

Note that Claude Code *on the web* (claude.ai/code) does not run user hooks, so those
prompts are captured by the Chrome extension as the ``claude_code_web`` source, not here.
"""

from __future__ import annotations

from typing import Any

from ..pii import scrub_path
from . import (
    Adapted,
    AdapterError,
    RawPrompt,
    RawTurnEnd,
    coerce_dict,
    coerce_str,
    project_from_cwd,
)

__all__ = ["parse", "PROMPT_EVENT", "TURN_END_EVENT"]

PROMPT_EVENT = "UserPromptSubmit"
TURN_END_EVENT = "Stop"

_META_KEYS = (
    "permission_mode",
    "hook_event_name",
    "model",
    "prompt_id",
    "agent_type",
    "agent_id",
)


def parse(payload: Any) -> Adapted:
    """Branch on ``hook_event_name``; anything unexpected is an :class:`AdapterError`."""
    data = coerce_dict(payload)
    event = coerce_str(data.get("hook_event_name"))

    if event == TURN_END_EVENT:
        if data.get("stop_hook_active") is True:
            # Claude Code is continuing *because of* a stop hook: not a real turn end.
            return None
        if _has_background_work(data):
            # hook-specs.md 6.a: a non-empty ``background_tasks`` means "paused waiting
            # on background work", not "done". Ending the turn here would understate
            # agent time; the next real Stop closes it.
            return None
        return RawTurnEnd(
            session_id=coerce_str(data.get("session_id")),
            ts=coerce_str(data.get("ts")),
            metadata=_metadata(data),
        )

    if event != PROMPT_EVENT:
        raise AdapterError(
            f"claude_code: unsupported hook_event_name {event!r}"
            f" (expected {PROMPT_EVENT!r} or {TURN_END_EVENT!r})"
        )

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None

    cwd = coerce_str(data.get("cwd"))
    return RawPrompt(
        prompt=prompt,
        session_id=coerce_str(data.get("session_id")),
        cwd=cwd,
        project=project_from_cwd(cwd),
        metadata=_metadata(data),
        ts=coerce_str(data.get("ts")),
    )


def _has_background_work(data: dict[str, Any]) -> bool:
    """``background_tasks`` is present and non-empty (the registry is reachable)."""
    tasks = data.get("background_tasks")
    return bool(tasks) if isinstance(tasks, list) else False


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in _META_KEYS:
        value = data.get(key)
        if value is not None:
            metadata[key] = value
    # transcript_path embeds the home-directory username: never store it raw.
    transcript = coerce_str(data.get("transcript_path"))
    if transcript:
        metadata["transcript_path"] = scrub_path(transcript)
    return metadata
