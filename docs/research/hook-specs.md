# Hook specs: verified payload shapes and sources

Research date: **2026-09-19**. Every claim below cites the URL it was read from.
Anything not verifiable from a primary source is labelled **ASSUMPTION**.

Egress notes for reproducing this research: `docs.anthropic.com`,
`developers.openai.com` and `doc.jarvisuni.com` are blocked by the network egress
proxy in this environment. The Claude Code docs are mirrored at
`code.claude.com/docs/en/<page>` (append `.md` for the raw Markdown), and the Codex
CLI docs now redirect to `developers.openai.com`, so Codex facts below were read
directly from the Rust source on `raw.githubusercontent.com/openai/codex/main/`.

```
  capture surface            mechanism                      transport
  ---------------------------------------------------------------------------
  Claude Code CLI     UserPromptSubmit hook (settings.json) --> stdin JSON
  Codex CLI           UserPromptSubmit hook (hooks.json)    --> stdin JSON
                      notify = [...] (config.toml)          --> argv[last] JSON
  OpenCode            plugin hook "chat.message"            --> we spawn + stdin
  4 x browser         Chrome MV3 content script             --> HTTP POST
```

---

## 1. Claude Code `UserPromptSubmit` hook

Source: <https://code.claude.com/docs/en/hooks> (read as
<https://code.claude.com/docs/en/hooks.md>), accessed 2026-09-19.

### 1.1 Settings locations

From the "Hook locations" table: `~/.claude/settings.json` (user, all projects),
`.claude/settings.json` (project, committable), `.claude/settings.local.json`
(project, gitignored), managed policy settings (org-wide), plugin `hooks/hooks.json`,
skill frontmatter, subagent frontmatter.

Precedence (<https://code.claude.com/docs/en/settings>, accessed 2026-09-19), highest
first: managed, `claude --settings`, `.claude/settings.local.json`,
`.claude/settings.json`, `~/.claude/settings.json`. Importantly for us: *"Hook entries
merge across settings levels rather than replacing each other"* (hooks.md), so
`apc install claude-code` must **merge into** the `UserPromptSubmit` array, not overwrite it.

### 1.2 Exact hooks JSON structure

Three levels: **hook event** -> **matcher group** -> **hook handler**.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "apc capture claude-code",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Handler common fields (hooks.md, "Common fields"):

* `type` (required): `"command" | "http" | "mcp_tool" | "prompt" | "agent"`.
* `timeout` (optional, seconds). Verbatim: *"Defaults: 600 for `command`, `http`,
  and `mcp_tool`; 30 for `prompt`; 60 for `agent`. Claude Code lowers the `command`,
  `http`, and `mcp_tool` default to 30 on `UserPromptSubmit`…"* So the effective
  default for our hook is **30 s**, not 600 s.
* `statusMessage`, `once`, and `if` (the `if` filter is only evaluated on tool
  events; *"On other events, a hook with `if` set never runs"* — so never set `if`
  on `UserPromptSubmit`).
* Command-hook extras: `args` (exec form, no shell), `async`, `asyncRewake`, `shell`.

### 1.3 Matcher semantics for this event

Verbatim from the matcher table: `UserPromptSubmit`, `PostToolBatch`, `Stop`,
`TeammateIdle`, `TaskCreated`, `TaskCompleted`, `WorktreeCreate`, `WorktreeRemove`,
`MessageDisplay` have **"no matcher support"** and **"always fires on every occurrence"**.

So the matcher group for `UserPromptSubmit` must omit `matcher` entirely. Writing one
is harmless but meaningless.

### 1.4 Exact stdin JSON

Verbatim example from hooks.md, "UserPromptSubmit input":

```json
{
  "session_id": "abc123",
  "transcript_path": "/Users/.../.claude/projects/.../00893aaf-19fa-41d2-8238-13269b9b3ca0.jsonl",
  "cwd": "/Users/...",
  "permission_mode": "default",
  "hook_event_name": "UserPromptSubmit",
  "prompt": "Write a function to calculate the factorial of a number"
}
```

Optional common input fields that may also appear (hooks.md, "Common input fields"):

* `prompt_id` — UUID for the prompt, matches the OTel `prompt.id` attribute.
  **Requires v2.1.196+**; absent until the first user input.
* `scratchpad_dir` — **Requires v2.1.257+**; absent when unavailable.
* `permission_mode` — `"default" | "plan" | "acceptEdits" | "auto" | "dontAsk" | "bypassPermissions"`.
  The mode labelled **Manual** arrives as `"default"`.
* `effort` — `{ "level": "low"|"medium"|"high"|"xhigh"|"max" }`. Documented as present for
  tool-context events (`PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`); **not** listed
  for `UserPromptSubmit`.
* `agent_id`, `agent_type` — only under `--agent` or inside a subagent.
* `transcript_path` is written asynchronously and may lag the in-memory conversation.

The `ARCHITECTURE.md` contract matches the documented example exactly; treat the above
as optional extras.

### 1.5 stdout / exit code semantics — and the safe behaviour for us

From "Exit code 0": *"For most events, Claude Code writes stdout to the debug log and
doesn't show it in the transcript. The exceptions are `UserPromptSubmit`,
`UserPromptExpansion`, `SessionStart`, and `PostModelSwitch`, where Claude Code adds
plain-text stdout as context that Claude can see and act on."* JSON vs plain text is
decided by shape (`{`…`}` -> parsed as JSON; anything else -> plain text). *"Stderr from
a hook that exits 0 goes to the debug log only, never the transcript."*

From "Exit code 2": exit 2 blocks *"whether or not you print JSON"*; the per-event table
gives `UserPromptSubmit` -> **"Blocks prompt processing and erases the prompt"**.

From "Other exit codes": with plain-text or empty stdout, any code other than 0 or 2 is a
*non-blocking error* — the prompt proceeds but the transcript shows `<hook name> hook
error` + the first stderr line, prefixed `Failed with non-blocking status code:`.

**Safe behaviour for `apc capture claude-code`** (enforce in `cli.py`):

1. **Always `exit 0`.** Exit 2 erases the user's prompt; any other non-zero code prints
   an error notice into their transcript.
2. **Write nothing to stdout.** Empty stdout on exit 0 is the documented no-op. Any
   stdout on this event is silently injected into Claude's context on every turn.
3. Stderr on exit 0 is discarded, but log to `apc.log` anyway.
4. Finish well inside the 30 s default; a timed-out hook is cancelled, its output
   discarded, and a timeout notice shown.

### 1.6 Does Claude Code on the web run user hooks?

**Verified: not your personal ones.** From
<https://code.claude.com/docs/en/settings> ("Settings in cloud sessions"), accessed
2026-09-19, verbatim: *"**User and project local settings** (`~/.claude/settings.json`
and `.claude/settings.local.json`): **not read**. Both stay on your machine…"*, while
*"**Shared project settings** (`.claude/settings.json`): read in a session with one
repository"* — and in a multi-repo session it loads *"only the plugins and marketplaces
the file declares, **not permission rules, hooks, `env`, or other keys**"*.

