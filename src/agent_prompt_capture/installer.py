"""``apc install`` / ``apc uninstall``: write the hook configuration for each agent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .config import get_logger

__all__ = [
    "install",
    "ConfigFormatError",
    "uninstall",
    "TARGETS",
    "HOOK_COMMAND",
    "CODEX_HOOK_COMMAND",
    "CODEX_HOOK_EVENTS",
    "CLAUDE_HOOK_EVENTS",
    "HOOK_TIMEOUT",
    "OPENCODE_PLUGIN_NAME",
    "OPENCODE_PLUGIN_SOURCE",
    "claude_settings_path",
    "codex_config_path",
    "codex_event_label",
    "codex_expected_trust",
    "codex_hook_state_key",
    "codex_hook_trust_hash",
    "codex_hooks_path",
    "opencode_plugin_path",
]

_log = get_logger()

TARGETS = ("claude-code", "codex", "opencode")

HOOK_COMMAND = "apc capture claude-code"
CLAUDE_HOOK_EVENTS = ("UserPromptSubmit", "Stop")
HOOK_TIMEOUT = 10

CODEX_HOOK_COMMAND = "apc capture codex"
CODEX_HOOK_EVENTS = ("UserPromptSubmit", "Stop")
#: What Codex normalizes a handler with no ``timeout`` to, and therefore hashes:
#: ``timeout_sec.unwrap_or(600)`` in ``discovery.rs::normalize_command_hook``.
#: See docs/research/hook-specs.md section 2.
CODEX_DEFAULT_TIMEOUT = 600
CODEX_HOOKS_DESCRIPTION = "agent-prompt-capture"
CODEX_NOTIFY = ["apc", "capture", "codex"]

OPENCODE_PLUGIN_NAME = "agent-prompt-capture.js"

#: Written when ``opencode-plugin/agent-prompt-capture.js`` is not in the source tree.
OPENCODE_PLUGIN_SOURCE = r"""// agent-prompt-capture: OpenCode plugin.
//
// TODO: this is the fallback copy emitted by `apc install opencode` when the packaged
// opencode-plugin/agent-prompt-capture.js is not on disk. Keep the two in sync.
//
// Pipes every submitted user message to `apc capture opencode` as JSON on stdin, and
// every `session.idle` transition as a turn-end event. The child is spawned detached and
// never awaited from `chat.message`: that hook is awaited by the session loop, so any
// work done there is latency the user feels. Everything is wrapped in try/catch —
// capture is best effort and must never slow down or break OpenCode.
//
// Per session the children are *ordered*, though: `session.idle` can arrive while the
// prompt's child is still starting up, and a turn_end that runs first finds no row to
// stamp and is lost. Each session gets a promise chain, so its turn_end child is only
// spawned once the prompt child has exited.
//
// Install with `apc install opencode` (copies this file to
// ~/.config/opencode/plugin/agent-prompt-capture.js).

const COMMAND = "apc";
const ARGS = ["capture", "opencode"];
// A capture writes one row; if the child has not exited by then it never will, and the
// session's next event must not wait on it forever.
const SPAWN_TIMEOUT_MS = 10000;

/**
 * Spawn `apc capture opencode`, feed it `json`, and resolve when the child has exited
 * (or could not be started at all). The promise never rejects.
 */
function spawnDetached(json) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;

    function done() {
      if (settled) return;
      settled = true;
      if (timer !== null) clearTimeout(timer);
      resolve();
    }

    // The child is unref'd below (a hung `apc capture` must never keep OpenCode
    // alive), so this timer is what holds the event loop open instead: a caller that
    // awaits this promise (the per-session chain, or a short-lived host such as the
    // e2e driver) must not drain before the child exits. Events on an unref'd child
    // still fire while the timer runs, and done() clears the timer, so a healthy
    // child releases the host as soon as it exits and a hung one after
    // SPAWN_TIMEOUT_MS. The timer itself is deliberately NOT unref'd.
    timer = setTimeout(done, SPAWN_TIMEOUT_MS);

    // Bun first (OpenCode's server runs on Bun), node:child_process otherwise.
    let spawned = null;
    try {
      if (globalThis.Bun && typeof globalThis.Bun.spawn === "function") {
        // Bun.spawn throws synchronously when `apc` is not on PATH; that is the only
        // reason to fall through to node. A failure AFTER the spawn must not retry,
        // or one prompt is captured twice.
        spawned = globalThis.Bun.spawn([COMMAND, ...ARGS], {
          stdin: "pipe",
          stdout: "ignore",
          stderr: "ignore",
        });
      }
    } catch (_) {
      spawned = null; // fall through to node
    }

    if (spawned) {
      try {
        // Detach from the host's event loop: a hung child must not keep it alive.
        if (typeof spawned.unref === "function") spawned.unref();
      } catch (_) {
        // older Bun without unref(); the 10 s timer is still the backstop
      }
      try {
        spawned.stdin.write(json);
        spawned.stdin.end();
      } catch (_) {
        // the child died before it read us; nothing to salvage, never retry
        done();
        return;
      }
      if (spawned.exited && typeof spawned.exited.then === "function") {
        spawned.exited.then(done, done);
      } else {
        done();
      }
      return;
    }

    import("node:child_process")
      .then(({ spawn }) => {
        const child = spawn(COMMAND, ARGS, {
          stdio: ["pipe", "ignore", "ignore"],
          detached: true,
        });
        // Detach from the host's event loop: a hung child must not keep it alive.
        // The SPAWN_TIMEOUT_MS timer above, not the child, is what keeps a
        // short-lived host running long enough to see "close".
        child.unref();
        // A missing `apc` surfaces as an async "error" event, never a throw.
        child.on("error", () => done());
        child.on("close", () => done());
        if (child.stdin) {
          child.stdin.on("error", () => {});
          // end() both writes and closes the pipe; "close" fires once the child exits.
          child.stdin.end(json);
        }
      })
      .catch(() => done());
  });
}

