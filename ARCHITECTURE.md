# Agent Prompt Capture MCP: Architecture and Contracts

This document is the single source of truth for how the pieces fit together.
Every component is built against the contracts below. If you change a contract,
change it here first.

## Goal

Capture every prompt a user submits to their coding agents and chat UIs,
scrub PII **before** anything is written to disk, store the scrubbed prompts
locally in SQLite, and expose them through a stdio MCP server so other agents
can query "what have I been asking my agents lately".

Sources:

| source id          | where it comes from                                   | transport                    |
|--------------------|-------------------------------------------------------|------------------------------|
| `claude_code`      | Claude Code CLI `UserPromptSubmit` hook               | stdin JSON -> `apc capture`  |
| `codex_cli`        | Codex CLI hook (or `notify` fallback)                 | stdin JSON -> `apc capture`  |
| `opencode`         | OpenCode plugin `chat.message` hook                   | stdin JSON -> `apc capture`  |
| `claude_web`       | Chrome extension on claude.ai (chat UI)               | HTTP POST to local listener  |
| `claude_code_web`  | Chrome extension on claude.ai/code                    | HTTP POST to local listener  |
| `chatgpt_web`      | Chrome extension on chatgpt.com (chat UI)             | HTTP POST to local listener  |
| `codex_cloud`      | Chrome extension on chatgpt.com/codex                 | HTTP POST to local listener  |

Browser sources are captured **only** for accounts on the user's allowlist.

## High level diagram

```
 LOCAL AGENT CLIs                         BROWSER (Chrome MV3 extension)
 +------------------------+               +-----------------------------------+
 | Claude Code            |               | claude.ai         -> claude_web   |
 |   UserPromptSubmit hook|               | claude.ai/code    -> claude_code_web
 | Codex CLI              |               | chatgpt.com       -> chatgpt_web  |
 |   hook / notify        |               | chatgpt.com/codex -> codex_cloud  |
 | OpenCode               |               |  account allowlist enforced       |
 |   plugin chat.message  |               +-----------------+-----------------+
 +-----------+------------+                                 |
             | stdin JSON                                   | POST 127.0.0.1:47821
             | `apc capture <source>`                       | /v1/prompts  (+token)
             v                                              v
 +-----------+----------------------------------------------+-------------+
 |  INGEST  (src/agent_prompt_capture/ingest.py)                          |
 |   adapters/{claude_code,codex,opencode,browser}.py -> PromptRecord     |
 |   account allowlist check (browser sources)                            |
 +-------------------------------------+----------------------------------+
                                       v
 +-------------------------------------+----------------------------------+
 |  PII SCRUBBER  (pii.py)   raw text never touches disk                  |
 |   emails, phones, IPs, cards(Luhn), SSN, IBAN, API keys, JWT,          |
 |   private keys, URL creds, home-dir usernames, user terms, opt. NER    |
 |   deterministic placeholders  [EMAIL_1] [PHONE_1] ...                  |
 +-------------------------------------+----------------------------------+
                                       v
 +-------------------------------------+----------------------------------+
 |  STORE  ~/.agent-prompt-capture/prompts.db  (SQLite + FTS5)            |
 +-------------------------------------+----------------------------------+
                                       v
 +-------------------------------------+----------------------------------+
 |  STDIO MCP SERVER  `apc mcp`                                           |
 |   tools: list_prompts, search_prompts, get_prompt, prompt_stats,       |
 |          list_sources, list_sessions                                   |
 +------------------------------------------------------------------------+
```

## Repository layout

