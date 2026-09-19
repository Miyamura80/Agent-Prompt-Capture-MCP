"""SQLite storage (stdlib ``sqlite3``, WAL, FTS5 external content)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import db_path
from .models import PromptRecord, Source
from .timeutil import parse_dt as _parse_dt
from .timeutil import parse_time

__all__ = [
    "Store",
    "SCHEMA_VERSION",
    "DEFAULT_DB_PATH",
    "GROUP_BY_CHOICES",
    "ORDER_CHOICES",
    "BUSY_TIMEOUT_MS",
]

SCHEMA_VERSION = 2
DEFAULT_DB_PATH = db_path()

GROUP_BY_CHOICES = ("source", "day", "week", "project", "account", "session")
ORDER_CHOICES = ("asc", "desc")

DEDUP_WINDOW_SECONDS = 5.0

#: Two processes write this database (the capture hook and the HTTP listener), so a
#: writer that finds the lock held must wait rather than raise ``database is locked``.
BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  prompt TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  session_id TEXT,
  account TEXT,
  cwd TEXT,
  project TEXT,
  char_count INTEGER NOT NULL,
  pii_findings TEXT NOT NULL DEFAULT '{}',
  metadata TEXT NOT NULL DEFAULT '{}',
  turn_end_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompts_ts ON prompts(ts);
CREATE INDEX IF NOT EXISTS idx_prompts_source_ts ON prompts(source, ts);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);
CREATE INDEX IF NOT EXISTS idx_prompts_session_ts ON prompts(session_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_dedup
  ON prompts(source, session_id, prompt_hash, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts
  USING fts5(prompt, content='prompts', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS prompts_ai AFTER INSERT ON prompts BEGIN
  INSERT INTO prompts_fts(rowid, prompt) VALUES (new.rowid, new.prompt);
END;
CREATE TRIGGER IF NOT EXISTS prompts_ad AFTER DELETE ON prompts BEGIN
  INSERT INTO prompts_fts(prompts_fts, rowid, prompt) VALUES('delete', old.rowid, old.prompt);
END;
CREATE TRIGGER IF NOT EXISTS prompts_au AFTER UPDATE ON prompts BEGIN
  INSERT INTO prompts_fts(prompts_fts, rowid, prompt) VALUES('delete', old.rowid, old.prompt);
  INSERT INTO prompts_fts(rowid, prompt) VALUES (new.rowid, new.prompt);
END;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""


def _source_value(source: Source | str | None) -> str | None:
    if source is None:
        return None
    return source.value if isinstance(source, Source) else str(source)


def _source_list(source: Source | str | Iterable[Source | str] | None) -> list[str]:
    if source is None:
        return []
    if isinstance(source, str | Source):
        value = _source_value(source)
        return [value] if value else []
    return [v for v in (_source_value(s) for s in source) if v]


def _fts_escape(query: str) -> str:
    """Make a user query safe for FTS5 while keeping its operators when it parses."""
    return '"' + query.replace('"', '""') + '"'


class Store:
    """Everything that touches ``prompts.db``."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- schema --------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._ensure_columns()
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            current = int(row["version"]) if row else 0
            if current < SCHEMA_VERSION:
                self._apply_migrations(current)
                self._conn.execute("DELETE FROM schema_version")
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )

    def _ensure_columns(self) -> None:
        """Additive column migrations for databases created by older versions."""
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(prompts)")}
        if "turn_end_ts" not in existing:
            self._conn.execute("ALTER TABLE prompts ADD COLUMN turn_end_ts TEXT")

    def _apply_migrations(self, from_version: int) -> None:
        """Version-to-version migrations. v0 -> v1 is the initial schema itself."""
        if 0 < from_version < 2:
            # v1 databases predate ``turn_end_ts`` (added by ``_ensure_columns`` above)
            # and may carry an FTS index built before the update/delete triggers
            # existed. Rebuilding is cheap and leaves search correct after the upgrade.
            self._conn.execute("INSERT INTO prompts_fts(prompts_fts) VALUES('rebuild')")

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row else 0

    # -- writes --------------------------------------------------------
    def insert(self, rec: PromptRecord) -> bool:
        """Insert a record. Returns ``False`` when it was deduplicated."""
        with self._lock:
            if self._is_duplicate(rec):
                return False
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO prompts (id, ts, source, prompt, prompt_hash, session_id,"
                        " account, cwd, project, char_count, pii_findings, metadata, turn_end_ts)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            rec.id,
                            rec.ts,
                            rec.source.value,
                            rec.prompt,
                            rec.prompt_hash,
                            rec.session_id,
                            rec.account,
                            rec.cwd,
                            rec.project,
                            rec.char_count,
                            json.dumps(rec.pii_findings, sort_keys=True),
                            json.dumps(rec.metadata, sort_keys=True, default=str),
                            rec.turn_end_ts,
                        ),
                    )
            except sqlite3.IntegrityError:
                return False
            return True

    def _is_duplicate(self, rec: PromptRecord) -> bool:
        """Same (source, session_id, prompt_hash) within 5 seconds of an existing row."""
        if rec.session_id is None:
            rows = self._conn.execute(
                "SELECT ts FROM prompts WHERE source=? AND session_id IS NULL AND prompt_hash=?"
                " ORDER BY ts DESC LIMIT 20",
                (rec.source.value, rec.prompt_hash),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT ts FROM prompts WHERE source=? AND session_id=? AND prompt_hash=?"
                " ORDER BY ts DESC LIMIT 20",
                (rec.source.value, rec.session_id, rec.prompt_hash),
            ).fetchall()
        if not rows:
            return False
        new_ts = _parse_dt(rec.ts)
        if new_ts is None:
            return False
        window = timedelta(seconds=DEDUP_WINDOW_SECONDS)
        for row in rows:
            other = _parse_dt(row["ts"])
            if other is not None and abs(new_ts - other) <= window:
                return True
        return False

    def mark_turn_end(
        self, source: Source | str, session_id: str | None, ts: str | None = None
    ) -> str | None:
        """Stamp ``turn_end_ts`` on the latest open prompt of a session.

        Returns the id of the updated row, or ``None`` when there was nothing to mark.
        """
        if not session_id:
            return None
        value = parse_time(ts) if ts else None
        if value is None:
            from .models import utc_now_iso

            value = utc_now_iso()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id FROM prompts WHERE source=? AND session_id=? AND turn_end_ts IS NULL"
                " ORDER BY ts DESC LIMIT 1",
                (_source_value(source), session_id),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE prompts SET turn_end_ts=? WHERE id=?", (value, row["id"]))
            return str(row["id"])

    def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        source: Source | str | None = None,
        before: str | datetime | None = None,
    ) -> int:
        """Delete by id, source and/or upper time bound. Returns rows removed."""
        clauses: list[str] = []
        params: list[Any] = []
        if ids:
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if source is not None:
            clauses.append("source = ?")
            params.append(_source_value(source))
        bound = parse_time(before)
        if bound:
            clauses.append("ts < ?")
            params.append(bound)
        if not clauses:
            return 0
        sql = f"DELETE FROM prompts WHERE {' AND '.join(clauses)}"
        with self._lock, self._conn:
            cur = self._conn.execute(sql, params)
            return int(cur.rowcount or 0)

    # -- reads ---------------------------------------------------------
    def get(self, id: str) -> PromptRecord | None:  # noqa: A002 - contract name
        row = self._conn.execute("SELECT * FROM prompts WHERE id=?", (id,)).fetchone()
        return PromptRecord.from_row(row) if row else None

    def _filters(
        self,
        *,
        source: Source | str | Iterable[Source | str] | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        project: str | None = None,
        session_id: str | None = None,
        account: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        sources = _source_list(source)
        if sources:
            clauses.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        low = parse_time(since)
        if low:
            clauses.append("ts >= ?")
            params.append(low)
        high = parse_time(until)
        if high:
            clauses.append("ts <= ?")
            params.append(high)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if account:
            clauses.append("account = ?")
            params.append(account)
        return clauses, params

    def list(  # noqa: A003 - contract name
        self,
        *,
        source: Source | str | Iterable[Source | str] | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        project: str | None = None,
        session_id: str | None = None,
        account: str | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "desc",
    ) -> list[PromptRecord]:
        clauses, params = self._filters(
            source=source,
            since=since,
            until=until,
            project=project,
            session_id=session_id,
            account=account,
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # Never interpolate a caller-supplied string into SQL: map it through a
        # closed whitelist and reject anything that is not in it.
        key = str(order).lower().strip()
        if key not in ORDER_CHOICES:
            raise ValueError(f"order must be one of {', '.join(ORDER_CHOICES)}, got {order!r}")
        direction = "ASC" if key == "asc" else "DESC"
        sql = f"SELECT * FROM prompts {where} ORDER BY ts {direction}, id {direction}"
        if limit is not None and int(limit) >= 0:
            sql += " LIMIT ? OFFSET ?"
            params = [*params, int(limit), int(offset or 0)]
        rows = self._conn.execute(sql, params).fetchall()
        return [PromptRecord.from_row(r) for r in rows]

    def count(
        self,
        *,
        source: Source | str | Iterable[Source | str] | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        project: str | None = None,
        session_id: str | None = None,
        account: str | None = None,
    ) -> int:
        clauses, params = self._filters(
            source=source,
            since=since,
            until=until,
            project=project,
            session_id=session_id,
            account=account,
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(f"SELECT COUNT(*) AS n FROM prompts {where}", params).fetchone()
        return int(row["n"]) if row else 0

    def search(
        self,
        query: str,
        *,
        source: Source | str | Iterable[Source | str] | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        limit: int = 50,
    ) -> list[tuple[PromptRecord, float]]:
        """Full text search. Returns ``(record, rank)`` pairs, best match first."""
        if not query or not query.strip():
            return []
        clauses, params = self._filters(source=source, since=since, until=until)
        extra = (" AND " + " AND ".join(f"p.{c}" for c in clauses)) if clauses else ""
        sql = (
            "SELECT p.*, bm25(prompts_fts) AS rank FROM prompts_fts"
            " JOIN prompts p ON p.rowid = prompts_fts.rowid"
            f" WHERE prompts_fts MATCH ?{extra}"
            " ORDER BY rank LIMIT ?"
        )
        for candidate in (query, _fts_escape(query)):
            try:
                rows = self._conn.execute(sql, [candidate, *params, int(limit)]).fetchall()
            except sqlite3.OperationalError:
                continue
            return [(PromptRecord.from_row(r), float(r["rank"])) for r in rows]
        return []

    def stats(
        self,
        *,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        group_by: str = "source",
    ) -> list[dict[str, Any]]:
        """Counts grouped by source / day / week / project / account / session."""
        key = str(group_by).lower()
        if key not in GROUP_BY_CHOICES:
            raise ValueError(
                f"group_by must be one of {', '.join(GROUP_BY_CHOICES)}, got {group_by!r}"
            )
        expressions = {
            "source": "source",
            "day": "substr(ts, 1, 10)",
            "week": "strftime('%Y-W%W', ts)",
            "project": "COALESCE(project, '(none)')",
            "account": "COALESCE(account, '(none)')",
            "session": "COALESCE(session_id, '(none)')",
        }
        expression = expressions[key]
        clauses, params = self._filters(since=since, until=until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {expression} AS key, COUNT(*) AS prompt_count,"
            " SUM(char_count) AS chars, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
            f" FROM prompts {where} GROUP BY key ORDER BY prompt_count DESC, key ASC"
        )
        return [
            {
                "key": r["key"],
                "prompt_count": int(r["prompt_count"]),
                "chars": int(r["chars"] or 0),
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
            }
            for r in self._conn.execute(sql, params).fetchall()
        ]

    def sessions(
        self,
        *,
        source: Source | str | Iterable[Source | str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses, params = self._filters(source=source)
        clauses.append("session_id IS NOT NULL")
        where = f"WHERE {' AND '.join(clauses)}"
        sql = (
            "SELECT session_id, source, MAX(project) AS project, MIN(ts) AS first_ts,"
            " MAX(COALESCE(turn_end_ts, ts)) AS last_ts, COUNT(*) AS prompt_count"
            f" FROM prompts {where} GROUP BY session_id, source"
            " ORDER BY last_ts DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, [*params, int(limit)]).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "source": r["source"],
                "project": r["project"],
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "prompt_count": int(r["prompt_count"]),
            }
            for r in rows
        ]

    def sources(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) AS prompt_count, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
            " FROM prompts GROUP BY source ORDER BY prompt_count DESC"
        ).fetchall()
        return [
            {
                "source": r["source"],
                "prompt_count": int(r["prompt_count"]),
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
            }
            for r in rows
        ]

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