hooks.md, "Hook locations" agrees: *"Cloud sessions don't read your local
`~/.claude/settings.json`; hooks there come from the repo… and from your organization's
server-managed settings."*

**Consequence:** `claude.ai/code` prompts can only be captured by the Chrome extension
(`claude_code_web`). The cloud VM is a different machine with no `apc` binary. Do not
try to solve `claude_code_web` with a hook.

---

## 2. Codex CLI

### 2.1 Does Codex CLI have a hooks system in 2026? **Yes.**

Verified from source, not from a blog. Evidence:

* <https://raw.githubusercontent.com/openai/codex/main/docs/config.md> (accessed
  2026-09-19) has a "Lifecycle hooks" section: *"Admins can set top-level
  `allow_managed_hooks_only = true` in `requirements.toml` to ignore user, project, and
  session hook configs while still allowing managed hooks…"*
* The crate <https://github.com/openai/codex/tree/main/codex-rs/hooks> exists, with
  per-event generated JSON Schemas under `schema/generated/`, including
  `user-prompt-submit.command.{input,output}.schema.json`.

Event list (`codex-rs/config/src/hook_config.rs`, `HookEventsToml`): `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`,
`SessionEnd`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`.

Feature gating, from `codex-rs/features/src/lib.rs` (accessed 2026-09-19):

```rust
FeatureSpec {
    id: Feature::CodexHooks,
    key: "hooks",
    stage: Stage::Stable,
    default_enabled: true,
},
```

with the doc comment `/// Enable Claude-style lifecycle hooks loaded from hooks.json files.`
So on current `main` hooks are **stable and on by default**; the feature key is `hooks`
(`[features] hooks = true` re-enables it if an admin turned it off). Third-party
write-ups cite an older `[features] codex_hooks = true` spelling; **ASSUMPTION:** that
was an earlier release's key, so `apc install codex` should write `hooks.json` and leave
feature flags alone.

### 2.2 Config file path and structure

From `codex-rs/hooks/src/engine/discovery.rs` (accessed 2026-09-19):

* `load_hooks_json()` resolves `config_folder?.join("hooks.json")` — i.e. **`hooks.json`
  sits next to `config.toml` in each config layer**: `~/.codex/hooks.json` for the user
  layer, `<project>/.codex/hooks.json` for the project layer.
* Hooks can equivalently be an inline `[hooks]` table inside the layer's `config.toml`.
  Declaring both in one layer logs: *"loading hooks from both [json] and [toml]; prefer
  a single representation for this layer"*.
* Layer order low-to-high: `PackagedDefaults`, `System`, `User`, `Project`, `MDM`,
  `EnterpriseManaged`, `SessionFlags`. All matching hooks from all layers run.

Exact shape, from `codex-rs/config/src/hook_config.rs` (abridged):

```rust
pub struct HooksFile { pub description: Option<String>, pub hooks: HookEventsToml }
pub struct HookEventsToml { #[serde(rename = "UserPromptSubmit")] pub user_prompt_submit: Vec<MatcherGroup>, /* 11 more, all PascalCase */ }
pub struct MatcherGroup { pub matcher: Option<String>, pub hooks: Vec<HookHandlerConfig> }
#[serde(tag = "type")] pub enum HookHandlerConfig {
  #[serde(rename = "command")] Command {
     command: String,
     #[serde(rename = "commandWindows", alias = "command_windows")] command_windows: Option<String>,
     #[serde(rename = "timeout")] timeout_sec: Option<u64>,
     r#async: bool,
     #[serde(rename = "statusMessage")] status_message: Option<String>,
     #[serde(rename = "additionalContextLimit")] additional_context_limit: Option<usize>,
  },
  #[serde(rename = "mcp_tool")] McpTool { .. }, #[serde(rename = "prompt")] Prompt {}, #[serde(rename = "agent")] Agent {},
}
```

Note the event keys are **PascalCase**, and `timeout` is the JSON spelling of `timeout_sec`.
So `~/.codex/hooks.json` for us is:

```json
{
  "description": "agent-prompt-capture",
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "apc capture codex", "timeout": 10 }
        ]
      }
    ]
  }
}
```

The `config.toml` equivalent (same data, TOML spelling):

```toml
[[hooks.UserPromptSubmit]]
  [[hooks.UserPromptSubmit.hooks]]
  type = "command"
  command = "apc capture codex"
  timeout = 10
```

Default timeout (`discovery.rs`, `normalize_command_hook`): `timeout_sec.unwrap_or(600).max(1)`
for every event except `SessionEnd`/`Interrupt`, which use their own clamped default.
Note Codex does **not** lower the default for `UserPromptSubmit` the way Claude Code does.

#### Hook trust (**Verified live 2026-09-20**, Codex CLI 0.155.1)

Writing `hooks.json` is **not enough**. Codex discovers the file but never executes a hook
until that hook's identity hash has been persisted as trusted; with our two hooks installed
and no trust entry, the real binary ran neither of them and offered a "hooks need review"
screen on the next interactive start ("Trust all and continue" trusts them all).
`codex exec --dangerously-bypass-hook-trust` runs untrusted hooks without persisting
anything.

Trust state lives in the **USER layer `config.toml`** (`$CODEX_HOME/config.toml`), in a
`hooks.state` table keyed by
`"<absolute path of hooks.json>:<event_label>:<group_index>:<handler_index>"`:

```toml
[hooks.state]
"/Users/alice/.codex/hooks.json:user_prompt_submit:0:0" = { trusted_hash = "sha256:..." }
"/Users/alice/.codex/hooks.json:stop:0:0" = { trusted_hash = "sha256:..." }
```

The event label is the snake_case of the PascalCase event name: `UserPromptSubmit` ->
`user_prompt_submit`, `Stop` -> `stop`. The group and handler indices are the hook's
position in `hooks.json`, so a hook appended after somebody else's group is `...:1:0`, not
`...:0:0`.

