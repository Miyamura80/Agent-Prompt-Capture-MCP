# agent-prompt-capture

Local-only telemetry for the prompts you send to your coding agents and chat UIs. Hooks in
Claude Code, Codex CLI and OpenCode, plus a Chrome extension for claude.ai and chatgpt.com,
feed every prompt you submit into a PII scrubber that runs *before* anything is written to
disk; the scrubbed text lands in a SQLite database under your home directory, and a stdio
MCP server (`apc mcp`) lets an agent read it back and answer "where did my week go, and what
should I do differently". Nothing leaves the machine: the browser extension posts only to
`127.0.0.1`, the listener binds loopback and refuses anything else without an explicit flag,
there is no account, no sync and no outbound network call anywhere in the package.

## How it fits together

```
 LOCAL AGENT CLIs                       BROWSER (Chrome MV3 extension)
 +-----------------------------+       +------------------------------+
 | Claude Code  hooks          |       | claude.ai          (chat)    |
 | Codex CLI    hooks.json     |       | claude.ai/code               |
 | OpenCode     plugin         |       | chatgpt.com        (chat)    |
 +--------------+--------------+       | chatgpt.com/codex            |
                |                      |  account allowlist enforced  |
   stdin or argv JSON                  +---------------+--------------+
   apc capture <target>                                |
                |            POST http://127.0.0.1:47821/v1/prompts
                |            X-APC-Token: <token>   ->   apc serve
                v                                       v
 +--------------+---------------------------------------+-------------+
 |  INGEST    adapters -> RawPrompt / RawTurnEnd, allowlist check     |
 +---------------------------------+----------------------------------+
                                   v
 +---------------------------------+----------------------------------+
 |  PII SCRUB (pii.py)   raw text never reaches disk -> [EMAIL_1]     |
 +---------------------------------+----------------------------------+
                                   v
 +---------------------------------+----------------------------------+
 |  STORE   $APC_HOME/prompts.db   SQLite + FTS5 (+ turn_end_ts)      |
 +---------------------------------+----------------------------------+
                                   v
 +---------------------------------+----------------------------------+
 |  apc mcp   stdio MCP: 9 tools, 3 resources    and the `apc` CLI    |
 +--------------------------------------------------------------------+
```

| source id         | where it comes from                              | transport                   |
|-------------------|--------------------------------------------------|-----------------------------|
| `claude_code`     | Claude Code `UserPromptSubmit` + `Stop` hooks    | stdin JSON to `apc capture` |
| `codex_cli`       | Codex CLI `hooks.json`, or the legacy `notify`   | stdin or argv JSON          |
| `opencode`        | OpenCode plugin `chat.message` + `session.idle`  | stdin JSON                  |
| `claude_web`      | Chrome extension on claude.ai                    | HTTP POST to the listener   |
| `claude_code_web` | Chrome extension on claude.ai/code               | HTTP POST to the listener   |
| `chatgpt_web`     | Chrome extension on chatgpt.com                  | HTTP POST to the listener   |
| `codex_cloud`     | Chrome extension on chatgpt.com/codex            | HTTP POST to the listener   |

[ARCHITECTURE.md](ARCHITECTURE.md) holds the contracts;
[docs/research/hook-specs.md](docs/research/hook-specs.md) holds the verified hook payloads.

## Quickstart

```sh
git clone https://github.com/gatlingx/Agent-Prompt-Capture-MCP && cd Agent-Prompt-Capture-MCP
uv tool install .                 # puts `apc` on your PATH
apc install claude-code           # and/or: apc install codex, apc install opencode
apc doctor                        # checks home, db, token, listener, hooks
claude mcp add agent-prompt-capture -- apc mcp
```

The package is not on PyPI yet, so `uvx agent-prompt-capture` does not work. If you would
rather not install it, `uvx --from /path/to/Agent-Prompt-Capture-MCP apc <args>` runs the
same CLI from a clone, and works anywhere `apc` is written below.

Then register the MCP server with whichever clients you use.

**Claude Code** [^cc]

```sh
claude mcp add agent-prompt-capture -- apc mcp
```

