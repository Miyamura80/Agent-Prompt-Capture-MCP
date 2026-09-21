"""Small builders shared by the store/timeline tests."""

from __future__ import annotations

import hashlib
import itertools
from typing import Any

from agent_prompt_capture.models import PromptRecord, Source

_counter = itertools.count(1)


def make_record(
    prompt: str = "hello",
    *,
    ts: str = "2026-09-19T10:00:00.000Z",
    source: Source | str = Source.CLAUDE_CODE,
    session_id: str | None = "s1",
    project: str | None = "proj",
    account: str | None = None,
    cwd: str | None = "/home/[USER]/proj",
    turn_end_ts: str | None = None,
    id: str | None = None,  # noqa: A002
    metadata: dict[str, Any] | None = None,
) -> PromptRecord:
    return PromptRecord(
        id=id or f"id-{next(_counter):04d}",
        ts=ts,
        source=source if isinstance(source, Source) else Source(source),
        prompt=prompt,
        prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
        session_id=session_id,
        account=account,
        cwd=cwd,
        project=project,
        char_count=len(prompt),
        pii_findings={},
        metadata=metadata or {},
        turn_end_ts=turn_end_ts,
    )