The hash is `"sha256:" + sha256(canonical_json)`, where `canonical_json` is compact
(`separators=(",", ":")`) and recursively key-sorted JSON of the normalized identity:

```python
{"event_name": "<label>", "hooks": [{"async": False, "command": "<command>", "timeout": <int>, "type": "command"}]}
```

`matcher` is omitted when it is null, `timeout` is the configured timeout (600 when the
config leaves it out), and every other optional field is omitted when unset. Two values
confirmed against the running binary for command `apc capture codex` with `timeout` 10:

| event label | trusted_hash |
|---|---|
| `user_prompt_submit` | `sha256:84bac188cd8cd2224b7d68e5b2bd25390fa243baea19406b97983e6cb3ef61bc` |
| `stop` | `sha256:d955e4abf3ea73d405f18346ddf4eb848ed4b574ac58c6635614ba260151f987` |

Consequences for us: `apc install codex` writes these entries itself (`--no-trust` opts
out), the hash must be computed from the exact command and timeout the installer writes so
the two cannot drift, and `apc doctor` reports installed-but-untrusted hooks, including the
case where the keys name a `hooks.json` at a path we are no longer using.

### 2.3 Exact stdin payload for the user-prompt event

`codex-rs/hooks/schema/generated/user-prompt-submit.command.input.schema.json`
(accessed 2026-09-19), verbatim required set and properties:

```json
{ "additionalProperties": false,
  "required": ["cwd","hook_event_name","model","permission_mode","prompt","session_id","transcript_path","turn_id"],
  "properties": {
    "agent_id": {"type":"string"}, "agent_type": {"type":"string"},
    "cwd": {"type":"string"},
    "hook_event_name": {"const":"UserPromptSubmit","type":"string"},
    "model": {"type":"string"},
    "permission_mode": {"enum":["default","acceptEdits","plan","dontAsk","bypassPermissions"]},
    "prompt": {"type":"string"}, "session_id": {"type":"string"},
    "transcript_path": {"type":["string","null"]},
    "turn_id": {"type":"string","description":"Codex extension: expose the active turn id to internal turn-scoped hooks."}
  } }
```

A concrete instance:

```json
{
  "session_id": "b5f6c1c2-1111-2222-3333-444455556666",
  "turn_id": "12345",
  "transcript_path": "/Users/alice/.codex/sessions/2026/09/19/rollout-2026-09-19T20-11-03-b5f6c1c2-1111-2222-3333-444455556666.jsonl",
  "cwd": "/Users/alice/dev/proj",
  "model": "gpt-5.1-codex",
  "permission_mode": "default",
  "hook_event_name": "UserPromptSubmit",
  "prompt": "Rename `foo` to `bar` and update the callsites."
}
```

Payload delivery is stdin JSON: `engine/command_runner.rs` does
`stdin.write_all(input_json.as_bytes()).await`.

### 2.4 Exit code semantics (Codex)

**Different from Claude Code.** I read `codex-rs/hooks/src/engine/{command_runner,
output_parser,dispatcher,mod}.rs` (accessed 2026-09-19) and grepped for `exit_code`: the
only occurrence is the plain field `pub exit_code: Option<i32>` on `HandlerRunResult`.
There is **no `== 2` branch and no special-casing of any exit code**. Blocking is
expressed purely through parsed stdout JSON
(`user-prompt-submit.command.output.schema.json`):

```json
{ "properties": {
    "continue":           {"type":"boolean","default":true},
    "decision":           {"enum":["block"],"default":null},
    "hookSpecificOutput": {"required":["hookEventName"],
                           "properties":{"hookEventName":{"const":"UserPromptSubmit"},
                                         "additionalContext":{"type":"string"}}},
    "reason":             {"type":"string"},
    "stopReason":         {"type":"string"},
    "suppressOutput":     {"type":"boolean","default":false},
    "systemMessage":      {"type":"string"} } }
```

**Safe behaviour for `apc capture codex`:** same rule as Claude Code — exit 0, empty
stdout, never emit `decision`. (**ASSUMPTION**, unverified: a non-zero exit is logged and
otherwise ignored. Exit 0 always keeps one `cli.py` rule covering both CLIs.)

### 2.5 The `notify` mechanism

Verified from
<https://raw.githubusercontent.com/openai/codex/main/codex-rs/hooks/src/legacy_notify.rs>
(accessed 2026-09-19). Key facts, several of which differ from the common folklore:

* The file's own doc comment: *"Legacy notify payload appended as the **final argv
  argument** for backward compatibility."* The JSON arrives as **`argv[last]`, not on
  stdin**. The spawned process gets `stdin(Stdio::null())`, `stdout(Stdio::null())`,
  `stderr(Stdio::null())`.
* There is exactly **one** notification variant — `HookEvent::AfterAgent` ->
  `UserNotification::AgentTurnComplete`. `notify` therefore fires **only at
  agent-turn-complete**, never on prompt submit.
* The process is spawned fire-and-forget (`command.spawn()`); Codex does not wait for it
  and ignores its exit code (`Ok(_) => HookResult::Success`).
* The source carries a `// TODO: Remove this hook and its environment plumbing when
  legacy `notify` support is removed.` — treat `notify` as deprecated and prefer hooks.

Serde definition: `#[serde(tag = "type", rename_all = "kebab-case")] enum UserNotification`
with one variant `AgentTurnComplete { thread_id, turn_id, cwd, client: Option<String>
(skipped if none), input_messages: Vec<String>, last_assistant_message: Option<String> }`,
itself `#[serde(rename_all = "kebab-case")]`.

Real example, verbatim from the crate's own test `expected_notification_json()`:

```json
{
  "type": "agent-turn-complete",
  "thread-id": "b5f6c1c2-1111-2222-3333-444455556666",
  "turn-id": "12345",
  "cwd": "/Users/example/project",
  "client": "codex-tui",
  "input-messages": ["Rename `foo` to `bar` and update the callsites."],
  "last-assistant-message": "Rename complete and verified `cargo build` succeeds."
}
```

Config spelling in `~/.codex/config.toml` (**ASSUMPTION** on the exact line — the
config reference now lives on the blocked `developers.openai.com`; this is the
long-standing documented form and matches `command_from_argv(&argv, ...)` taking an
argv vector):

```toml
notify = ["apc", "capture", "codex", "--argv-payload"]
```

Note the implication for our CLI: in `notify` mode the JSON is **not on stdin**, so
`apc capture codex` must accept the payload as a trailing argv argument too, falling
back to stdin when argv carries none.

