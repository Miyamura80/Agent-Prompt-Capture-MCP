"""Turn a raw payload into a scrubbed, stored :class:`PromptRecord`."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from .adapters import AdapterError, RawPrompt, RawTurnEnd
from .adapters import browser as browser_adapter
from .adapters import claude_code as claude_code_adapter
from .adapters import codex as codex_adapter
from .adapters import opencode as opencode_adapter
from .config import Config, get_logger
from .models import BROWSER_SOURCES, PromptRecord, Source, new_id, utc_now_iso
from .pii import MAX_SCRUB_CHARS, scrub, scrub_path
from .store import Store
from .timeutil import parse_dt, parse_time

__all__ = [
    "ingest",
    "IngestResult",
    "adapter_for",
    "SOURCE_ALIASES",
    "MAX_PROMPT_CHARS",
    "MAX_METADATA_CHARS",
]

#: Prompts longer than this are truncated before scrubbing; the loss is recorded in
#: ``metadata.prompt_truncated`` / ``metadata.prompt_original_chars``.
MAX_PROMPT_CHARS = MAX_SCRUB_CHARS
#: Individual metadata strings are capped much harder: they are labels, not content.
MAX_METADATA_CHARS = 4096
#: How far ahead of our own clock a client-supplied ``ts`` may be before we distrust it.
#: Browser payloads carry the *browser's* clock; a past ts is legitimate (the extension
#: queues failed POSTs and replays them), a future one is skew and poisons every
#: time-window query as well as the 5 s dedup window.
MAX_FUTURE_SKEW_SECONDS = 300.0

_log = get_logger()

#: CLI-friendly aliases for the ``apc capture <source>`` argument.
SOURCE_ALIASES: dict[str, Source] = {
    "claude-code": Source.CLAUDE_CODE,
    "claude_code": Source.CLAUDE_CODE,
    "codex": Source.CODEX_CLI,
    "codex-cli": Source.CODEX_CLI,
    "codex_cli": Source.CODEX_CLI,
    "opencode": Source.OPENCODE,
}


def adapter_for(source: Source):
    """The parse function for a source."""
    if source is Source.CLAUDE_CODE:
        return claude_code_adapter.parse
    if source is Source.CODEX_CLI:
        return codex_adapter.parse
    if source is Source.OPENCODE:
        return opencode_adapter.parse
    if source in BROWSER_SOURCES:
        return lambda payload: browser_adapter.parse(payload, source=source)
    raise AdapterError(f"no adapter for source {source!r}")


class IngestResult:
    """Why an ingest did not store a record (used by the HTTP listener)."""

    ACCOUNT_NOT_ALLOWED = "account_not_allowed"
    SOURCE_DISABLED = "source_disabled"
    DEDUPED = "deduped"
    EMPTY = "empty"
    TURN_END = "turn_end"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_ts(raw_ts: str | None) -> tuple[str, str | None]:
    """``(ts_to_store, rejected_client_ts)``.

    An unparseable or implausibly-future client timestamp falls back to our own clock.
    The skewed value comes back (already normalised, so it carries nothing but a time)
    so the caller can record it in metadata instead of silently losing it.
    """
    try:
        candidate = parse_time(raw_ts) if raw_ts else None
    except ValueError:
        candidate = None
    if candidate is None:
        return utc_now_iso(), None
    parsed = parse_dt(candidate)
    if parsed is not None and (parsed - datetime.now(UTC)).total_seconds() > (
        MAX_FUTURE_SKEW_SECONDS
    ):
        return utc_now_iso(), candidate
    return candidate, None


def _scrub_metadata(metadata: dict[str, Any], config: Config) -> dict[str, Any]:
    """Scrub every string leaf of the metadata mapping."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return scrub(
                value[:MAX_METADATA_CHARS],
                extra_terms=config.extra_terms,
                extra_patterns=config.extra_patterns,
                enable_ner=config.enable_ner,
            ).text
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return {str(k): walk(v) for k, v in metadata.items()}


def ingest(
    source: Source | str,
    payload: dict[str, Any],
    *,
    config: Config,
    store: Store,
) -> PromptRecord | None:
    """Parse, filter, scrub and store one payload.

    Returns the stored record, or ``None`` when nothing was stored (disabled source,
    account not on the allowlist, empty prompt, dedup, or a pure turn-end event).
    """
    src = source if isinstance(source, Source) else Source(str(source))

    if config.is_source_disabled(src):
        _log.debug("source %s is disabled, dropping payload", src.value)
        return None

    parsed = adapter_for(src)(payload)
    if parsed is None:
        return None

    if isinstance(parsed, RawTurnEnd):
        updated = store.mark_turn_end(src, parsed.session_id, parsed.ts)
        _log.debug("turn end for %s/%s -> %s", src.value, parsed.session_id, updated)
        return None

    return _ingest_prompt(src, parsed, config=config, store=store)


def _ingest_prompt(
    src: Source, raw: RawPrompt, *, config: Config, store: Store
) -> PromptRecord | None:
    if src in BROWSER_SOURCES and not config.is_account_allowed(raw.account):
        _log.debug("dropping %s prompt: account not on the allowlist", src.value)
        return None

    account = config.resolve_account(raw.account)

    # A pasted log or a whole file can be arbitrarily large. Cap it *before* scrubbing:
    # the span machinery is linear in the text per pattern, and nothing downstream
    # wants a megabyte of prompt. The hash is taken over the same truncated text so
    # dedup stays consistent with what we store.
    original_chars = len(raw.prompt or "")
    raw_prompt = (raw.prompt or "")[:MAX_PROMPT_CHARS]
    truncated = original_chars > len(raw_prompt)

    result = scrub(
        raw_prompt,
        extra_terms=config.extra_terms,
        extra_patterns=config.extra_patterns,
        enable_ner=config.enable_ner,
    )
    if not result.text.strip():
        return None

    session_id = (
        scrub(raw.session_id[:MAX_METADATA_CHARS], extra_terms=config.extra_terms).text
        if raw.session_id
        else None
    )
    cwd = scrub_path(raw.cwd[:MAX_METADATA_CHARS] if raw.cwd else raw.cwd)
    project = (
        scrub(raw.project[:MAX_METADATA_CHARS], extra_terms=config.extra_terms).text
        if raw.project
        else None
    )
    metadata = _scrub_metadata(raw.metadata or {}, config)
    if truncated:
        metadata["prompt_truncated"] = True
        metadata["prompt_original_chars"] = original_chars

    ts, skewed_ts = _resolve_ts(raw.ts)
    if skewed_ts is not None:
        metadata["client_ts"] = skewed_ts
        metadata["client_clock_skew"] = True
    turn_end_ts = parse_time(raw.turn_end_ts) if raw.turn_end_ts else None
    if turn_end_ts is not None and turn_end_ts < ts:
        # A turn cannot end before it started: trust the ordering, not the clock.
        turn_end_ts = ts

    record = PromptRecord(
        id=new_id(),
        ts=ts,
        source=src,
        prompt=result.text,
        prompt_hash=_hash(raw_prompt),
        session_id=session_id,
        account=account,
        cwd=cwd,
        project=project,
        char_count=len(result.text),
        pii_findings=result.findings,
        metadata=metadata,
        turn_end_ts=turn_end_ts,
    )

    if not store.insert(record):
        _log.debug("deduped %s prompt for session %s", src.value, session_id)
        if turn_end_ts and session_id:
            store.mark_turn_end(src, session_id, turn_end_ts)
        return None

    return record
