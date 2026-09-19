# agent-prompt-capture

Capture every prompt you send to your coding agents and chat UIs, scrub PII **before**
anything touches disk, store it locally in SQLite, and query it over MCP — so an agent can
answer *"what have I been spending my time on, and how could I use it better?"*.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the contracts and
[docs/research/hook-specs.md](docs/research/hook-specs.md) for the verified hook payloads.

## Sources

| source id         | where it comes from                          |
|-------------------|----------------------------------------------|
| `claude_code`     | Claude Code `UserPromptSubmit` + `Stop` hooks |
| `codex_cli`       | Codex CLI `hooks.json` (or legacy `notify`)   |
| `opencode`        | OpenCode plugin `chat.message` + `session.idle` |
| `claude_web`      | Chrome extension on claude.ai                |
| `claude_code_web` | Chrome extension on claude.ai/code           |
| `chatgpt_web`     | Chrome extension on chatgpt.com              |
| `codex_cloud`     | Chrome extension on chatgpt.com/codex        |

Browser sources are captured **only** for accounts on your allowlist. Claude Code on the
web does not run user hooks, which is why `claude_code_web` comes from the extension.

## Install

```sh
uv sync --dev
uv run apc doctor
```

```sh
apc install claude-code    # merges UserPromptSubmit + Stop hooks into ~/.claude/settings.json
apc install codex          # merges UserPromptSubmit + Stop hooks into ~/.codex/hooks.json
apc install codex --legacy # instead sets the deprecated notify = ["apc","capture","codex"]
apc install opencode       # copies the plugin into ~/.config/opencode/plugin/
claude mcp add agent-prompt-capture -- apc mcp
```

Install Codex one way or the other, not both: `apc doctor` warns if it finds hooks *and*
a `notify` line, because that captures every turn twice.

## Running the listener

```sh
apc serve                  # 127.0.0.1:47821 by default
apc token                  # the shared secret the Chrome extension needs
```

Example service units are in [`docs/service/`](docs/service/): `apc-listener.service`
(systemd user unit) and `com.agent-prompt-capture.listener.plist` (launchd).

## Querying

```sh
apc list --since 24h
apc search "refactor"
apc stats --group-by day
apc time --since 7d --group-by project     # where the time went
apc digest 2026-09-19                      # one day in detail
apc export --format csv --since 7d
apc purge --before 2026-01-01 --yes
```

The MCP server exposes the same data as tools (`list_prompts`, `search_prompts`,
`get_prompt`, `prompt_stats`, `list_sources`, `list_sessions`, `time_summary`,
`activity_timeline`, `daily_digest`) and resources (`apc://prompts/recent`,
`apc://stats/summary`, `apc://digest/today`).

## Configuration

`$APC_HOME/config.toml` (default `~/.agent-prompt-capture/config.toml`):

```toml
[capture]
allowed_accounts = ["me@work.com"]
disabled_sources = []

[accounts]
"me@work.com" = "work"

[pii]
extra_terms = []
extra_patterns = []
enable_ner = false

[server]
host = "127.0.0.1"
port = 47821

[time]
idle_gap_minutes = 30
tail_minutes = 5
```

## Privacy

Raw prompt text is never written to disk. Everything is scrubbed in memory first:
private keys, JWTs, API keys, URL credentials, credit cards (Luhn-checked), IBANs, SSNs,
emails, phone numbers, IPs, MAC addresses, home-directory usernames, and any extra terms
or patterns you configure. `prompt_hash` is a SHA-256 of the raw text used only for
deduplication. Account emails are stored as an alias or `sha256:<12 hex>`, never raw.

One behaviour worth knowing: OpenCode expands slash commands before the plugin sees them,
so `/foo` is captured as its expanded text, not as `/foo`.

## Development

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```
