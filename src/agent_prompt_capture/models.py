"""Core data model: :class:`Source` and :class:`PromptRecord`."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

__all__ = ["Source", "PromptRecord", "BROWSER_SOURCES", "CLI_SOURCES", "utc_now_iso", "new_id"]


def _row_get(row: Any, key: str) -> Any:
    """Read an optional column from a sqlite3.Row without exploding."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


class Source(str, Enum):  # noqa: UP042 - the contract in ARCHITECTURE.md is `str, Enum`
    """Every place a prompt can come from."""

    CLAUDE_CODE = "claude_code"
    CODEX_CLI = "codex_cli"
    OPENCODE = "opencode"
    CLAUDE_WEB = "claude_web"
    CLAUDE_CODE_WEB = "claude_code_web"
    CHATGPT_WEB = "chatgpt_web"
    CODEX_CLOUD = "codex_cloud"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


BROWSER_SOURCES: frozenset[Source] = frozenset(
    {Source.CLAUDE_WEB, Source.CLAUDE_CODE_WEB, Source.CHATGPT_WEB, Source.CODEX_CLOUD}
)
CLI_SOURCES: frozenset[Source] = frozenset({Source.CLAUDE_CODE, Source.CODEX_CLI, Source.OPENCODE})


def utc_now_iso() -> str:
    """Now, as ``2026-09-19T20:11:03.123Z``."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def new_id() -> str:
    """A fresh record id."""
    return str(uuid.uuid4())


@dataclass
class PromptRecord:
    """One captured prompt. ``prompt`` is always the scrubbed text."""

    id: str
    ts: str
    source: Source
    prompt: str
    prompt_hash: str
    session_id: str | None = None
    account: str | None = None
    cwd: str | None = None
    project: str | None = None
    char_count: int = 0
    pii_findings: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    turn_end_ts: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, Source):
            self.source = Source(self.source)
        if not self.char_count:
            self.char_count = len(self.prompt)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation (used by the CLI and the MCP tools)."""
        return {
            "id": self.id,
            "ts": self.ts,
            "source": self.source.value,
            "prompt": self.prompt,
            "prompt_hash": self.prompt_hash,
            "session_id": self.session_id,
            "account": self.account,
            "cwd": self.cwd,
            "project": self.project,
            "char_count": self.char_count,
            "pii_findings": dict(self.pii_findings),
            "metadata": dict(self.metadata),
            "turn_end_ts": self.turn_end_ts,
        }

    @classmethod
    def from_row(cls, row: Any) -> PromptRecord:
        """Build a record from a :class:`sqlite3.Row`."""
        return cls(
            id=row["id"],
            ts=row["ts"],
            source=Source(row["source"]),
            prompt=row["prompt"],
            prompt_hash=row["prompt_hash"],
            session_id=row["session_id"],
            account=row["account"],
            cwd=row["cwd"],
            project=row["project"],
            char_count=row["char_count"],
            pii_findings=json.loads(row["pii_findings"] or "{}"),
            metadata=json.loads(row["metadata"] or "{}"),
            turn_end_ts=_row_get(row, "turn_end_ts"),
        )