```
.
├── ARCHITECTURE.md              this file
├── README.md                    user-facing install + usage
├── pyproject.toml               package `agent-prompt-capture`, console script `apc`
├── src/agent_prompt_capture/
│   ├── __init__.py
│   ├── config.py                paths, config.toml loading, token management
│   ├── models.py                PromptRecord + Source enum
│   ├── pii.py                   scrub(text, ...) -> ScrubResult
│   ├── store.py                 Store class over SQLite (+ FTS5)
│   ├── ingest.py                ingest(source, payload) -> PromptRecord | None
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── claude_code.py
│   │   ├── codex.py
│   │   ├── opencode.py
│   │   └── browser.py
│   ├── http_listener.py         `apc serve`: localhost HTTP for the extension
│   ├── mcp_server.py            `apc mcp`: stdio MCP server
│   ├── installer.py             `apc install <target>`: writes hook config
│   └── cli.py                   `apc` argparse entrypoint
├── opencode-plugin/
│   └── agent-prompt-capture.js  OpenCode plugin (copied by `apc install opencode`)
├── extension/                   Chrome MV3 extension (no build step)
│   ├── manifest.json
│   ├── background.js            service worker: queue + POST to listener
│   ├── content/
│   │   ├── common.js            shared capture helpers
│   │   ├── claude.js            claude.ai (chat + /code)
│   │   └── chatgpt.js           chatgpt.com (chat + /codex)
│   ├── options.html / options.js
│   ├── popup.html / popup.js
│   └── icons/
├── tests/                       pytest
├── docs/
│   └── research/hook-specs.md   verified hook payload shapes and sources
└── .github/workflows/ci.yml
```

## Runtime files

All under `$APC_HOME` (default `~/.agent-prompt-capture/`):

| file          | purpose                                                |
|---------------|--------------------------------------------------------|
| `config.toml` | user config (see below)                                |
| `prompts.db`  | SQLite database                                        |
| `token`       | random shared secret for the HTTP listener (0600)      |
| `apc.log`     | rotating log (hooks must never write to stdout/stderr) |

## config.toml

```toml
[capture]
# Browser sources are captured ONLY for these accounts. Empty list = capture nothing
# from the browser. CLI sources are always captured.
allowed_accounts = ["me@work.com"]
# Optional: disable individual sources entirely.
disabled_sources = []

[accounts]
# Optional aliases so the stored `account` column never holds a raw email.
# Accounts without an alias are stored as "sha256:<first 12 hex chars>".
"me@work.com" = "work"

[pii]
# Extra literal terms to redact (your name, employer, project codenames...). Case-insensitive.
extra_terms = []
# Extra regexes to redact, applied after the built-ins.
extra_patterns = []
# Optional NER-based name/location detection (requires `pip install agent-prompt-capture[ner]`).
enable_ner = false

[server]
host = "127.0.0.1"
port = 47821
```

## Data model (`models.py`)

```python
class Source(str, Enum):
    CLAUDE_CODE = "claude_code"
    CODEX_CLI = "codex_cli"
    OPENCODE = "opencode"
    CLAUDE_WEB = "claude_web"
    CLAUDE_CODE_WEB = "claude_code_web"
    CHATGPT_WEB = "chatgpt_web"
    CODEX_CLOUD = "codex_cloud"

@dataclass
class PromptRecord:
    id: str                 # uuid4
    ts: str                 # ISO 8601 UTC, e.g. "2026-09-19T20:11:03.123Z"
    source: Source
    prompt: str             # SCRUBBED text. Raw text is never persisted.
    prompt_hash: str        # sha256 hex of the RAW text (dedup only; irreversible)
    session_id: str | None  # agent/conversation session identifier, scrubbed
    account: str | None     # alias or "sha256:xxxxxxxxxxxx", never a raw email
    cwd: str | None         # scrubbed path (home dir username replaced)
    project: str | None     # basename of cwd, or repo/conversation title if known
    char_count: int         # length of SCRUBBED prompt
    pii_findings: dict[str, int]   # {"email": 2, "api_key": 1}
    metadata: dict[str, Any]       # source-specific extras, already scrubbed
```

## SQLite schema (`store.py`)

```sql
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
  pii_findings TEXT NOT NULL DEFAULT '{}',   -- JSON
  metadata TEXT NOT NULL DEFAULT '{}'        -- JSON
);
CREATE INDEX IF NOT EXISTS idx_prompts_ts ON prompts(ts);
CREATE INDEX IF NOT EXISTS idx_prompts_source_ts ON prompts(source, ts);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_dedup ON prompts(source, session_id, prompt_hash, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(prompt, content='prompts', content_rowid='rowid');
-- plus the standard FTS5 external-content triggers for insert/delete/update
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
```

