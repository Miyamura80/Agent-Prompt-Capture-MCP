# Privacy model

Everything here is local. There is no account, no sync, no telemetry and no outbound network
call in the package. The Chrome extension posts to `http://127.0.0.1:47821` and nowhere else;
the listener binds a loopback address and refuses anything else unless someone passes
`--allow-remote` on purpose. This document says exactly what is stored, what is not, and
where the scrubber is known to be weak.

## What is stored

One row per prompt, in `$APC_HOME/prompts.db`:

| Column | Contents |
|---|---|
| `id` | a random uuid4 |
| `ts` | when the prompt was submitted, ISO 8601 UTC |
| `turn_end_ts` | when the agent finished that turn, or NULL (always NULL for browser sources) |
| `source` | one of the seven source ids |
| `prompt` | **the scrubbed text**. The raw text is never written to disk |
| `prompt_hash` | SHA-256 of the raw text, used only for deduplication |
| `session_id` | the agent or conversation id, itself run through the scrubber |
| `account` | an alias or `sha256:<12 hex>`, never a raw address |
| `cwd` | the working directory with the home-dir username replaced by `[USER]` |
| `project` | directory basename, repo id or conversation title, scrubbed against your extra terms |
| `char_count` | length of the scrubbed prompt |
| `pii_findings` | per-category counts, for example `{"email": 2, "api_key": 1}`. Counts only, no values |
| `metadata` | source-specific extras (model, turn id, attachment count, stripped URL), every string leaf scrubbed |

## What is never stored

* Assistant and model responses. Only what you typed into the composer.
* Tool calls, tool output, command output and diffs.
* File contents, attachments and images. An OpenCode turn records how many files were
  attached, not what they were.
* Raw account addresses.
* Anything from a browser account that is not on your allowlist, and anything from a
  browser tab where the extension could not determine the account.

## The scrub pipeline

`pii.py` runs in a single pass. Every rule proposes candidate spans over the original text;
the spans are then reconciled (earliest start wins, the longest wins a tie, category order
breaks the rest) and only the survivors are replaced. That is what stops a JWT from being
half-eaten by the phone-number rule.

Each surviving span becomes `[CATEGORY_N]`. `N` counts per category per prompt, and repeats
of the same value reuse the same placeholder, so a prompt that mentions one address twice
reads `[EMAIL_1]` in both places and a second address becomes `[EMAIL_2]`. Placeholders never
span lines.

Categories, in the order they are applied:

| Category | Matches |
|---|---|
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` blocks, including the body |
| `ssh_key` | SSH material. Declared in `CATEGORIES`; the matching rule was still being written when this was documented, so check `pii.py` before relying on it |
| `jwt` | `eyJ...` three-part tokens |
| `webhook_url` | webhook endpoints. Same caveat as `ssh_key`: declared, rule pending |
| `api_key` | `sk-`, `sk-ant-`, `sk-proj-`, `github_pat_`, `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, `glpat-`, `xox[abpsr]-`, `AKIA`/`ASIA`, `AIza`, `npm_`, `pypi-`, `hf_`, `Bearer <token>`, and the value of a generic `api_key`/`token`/`secret`/`password` assignment |
| `url_credentials` | the `user:pass` between `://` and `@` in a URL |
| `credit_card` | 13 to 19 digits with optional spaces or dashes, Luhn-valid |
| `iban` | two letters, two check digits, then 11 to 30 alphanumerics, mod-97 valid |
| `ssn` | US social security numbers with separators |
| `email` | addresses with a real TLD, so `foo@bar` in code is left alone |
| `phone` | E.164 and common grouped formats, 7 to 15 digits, rejecting dates and version strings |
| `ipv6` | full and compressed forms |
| `ipv4` | dotted quads, **including private and loopback ranges**: a private address still identifies a network |
| `mac_address` | colon or dash separated |
| `uk_postcode` | UK postcodes. Same caveat as `ssh_key`: declared, rule pending |
| `home_path` | the username in `/Users/x`, `/home/x`, `C:\Users\x`, and bare `/root` |
| `user_term` | your `extra_terms`, word-bounded and case-insensitive |
| `custom` | your `extra_patterns` |
| `person`, `location` | only when `enable_ner = true` |