function send(payload) {
  try {
    return spawnDetached(JSON.stringify(payload));
  } catch (_) {
    return Promise.resolve(); // never propagate
  }
}

function basename(p) {
  if (!p) return null;
  const parts = String(p).replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
}

function promptText(parts) {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter((p) => p && p.type === "text" && !p.synthetic && !p.ignored)
    .map((p) => p.text || "")
    .join("\n");
}

function attachmentCount(parts) {
  if (!Array.isArray(parts)) return 0;
  return parts.filter((p) => p && p.type === "file").length;
}

function sessionIdOf(event) {
  // session.idle carries `properties.sessionID`, but session.created/updated/deleted
  // put the id at `properties.info.id` instead. Getting this wrong is the easiest
  // mistake in the whole plugin (hook-specs.md 6.c).
  const props = (event && event.properties) || {};
  if (props.sessionID) return props.sessionID;
  if (props.info && props.info.id) return props.info.id;
  if (props.part && props.part.sessionID) return props.part.sessionID;
  return null;
}

// session.idle also fires for sub-sessions and for sessions that were already running
// when this plugin loaded. We have no open turn for those, so emitting a turn end
// would spawn a process for nothing. Remember the sessions we actually sent a prompt
// for, bounded so a long-lived server cannot grow this without limit.
const OPEN_SESSIONS = new Set();
const MAX_OPEN_SESSIONS = 512;

function rememberSession(sessionID) {
  if (!sessionID) return;
  if (OPEN_SESSIONS.size >= MAX_OPEN_SESSIONS) {
    // Sets iterate in insertion order, so this drops the oldest.
    OPEN_SESSIONS.delete(OPEN_SESSIONS.values().next().value);
  }
  OPEN_SESSIONS.add(sessionID);
}

// One promise chain per session: the prompt's child must have exited before the
// turn_end child starts, or `apc capture` finds no open turn to stamp. Different
// sessions never wait on each other.
const SESSION_CHAINS = new Map();

function enqueueForSession(sessionID, payload) {
  const key = sessionID || "";
  const previous = SESSION_CHAINS.get(key);
  const next = (previous || Promise.resolve()).then(
    () => send(payload),
    () => send(payload),
  );
  if (!SESSION_CHAINS.has(key) && SESSION_CHAINS.size >= MAX_OPEN_SESSIONS) {
    // Maps iterate in insertion order, so this drops the oldest.
    SESSION_CHAINS.delete(SESSION_CHAINS.keys().next().value);
  }
  SESSION_CHAINS.set(key, next);
  const forget = () => {
    if (SESSION_CHAINS.get(key) === next) SESSION_CHAINS.delete(key);
  };
  next.then(forget, forget);
  return next;
}

export const AgentPromptCapture = async ({ project, client, $, directory, worktree }) => ({
  "chat.message": async (input, output) => {
    try {
      const parts = (output && output.parts) || [];
      const prompt = promptText(parts);
      if (!prompt || !prompt.trim()) return; // image-only turn or a bare @agent mention

      const message = (output && output.message) || {};
      const created = message.time && message.time.created;
      const model = input && input.model
        ? `${input.model.providerID}/${input.model.modelID}`
        : null;

      const sessionID = (input && input.sessionID) || message.sessionID || null;
      rememberSession(sessionID);

      // Deliberately not awaited: the chain only orders this session's children.
      enqueueForSession(sessionID, {
        session_id: sessionID,
        cwd: directory || null,
        project: (project && project.id) || basename(worktree) || null,
        model,
        agent: (input && input.agent) || message.agent || null,
        prompt,
        ts: new Date(typeof created === "number" ? created : Date.now()).toISOString(),
        messageID: message.id || (input && input.messageID) || null,
        attachments: attachmentCount(parts),
      });
    } catch (_) {
      // capture is best effort
    }
  },

  event: async ({ event }) => {
    try {
      if (!event || event.type !== "session.idle") return;
      const sessionID = sessionIdOf(event);
      if (!sessionID) return;
      if (!OPEN_SESSIONS.has(sessionID)) return; // no open turn of ours: nothing to end
      OPEN_SESSIONS.delete(sessionID);
      // `ts` is stamped now, not when the child finally runs, so a queued turn end
      // still records when the session actually went idle.
      await enqueueForSession(sessionID, {
        event: "turn_end",
        session_id: sessionID,
        ts: new Date().toISOString(),
      });
    } catch (_) {
      // capture is best effort
    }
  },
});