Dedup rule: a record with the same `(source, session_id, prompt_hash)` submitted within
5 seconds of an existing one is dropped (browser extensions can double-fire).

`Store` API (sync, stdlib sqlite3, WAL mode, `check_same_thread=False`):

```python
class Store:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH): ...
    def insert(self, rec: PromptRecord) -> bool            # False if deduped
    def get(self, id: str) -> PromptRecord | None
    def list(self, *, source=None, since=None, until=None, project=None,
             session_id=None, account=None, limit=50, offset=0,
             order="desc") -> list[PromptRecord]
    def search(self, query: str, *, source=None, since=None, until=None,
               limit=50) -> list[tuple[PromptRecord, float]]   # FTS5 bm25 rank
    def stats(self, *, since=None, until=None, group_by="source") -> list[dict]
        # group_by in {"source","day","week","project","account","session"}
    def sessions(self, *, source=None, limit=50) -> list[dict]
        # {session_id, source, project, first_ts, last_ts, prompt_count}
    def sources(self) -> list[dict]   # {source, prompt_count, first_ts, last_ts}
    def delete(self, *, ids=None, source=None, before=None) -> int
    def close(self)
```

## PII scrubber (`pii.py`)

```python
@dataclass
class ScrubResult:
    text: str
    findings: dict[str, int]      # category -> count

def scrub(text: str, *, extra_terms=(), extra_patterns=(), enable_ner=False) -> ScrubResult
def scrub_path(path: str) -> str   # /Users/alice/x -> /Users/[USER]/x, /home/alice, C:\Users\alice
```

Rules:

* Placeholders are `[CATEGORY_N]` where N is a per-call counter so the same value maps to
  the same placeholder inside one prompt (`[EMAIL_1]` twice if the same email appears twice).
* Categories (in application order, most specific first):
  `private_key` (PEM blocks), `jwt`, `api_key` (sk-, sk-ant-, sk-proj-, ghp_, gho_, ghu_, ghs_,
  github_pat_, glpat-, xox[abpsr]-, AKIA..., AIza..., npm_..., pypi-..., hf_..., `Bearer <tok>`,
  generic `(api[_-]?key|secret|token|password)\s*[:=]\s*\S+` values), `url_credentials`
  (`scheme://user:pass@host` -> `scheme://[URL_CREDENTIALS_1]@host`), `credit_card` (13-19 digits
  with optional separators, Luhn-valid), `iban`, `ssn` (US), `email`, `phone` (E.164 and common
  US/UK/EU formats, min 7 digits, must not be inside a longer digit run), `ipv6`, `ipv4`
  (skip 127.0.0.1, 0.0.0.0, and RFC1918? NO: redact private IPs too, they identify a network),
  `mac_address`, `home_path` (via scrub_path rules), `user_term` (extra_terms, word-bounded,
  case-insensitive), `custom` (extra_patterns), `person` / `location` (only when enable_ner).
* Must not mangle code: do not treat `foo@bar` without a TLD as an email; do not treat
  semver or hashes as phone numbers; keep placeholders on one line.
* Pure functions, no I/O, no global state. Must be fast (regexes compiled once at import).
* NER is optional via extra `ner = ["presidio-analyzer", "spacy"]`; import lazily; if
  `enable_ner` is true but the extra is missing, log a warning once and continue without it.

## Ingest (`ingest.py`)

```python
def ingest(source: Source, payload: dict, *, config: Config, store: Store) -> PromptRecord | None
```

1. Pick the adapter for `source`; adapter returns `RawPrompt(prompt, session_id, cwd, account,
   project, metadata, ts)`. Adapters raise `AdapterError` on unrecognised payloads.
