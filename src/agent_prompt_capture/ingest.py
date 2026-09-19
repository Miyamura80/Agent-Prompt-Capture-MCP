"""Turn a raw payload into a scrubbed, stored :class:`PromptRecord`."""

from __future__ import annotations

import hashlib
from typing import Any

from .adapters import AdapterError, RawPrompt, RawTurnEnd
from .adapters import browser as browser_adapter
from .adapters import claude_code as claude_code_adapter
from .adapters import codex as codex_adapter
from .adapters import opencode as opencode_adapter
from .config import Config, get_logger
from .models import BROWSER_SOURCES, PromptRecord, Source, new_id, utc_now_iso
from .pii import scrub, scrub_path
from .store import Store
from .timeutil import parse_time

__all__ = ["ingest", "IngestResult", "adapter_for", "SOURCE_ALIASES"]

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


def _scrub_metadata(metadata: dict[str, Any], config: Config) -> dict[str, Any]:
    """Scrub every string leaf of the metadata mapping."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return scrub(
                value,
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

    result = scrub(
        raw.prompt,
        extra_terms=config.extra_terms,
        extra_patterns=config.extra_patterns,
        enable_ner=config.enable_ner,
    )
    if not result.text.strip():
        return None

    session_id = (
        scrub(raw.session_id, extra_terms=config.extra_terms).text if raw.session_id else None
    )
    cwd = scrub_path(raw.cwd)
    project = scrub(raw.project, extra_terms=config.extra_terms).text if raw.project else None
    metadata = _scrub_metadata(raw.metadata or {}, config)

    ts = parse_time(raw.ts) or utc_now_iso()
    turn_end_ts = parse_time(raw.turn_end_ts) if raw.turn_end_ts else None

    record = PromptRecord(
        id=new_id(),
        ts=ts,
        source=src,
        prompt=result.text,
        prompt_hash=_hash(raw.prompt),
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