export default AgentPromptCapture;
"""


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home()).expanduser()


def claude_settings_path() -> Path:
    """``$CLAUDE_CONFIG_DIR/settings.json`` or ``~/.claude/settings.json``."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else _home() / ".claude"
    return base / "settings.json"


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else _home() / ".codex"


def codex_config_path() -> Path:
    return _codex_home() / "config.toml"


def codex_hooks_path() -> Path:
    """``~/.codex/hooks.json`` — Codex's native hooks file, next to ``config.toml``."""
    return _codex_home() / "hooks.json"


def opencode_plugin_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured).expanduser() if configured else _home() / ".config"
    return base / "opencode" / "plugin" / OPENCODE_PLUGIN_NAME


def _packaged_plugin() -> Path:
    """``opencode-plugin/agent-prompt-capture.js`` in the source checkout, if present."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "opencode-plugin" / OPENCODE_PLUGIN_NAME
        if candidate.is_file():
            return candidate
    return here.parent / "_missing" / OPENCODE_PLUGIN_NAME


def _write_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp file and one rename, so a reader never sees a half file.

    The temp file is created 0600 and only widened to the mode of the file it replaces
    (0644 for a new one): a config file must never be briefly world-readable while it
    is being written, and an existing file's permissions must survive the rename.

    ``mkstemp`` picks the name (and creates it O_EXCL): a fixed ``<name>.apc-tmp`` that
    somebody has pre-created as a symlink would be followed, and we would write the
    config - and chmod it - straight through to whatever the link points at.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    descriptor, name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".apc-tmp"
    )
    tmp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)  # mkstemp already made it 0600
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# claude code
# --------------------------------------------------------------------------


class ConfigFormatError(ValueError):
    """An existing config file we refuse to touch because we cannot parse it."""


def _load_json(path: Path) -> dict[str, Any]:
    """Parse an existing config file, or fail loudly enough to be actionable.

    We never repair or replace a file we could not read: silently clobbering a user's
    ``settings.json`` because of a trailing comma is far worse than not installing.
    A UTF-8 BOM (what Notepad and some editors write) is tolerated; everything else
    that is not a JSON object is an error naming the file and the reason.
    """
    if not path.exists():
        return {}
    try:
        # utf-8-sig strips a leading BOM if present and behaves like utf-8 otherwise.
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("%s could not be read (%s); refusing to overwrite it", path, exc)
        raise ConfigFormatError(
            f"{path} could not be read as UTF-8 text ({exc}). "
            "Fix or move the file, then re-run the install."
        ) from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("%s is not valid JSON; refusing to overwrite it", path)
        raise ConfigFormatError(
            f"{path} is not valid JSON ({exc.msg} at line {exc.lineno} column {exc.colno}). "
            "Refusing to overwrite it - JSON allows no trailing commas or comments. "
            "Fix the file (or move it aside), then re-run the install."
        ) from exc
    if not isinstance(data, dict):
        raise ConfigFormatError(
            f"{path} holds a JSON {type(data).__name__}, not an object. "
            "Refusing to overwrite it; fix the file, then re-run the install."
        )
    return data


def _has_command(matchers: list[Any], command: str) -> bool:
    """Is ``command`` already registered in this event's matcher groups?"""
    for matcher in matchers:
        if not isinstance(matcher, dict):
            continue
        for hook in matcher.get("hooks") or []:
            if isinstance(hook, dict) and command in str(hook.get("command", "")):
                return True
    return False


def _strip_command(matchers: list[Any], command: str) -> list[Any]:
    cleaned: list[Any] = []
    for matcher in matchers:
        if not isinstance(matcher, dict):
            cleaned.append(matcher)
            continue
        hooks = [
            h
            for h in (matcher.get("hooks") or [])
            if not (isinstance(h, dict) and command in str(h.get("command", "")))
        ]
        if hooks:
            cleaned.append({**matcher, "hooks": hooks})
        elif matcher.get("hooks") is None:
            cleaned.append(matcher)
    return cleaned


