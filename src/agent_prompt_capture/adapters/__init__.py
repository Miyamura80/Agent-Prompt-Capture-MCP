"""Source adapters: turn a raw hook/HTTP payload into a :class:`RawPrompt`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["RawPrompt", "RawTurnEnd", "AdapterError", "Adapted", "coerce_str", "coerce_dict"]


class AdapterError(ValueError):
    """Raised when a payload does not match the adapter's contract."""


@dataclass
class RawPrompt:
    """An unscrubbed prompt straight out of an adapter."""

    prompt: str
    session_id: str | None = None
    cwd: str | None = None
    account: str | None = None
    project: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    ts: str | None = None
    turn_end_ts: str | None = None


@dataclass
class RawTurnEnd:
    """A signal that the agent finished working on the latest prompt of a session."""

    session_id: str | None = None
    ts: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


#: What every adapter returns.
Adapted = RawPrompt | RawTurnEnd | None


def coerce_dict(payload: Any) -> dict[str, Any]:
    """Every adapter takes a JSON object; anything else is an error."""
    if not isinstance(payload, dict):
        raise AdapterError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def coerce_str(value: Any) -> str | None:
    """Return a non-empty stripped string, or ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int | float):
        return str(value)
    return None
