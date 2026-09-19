"""``apc install`` / ``apc uninstall``: write the hook configuration for each agent."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .config import get_logger

__all__ = [
    "install",
    "uninstall",
    "TARGETS",
    "HOOK_COMMAND",
    "CODEX_HOOK_COMMAND",
    "CODEX_HOOK_EVENTS",
    "CLAUDE_HOOK_EVENTS",
    "OPENCODE_PLUGIN_NAME",
    "OPENCODE_PLUGIN_SOURCE",
    "claude_settings_path",
    "codex_config_path",
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
// never awaited: `chat.message` is awaited by the session loop, so any work done here is
// latency the user feels. Everything is wrapped in try/catch — capture is best effort and
// must never slow down or break OpenCode.
//
// Install with `apc install opencode` (copies this file to
// ~/.config/opencode/plugin/agent-prompt-capture.js).

const COMMAND = "apc";
const ARGS = ["capture", "opencode"];

function spawnDetached(json) {
  // Bun first (OpenCode's server runs on Bun), node:child_process otherwise.
  try {
    if (globalThis.Bun && typeof globalThis.Bun.spawn === "function") {
      const child = globalThis.Bun.spawn([COMMAND, ...ARGS], {
        stdin: "pipe",
        stdout: "ignore",
        stderr: "ignore",
      });
      child.stdin.write(json);
      child.stdin.end();
      if (typeof child.unref === "function") child.unref();
      return;
    }
  } catch (_) {
    // fall through to node
  }

  import("node:child_process")
    .then(({ spawn }) => {
      const child = spawn(COMMAND, ARGS, {
        stdio: ["pipe", "ignore", "ignore"],
        detached: true,
      });
      child.on("error", () => {});
      child.stdin.on("error", () => {});
      child.stdin.end(json);
      child.unref();
    })
    .catch(() => {});
}

function send(payload) {
  try {
    spawnDetached(JSON.stringify(payload));
  } catch (_) {
    // never propagate
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
  const props = (event && event.properties) || {};
  if (props.sessionID) return props.sessionID;
  if (props.info && props.info.id) return props.info.id;
  if (props.part && props.part.sessionID) return props.part.sessionID;
  return null;
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

      send({
        session_id: (input && input.sessionID) || message.sessionID || null,
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
      send({
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".apc-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# claude code
# --------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        _log.warning("%s is not valid JSON; refusing to overwrite it", path)
        raise
    return data if isinstance(data, dict) else {}


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
_APC_NOTIFY = re.compile(r"^\s*notify\s*=\s*\[\s*[\"']apc[\"']", re.MULTILINE)

CODEX_NOTIFY_LINE = 'notify = ["apc", "capture", "codex"]'


def _top_level_region(text: str) -> tuple[int, int]:
    """The byte range of the file before the first ``[section]`` header."""
    match = _SECTION.search(text)
    return (0, match.start() if match else len(text))


def _install_codex_hooks(*, dry_run: bool) -> str:
    """Merge our two hooks into Codex's native ``hooks.json``."""
    path = codex_hooks_path()
    document = _load_json(path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    added: list[str] = []
    for event in CODEX_HOOK_EVENTS:
        matchers = hooks.get(event)
        if not isinstance(matchers, list):
            matchers = []
        if _has_command(matchers, CODEX_HOOK_COMMAND):
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
        return f"codex hooks already installed in {path} (no changes)"
    return f"installed codex hooks ({', '.join(added)}) in {path}"


def _install_codex_notify(*, dry_run: bool) -> str:
    """Legacy path: the deprecated ``notify`` program in ``config.toml``."""
    path = codex_config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    start, end = _top_level_region(text)
    head = text[start:end]

    existing = _NOTIFY_LINE.search(head)
    if existing and not _APC_NOTIFY.search(head):
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


def _install_codex(*, dry_run: bool, legacy: bool = False) -> str:
    if legacy:
        return _install_codex_notify(dry_run=dry_run)
    return _install_codex_hooks(dry_run=dry_run)


def _uninstall_codex() -> str:
    """Remove both the hooks.json entries and any apc notify line."""
    messages: list[str] = []

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

    config = codex_config_path()
    if config.exists():
        text = config.read_text(encoding="utf-8")
        start, end = _top_level_region(text)
        head = text[start:end]
        if _APC_NOTIFY.search(head):
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


def install(target: str, dry_run: bool = False, *, legacy: bool = False) -> str:
    """Install the hook configuration for ``target``. Idempotent.

    ``legacy`` only affects ``codex``: it writes the deprecated ``notify`` line into
    ``config.toml`` instead of merging into Codex's native ``hooks.json``.
    """
    key = str(target).strip().lower().replace("_", "-")
    if key in ("claude-code", "claudecode"):
        return _install_claude_code(dry_run=dry_run)
    if key in ("codex", "codex-cli"):
        return _install_codex(dry_run=dry_run, legacy=legacy)
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
