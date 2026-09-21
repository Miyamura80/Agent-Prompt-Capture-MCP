"""OpenCode plugin payloads (``chat.message`` and the ``session.idle`` turn end)."""

from __future__ import annotations

from typing import Any

from . import Adapted, RawPrompt, RawTurnEnd, coerce_dict, coerce_str, project_from_cwd

__all__ = ["parse"]

_TURN_END_EVENTS = {"turn_end", "turn-end", "session.idle", "session_idle"}


def parse(payload: Any) -> Adapted:
    data = coerce_dict(payload)

    event = coerce_str(data.get("event"))
    if event and event in _TURN_END_EVENTS:
        return RawTurnEnd(
            session_id=coerce_str(data.get("session_id")) or coerce_str(data.get("sessionID")),
            ts=coerce_str(data.get("ts")),
            metadata={"event": event},
        )

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None

    cwd = coerce_str(data.get("cwd"))
    metadata: dict[str, Any] = {}
    for key in ("model", "provider", "agent", "mode"):
        value = coerce_str(data.get(key))
        if value:
            metadata[key] = value
    # The plugin sends these (hook-specs.md §5): messageID is the dedupe key, and the
    # attachment count is the only trace of an image-only or file-carrying turn.
    message_id = coerce_str(data.get("messageID")) or coerce_str(data.get("message_id"))
    if message_id:
        metadata["messageID"] = message_id
    attachments = _attachment_count(data.get("attachments"))
    if attachments is not None:
        metadata["attachments"] = attachments

    return RawPrompt(
        prompt=prompt,
        session_id=coerce_str(data.get("session_id")) or coerce_str(data.get("sessionID")),
        cwd=cwd,
        project=coerce_str(data.get("project")) or project_from_cwd(cwd),
        metadata=metadata,
        ts=coerce_str(data.get("ts")),
    )


def _attachment_count(value: Any) -> int | None:
    """``metadata.attachments`` is a count, and stays an ``int``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