### 2.6 Session logs as a fallback capture path

From `codex-rs/rollout/src/{lib,recorder}.rs` (accessed 2026-09-19):
`pub const SESSIONS_SUBDIR: &str = "sessions";` and the recorder resolves
`~/.codex/sessions/YYYY/MM/DD` (its own comment: `// Resolve ~/.codex/sessions/YYYY/MM/DD path.`).
Filenames are `rollout-<timestamp>-<conversation_id>.jsonl`, or
`rollout-<timestamp>-<conversation_id>_<rollout_id>.jsonl` after `thread/revert`. Older
releases wrote flat into `~/.codex/sessions/` (the recorder's doc example still shows
`~/.codex/sessions/rollout-2025-05-07T17-24-21-<uuid>.jsonl`), so glob recursively.

Each line is `RolloutLineRef { timestamp, ordinal: Option<u64>, #[serde(flatten)] item }`
over `RolloutItemWire`, which is `#[serde(tag = "type", rename_all = "snake_case")]`.
A user message is a `response_item` whose payload is `ResponseItem::Message`
(same tag/rename convention, `ContentItem::InputText { text }`):

```json
{"timestamp":"2026-09-19T20:11:03.123Z","ordinal":4,"type":"response_item",
 "payload":{"type":"message","role":"user",
            "content":[{"type":"input_text","text":"Rename `foo` to `bar`."}]}}
```

Other `type` values you will see and must skip: `session_meta`, `turn_context`,
`token_usage_record`, `compacted`, `event_msg`, `world_state`, `realtime_item`,
`security_risk_score`, `retained_context`, `inter_agent_communication`.
Images arrive as `{"type":"input_image", ...}` content items with no `text`.

---

## 3. OpenCode

Upstream repo has moved: `github.com/sst/opencode` now lives at
**`github.com/anomalyco/opencode`** and the default branch is `dev`. All source
citations below are `https://raw.githubusercontent.com/anomalyco/opencode/dev/<path>`,
accessed 2026-09-19. Docs: <https://opencode.ai/docs/plugins/>, accessed 2026-09-19.

### 3.1 Plugin file locations

From `packages/opencode/src/config/plugin.ts`:

```ts
export async function load(dir: string) {
  const plugins: ConfigPluginV1.Spec[] = []
  for (const item of await Glob.scan("{plugin,plugins}/*.{ts,js}", { ... })) { ... }
```

So **both `plugin/` and `plugins/`** are scanned, **both `.ts` and `.js`** load, and the
glob is one level deep only (no recursion, no `index.js` in a subfolder).

The directories scanned come from `packages/opencode/src/config/paths.ts`
(`ConfigPaths.directories`): `Global.Path.config`, then every `.opencode` directory
walked up from the cwd to the worktree root, then `~/.opencode`, then
`$OPENCODE_CONFIG_DIR`. `Global.Path.config` is `path.join(xdgConfig!, "opencode")`
(`packages/core/src/global.ts`), i.e. **`~/.config/opencode`**.

Concrete install targets for `apc install opencode`:

```
~/.config/opencode/plugin/agent-prompt-capture.js     (global)
<project>/.opencode/plugin/agent-prompt-capture.js    (project)
```

### 3.2 Exported function signature

From `packages/plugin/src/index.ts`:

```ts
export type PluginInput = {
  client: ReturnType<typeof createOpencodeClient>
  project: Project
  directory: string
  worktree: string
  experimental_workspace: { register(type: string, adapter: WorkspaceAdapter): void }
  serverUrl: URL
  $: BunShell
}
export type Plugin = (input: PluginInput, options?: PluginOptions) => Promise<Hooks>
export type PluginModule = { id?: string; server: Plugin; tui?: never }
```

`{ project, client, $, directory, worktree }` is correct but incomplete — `serverUrl` and
`experimental_workspace` are also passed.

### 3.3 `chat.message` — exact input and output

Verbatim from `packages/plugin/src/index.ts`:

```ts
  /**
   * Called when a new message is received
   */
  "chat.message"?: (
    input: {
      sessionID: string
      agent?: string
      model?: { providerID: string; modelID: string }
      messageID?: string
      variant?: string
    },
    output: { message: UserMessage; parts: Part[] },
  ) => Promise<void>
```

`UserMessage` and `TextPart` / `FilePart`, from
`packages/sdk/js/src/gen/types.gen.ts`:

```ts
export type UserMessage = {
  id: string
  sessionID: string
  role: "user"
  time: { created: number }              // epoch ms
  summary?: { title?: string; body?: string; diffs: Array<FileDiff> }
  agent: string
  model: { providerID: string; modelID: string }
  system?: string
  tools?: { [key: string]: boolean }
}
export type TextPart = {
  id: string; sessionID: string; messageID: string
  type: "text"; text: string
  synthetic?: boolean; ignored?: boolean
  time?: { start: number; end?: number }
  metadata?: { [key: string]: unknown }
}
export type FilePart = {
  id: string; sessionID: string; messageID: string
  type: "file"; mime: string; filename?: string; url: string; source?: FilePartSource
}
```

**The user text lives in `output.parts.filter(p => p.type === "text").map(p => p.text)`.**
`input.sessionID` is the session id; `output.message.id` is the message id (also
`input.messageID`, which is optional). `input.agent` and `input.model` give the agent
name and `{providerID, modelID}`.

Where it fires — `packages/opencode/src/session/prompt.ts` (accessed 2026-09-19):

```ts
      const resolvedParts = yield* Effect.forEach(input.parts, resolvePart, { concurrency: "unbounded" })
        .pipe(Effect.map((x) => x.flat().map(assign)))

      yield* plugin.trigger(
        "chat.message",
        { sessionID: input.sessionID, agent: input.agent, model: input.model,
          messageID: input.messageID, variant: input.variant },
        { message: info, parts: resolvedParts },
      )
```

That is **after** part resolution (agent `@mentions` expanded, synthetic instruction text
appended) and **before** image normalisation and the message being saved. Two direct
consequences for our plugin:

1. `parts` can contain entries with `synthetic: true` that the user never typed — e.g.
   the `" Use the above message and context to generate a prompt and call the task tool
   with subagent: …"` text the resolver appends for an `@agent` mention. **Filter
   `synthetic === true` and `ignored === true` out before capturing.**
2. `parts` is mutable and the hook is awaited, so a slow hook delays the user's turn.
   Spawn and detach; never `await` the child process.

### 3.4 The `event` hook, and which events carry user messages

```ts
event?: (input: { event: Event }) => Promise<void>
```