2. If the source is disabled in config -> return None.
3. Browser sources: if `account` is None or not in `allowed_accounts` -> return None (log at DEBUG).
   CLI sources ignore the allowlist.
4. Resolve `account` to alias or `sha256:` prefix.
5. `scrub()` prompt; `scrub_path()` cwd; scrub every string value in metadata; scrub project name
   against extra_terms only.
6. Build `PromptRecord`, `store.insert()`, return it (or None if deduped).

## Adapters: payload contracts

### `claude_code` (Claude Code `UserPromptSubmit` hook)

stdin JSON from Claude Code:

```json
{
  "session_id": "abc123",
  "transcript_path": "/Users/alice/.claude/projects/.../abc123.jsonl",
  "cwd": "/Users/alice/dev/proj",
  "permission_mode": "default",
  "hook_event_name": "UserPromptSubmit",
  "prompt": "the user's text"
}
```

Hook config written by `apc install claude-code` into `~/.claude/settings.json`:

```json
{ "hooks": { "UserPromptSubmit": [ { "hooks": [
  { "type": "command", "command": "apc capture claude-code", "timeout": 10 } ] } ] } }
```

The hook must always exit 0 with empty stdout so it never blocks or alters the prompt.

### `codex_cli`

See `docs/research/hook-specs.md` for the verified shape. The adapter must accept both:
(a) the Codex hooks payload for a user-prompt-submit style event, and
(b) the `notify` program payload (`{"type": "agent-turn-complete", "turn-id": ..., "input-messages": [...], ...}`),
extracting the last user message from `input-messages` in case (b).

### `opencode`

The plugin in `opencode-plugin/agent-prompt-capture.js` implements the `chat.message` hook
and pipes this JSON to `apc capture opencode` on stdin:

```json
{
  "session_id": "...", "cwd": "...", "project": "...", "model": "...",
  "prompt": "concatenated text parts of the user message",
  "ts": "2026-09-19T20:11:03.123Z"
}
```

### `browser` (all four web sources)

HTTP POST body from the extension, one prompt per request:

```json
{
  "source": "claude_web",
  "prompt": "...",
  "account": "me@work.com",
  "conversation_id": "uuid-or-null",
  "url": "https://claude.ai/chat/uuid",
  "title": "conversation title or null",
  "ts": "2026-09-19T20:11:03.123Z",
  "client_version": "0.1.0"
}
```

`session_id` := `conversation_id`; `project` := `title`; `metadata.url` := url with query
string and fragment stripped.

## HTTP listener (`http_listener.py`, `apc serve`)

* Binds `config.server.host:port` (default `127.0.0.1:47821`). Refuses to bind non-loopback
  unless `--allow-remote` is passed.
* Auth: header `X-APC-Token: <token>` must equal contents of `$APC_HOME/token`.
  `apc token` prints it; `apc token --rotate` regenerates it.
* Endpoints:
  * `GET  /v1/health` -> `{"ok": true, "version": "..."}` (no auth)
  * `POST /v1/prompts` -> `202 {"stored": true, "id": "..."}` or `202 {"stored": false, "reason": "account_not_allowed"|"deduped"|"source_disabled"}`
    Errors: `401` bad token, `400` bad payload, `413` body > 1 MiB.
  * `GET  /v1/config` -> `{"allowed_accounts_count": N, "sources": [...]}` (auth required; never returns the emails)
* CORS: allow `chrome-extension://*` origins only; handle `OPTIONS` preflight.
* Stdlib `http.server` + `ThreadingHTTPServer`. No third-party web framework.
* `apc serve --daemon` is out of scope; document running it via launchd/systemd user unit
  in README (provide example unit files under `docs/`).

## MCP server (`mcp_server.py`, `apc mcp`)

Built on the official `mcp` Python SDK, stdio transport. Never writes to stdout except MCP
frames; logs go to `apc.log`.

Tools (all return JSON text content; keep `prompt` text in results scrubbed as stored):