**Codex CLI**, in `~/.codex/config.toml` (check the docs: this one could not be verified
against developers.openai.com from the machine this was written on)

```toml
[mcp_servers.agent-prompt-capture]
command = "apc"
args = ["mcp"]
```

**OpenCode**, in `opencode.json` [^oc]

```json
{
  "mcp": {
    "agent-prompt-capture": { "type": "local", "command": ["apc", "mcp"] }
  }
}
```

**Claude Desktop**, in `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows) [^cd]

```json
{
  "mcpServers": {
    "agent-prompt-capture": { "command": "apc", "args": ["mcp"] }
  }
}
```

If `apc` is not on the PATH of the app you are configuring, use the absolute path from
`which apc`.

## Browser capture

CLI hooks cover the three terminal agents. Everything you type into claude.ai or
chatgpt.com needs the Chrome extension and the local listener.

1. Run the listener. In the foreground:

   ```sh
   apc serve            # binds 127.0.0.1:47821, ctrl-c to stop
   ```

   As a background service, copy one of the units in [`docs/service/`](docs/service/):

   ```sh
   # Linux (systemd user unit)
   mkdir -p ~/.config/systemd/user
   cp docs/service/apc-listener.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now apc-listener

   # macOS (launchd user agent)
   cp docs/service/com.agent-prompt-capture.listener.plist ~/Library/LaunchAgents/
   launchctl load -w ~/Library/LaunchAgents/com.agent-prompt-capture.listener.plist
   ```

   Both units hard-code a path to `apc`; replace it with the output of `which apc`.

2. Print the shared secret the extension needs:

   ```sh
   apc token            # `apc token --rotate` issues a new one
   ```

3. Load the extension: `chrome://extensions` -> Developer mode -> **Load unpacked** ->
   pick the `extension/` directory. Fill in the server URL, the token and the allowlist in
   its Options page. Full walkthrough and troubleshooting:
   [extension/README.md](extension/README.md).

4. Set the allowlist in **both** places: `[capture].allowed_accounts` in `config.toml` and
   **Allowed accounts** in the extension Options. They are two different checks. The
   extension one stops a prompt from ever leaving the page when the detected account is not
   yours, and the listener one is the authority that decides what gets stored, so a stale or
   tampered extension still cannot write someone else's prompts into your database. An empty
   list on either side captures nothing.

The four web sources are `claude_web` (claude.ai chat), `claude_code_web` (claude.ai/code),
`chatgpt_web` (chatgpt.com chat) and `codex_cloud` (chatgpt.com/codex). Claude Code on the
web does not read your local `~/.claude/settings.json` and does not run your hooks, so the
extension is the only way to capture `claude_code_web` prompts. Do not try to solve that one
with a hook; the cloud VM is a different machine with no `apc` on it.

## Asking your agent where your time went

Once the MCP server is registered, ask in plain language. The tool each question lands on:

| What they ask                                              | Tool                |
|------------------------------------------------------------|---------------------|
| "Which projects ate my week?"                               | `time_summary` (`group_by="project"`) |
| "Am I most productive in the morning or after lunch?"       | `time_summary` (`group_by="hour_of_day"` or `"weekday"`) |
| "What did I do yesterday? Write my standup."                | `daily_digest` (`date="2026-09-18"`) |
| "When was I actually at the keyboard today?"                | `activity_timeline` (`bucket="hour"`) |
| "Have I asked about this migration before?"                 | `search_prompts` (FTS5 syntax) |
| "How many sessions did I start on this repo, and how long?" | `list_sessions`, then `time_summary` (`group_by="session"`) |
| "Am I using Codex more than Claude Code now?"               | `prompt_stats` (`group_by="source"`) |
| "Show me the last ten things I asked about auth."           | `search_prompts` then `list_prompts` |

**The activity-session heuristic.** Nothing about duration is stored. Activity sessions are
recomputed on every read from prompt timestamps and, where a source gives one, the turn-end
timestamp:

* Prompts are walked in time order. A gap longer than `idle_gap_minutes` (default 30) since
  the end of the previous event starts a new activity session.