def _install_claude_code(*, dry_run: bool) -> str:
    path = claude_settings_path()
    settings = _load_json(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    added: list[str] = []
    for event in CLAUDE_HOOK_EVENTS:
        matchers = hooks.get(event)
        if not isinstance(matchers, list):
            matchers = []
        if _has_command(matchers, HOOK_COMMAND):
            continue
        matchers = [
            *matchers,
            {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT}]},
        ]
        hooks[event] = matchers
        added.append(event)

    settings["hooks"] = hooks
    rendered = json.dumps(settings, indent=2) + "\n"

    if dry_run:
        return f"[dry-run] would write {path}:\n{rendered}"
    _write_atomic(path, rendered)
    if not added:
        return f"claude-code hooks already installed in {path} (no changes)"
    return f"installed claude-code hooks ({', '.join(added)}) in {path}"


def _uninstall_claude_code() -> str:
    path = claude_settings_path()
    if not path.exists():
        return f"nothing to remove: {path} does not exist"
    settings = _load_json(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return f"nothing to remove in {path}"
    removed: list[str] = []
    for event in CLAUDE_HOOK_EVENTS:
        matchers = hooks.get(event)
        if not isinstance(matchers, list):
            continue
        cleaned = _strip_command(matchers, HOOK_COMMAND)
        if cleaned != matchers:
            removed.append(event)
        if cleaned:
            hooks[event] = cleaned
        else:
            hooks.pop(event, None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    _write_atomic(path, json.dumps(settings, indent=2) + "\n")
    if not removed:
        return f"no apc hooks found in {path}"
    return f"removed claude-code hooks ({', '.join(removed)}) from {path}"


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------

_NOTIFY_LINE = re.compile(r"^\s*notify\s*=.*$", re.MULTILINE)
_SECTION = re.compile(r"^\s*\[", re.MULTILINE)

#: Only *our* notify line: the complete argv, in order, with nothing after it but
#: whitespace or a comment. ``notify = ["apc", "wrap", "--mine"]`` merely starts with
#: "apc" and belongs to whoever wrote it - we neither overwrite nor remove it.
_APC_NOTIFY = re.compile(
    r"^[ \t]*notify[ \t]*=[ \t]*\[[ \t]*"
    + r"[ \t]*,[ \t]*".join("[\"']" + re.escape(arg) + "[\"']" for arg in CODEX_NOTIFY)
    + r"[ \t]*,?[ \t]*\][ \t]*(?:#[^\n]*)?$",
    re.MULTILINE,
)

CODEX_NOTIFY_LINE = 'notify = ["apc", "capture", "codex"]'


def _codex_notify_installed(text: str) -> bool:
    """Is apc's own ``notify`` line (and nobody else's) in this top-level region?"""
    return _APC_NOTIFY.search(text) is not None


def _top_level_region(text: str) -> tuple[int, int]:
    """The byte range of the file before the first ``[section]`` header."""
    match = _SECTION.search(text)
    return (0, match.start() if match else len(text))


def _normalize_codex_handlers(matchers: list[Any]) -> bool:
    """Give every apc handler in ``matchers`` the ``type``/``timeout`` we hash.

    Returns whether anything changed. Only handlers whose command is exactly ours are
    touched: somebody's ``apc capture codex --their-flag`` wrapper is their business.
    """
    changed = False
    for matcher in matchers:
        if not isinstance(matcher, dict):
            continue
        for handler in matcher.get("hooks") or []:
            if not isinstance(handler, dict):
                continue
            if str(handler.get("command", "")) != CODEX_HOOK_COMMAND:
                continue
            if handler.get("timeout") != HOOK_TIMEOUT:
                handler["timeout"] = HOOK_TIMEOUT
                changed = True
            if handler.get("type") != "command":
                handler["type"] = "command"
                changed = True
    return changed


def _install_codex_hooks(*, dry_run: bool) -> str:
    """Merge our two hooks into Codex's native ``hooks.json``."""
    path = codex_hooks_path()
    document = _load_json(path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    added: list[str] = []
    normalized = False
    for event in CODEX_HOOK_EVENTS:
        matchers = hooks.get(event)
        if not isinstance(matchers, list):
            matchers = []
        if _has_command(matchers, CODEX_HOOK_COMMAND):
            # An entry already there (a hand-written one, say) may be missing the
            # timeout we hash for the trust entry; Codex would normalize it to its own
            # 600 s default and the hashes would never match, so the hook would never
            # run. Make the file say what we are about to trust.
            if _normalize_codex_handlers(matchers):
                hooks[event] = matchers
                normalized = True
            continue
        matchers = [
            *matchers,
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": CODEX_HOOK_COMMAND,
                        "timeout": HOOK_TIMEOUT,
                    }
                ]
            },
        ]
        hooks[event] = matchers
        added.append(event)

    document.setdefault("description", CODEX_HOOKS_DESCRIPTION)
    document["hooks"] = hooks
    rendered = json.dumps(document, indent=2) + "\n"

    if dry_run:
        return f"[dry-run] would write {path}:\n{rendered}"
    _write_atomic(path, rendered)
    if not added:
        if normalized:
            return f"normalized the codex hook timeouts in {path}"
        return f"codex hooks already installed in {path} (no changes)"
    return f"installed codex hooks ({', '.join(added)}) in {path}"