Relevant `Event` members (`packages/sdk/js/src/gen/types.gen.ts`):

```ts
export type EventMessageUpdated     = { type: "message.updated",      properties: { info: Message } }
export type EventMessagePartUpdated = { type: "message.part.updated", properties: { part: Part; delta?: string } }
export type EventSessionIdle        = { type: "session.idle",         properties: { sessionID: string } }
export type EventSessionCreated     = { type: "session.created",      properties: { info: Session } }
export type EventSessionUpdated     = { type: "session.updated",      properties: { info: Session } }
```

* `message.updated` carries the whole `Message` (a `UserMessage | AssistantMessage`
  union). `properties.info.role === "user"` identifies a user message, and `sessionID`
  is on `properties.info.sessionID`. It carries **no text** — parts are separate.
* `message.part.updated` carries one `Part`; the text of a user turn arrives here as
  `properties.part.type === "text"` with `properties.part.text`, and the session id is
  `properties.part.sessionID`. It fires repeatedly with `delta` during streaming, so it
  is a poor capture point (you would need to dedupe on `part.id`).

**Recommendation: use `chat.message`, not `event`.** `chat.message` fires exactly once
per submitted user message with the complete text; the event stream would require
reassembly and dedupe.

### 3.5 Plain `.js` and the `@opencode-ai/plugin` dependency

Plain `.js` is supported — the glob is `*.{ts,js}` (see 3.1), and the docs say
"JavaScript or TypeScript files". `@opencode-ai/plugin` is **only a types package** and
does **not** need to be installed for a `.js` plugin, which exports a function and never
imports the module. OpenCode installs it anyway: `packages/opencode/src/config/config.ts`
runs `npmSvc.install(dir, { add: [{ name: "@opencode-ai/plugin", … }] })` for every config
directory in a detached background fiber. Shipping
`opencode-plugin/agent-prompt-capture.js` with zero imports is the right call — no install
step, no version skew.

### 3.6 `experimental.hook` in `opencode.json` (fallback)

From the config schema in `packages/sdk/js/src/gen/types.gen.ts`, the only command hooks
in config are:

```ts
  experimental?: {
    hook?: {
      file_edited?: { [key: string]: Array<{ command: Array<string>; environment?: {...} }> }
      session_completed?: Array<{ command: Array<string>; environment?: {...} }>
    }
  }
```

There is **no prompt-submit command hook** in `opencode.json`. `session_completed` is the
only turn/session-level command hook, takes an argv array, and carries no prompt text. So
the plugin is the only viable prompt-capture path for OpenCode.

### 3.7 Spawning a subprocess with stdin from a plugin

OpenCode's server runs on Bun and `$` is Bun's shell (`packages/plugin/src/shell.ts`
declares `BunShell` with `braces`, `escape`, `env`, `cwd`, `nothrow`, `throws`, and a
`BunShellPromise` exposing `readonly stdin: WritableStream`). Three options:

```js
// (a) Bun shell — stdin redirect from a string/Buffer. Requires Bun.
await $`apc capture opencode < ${JSON.stringify(payload)}`.nothrow().quiet()

// (b) Bun.spawn — explicit, detachable, no shell parsing.
const p = Bun.spawn(["apc", "capture", "opencode"], { stdin: "pipe", stdout: "ignore", stderr: "ignore" })
p.stdin.write(JSON.stringify(payload)); p.stdin.end(); p.unref?.()

// (c) node:child_process — portable across Bun and Node, safest default.
const { spawn } = await import("node:child_process")
const c = spawn("apc", ["capture", "opencode"], { stdio: ["pipe", "ignore", "ignore"], detached: true })
c.on("error", () => {}); c.stdin.end(JSON.stringify(payload)); c.unref()
```

**Recommendation: (c).** It works under both Bun and Node, `detached: true` + `unref()`
means the plugin never blocks the user's turn, the `error` handler swallows a missing
`apc` binary, and there is no shell quoting to get wrong. **ASSUMPTION:** `node:child_process`
is available in the OpenCode plugin sandbox — this is Bun's Node compatibility layer and
is not something I could verify from the plugin loader source.

---

## 4. Chrome extension targets

Verification warning for this whole section: claude.ai and chatgpt.com sit behind
Cloudflare bot management. Unauthenticated probes from this environment on 2026-09-19:

```
https://claude.ai/api/organizations  -> 403  "<title>Just a moment...</title>"
https://claude.ai/api/bootstrap      -> 403  "<title>Just a moment...</title>"
https://claude.ai/api/account        -> 403  "<title>Just a moment...</title>"
https://chatgpt.com/api/auth/session -> 403 at the proxy CONNECT
```

The paths are served by the app origin, but nothing about their bodies is observable
from outside a logged-in browser. **Every response shape and DOM selector below that is
not from a primary doc is marked ASSUMPTION. Build the extension defensively: probe a
candidate list, tolerate total failure, never capture when the account is unknown.**

### 4.1 claude.ai chat UI (`claude_web`)

