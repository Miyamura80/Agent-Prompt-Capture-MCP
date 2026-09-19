"""OpenCode plugin payloads (``chat.message`` and the ``session.idle`` turn end)."""

from __future__ import annotations

from typing import Any

from . import Adapted, RawPrompt, RawTurnEnd, coerce_dict, coerce_str

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

    return RawPrompt(
        prompt=prompt,
        session_id=coerce_str(data.get("session_id")) or coerce_str(data.get("sessionID")),
        cwd=cwd,
        project=coerce_str(data.get("project")) or _project(cwd),
        metadata=metadata,
        ts=coerce_str(data.get("ts")),
    )


def _project(cwd: str | None) -> str | None:
    if not cwd:
        return None
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    return parts[-1] if parts else None