| tool             | args                                                                                       | returns                                  |
|------------------|--------------------------------------------------------------------------------------------|------------------------------------------|
| `list_prompts`   | `source?`, `since?`, `until?`, `project?`, `session_id?`, `account?`, `limit=50`, `offset=0` | `{prompts: [...], total: N}`             |
| `search_prompts` | `query` (FTS5 syntax), `source?`, `since?`, `until?`, `limit=50`                            | `{results: [{...record, rank}]}`         |
| `get_prompt`     | `id`                                                                                       | record or error                          |
| `prompt_stats`   | `since?`, `until?`, `group_by="source"`                                                      | `{groups: [{key, prompt_count, chars}]}` |
| `list_sources`   | none                                                                                       | `{sources: [...]}`                       |
| `list_sessions`  | `source?`, `limit=50`                                                                      | `{sessions: [...]}`                      |

`since`/`until` accept ISO 8601 or relative forms `"24h"`, `"7d"`, `"2w"`.

Resources:

* `apc://prompts/recent` -> last 50 prompts as JSON
* `apc://stats/summary` -> per-source counts for last 7 days

Install snippet for Claude Code: `claude mcp add agent-prompt-capture -- apc mcp`.

## CLI (`cli.py`)

```
apc capture <claude-code|codex|opencode>   read hook JSON on stdin, ingest, exit 0 always
apc serve [--host H] [--port P] [--allow-remote]
apc mcp
apc install <claude-code|codex|opencode> [--dry-run]     writes/merges hook config idempotently
apc uninstall <claude-code|codex|opencode>
apc token [--rotate]
apc list [--source S] [--since 24h] [--limit N] [--json]
apc search <query> [--json]
apc stats [--group-by source|day|project]
apc export [--format jsonl|csv] [--since ...]
apc purge [--before DATE] [--source S] [--yes]
apc doctor       checks config, db, token, listener reachability, hook installation
```

`apc capture` hard rules: never print to stdout, never raise, always exit 0, complete in
well under 1 second (import cost matters: lazy-import the mcp SDK and anything heavy).

## Chrome extension contracts

* Manifest V3, no bundler, plain ES modules where allowed.
* `host_permissions`: `https://claude.ai/*`, `https://chatgpt.com/*`, `http://127.0.0.1/*`,
  `http://localhost/*`.
* Options (chrome.storage.sync): `serverUrl` (default `http://127.0.0.1:47821`), `token`,
  `allowedAccounts` (list of emails), per-source enable toggles.
* Account detection (cached per tab for 10 minutes, refreshed on navigation):
  * claude.ai: try the account/organization API endpoints the app itself calls with
    `credentials: "include"`; fall back to reading the account menu in the DOM. See research doc.
  * chatgpt.com: `GET /api/auth/session` with `credentials: "include"` -> `user.email`.
* Capture trigger: intercept the composer submit (Enter without Shift, or click on the send
  button) and read the composer text **before** the app clears it. Never modify the page's
  behaviour. Never capture if account is unknown or not allowlisted (the server enforces
  this too, but the extension must not even send it).
* Source mapping by URL: `claude.ai/code/*` -> `claude_code_web`; other `claude.ai` -> `claude_web`;
  `chatgpt.com/codex*` -> `codex_cloud`; other `chatgpt.com` -> `chatgpt_web`.
* Extension applies a light pre-scrub (emails, obvious API keys) before sending, as defence
  in depth; the server does the full scrub.
* Background service worker queues failed POSTs in `chrome.storage.local` (max 500) and
  retries with backoff; popup shows listener status and last-24h capture count.

## Quality bar

* `ruff` clean, `pytest` green, type hints throughout, Python >= 3.11.
* Tests: pii (extensive table-driven), store (insert/dedup/search/stats), ingest (allowlist,
  disabled sources), adapters (each payload shape), http listener (auth, CORS, payload),
  mcp server (tool listing + one call per tool via in-memory client), installer (idempotent merge).
* No network access at test time. No writes outside a tmp `APC_HOME` in tests.

## Time tracking layer (primary use case)

