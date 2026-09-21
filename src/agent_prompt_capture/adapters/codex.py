"""Codex CLI: the native hooks payloads and the legacy ``notify`` payload.

Three shapes are accepted (see ``docs/research/hook-specs.md`` §2.3, §2.5, §5):

``a)`` the hooks ``UserPromptSubmit`` payload (stdin JSON)::

    {"hook_event_name": "UserPromptSubmit", "prompt": "...", "session_id": "...",
     "cwd": "...", "model": "...", "permission_mode": "default", "turn_id": "...",
     "transcript_path": "... or null"}

``b)`` the hooks ``Stop`` payload -> a :class:`RawTurnEnd`.

``c)`` the legacy ``notify`` payload, which Codex delivers as the *final argv argument*
with stdin closed, and which fires only at agent turn completion::

    {"type": "agent-turn-complete", "thread-id": "...", "turn-id": "...", "cwd": "...",
     "client": "codex-tui", "input-messages": [...], "last-assistant-message": "..."}

Case ``c`` is a turn end that also carries the prompt, so the returned
:class:`RawPrompt` has ``turn_end_ts`` set and ingest marks it ended immediately.
Any other ``type`` or ``hook_event_name`` yields ``None``, never an error, and unknown
extra keys never fail.
"""

from __future__ import annotations

from typing import Any

from ..pii import scrub_path
from . import Adapted, RawPrompt, RawTurnEnd, coerce_dict, coerce_str, project_from_cwd

__all__ = ["parse", "TURN_COMPLETE", "PROMPT_EVENT", "TURN_END_EVENT"]

TURN_COMPLETE = "agent-turn-complete"
PROMPT_EVENT = "UserPromptSubmit"
TURN_END_EVENT = "Stop"

_SESSION_KEYS = (
    "session_id",
    "sessionId",
    "thread_id",
    "thread-id",
    "threadId",
    "conversation_id",
)
_TURN_KEYS = ("turn_id", "turn-id", "turnId")
_PROMPT_KEYS = ("prompt", "user_prompt", "message", "text", "input")
_CWD_KEYS = ("cwd", "workdir", "working_directory")
_META_KEYS = ("model", "permission_mode", "client", "agent_type", "agent_id")


def _first(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = coerce_str(data.get(key))
        if value:
            return value
    return None


def _last_input_message(data: dict[str, Any]) -> str | None:
    """The last non-blank entry of ``input-messages``."""
    for item in reversed(_input_messages(data)):
        if isinstance(item, str) and item.strip():
            return item
        if isinstance(item, dict):
            text = coerce_str(item.get("text")) or coerce_str(item.get("content"))
            if text:
                return text
    return None


def _input_messages(data: dict[str, Any]) -> list[Any]:
    messages = data.get("input-messages")
    if messages is None:
        messages = data.get("input_messages")
    return messages if isinstance(messages, list) else []


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    turn_id = _first(data, _TURN_KEYS)
    if turn_id:
        metadata["turn_id"] = turn_id
    for key in _META_KEYS:
        value = coerce_str(data.get(key))
        if value:
            metadata[key] = value
    transcript = coerce_str(data.get("transcript_path"))  # nullable in the schema
    if transcript:
        metadata["transcript_path"] = scrub_path(transcript)
    return metadata


def parse(payload: Any) -> Adapted:
    data = coerce_dict(payload)
    session_id = _first(data, _SESSION_KEYS)
    cwd = _first(data, _CWD_KEYS)
    ts = coerce_str(data.get("ts")) or coerce_str(data.get("timestamp"))
    metadata = _metadata(data)

    # (a) / (b) native hooks -------------------------------------------
    event = coerce_str(data.get("hook_event_name"))
    if event:
        metadata["hook_event_name"] = event
        if event == TURN_END_EVENT:
            return RawTurnEnd(session_id=session_id, ts=ts, metadata=metadata)
        if event != PROMPT_EVENT:
            return None
        prompt = _first(data, _PROMPT_KEYS)
        if not prompt:
            return None
        return RawPrompt(
            prompt=prompt,
            session_id=session_id,
            cwd=cwd,
            project=project_from_cwd(cwd),
            metadata=metadata,
            ts=ts,
        )

    # (c) legacy notify -------------------------------------------------
    event_type = coerce_str(data.get("type"))
    if event_type:
        metadata["type"] = event_type
        if event_type != TURN_COMPLETE:
            return None
        prompt = _last_input_message(data)
        if not prompt:
            return None
        from ..models import utc_now_iso  # noqa: PLC0415 - keep the hot path cheap

        turn_complete_ts = ts or utc_now_iso()
        metadata["turn_complete_ts"] = turn_complete_ts
        metadata["input_message_count"] = len(_input_messages(data))
        if coerce_str(data.get("last-assistant-message")) or coerce_str(
            data.get("last_assistant_message")
        ):
            metadata["has_assistant_reply"] = True
        return RawPrompt(
            prompt=prompt,
            session_id=session_id,
            cwd=cwd,
            project=project_from_cwd(cwd),
            metadata=metadata,
            ts=ts,
            turn_end_ts=turn_complete_ts,
        )

    # Neither marker: accept a bare prompt payload, otherwise ignore.
    prompt = _first(data, _PROMPT_KEYS) or _last_input_message(data)
    if not prompt:
        return None
    return RawPrompt(
        prompt=prompt,
        session_id=session_id,
        cwd=cwd,
        project=project_from_cwd(cwd),
        metadata=metadata,
        ts=ts,
    )