* A session is credited with `(last end - first start) + tail_minutes` (default 5), so the
  minutes you spend reading the last answer are not lost.
* **Agent time** is `turn_end_ts - ts`: how long the agent worked. **Think time** is the gap
  from one turn ending to the next prompt in the same session: how long they spent reading,
  deciding and typing.
* A **context switch** is a consecutive pair of prompts inside one activity session whose
  `project` differs.

**What it cannot tell you.** Browser sources have no turn-end signal at all, so agent time
and think time are unknown for anything captured by the extension and those prompts
contribute only their timestamps. Work you do without prompting an agent is invisible. The
tail credit is a guess, not a measurement. Treat the numbers as a shape of the week, not a
stopwatch.

## CLI reference

Every subcommand of `apc`. `--since` / `--until` / `--before` take ISO 8601, a bare date
(`2026-09-19`), or a relative duration meaning "that long ago": `30m`, `24h`, `7d`, `2w`,
`3mo`, `1y`.

| Command | Flags | What it does |
|---|---|---|
| `apc --version` | | print the version |
| `apc capture <claude-code\|codex\|opencode> [payload]` | | read hook JSON from the trailing argument (Codex `notify`) or stdin, ingest it. Never prints, never raises, always exits 0 |
| `apc serve` | `--host`, `--port`, `--allow-remote` | run the loopback HTTP listener for the extension. Non-loopback hosts are refused unless `--allow-remote` |
| `apc mcp` | | run the stdio MCP server |
| `apc install <claude-code\|codex\|opencode>` | `--dry-run`, `--legacy`, `--no-trust` | write the hook config, idempotently. `--legacy` and `--no-trust` are Codex only; `--no-trust` skips the `[hooks.state]` trust entries in `config.toml` |
| `apc uninstall <claude-code\|codex\|opencode>` | | remove what `install` wrote, trust entries included |
| `apc token` | `--rotate` | print the listener token, or generate a new one |
| `apc list` | `--source`, `--since`, `--until`, `--project`, `--session-id`, `--account`, `--limit` (20), `--offset` (0), `--json` | list captured prompts, newest first |
| `apc search <query>` | `--source`, `--since`, `--until`, `--limit` (20), `--json` | FTS5 full-text search |
| `apc stats` | `--group-by source\|day\|week\|project\|account\|session` (source), `--since`, `--until`, `--json` | prompt and character counts |
| `apc time` | `--since` (7d), `--until`, `--group-by project\|source\|day\|hour_of_day\|weekday\|session` (project), `--json` | active minutes, agent minutes, average think time, context switches |
| `apc digest [DATE]` | `--json` | one local day: sessions, samples, top terms. Defaults to today |
| `apc export` | `--format jsonl\|csv` (jsonl), `--source`, `--since`, `--until`, `--limit` (100000), `--output`/`-o` | dump records to stdout or a file |
| `apc purge` | `--before`, `--source`, `--yes` | delete records. Refuses to run with neither `--before` nor `--source`; needs `--yes` when stdin is not a terminal |
| `apc doctor` | | check home, config, database, token, log, allowlist, listener and every hook install |

## Configuration

`$APC_HOME/config.toml`, default `~/.agent-prompt-capture/config.toml`. The file is
optional: a missing or unparseable one falls back to these defaults.

```toml
[capture]
# Browser sources are captured ONLY for these accounts, matched case-insensitively.
# An empty list captures nothing from the browser. CLI sources ignore this list.
allowed_accounts = []
# Source ids to drop entirely, e.g. ["chatgpt_web", "codex_cloud"].
disabled_sources = []

[accounts]
# Optional aliases, so the stored `account` column never holds a raw address.
# Anything without an alias is stored as "sha256:<first 12 hex chars>".
# "me@work.com" = "work"

[pii]
# Extra literal terms to redact: your name, employer, codenames. Case-insensitive,
# word-bounded, replaced with [USER_TERM_n].
extra_terms = []
# Extra Python regexes, replaced with [CUSTOM_n]. An invalid one is logged and skipped.
extra_patterns = []
# Optional NER-based person/location detection. Needs the `ner` extra
# (presidio-analyzer, spacy); if it is missing this logs a warning and continues.
enable_ner = false

[server]
host = "127.0.0.1"
port = 47821

[time]
idle_gap_minutes = 30   # a longer gap starts a new activity session
tail_minutes = 5        # credited after the last event of an activity session
```