The purpose of the data is to let an agent answer **"what has this person been spending
their time on, and how could they use it better"**. Prompt text alone is not enough; we
also need *when* work happened, *how long* the agent worked per prompt, and *how the user's
attention moved between projects*.

### Extra captured signal: turn-end events

| source        | event                              | how                                         |
|---------------|------------------------------------|---------------------------------------------|
| `claude_code` | `Stop` hook (agent finished turn)  | `apc capture claude-code` (same command; adapter branches on `hook_event_name`) |
| `codex_cli`   | `notify` `agent-turn-complete`     | already handled; it is a turn-end event that also carries the prompt text |
| `opencode`    | `event` hook `session.idle`        | plugin pipes `{"event":"turn_end","session_id":...,"ts":...}` to `apc capture opencode` |
| browser       | none for now                       | (DOM heuristics too fragile; future work)   |

Adapters therefore return either a `RawPrompt` or a `RawTurnEnd(session_id, ts, metadata)`.
`ingest()` handles `RawTurnEnd` by calling `store.mark_turn_end(source, session_id, ts)`,
which sets `turn_end_ts` on the most recent prompt in that session whose `turn_end_ts` is
NULL (no-op if none). For `codex_cli` `agent-turn-complete`, ingest first inserts the prompt
(with `ts` = now minus nothing better; keep `metadata.turn_complete_ts`) and then marks it
ended in the same call.

### Schema additions

```sql
ALTER TABLE prompts ADD COLUMN turn_end_ts TEXT;        -- ISO UTC, NULL until the agent finishes
CREATE INDEX IF NOT EXISTS idx_prompts_session_ts ON prompts(session_id, ts);
```

`PromptRecord` gains `turn_end_ts: str | None`. Derived (not stored) fields exposed by the
store/MCP: `agent_seconds = turn_end_ts - ts`, `think_seconds = next prompt ts in same
session - turn_end_ts` (NULL when unknown).

### config.toml additions

```toml
[time]
idle_gap_minutes = 30   # a gap longer than this splits an activity session
tail_minutes = 5        # credited after the last prompt / turn end of an activity session
```

### Derived activity sessions (heuristic, computed on read in `timeline.py`)

1. Take all prompts (and their `turn_end_ts`) in range, ordered by `ts`, grouped by
   `(source, project)` when `group_by` needs it, else globally.
2. Walk in time order. Start a new activity session when the gap between the previous
   event's end (`turn_end_ts` if set else `ts`) and the next `ts` exceeds `idle_gap_minutes`.
3. `active_minutes` of a session = `(last_end - first_ts) + tail_minutes`.
4. A **context switch** is a consecutive pair of prompts (within one activity session, any
   grouping) whose `project` differs.

### MCP tools added

| tool                | args                                                                 | returns |
|---------------------|----------------------------------------------------------------------|---------|
| `time_summary`      | `since="7d"`, `until?`, `group_by` in `project\|source\|day\|hour_of_day\|weekday\|session` | `{groups:[{key, active_minutes, prompt_count, agent_minutes, avg_think_seconds}], total_active_minutes, context_switches}` |
| `activity_timeline` | `since="24h"`, `until?`, `bucket` in `hour\|day`                     | `{buckets:[{start, prompt_count, active_minutes, projects:[...], sources:[...]}]}` |
| `daily_digest`      | `date` (YYYY-MM-DD, default today, local time)                      | `{date, first_activity, last_activity, active_minutes, sessions:[{project, source, start, end, prompt_count, sample_prompts:[3 shortest]}], context_switches, top_terms:[...]}` |

`top_terms` = top 15 lower-cased tokens of length >= 4 after removing a small stopword list
and any placeholder tokens like `[EMAIL_1]`. Hour-of-day and weekday grouping and `daily_digest`
use the local timezone of the machine running the MCP server.

Resource added: `apc://digest/today`.

CLI added: `apc time [--since 7d] [--group-by project]` and `apc digest [DATE]`.

The `prompt_stats` tool stays as the cheap count-only query; `time_summary` is the one an
agent should reach for when asked about time use.
