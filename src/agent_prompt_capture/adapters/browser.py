"""The Chrome extension payload, shared by all four browser sources."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..models import BROWSER_SOURCES, Source
from . import Adapted, AdapterError, RawPrompt, coerce_dict, coerce_str

__all__ = ["parse", "strip_url"]

BROWSER_SOURCE_VALUES = frozenset(s.value for s in BROWSER_SOURCES)


def strip_url(url: str | None) -> str | None:
    """Drop the query string and the fragment; keep scheme/host/path."""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) or url


def parse(payload: Any, *, source: Source | str | None = None) -> Adapted:
    data = coerce_dict(payload)

    declared = coerce_str(data.get("source")) or (
        source.value if isinstance(source, Source) else coerce_str(source)
    )
    if declared not in BROWSER_SOURCE_VALUES:
        raise AdapterError(
            f"browser: source must be one of {sorted(BROWSER_SOURCE_VALUES)}, got {declared!r}"
        )
    if source is not None:
        expected = source.value if isinstance(source, Source) else str(source)
        if declared != expected:
            raise AdapterError(f"browser: source mismatch ({declared!r} != {expected!r})")

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None

    metadata: dict[str, Any] = {"source_declared": declared}
    url = strip_url(coerce_str(data.get("url")))
    if url:
        metadata["url"] = url
    client_version = coerce_str(data.get("client_version"))
    if client_version:
        metadata["client_version"] = client_version

    return RawPrompt(
        prompt=prompt,
        session_id=coerce_str(data.get("conversation_id")),
        cwd=None,
        account=coerce_str(data.get("account")),
        project=coerce_str(data.get("title")),
        metadata=metadata,
        ts=coerce_str(data.get("ts")),
    )