Environment variables:

| Variable | Read by | Effect |
|---|---|---|
| `APC_HOME` | everything | the runtime directory. Default `~/.agent-prompt-capture`, created mode 0700 |
| `APC_DEBUG=1` | logging | DEBUG level, and a stderr handler in addition to `apc.log` |
| `CLAUDE_CONFIG_DIR` | `apc install/uninstall/doctor claude-code` | directory holding `settings.json`. Default `~/.claude` |
| `CODEX_HOME` | `apc install/uninstall/doctor codex` | directory holding `hooks.json` and `config.toml`. Default `~/.codex` |
| `XDG_CONFIG_HOME` | `apc install/uninstall/doctor opencode` | base for `opencode/plugin/`. Default `~/.config` |

## Privacy

Prompt text is scrubbed in memory and only the scrubbed text is written. Captured: the
scrubbed prompt, a timestamp, the source, the session id, an aliased or hashed account, a
scrubbed working directory and project name, a character count, a per-category count of what
was redacted, and source-specific metadata. Never captured: assistant responses, tool calls
and their output, file contents, images or attachments (only a count), and raw account
addresses.

The scrubber replaces each recognised value with `[CATEGORY_N]`, numbered per prompt so the
same value reads as the same placeholder throughout it. The categories, in the order they
are applied: `private_key`, `ssh_key`, `jwt`, `webhook_url`, `api_key`, `url_credentials`,
`credit_card`, `iban`, `ssn`, `email`, `phone`, `ipv6`, `ipv4`, `mac_address`,
`uk_postcode`, `home_path`, `user_term`, `custom`, `person`, `location`. Home paths are the
exception to the numbering: the username in `/Users/x`, `/home/x` or `C:\Users\x` becomes
the fixed `[USER]`.

Everything lives in `$APC_HOME`: `config.toml`, `prompts.db`, `token` (mode 0600) and
`apc.log`. `apc purge --before 2026-01-01 --yes` or `apc purge --source chatgpt_web --yes`
deletes records; deleting `prompts.db` deletes everything.

Full detail, including the account hashing rule, the dedup hash and the known gaps in the
scrubber, is in [docs/PRIVACY.md](docs/PRIVACY.md). Read that before you trust the database
with anything sensitive.

## Per-CLI notes

**Claude Code.** `apc install claude-code` merges two hooks into
`$CLAUDE_CONFIG_DIR/settings.json` (default `~/.claude/settings.json`): `UserPromptSubmit`
captures the prompt, `Stop` records when the turn ended so time tracking has an agent
duration. Both run `apc capture claude-code` with a `timeout` of 10 seconds, well inside
Claude Code's 30 second default. The hook always exits 0 with empty stdout, so it can never
block, alter or erase the prompt: exit 2 on `UserPromptSubmit` would erase it, and anything
printed to stdout on that event is injected into the model's context. Failures go to
`apc.log` and nowhere else.

**Codex CLI.** `apc install codex` merges `UserPromptSubmit` and `Stop` into
`$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`), running `apc capture codex`. The
older mechanism is behind a flag: `apc install codex --legacy` writes
`notify = ["apc", "capture", "codex"]` into the top-level section of `~/.codex/config.toml`
instead, and leaves an existing `notify` line belonging to something else alone rather than
clobbering it. Pick one. If both are present every turn is captured twice and the two records
do not dedupe, because the hook reports `session_id` and `notify` reports `thread-id`;
`apc doctor` prints a warning when it sees both. The `notify` payload arrives as the last
argv argument with stdin closed, which is why `apc capture` accepts a trailing JSON argument.