**(a) Account email — ASSUMPTION.** Try, with `credentials: "include"`, from the content
script: `/api/bootstrap`, `/api/account`, `/api/organizations`, then
`/api/organizations/<org_uuid>/account`. Deep-search each JSON body for the first value
matching `/^[^@\s]+@[^@\s]+\.[^@\s]+$/`. The field is most likely spelled
`email_address` — that is the spelling in the documented Admin API
(<https://platform.claude.com/docs/en/manage-claude/admin-api>, accessed 2026-09-19) —
but I could not confirm it for the claude.ai internal endpoints. Cache per tab for 10
minutes; fall back to the account menu in the DOM; if that fails too, capture nothing.

**(b) Composer and send — ASSUMPTION** (community-sourced:
<https://github.com/dpikalov/chrome-claude-ai>,
<https://gist.github.com/sshh12/e352c053627ccbe1636781f73d6d715b>, accessed 2026-09-19).
ProseMirror contenteditable. Probe in order: `div.ProseMirror[contenteditable="true"]`,
`div[contenteditable="true"][data-testid]`, `fieldset div[contenteditable="true"]`,
`div[contenteditable="true"]`. Read `el.innerText`, **not** `textContent` — ProseMirror
uses one `<p>` per line and `textContent` loses the newlines. Send control: match
case-insensitively on `button[aria-label*="send" i]` (historically "Send message"), plus
`button[type="submit"]` inside the composer form. Trigger on capture-phase `keydown`
Enter-without-Shift and on `click`/`pointerdown` of the send button — read the text
**before** the app clears it, and never `preventDefault`.

**(c) Conversation id and title.** URL `https://claude.ai/chat/<uuid>`; a new chat is
`/new` until the app rewrites the URL, so re-read on `popstate` and on a patched
`history.pushState`. ASSUMPTION: title is `document.title` minus a trailing ` - Claude`
suffix; a brand-new conversation has none at submit time, so send `title: null`.

### 4.2 claude.ai/code (`claude_code_web`)

**(a) Account email.** Same origin as claude.ai — same cookies, same endpoints.

**(b) URL structure — verified.** From
<https://code.claude.com/docs/en/claude-code-on-the-web> (accessed 2026-09-19), the CLI
prints `View: https://claude.ai/code/session_01DiUkqY2kzbUbDmW1w96rfi?from=cli&m=0`, and
the same page says *"pass the bare ID, such as `session_...` or `cse_...`, or the
session's `claude.ai/code/<id>` URL"*. So `/code` is the session list, `/code/<id>` is
one session, and **the id is not a bare UUID** — it is `session_<base62>` or `cse_<…>`.
Keep the prefix in `session_id`, and treat the bare `/code` list page as in-scope.

**(c) Composer — ASSUMPTION:** same ProseMirror component as the chat UI. One documented
behaviour affects dedupe: *"If you send a message while Claude is working, the message
queues… To take a queued message back, click the ✕ on it. The text returns to the
message box."* A cancelled-then-edited message is captured twice with different text.
Accept that; do not try to reconcile it.

**(d) Title — ASSUMPTION:** the session title next to the dropdown, `document.title` fallback.

### 4.3 chatgpt.com chat UI (`chatgpt_web`)

**(a) Account email.** `GET https://chatgpt.com/api/auth/session` with
`credentials: "include"` returns a NextAuth session object containing `user.email`. The
endpoint is discussed by name in <https://github.com/banddude/webgpt2mcp/issues/4>
(accessed 2026-09-19). **ASSUMPTION on current stability:** that same issue reports a UI
iteration where the response carried only `WARNING_BANNER`. Treat a missing `user.email`
as "unknown account" and capture nothing; do not scrape the avatar menu, which truncates.

**(b) Composer and send — ASSUMPTION** (same issue plus
<https://github.com/zabcore/ai-leak-guard/pull/72>, accessed 2026-09-19).
`#prompt-textarea` is still the composer id but is now a ProseMirror contenteditable div,
not a `<textarea>`. Probe: `#prompt-textarea`,
`div[contenteditable="true"][role="textbox"]`, `textarea[data-testid="prompt-textarea"]`,
`textarea[name="prompt-textarea"]`, `[data-testid*="composer"] [contenteditable="true"]`.
Read `innerText` for contenteditable, `value` for a textarea. Send:
`button[data-testid="send-button"]`, fallbacks `#composer-submit-button`,
`button[aria-label="Send prompt"]`, `button[aria-label="Send message"]`.

**(c) Conversation id and title.** URL `https://chatgpt.com/c/<uuid>`; a fresh chat is
`https://chatgpt.com/` until the first reply, so `conversation_id` is legitimately `null`
on the first prompt. Title: `document.title` minus a trailing ` - ChatGPT` (ASSUMPTION),
usually still the default at submit time.

### 4.4 chatgpt.com/codex (`codex_cloud`)

**(a) Account email.** Same origin as chatgpt.com — `/api/auth/session` again.

**(b) URL structure.** Verified only that the product lives at
<https://chatgpt.com/codex/> (linked from <https://openai.com/index/introducing-codex/>,
accessed 2026-09-19). **ASSUMPTION:** tasks are `https://chatgpt.com/codex/tasks/<task_id>`;
no primary source confirms the path segment. Select the source on the `/codex` prefix
alone (as `ARCHITECTURE.md` specifies) and derive `conversation_id` as the last non-empty
path segment when there is more than one segment after `/codex`, else `null`. That
degrades safely if the route changes.

**(c) Composer — ASSUMPTION:** a separate component from the chat composer. The docs
describe composer features (`@` task mentions; slash commands `/plan`, `/review`,
`/status` — <https://learn.chatgpt.com/docs/developer-commands>, accessed 2026-09-19) but
name no DOM ids. Reuse the 4.3 probe chain scoped to the visible form; this target needs
re-verification against the live DOM.

### 4.5 Anti-automation and CSP for content scripts

* **Cloudflare.** A `fetch(…, { credentials: "include" })` from a **content script**
  (page origin) carries `cf_clearance` and the session cookies and is indistinguishable
  from an app request. The same request from the **service worker** is cross-origin from
  `chrome-extension://…` and is far likelier to be challenged or CORS-blocked.
  **Do the account lookup in the content script.**
* **CSP.** MV3 content scripts run in an isolated world, so the page's
  `Content-Security-Policy` does not restrict their `fetch` (`connect-src` binds the
  page's own context). What CSP *does* block is injecting an inline `<script>` into the
  main world — so read the DOM rather than ProseMirror's internal state.
* **`host_permissions`.** Needed for the POST to `http://127.0.0.1:47821`. Same-origin
  `fetch` from a content script needs none, but `https://claude.ai/*` and
  `https://chatgpt.com/*` are still required to register the content scripts.
* **No response interception.** Do not sniff the app's own `POST /completion` or
  `/conversation` with `chrome.webRequest`/DNR: fragile, over-captures, and the MV3
  blocking APIs are unavailable to non-enterprise extensions. DOM capture on submit is
  the right design.
* **Rate/timing.** Do not poll the account endpoints per keystroke. Cache per tab for
  10 minutes, refresh on navigation. Repeated hits are what triggers a challenge.

---

## 5. Implications for our adapters

### `adapters/claude_code.py`

Read `prompt` (required), `session_id`, `cwd`, `transcript_path`, `permission_mode`,
`hook_event_name`. Put `prompt_id` (v2.1.196+, the best correlation key) and `agent_type`
in `metadata`. Never store `transcript_path` raw — it embeds the home-dir username; run
it through `scrub_path`. Raise `AdapterError` unless
`hook_event_name == "UserPromptSubmit"` so a mis-registered hook can't poison the DB.

Edge cases:

* **Empty prompt** — a whitespace-only submit still fires the hook. Drop on `prompt.strip() == ""`.
* **Slash commands** — `prompt` is the raw typed text, including `/compact`,
  `/model sonnet` and custom commands. They are real prompts; keep them, but expect them
  in the stats.
* **Images/pastes** — the hook gives a plain string, so pasted images never appear. Large
  pastes do; cap the size before scrubbing.
* **Subagents** — `UserPromptSubmit` is a main-thread event; if `agent_id` ever appears it
  is still one user submission.

### `adapters/codex.py`

Must accept **two** shapes, distinguished by `hook_event_name` / `type`:

* **(a) hooks payload** (`hook_event_name == "UserPromptSubmit"`): `prompt`, `session_id`,
  `cwd`, `model`, `permission_mode`, `turn_id`, and `transcript_path` — which is
  **nullable** in the schema, so guard it. `turn_id` + `model` go in `metadata`;
  `turn_id` is what pairs a prompt with its later turn-complete for timing.
* **(b) notify payload** (`type == "agent-turn-complete"`): `input-messages[-1]` is the
  prompt, `thread-id` is `session_id`, `cwd` is `cwd`, `turn-id` and `client` go in
  `metadata`. Note the **kebab-case keys** — `turn-id`, `input-messages`,
  `last-assistant-message`, `thread-id`.
* **In notify mode the payload is argv, not stdin.** `apc capture codex` must check for a
  trailing argument that parses as a JSON object and only fall back to stdin otherwise.

Edge cases:

* **Multi-part input** — `input-messages` is a `Vec<String>`; a turn can carry several
  queued user messages. Take the last for `prompt` but record the list length in
  `metadata` so we can spot the loss.
* **Double capture** — hook + notify both configured produces two records per turn that do
  **not** dedupe, because the hook gives `session_id` while notify gives `thread-id` and
  they arrive seconds apart. `apc install codex` must install one or the other, and
  `apc doctor` should warn when both are present.
* **Empty prompt** — `input-messages` can be empty on an auto-continued turn. Drop blanks.
* **Deprecation** — `notify` is marked for removal in the source. Default the installer to
  hooks; keep notify behind `--legacy`.

### `adapters/opencode.py`

The plugin does the flattening; the adapter contract stays as `ARCHITECTURE.md` defines
it (`{session_id, cwd, project, model, prompt, ts}`). What the **plugin** must do:

* `prompt = parts.filter(p => p.type === "text" && !p.synthetic && !p.ignored).map(p => p.text).join("\n")`.
  **Dropping `synthetic` parts is mandatory** (see §3.3): an `@agent` mention injects an
  instruction sentence the user never wrote.
* `session_id = input.sessionID`;
  `model = input.model ? `${providerID}/${modelID}` : null` — both `agent` and `model` are
  **optional** in the hook input.
* `cwd = directory` (not `worktree`; they differ inside a git worktree);
  `project = project.id ?? basename(worktree)`.
* `ts` from `output.message.time.created` (epoch **milliseconds**) -> ISO, falling back to
  `Date.now()`. Record `messageID` in `metadata` for dedupe.

Edge cases:

* **File/image attachments** arrive as `FilePart` (`mime`, `url`, no text). Skip them for
  the prompt text but count them into `metadata.attachments`, so an image-only prompt is
  not silently dropped as empty.
* **Empty prompt** — after filtering synthetics, `prompt` can be `""` (image-only turn, or
  a bare `@agent` mention). Skip the spawn entirely.
* **Slash commands** are expanded by `command.execute.before` into `parts` before
  `chat.message` sees them, so `/foo` is captured as its *expanded* text, not as `/foo`.
  That is a behaviour difference from the two other CLIs; note it in the README.
* **Latency** — `chat.message` is awaited by the session loop; a real bug report exists of
  a plugin adding 1.5–2.5 s per send by awaiting work in this hook
  (<https://github.com/volcengine/OpenViking/issues/5148>, accessed 2026-09-19). Spawn
  detached, never await, wrap everything in `try/catch`.

---

## 6. Turn-end events (for time tracking)

### 6.a Claude Code: `Stop`, `SessionStart`, `SessionEnd`

Source: <https://code.claude.com/docs/en/hooks.md>, accessed 2026-09-19.

**`Stop`.** Verbatim: *"Runs when the main Claude Code agent has finished responding.
Does not run if the stoppage occurred due to a user interrupt. API errors fire
StopFailure instead."* So: **once per completed assistant turn**, but **not** a
guaranteed turn-end signal — an interrupted turn fires nothing and an API error fires
`StopFailure`. Pair `UserPromptSubmit` with `Stop` and treat an unmatched
`UserPromptSubmit` as an aborted turn.

Input: common fields plus `stop_hook_active`, `last_assistant_message`,
`background_tasks`, `session_crons`. Verbatim example:

```json
{
  "session_id": "abc123",
  "transcript_path": "~/.claude/projects/.../00893aaf-19fa-41d2-8238-13269b9b3ca0.jsonl",
  "cwd": "/Users/...",
  "permission_mode": "default",
  "hook_event_name": "Stop",
  "stop_hook_active": true,
  "last_assistant_message": "I've completed the refactoring. Here's a summary...",
  "background_tasks": [
    { "id": "task-001", "type": "shell", "status": "running",
      "description": "tail logs", "command": "tail -f /var/log/syslog" }
  ],
  "session_crons": [
    { "id": "cron-001", "schedule": "0 9 * * 1-5", "recurring": true, "prompt": "check the build" }
  ]
}
```

`stop_hook_active` is *"`true` when Claude Code is already continuing as a result of a
stop hook"*. **Ignore any `Stop` with `stop_hook_active: true`** or you log several turn
ends for one turn. `background_tasks` / `session_crons` are both present when the task
registry is reachable and empty when nothing is in flight — a non-empty `background_tasks`
means "paused waiting on background work", not "done".

`Stop` has **no matcher support** (same table row as `UserPromptSubmit`). Exit 2 on `Stop`
*"Prevents Claude from stopping, continues the conversation"* (capped at 8 consecutive
blocks), so the exit-0/empty-stdout rule matters even more here.

**Registering one command for both events — yes.** The `hooks` object is keyed by event,
each key holding its own matcher-group array, and the same handler shape can appear under
any number of keys:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "apc capture claude-code", "timeout": 10 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "apc capture claude-code-stop", "timeout": 10 } ] }
    ]
  }
}
```

Use **distinct subcommands** rather than one command for both: the adapter dispatches on
`hook_event_name` anyway, and distinct commands make `apc doctor` and uninstall unambiguous.

**`SessionStart`** exists — *"when Claude Code starts a new session or resumes an existing
session"*. Adds `source` (`"startup" | "resume" | "clear" | "compact" | "fork"`), and
optionally `model`, `agent_type`, `session_title`; on `resume`/`fork` with a prior response
it also carries `seconds_since_last_response`, `context_tokens`,
`prompt_cache_likely_expired`, `estimated_cache_write_usd` (v2.1.251+). It **does** support
matchers (on `source`), accepts only `type: "command"` and `type: "mcp_tool"`, and exit 2
does not block.

**`SessionEnd`** exists. Verbatim example:

```json
{
  "session_id": "abc123",
  "transcript_path": "/Users/.../.claude/projects/.../00893aaf-19fa-41d2-8238-13269b9b3ca0.jsonl",
  "cwd": "/Users/...",
  "hook_event_name": "SessionEnd",
  "reason": "other"
}
```

`reason` is `"clear" | "resume" | "logout" | "prompt_input_exit" | "other"`.
**Critical: `SessionEnd` hooks share a 1.5-second budget by default** (raised to match the
highest per-hook `timeout` in your settings, up to 60 s, or overridden with
`CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`). No decision control; JSON output discarded. If
we use it for session-duration bookkeeping, set an explicit `"timeout": 5` and keep the
work to one SQLite write.

### 6.b Codex: when `notify` fires, and turn-start events

* **`notify` fires exactly at agent-turn-complete, and nowhere else.** Verified in
  `legacy_notify.rs`: `UserNotification` has a single variant, `AgentTurnComplete`, and
  `legacy_notify_json` matches only `HookEvent::AfterAgent { event }`. There is no
  turn-start, session-start or tool-level notify.
* **The payload does include cwd and a thread id.** Same file:
  `cwd: payload.cwd.display().to_string()`, `thread_id: event.thread_id.to_string()`,
  `turn_id: event.turn_id.clone()`. Serialized kebab-case as `"cwd"`, `"thread-id"`,
  `"turn-id"` (verbatim test JSON in §2.5). `client` (e.g. `"codex-tui"`) is optional.
* **Turn-start hook event: yes, `UserPromptSubmit`.** It is turn-scoped and carries
  `turn_id`, whose schema description is *"Codex extension: expose the active turn id to
  internal turn-scoped hooks."* So the clean Codex timing pair is
  `UserPromptSubmit.turn_id` -> the `Stop` hook's turn, or -> `notify`'s `turn-id`. Both
  `UserPromptSubmit` and `Stop` are in `HookEventsToml`, so a hooks-only setup covers both
  ends and `notify` is unnecessary.
* `SessionStart` and `SessionEnd` also exist in `HookEventsToml`. `SessionEnd` and
  `Interrupt` get a shorter clamped default in `discovery.rs::normalize_command_hook`;
  the constants live in `events/session_end.rs` (`SESSION_END_DEFAULT_TIMEOUT_SEC`,
  `SESSION_END_MAX_TIMEOUT_SEC`), which I did not read. **ASSUMPTION:** they are a few
  seconds, so set an explicit `timeout` if we register a `SessionEnd` hook.

### 6.c OpenCode: turn-finish and session-created events

Both arrive on the `event` plugin hook,
`event?: (input: { event: Event }) => Promise<void>`. Shapes verbatim from
`packages/sdk/js/src/gen/types.gen.ts` (accessed 2026-09-19):

```ts
export type EventSessionIdle    = { type: "session.idle",    properties: { sessionID: string } }
export type EventSessionCreated = { type: "session.created", properties: { info: Session } }
export type EventSessionUpdated = { type: "session.updated", properties: { info: Session } }
```

**Turn finishing: `session.idle`.** Confirmed at the emit site,
`packages/opencode/src/session/status.ts`:

```ts
const set = Effect.fn("SessionStatus.set")(function* (sessionID, status) {
  yield* events.publish(Event.Status, { sessionID, status })
  if (status.type === "idle") {
    yield* events.publish(Event.Idle, { sessionID })
    data.delete(sessionID); return
  }
  data.set(sessionID, status)
})
```

So `session.idle` is published whenever the session's status transitions to `idle` — the
turn-finished signal. **`sessionID` is at `event.properties.sessionID`**, a bare string.
A richer `{ type: "session.status", properties: { sessionID, status } }` event carries the
intermediate states if we ever need them.

**Session created: `session.created`**, with the whole `Session` at
`event.properties.info` — so the id is `event.properties.info.id`, **not**
`event.properties.sessionID`. Same for `session.updated` / `session.deleted`. That
asymmetry is the easiest thing to get wrong; write the extractor as
`e.properties.sessionID ?? e.properties.info?.id ?? e.properties.part?.sessionID`.

Caveat: `session.idle` can fire for sub-sessions and for sessions we never saw a
`chat.message` for. Only record a turn end when we hold an open turn for that `sessionID`.

---

## Source list

All accessed **2026-09-19**.

* <https://code.claude.com/docs/en/hooks> / <https://code.claude.com/docs/en/hooks.md>
* <https://code.claude.com/docs/en/settings>
* <https://code.claude.com/docs/en/claude-code-on-the-web>
* <https://raw.githubusercontent.com/openai/codex/main/docs/config.md>
* <https://github.com/openai/codex/tree/main/codex-rs/hooks>
* `codex-rs/hooks/schema/generated/user-prompt-submit.command.{input,output}.schema.json`
* `codex-rs/hooks/src/legacy_notify.rs`, `src/engine/{discovery,command_runner,output_parser,dispatcher,mod}.rs`, `src/schema.rs`
* `codex-rs/config/src/hook_config.rs`, `codex-rs/features/src/lib.rs`
* `codex-rs/rollout/src/{lib,recorder}.rs`, `codex-rs/history/src/{lib,rollout_payload}.rs`, `codex-rs/protocol/src/models.rs`
* <https://opencode.ai/docs/plugins/>
* `anomalyco/opencode@dev`: `packages/plugin/src/{index,shell}.ts`,
  `packages/sdk/js/src/gen/types.gen.ts`,
  `packages/opencode/src/config/{config,plugin,paths}.ts`,
  `packages/opencode/src/session/{prompt,status}.ts`,
  `packages/core/src/global.ts`
* <https://platform.claude.com/docs/en/manage-claude/admin-api> (for the `email_address` field spelling only)
* <https://github.com/banddude/webgpt2mcp/issues/4>, <https://github.com/zabcore/ai-leak-guard/pull/72>,
  <https://github.com/dpikalov/chrome-claude-ai>, <https://github.com/volcengine/OpenViking/issues/5148>
  (community sources; treated as ASSUMPTION-grade)