Home paths are the one category that does not get a number: the username is replaced with
the fixed `[USER]`, so `/Users/alice/dev/proj` becomes `/Users/[USER]/dev/proj` and paths
stay readable and comparable. `shared`, `public`, `default` and `all users` are skipped as
they are not usernames. Bare `/root` becomes `/home/[USER]`.

## Accounts

The allowlist is compared against the raw address, because that is the only way to decide
whether a capture belongs to you, but the raw address is never stored. On the way in:

1. If `[accounts]` in `config.toml` has an alias for that address, the alias is stored
   (`"me@work.com" = "work"` stores `work`).
2. Otherwise the address is lowercased, hashed with SHA-256, and stored as `sha256:` plus
   the first 12 hex characters.

The hash is stable, so grouping by account works, and it is short, so it is not a useful
handle for anything else. If you would rather not think about it, set an alias.

## The dedup hash

`prompt_hash` is a SHA-256 of the **raw**, unscrubbed prompt. It exists because browser
extensions double-fire: a record with the same `(source, session_id, prompt_hash)` arriving
within 5 seconds of an existing one is dropped. A hash is one-way, so the column cannot be
turned back into your text. It is worth being precise about what that does and does not
mean: someone who already has a guess at an exact prompt could confirm it by hashing their
guess. Nobody can recover an unknown prompt from it.

## Where the data lives

`$APC_HOME`, default `~/.agent-prompt-capture`, created mode 0700:

| File | Contents |
|---|---|
| `config.toml` | your configuration |
| `prompts.db` | the SQLite database, plus its WAL files |
| `token` | the listener's shared secret, mode 0600, regenerated by `apc token --rotate` |
| `apc.log` | a rotating log, 1 MiB by 3 files. INFO by default, DEBUG under `APC_DEBUG=1` |

The log records ids, sources and character counts, not prompt text. Under `APC_DEBUG=1` it
becomes more verbose, so treat a debug log as sensitive.

## Deleting data

```sh
apc purge --before 2026-01-01 --yes      # everything older than a date
apc purge --source chatgpt_web --yes     # everything from one source
apc purge --source claude_web --before 7d --yes
```

`apc purge` refuses to run with neither `--before` nor `--source`, so there is no single
command that wipes the database by accident. To wipe it on purpose, delete `prompts.db`
(the next command recreates an empty one). To stop capturing without uninstalling, add
source ids to `disabled_sources`, or run `apc uninstall claude-code` / `codex` / `opencode`.

## Known limitations of the scrubber

These are real gaps, reported by the implementer. None of them is theoretical.

* **Four-part dotted version strings are redacted as IPv4.** `1.2.3.4` is a valid dotted
  quad, so a version number written that way becomes `[IPV4_1]`. The phone rule knows about
  version strings; the IPv4 rule cannot tell them apart. This over-redacts rather than
  under-redacts, but it does mangle text an agent may need.
* **IBANs written with spaces are not caught.** The checksum validator strips spaces, but
  the pattern that finds candidates does not allow them, so `GB82 WEST 1234 5698 7654 32`
  passes through while the unspaced form is redacted.
* **NER is optional and off by default.** Personal names, company names and place names in
  running text are not redacted unless you install the `ner` extra
  (`presidio-analyzer`, `spacy`) and set `enable_ner = true`. Until then, the honest way to
  redact "my manager Dana at Contoso" is to put those words in `extra_terms`.
* **The extension pre-scrub is defence in depth, not the scrub.** `lib/prescrub.js` covers
  emails and a few obvious token shapes, and it exists to make the hop to localhost less
  interesting. The real scrub happens server-side, after the POST. A prompt sitting in the
  extension's retry queue is only pre-scrubbed; **Clear queue** in Options discards it.
* Regexes are a blunt instrument in general. A secret in a shape nobody has written a rule
  for goes into the database as text. If you paste credentials into agents, rotate them; do
  not rely on this to have caught them.

## For the agent reading this data

Everything returned by the MCP tools is the stored, scrubbed text. Placeholders like
`[EMAIL_1]` or `[USER]` are redactions, not literal content, and `pii_findings` says how
many of each were removed. There is no way to ask the server for the original.