**Codex hook trust.** Codex discovers `hooks.json` but refuses to run anything in it until
that hook has been trusted, so a freshly written `hooks.json` captures nothing on its own.
Trust lives in the user layer's `~/.codex/config.toml`, under `[hooks.state]`, keyed by
`"<absolute path of hooks.json>:<event_label>:<group_index>:<handler_index>"` with the
SHA-256 of the hook's normalized identity as `trusted_hash`, which is why `apc install codex`
writes those two entries for you and `apc doctor` re-checks them. Pass `apc install codex
--no-trust` if you would rather Codex ask you itself: start `codex` once and its "hooks need
review" screen offers "Trust all and continue" (`codex exec --dangerously-bypass-hook-trust`
runs untrusted hooks for a single command). Change the hook command or its timeout and the
hash changes with it, so re-run `apc install codex` after any edit to `hooks.json`.

**OpenCode.** `apc install opencode` copies the plugin to
`$XDG_CONFIG_HOME/opencode/plugin/agent-prompt-capture.js` (default
`~/.config/opencode/plugin/`). It hooks `chat.message` for prompts and the `session.idle`
event for turn ends. Three things to know: slash commands are expanded before the plugin
sees them, so `/foo` is captured as the text it expands to and not as `/foo`; synthetic and
ignored parts are dropped, so an `@agent` mention does not store an instruction the user
never wrote; and the capture process is spawned detached and never awaited, because
`chat.message` is awaited by the session loop and anything slow there is latency you feel on
every send.

## Development

```sh
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run pytest -q

node extension/scripts/test-prescrub.js   # pre-scrub unit tests
node extension/scripts/smoke.mjs          # loads the unpacked extension against fixtures

uv run python scripts/e2e.py              # end-to-end run in a throwaway HOME
```

### Testing Codex against OpenRouter

Driving the real Codex CLI without an OpenAI account is the quickest way to check that the
hooks fire and that trust is being written correctly. In the `CODEX_HOME` you are testing
with, put this in `config.toml` next to the `[hooks.state]` entries `apc install codex`
wrote:

```toml
model = "openai/gpt-5.6-luna"
model_provider = "openrouter"
[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"
```

Then, with `OPENROUTER_API_KEY` exported:

```sh
codex exec --skip-git-repo-check "say hi" </dev/null
```

Redirecting stdin from `/dev/null` is not optional: `codex exec` waits on stdin otherwise
and the run hangs. Check the result with `apc list --source codex_cli`.

`scripts/e2e.py` drives the whole pipeline in a temporary `HOME`/`APC_HOME`: install the
hooks, feed a hook payload through `apc capture`, POST one through the listener, then read
it back over the CLI and the MCP server, without touching your real configuration. It is
being written alongside this document, so check its `--help` before relying on the details.

## Limitations and roadmap

* Browser sources have no turn-end signal, so `agent_minutes` and think time are blank for
  anything captured on claude.ai or chatgpt.com. DOM-based turn-end detection is the next
  thing worth building.
* Not on PyPI yet, so installation means cloning. Publishing is planned.
* Chrome only. The listener already accepts `moz-extension://` origins, but there is no
  Firefox build of the extension.
* Every prompt is treated alike. Per-prompt intent tagging (debugging, feature work,
  review, learning) would make `time_summary` far more useful than "minutes per project".
* The scrubber is regex-first and has known gaps; see [docs/PRIVACY.md](docs/PRIVACY.md).

## License

MIT. See [LICENSE](LICENSE).

[^cc]: `claude mcp add [options] <name> -- <command> [args...]`, from
    <https://code.claude.com/docs/en/mcp> (accessed 2026-09-19). The `--` separator is
    required; everything after it is passed to the server untouched.
[^oc]: `"mcp": { "<name>": { "type": "local", "command": [...] } }` in `opencode.json`,
    from <https://opencode.ai/docs/mcp-servers/> (accessed 2026-09-19).
[^cd]: `"mcpServers": { "<name>": { "command": ..., "args": [...] } }` in
    `claude_desktop_config.json`, from
    <https://modelcontextprotocol.io/docs/develop/connect-local-servers> (accessed
    2026-09-19), which also gives the macOS and Windows file locations.