# -- hook trust -------------------------------------------------------------
#
# Codex DISCOVERS hooks.json but refuses to RUN anything in it until the hook's
# identity hash has been trusted in the user layer's config.toml, under
# ``[hooks.state]``, keyed by
# ``"<absolute path of hooks.json>:<event_label>:<group_index>:<handler_index>"``.
# Verified live against Codex CLI 0.155.1 on 2026-09-20; see
# docs/research/hook-specs.md section 2.


def codex_event_label(event: str) -> str:
    """``UserPromptSubmit`` -> ``user_prompt_submit``: the label Codex keys trust on."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def codex_hook_trust_hash(event_label: str, command: str, timeout: int) -> str:
    """``"sha256:" + sha256(canonical_json)`` over the hook's normalized identity.

    Canonical JSON is compact and recursively key-sorted. Optional fields Codex
    leaves unset (``matcher`` above all) are omitted, not sent as null.
    """
    identity = {
        "event_name": event_label,
        "hooks": [{"async": False, "command": command, "timeout": int(timeout), "type": "command"}],
    }
    canonical = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def codex_hook_state_key(
    hooks_json_path: Path | str,
    event_label: str,
    group: int = 0,
    handler: int = 0,
) -> str:
    """The ``[hooks.state]`` key Codex looks the trusted hash up under."""
    absolute = os.path.abspath(os.path.expanduser(str(hooks_json_path)))
    return f"{absolute}:{event_label}:{int(group)}:{int(handler)}"


def codex_expected_trust(hooks_path: Path | None = None) -> dict[str, str]:
    """``state key -> trusted hash`` for every apc hook actually in ``hooks.json``.

    The key carries the group and handler index, so it is read back off the file we
    just wrote rather than assumed to be ``0:0``: our group lands after whatever
    hooks were already registered for that event.
    """
    path = hooks_path or codex_hooks_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return {}

    entries: dict[str, str] = {}
    for event in CODEX_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        label = codex_event_label(event)
        for group_index, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler_index, handler in enumerate(handlers):
                if not isinstance(handler, dict):
                    continue
                if str(handler.get("command", "")) != CODEX_HOOK_COMMAND:
                    continue
                timeout = handler.get("timeout")
                # Hash what Codex will hash: a handler with no timeout is normalized to
                # Codex's own default, NOT to ours, before its identity is hashed.
                entries[codex_hook_state_key(path, label, group_index, handler_index)] = (
                    codex_hook_trust_hash(
                        label,
                        CODEX_HOOK_COMMAND,
                        CODEX_DEFAULT_TIMEOUT if timeout is None else int(timeout),
                    )
                )
    return entries


_TOML_HEADER = re.compile(r"^[ \t]*\[\[?[^\]\n]*\]\]?[ \t]*(?:#[^\n]*)?$", re.MULTILINE)
_TRUSTED_HASH = re.compile(r'(trusted_hash[ \t]*=[ \t]*)"(?:[^"\\]|\\.)*"')


def _toml_tables(text: str) -> list[tuple[str, int, int, int]]:
    """``(header, header start, body start, body end)`` for every table in ``text``."""
    tables: list[tuple[str, int, int, int]] = []
    headers = list(_TOML_HEADER.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        tables.append((header.group(0).strip(), header.start(), header.end(), end))
    return tables


def _toml_key(key: str) -> str:
    """``key`` as a TOML basic string. JSON and TOML agree on the escapes we can hit."""
    return json.dumps(key)


def _state_table_body(text: str) -> tuple[int, int] | None:
    for header, _start, body_start, body_end in _toml_tables(text):
        if re.fullmatch(r"\[[ \t]*hooks[ \t]*\.[ \t]*state[ \t]*\]", header):
            return body_start, body_end
    return None


def _state_entry_table(text: str, key: str) -> tuple[int, int, int] | None:
    """``(header start, body start, body end)`` of a ``[hooks.state."<key>"]`` block."""
    quoted = re.escape(_toml_key(key))
    pattern = re.compile(r"\[[ \t]*hooks[ \t]*\.[ \t]*state[ \t]*\.[ \t]*" + quoted + r"[ \t]*\]")
    for header, start, body_start, body_end in _toml_tables(text):
        if pattern.fullmatch(header):
            return start, body_start, body_end
    return None


def _set_trust_entry(text: str, key: str, digest: str) -> str:
    """Set ``trusted_hash`` for ``key``, updating an existing entry in place if there is one.

    Three shapes are handled: the ``[hooks.state."<key>"]`` table header we write, an
    inline ``"<key>" = { trusted_hash = "..." }`` inside ``[hooks.state]``, and no
    entry at all. Appending always uses the self-contained header form, which cannot
    land inside whatever table happens to be last in the file.
    """
    rendered = f'trusted_hash = "{digest}"'

    span = _state_entry_table(text, key)
    if span is not None:
        _, body_start, body_end = span
        body = text[body_start:body_end]
        if _TRUSTED_HASH.search(body):
            patched = _TRUSTED_HASH.sub(lambda m: m.group(1) + f'"{digest}"', body, count=1)
        else:
            patched = ("\n" if not body.startswith("\n") else "") + rendered + "\n" + body
        return text[:body_start] + patched + text[body_end:]

    state = _state_table_body(text)
    if state is not None:
        body = text[state[0] : state[1]]
        inline = re.compile(
            r"^([ \t]*" + re.escape(_toml_key(key)) + r"[ \t]*=[ \t]*\{[^}\n]*?"
            r"trusted_hash[ \t]*=[ \t]*)\"(?:[^\"\\]|\\.)*\"",
            re.MULTILINE,
        )
        patched, count = inline.subn(lambda m: m.group(1) + f'"{digest}"', body, count=1)
        if count:
            return text[: state[0]] + patched + text[state[1] :]

    block = f"[hooks.state.{_toml_key(key)}]\n{rendered}\n"
    if not text:
        return block
    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + separator + block


def _parse_toml(path: Path, text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        _log.warning("%s is not valid TOML; refusing to touch it", path)
        raise ConfigFormatError(
            f"{path} is not valid TOML ({exc}). Refusing to touch it. "
            "Fix the file (or move it aside), then re-run the install."
        ) from exc


def _trust_state(data: dict[str, Any]) -> dict[str, Any]:
    hooks = data.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    return state if isinstance(state, dict) else {}


def _stored_hash(state: dict[str, Any], key: str) -> str | None:
    entry = state.get(key)
    if isinstance(entry, dict):
        value = entry.get("trusted_hash")
        return value if isinstance(value, str) else None
    return None


def _install_codex_trust(entries: dict[str, str], *, dry_run: bool) -> str:
    """Persist ``entries`` into ``$CODEX_HOME/config.toml`` under ``[hooks.state]``."""
    path = codex_config_path()
    if not entries:
        return f"no codex hooks to trust in {path}"

    existed = path.exists()
    try:
        original = path.read_text(encoding="utf-8") if existed else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigFormatError(
            f"{path} could not be read as UTF-8 text ({exc}). "
            "Fix or move the file, then re-run the install."
        ) from exc
    state = _trust_state(_parse_toml(path, original))
    pending = {k: v for k, v in entries.items() if _stored_hash(state, k) != v}

    if not pending:
        return f"codex hooks already trusted in {path} (no changes)"
    if dry_run:
        return f"[dry-run] would trust {len(pending)} codex hook(s) in {path}"

    text = original
    for key, digest in pending.items():
        text = _set_trust_entry(text, key, digest)

    # Validate before writing: a file we cannot parse back is never worth shipping.
    # An unusual spelling of an existing entry lands here rather than on disk.
    try:
        candidate = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        candidate = {}
    if not _trust_is_applied(candidate, entries):
        raise ConfigFormatError(
            f"refusing to write {path}: the hook trust entries could not be set without "
            "disturbing the rest of the file. Add them by hand, or start `codex` once and "
            'choose "Trust all and continue".'
        )

    _write_atomic(path, text)
    try:
        written = tomllib.loads(path.read_text(encoding="utf-8"))
        applied = _trust_is_applied(written, entries)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        applied = False
    if not applied:
        if existed:
            _write_atomic(path, original)
        else:
            path.unlink(missing_ok=True)
        raise ConfigFormatError(
            f"{path} did not parse back after writing the codex hook trust entries; "
            "the file has been restored unchanged."
        )
    return f"trusted codex hooks in {path} ({len(pending)} entries)"


def _trust_is_applied(data: dict[str, Any], entries: dict[str, str]) -> bool:
    state = _trust_state(data)
    return all(_stored_hash(state, key) == digest for key, digest in entries.items())


def _codex_trust_keys(state: dict[str, Any]) -> list[str]:
    """Every ``[hooks.state]`` key that we wrote: our hooks.json, our labels, our hash."""
    prefixes = {
        f"{os.path.abspath(str(codex_hooks_path()))}:{codex_event_label(event)}:": (
            codex_hook_trust_hash(codex_event_label(event), CODEX_HOOK_COMMAND, HOOK_TIMEOUT)
        )
        for event in CODEX_HOOK_EVENTS
    }
    ours = codex_expected_trust()
    keys: list[str] = []
    for key in state:
        if not isinstance(key, str):
            continue
        stored = _stored_hash(state, key)
        if ours.get(key) == stored and stored is not None:
            keys.append(key)
            continue
        for prefix, digest in prefixes.items():
            if key.startswith(prefix) and stored == digest:
                keys.append(key)
                break
    return keys


def _drop_trust_entry(text: str, key: str) -> str:
    span = _state_entry_table(text, key)
    if span is not None:
        # The whole `[hooks.state."<key>"]` block, header line included.
        start, _body_start, body_end = span
        return text[:start] + text[body_end:]
    state = _state_table_body(text)
    if state is not None:
        body = text[state[0] : state[1]]
        inline = re.compile(
            r"^[ \t]*" + re.escape(_toml_key(key)) + r"[ \t]*=[ \t]*\{[^}\n]*\}[ \t]*\n?",
            re.MULTILINE,
        )
        patched, count = inline.subn("", body, count=1)
        if count:
            return text[: state[0]] + patched + text[state[1] :]
    return text


def _uninstall_codex_trust() -> str:
    path = codex_config_path()
    if not path.exists():
        return f"no codex hook trust entries in {path}"
    # A config we cannot read must not stop the rest of the uninstall.
    try:
        original = path.read_text(encoding="utf-8")
        keys = _codex_trust_keys(_trust_state(_parse_toml(path, original)))
    except (OSError, UnicodeDecodeError, ConfigFormatError) as exc:
        return f"could not read the codex hook trust entries in {path} ({exc})"
    if not keys:
        return f"no codex hook trust entries in {path}"

    text = original
    for key in keys:
        text = _drop_trust_entry(text, key)
    text = re.sub(r"\n{3,}", "\n\n", text)
    remaining = _trust_state(_parse_toml(path, text))
    if any(key in remaining for key in keys):
        return f"could not remove the codex hook trust entries from {path}; remove them by hand"
    _write_atomic(path, text)
    return f"removed codex hook trust entries ({len(keys)}) from {path}"


def _install_codex_notify(*, dry_run: bool) -> str:
    """Legacy path: the deprecated ``notify`` program in ``config.toml``."""
    path = codex_config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    start, end = _top_level_region(text)
    head = text[start:end]

    existing = _NOTIFY_LINE.search(head)
    if existing and not _codex_notify_installed(head):
        return (
            f"{path} already sets notify:\n"
            f"    {existing.group(0).strip()}\n"
            "Leaving it alone. To capture Codex prompts, chain apc from your own notify "
            "program, or set it manually to:\n"
            f"    {CODEX_NOTIFY_LINE}"
        )

    if existing:
        updated = head[: existing.start()] + CODEX_NOTIFY_LINE + head[existing.end() :]
        changed = updated != head
    else:
        prefix = head if head.endswith("\n") or not head else head + "\n"
        updated = prefix + CODEX_NOTIFY_LINE + "\n"
        changed = True
    new_text = updated + text[end:]

    if dry_run:
        return f"[dry-run] would write {path}:\n{new_text}"
    if not changed:
        return f"codex notify already installed in {path} (no changes)"
    _write_atomic(path, new_text)
    return f"installed codex notify (legacy) in {path}"


#: Printed when the user asked us not to write the trust entries ourselves.
CODEX_TRUST_ALTERNATIVE = (
    "not trusting the codex hooks (--no-trust): start `codex` once and choose "
    '"Trust all and continue", or they will never run.'
)


def _install_codex(*, dry_run: bool, legacy: bool = False, trust: bool = True) -> str:
    if legacy:
        return _install_codex_notify(dry_run=dry_run)
    if not trust:
        return f"{_install_codex_hooks(dry_run=dry_run)}\n{CODEX_TRUST_ALTERNATIVE}"
    if dry_run:
        # dry run has nothing on disk to read back
        message = _install_codex_hooks(dry_run=True)
        return f"{message}\n{_install_codex_trust(_planned_codex_trust(), dry_run=True)}"

    # An untrusted hook never runs, so a hooks.json we cannot trust is worse than no
    # install at all: parse config.toml first, and put hooks.json back if the trust
    # step fails anyway. Half an install is the one outcome with no error to act on.
    _check_codex_config()
    hooks_path = codex_hooks_path()
    before = hooks_path.read_bytes() if hooks_path.is_file() else None
    message = _install_codex_hooks(dry_run=False)
    try:
        trust_message = _install_codex_trust(codex_expected_trust(), dry_run=False)
    except BaseException:
        if before is None:
            hooks_path.unlink(missing_ok=True)
        else:
            hooks_path.write_bytes(before)
        raise
    return f"{message}\n{trust_message}"


def _check_codex_config() -> None:
    """Fail before anything is written if ``config.toml`` is unreadable or malformed."""
    path = codex_config_path()
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigFormatError(
            f"{path} could not be read as UTF-8 text ({exc}). "
            "Fix or move the file, then re-run the install."
        ) from exc
    _parse_toml(path, text)


def _planned_codex_trust() -> dict[str, str]:
    """What ``codex_expected_trust`` would return once the hooks are on disk.

    The install normalizes every handler of ours to ``HOOK_TIMEOUT``, so that is what
    the entries will hash to - whatever timeout (or none) the file carries today.
    """
    entries = {
        # "<abs hooks.json>:<label>:<group>:<handler>"
        key: codex_hook_trust_hash(key.rsplit(":", 3)[1], CODEX_HOOK_COMMAND, HOOK_TIMEOUT)
        for key in codex_expected_trust()
    }
    path = codex_hooks_path()
    for event in CODEX_HOOK_EVENTS:
        label = codex_event_label(event)
        if any(key.startswith(f"{os.path.abspath(str(path))}:{label}:") for key in entries):
            continue
        entries[codex_hook_state_key(path, label)] = codex_hook_trust_hash(
            label, CODEX_HOOK_COMMAND, HOOK_TIMEOUT
        )
    return entries


def _uninstall_codex() -> str:
    """Remove the hooks.json entries, their trust entries and any apc notify line."""
    messages: list[str] = []

    # Before the hooks go: the trust keys carry the indices they had in hooks.json.
    trust_message = _uninstall_codex_trust()

    hooks_path = codex_hooks_path()
    if hooks_path.exists():
        document = _load_json(hooks_path)
        hooks = document.get("hooks")
        removed: list[str] = []
        if isinstance(hooks, dict):
            for event in CODEX_HOOK_EVENTS:
                matchers = hooks.get(event)
                if not isinstance(matchers, list):
                    continue
                cleaned = _strip_command(matchers, CODEX_HOOK_COMMAND)
                if cleaned != matchers:
                    removed.append(event)
                if cleaned:
                    hooks[event] = cleaned
                else:
                    hooks.pop(event, None)
            document["hooks"] = hooks
        if removed:
            _write_atomic(hooks_path, json.dumps(document, indent=2) + "\n")
            messages.append(f"removed codex hooks ({', '.join(removed)}) from {hooks_path}")
        else:
            messages.append(f"no apc hooks found in {hooks_path}")
    else:
        messages.append(f"nothing to remove: {hooks_path} does not exist")

    messages.append(trust_message)

    config = codex_config_path()
    if config.exists():
        text = config.read_text(encoding="utf-8")
        start, end = _top_level_region(text)
        head = text[start:end]
        if _codex_notify_installed(head):
            cleaned = _NOTIFY_LINE.sub("", head, count=1)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            _write_atomic(config, cleaned + text[end:])
            messages.append(f"removed codex notify from {config}")
        else:
            messages.append(f"no apc notify entry in {config}")

    return "\n".join(messages)


# --------------------------------------------------------------------------
# opencode
# --------------------------------------------------------------------------


def _install_opencode(*, dry_run: bool) -> str:
    target = opencode_plugin_path()
    source = _packaged_plugin()
    if dry_run:
        origin = str(source) if source.is_file() else "the built-in template"
        return f"[dry-run] would copy {origin} to {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copyfile(source, target)
        return f"installed opencode plugin from {source} to {target}"
    _write_atomic(target, OPENCODE_PLUGIN_SOURCE)
    return f"installed opencode plugin (built-in template) to {target}"


def _uninstall_opencode() -> str:
    target = opencode_plugin_path()
    if not target.exists():
        return f"nothing to remove: {target} does not exist"
    target.unlink()
    return f"removed opencode plugin {target}"


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def install(
    target: str,
    dry_run: bool = False,
    *,
    legacy: bool = False,
    trust: bool = True,
) -> str:
    """Install the hook configuration for ``target``. Idempotent.

    ``legacy`` only affects ``codex``: it writes the deprecated ``notify`` line into
    ``config.toml`` instead of merging into Codex's native ``hooks.json``.

    ``trust`` only affects ``codex`` as well: Codex will not run a hook it has not been
    told to trust, so by default the install also writes the hooks' identity hashes into
    ``$CODEX_HOME/config.toml``. ``trust=False`` leaves that to the user.
    """
    key = str(target).strip().lower().replace("_", "-")
    if key in ("claude-code", "claudecode"):
        return _install_claude_code(dry_run=dry_run)
    if key in ("codex", "codex-cli"):
        return _install_codex(dry_run=dry_run, legacy=legacy, trust=trust)
    if key == "opencode":
        return _install_opencode(dry_run=dry_run)
    raise ValueError(f"unknown install target {target!r}; expected one of {', '.join(TARGETS)}")


def uninstall(target: str) -> str:
    """Undo :func:`install` for ``target``."""
    key = str(target).strip().lower().replace("_", "-")
    if key in ("claude-code", "claudecode"):
        return _uninstall_claude_code()
    if key in ("codex", "codex-cli"):
        return _uninstall_codex()
    if key == "opencode":
        return _uninstall_opencode()
    raise ValueError(f"unknown uninstall target {target!r}; expected one of {', '.join(TARGETS)}")
