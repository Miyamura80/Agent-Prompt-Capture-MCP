#!/usr/bin/env python3
"""End-to-end integration test for agent-prompt-capture.

    uv run python scripts/e2e.py

Everything the unit tests mock is real here: the installed hook config, the `apc`
console script, the OpenCode plugin file that `apc install opencode` copied, the
`apc serve` HTTP listener, the Chrome extension driven by Playwright, and the
stdio MCP server spoken to with the official `mcp` SDK client.

Nothing touches the real home directory: HOME, APC_HOME, CLAUDE_CONFIG_DIR,
CODEX_HOME and XDG_CONFIG_HOME all point inside one temporary tree that is
removed on the way out.

Exit code is 0 only when every step passed (expected-failure markers for known
`src/` bugs are reported loudly but do not fail the run).

Environment:
    APC_E2E_SKIP_EXTENSION=1   skip step 5b (Playwright/Chromium unavailable)
    APC_E2E_KEEP=1             leave the temporary tree on disk for inspection
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# known src/ bugs: asserted anyway, reported loudly, do not fail the run
# --------------------------------------------------------------------------

KNOWN_BUGS: dict[str, str] = {
    # id -> description. Empty: every src/ bug found so far has been fixed.
}

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

ACCOUNT = "me@work.com"  # must match what extension/scripts/smoke.mjs's
ACCOUNT_ALIAS = "work"  # /api/auth/session fixture returns
DENIED_ACCOUNT = "someone.else@example.org"

SECRET_EMAIL = "alice.smith@example.com"
SECRET_KEY = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
SECRET_PATH = "/Users/alice/dev/proj"
LISTENER_EMAIL = "carol.jones@example.net"
LISTENER_KEY = "sk-proj-ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210zyxwvu"

CC_SESSION = "cc-sess-e2e-1"
CX_SESSION = "cx-sess-e2e-1"
CX_NOTIFY_SESSION = "cx-sess-e2e-2"
OC_SESSION = "ses_1"

OC_REAL_TEXT = "refactor the tokenizer and explain the tradeoffs"
OC_SYNTHETIC_TEXT = "Use the above message and context to generate a prompt and call the task tool"
SMOKE_PROMPT_HEAD = "summarise the changelog"

#: Every raw secret that must never appear in stored data or MCP responses.
RAW_SECRETS = (SECRET_EMAIL, SECRET_KEY, LISTENER_EMAIL, LISTENER_KEY, "/Users/alice", ACCOUNT)

CONFIG_TOML = """\
[capture]
allowed_accounts = ["{account}"]
disabled_sources = []

[accounts]
"{account}" = "{alias}"

[pii]
extra_terms = []
extra_patterns = []
enable_ner = false

[server]
host = "127.0.0.1"
port = {port}

[time]
idle_gap_minutes = 30
tail_minutes = 5
"""


# --------------------------------------------------------------------------
# checklist
# --------------------------------------------------------------------------


class Checklist:
    """Prints a running checklist and a per-step summary table at the end."""

    def __init__(self) -> None:
        self.step_name = "setup"
        self.steps: list[str] = []
        self.results: dict[str, dict[str, int]] = {}
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.xfails: list[str] = []

    def step(self, name: str) -> None:
        self.step_name = name
        if name not in self.results:
            self.steps.append(name)
            self.results[name] = {"pass": 0, "fail": 0, "skip": 0, "xfail": 0}
        print(f"\n=== {name}")

    def _bump(self, key: str) -> None:
        self.results.setdefault(self.step_name, {"pass": 0, "fail": 0, "skip": 0, "xfail": 0})[
            key
        ] += 1
        if self.step_name not in self.steps:
            self.steps.append(self.step_name)

    def check(
        self,
        name: str,
        condition: Any,
        detail: str = "",
        *,
        known_bug: str | None = None,
    ) -> bool:
        ok = bool(condition)
        if ok:
            print(f"  ok    {name}")
            if known_bug:
                print(f"        NOTE known bug {known_bug!r} looks FIXED; drop the xfail marker")
            self._bump("pass")
            return True
        if known_bug:
            print(f"  XFAIL {name}" + (f" -- {detail}" if detail else ""))
            print(f"        KNOWN SRC BUG [{known_bug}]: {KNOWN_BUGS[known_bug]}")
            self._bump("xfail")
            self.xfails.append(f"[{self.step_name}] {name} -- known bug {known_bug!r}")
            return False
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))
        self._bump("fail")
        self.failures.append(f"[{self.step_name}] {name}" + (f" -- {detail}" if detail else ""))
        return False

    def skip(self, name: str, reason: str) -> None:
        print(f"  SKIP  {name} -- {reason}")
        self._bump("skip")

    def warn(self, message: str) -> None:
        print(f"  WARNING {message}")
        self.warnings.append(f"[{self.step_name}] {message}")

    def summary(self) -> int:
        width = max([len(s) for s in self.steps] + [len("step")])
        print("\n" + "=" * (width + 40))
        print("SUMMARY")
        print("=" * (width + 40))
        header = f"{'step'.ljust(width)}  {'pass':>5} {'fail':>5} {'skip':>5} {'xfail':>6}  result"
        print(header)
        print("-" * len(header))
        for name in self.steps:
            r = self.results[name]
            verdict = "FAIL" if r["fail"] else ("SKIP" if r["skip"] and not r["pass"] else "ok")
            print(
                f"{name.ljust(width)}  {r['pass']:>5} {r['fail']:>5} "
                f"{r['skip']:>5} {r['xfail']:>6}  {verdict}"
            )
        print("-" * len(header))

        if self.xfails:
            print(f"\nKNOWN src/ BUGS ({len(self.xfails)} expected failures, not counted as fail):")
            for bug_id, text in KNOWN_BUGS.items():
                hits = [x for x in self.xfails if f"'{bug_id}'" in x]
                if hits:
                    print(f"  - {bug_id}: {text}")
                    for hit in hits:
                        print(f"      {hit}")
        if self.warnings:
            print(f"\nWARNINGS ({len(self.warnings)}):")
            for w in self.warnings:
                print(f"  - {w}")
        if self.failures:
            print(f"\nFAILURES ({len(self.failures)}):")
            for f in self.failures:
                print(f"  - {f}")
            print("\nE2E FAILED")
            return 1
        print("\nE2E PASSED")
        return 0


R = Checklist()


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------


def _apc_command() -> list[str]:
    console = REPO / ".venv" / "bin" / "apc"
    if console.is_file() and os.access(console, os.X_OK):
        return [str(console)]
    return ["uv", "run", "--project", str(REPO), "apc"]


APC = _apc_command()


class Env:
    """The isolated HOME tree every child process runs in."""

    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.home = root / "home"
        self.apc_home = self.home / ".agent-prompt-capture"
        self.claude_dir = self.home / ".claude"
        self.codex_home = self.home / ".codex"
        self.xdg_config = self.home / ".config"
        self.port = port
        for path in (self.home, self.apc_home, self.claude_dir, self.codex_home, self.xdg_config):
            path.mkdir(parents=True, exist_ok=True)
        (self.apc_home / "config.toml").write_text(
            CONFIG_TOML.format(account=ACCOUNT, alias=ACCOUNT_ALIAS, port=port), encoding="utf-8"
        )

    @property
    def vars(self) -> dict[str, str]:
        path = os.pathsep.join([str(REPO / ".venv" / "bin"), os.environ.get("PATH", "")])
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(self.home),
                "APC_HOME": str(self.apc_home),
                "CLAUDE_CONFIG_DIR": str(self.claude_dir),
                "CODEX_HOME": str(self.codex_home),
                "XDG_CONFIG_HOME": str(self.xdg_config),
                "PATH": path,
                "PYTHONUNBUFFERED": "1",
            }
        )
        # Never let a stray outer setting leak into the isolated run.
        env.pop("APC_DEBUG", None)
        return env

    @property
    def settings_json(self) -> Path:
        return self.claude_dir / "settings.json"

    @property
    def codex_hooks_json(self) -> Path:
        return self.codex_home / "hooks.json"

    @property
    def opencode_plugin(self) -> Path:
        return self.xdg_config / "opencode" / "plugin" / "agent-prompt-capture.js"

    @property
    def db(self) -> Path:
        return self.apc_home / "prompts.db"


def apc(
    env: Env,
    *args: str,
    stdin_text: str | None = None,
    stdin_devnull: bool = False,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [*APC, *args],
        input=None if stdin_devnull else (stdin_text or ""),
        stdin=subprocess.DEVNULL if stdin_devnull else None,
        capture_output=True,
        text=True,
        env=env.vars,
        cwd=str(REPO),
        timeout=timeout,
        check=False,
    )


def apc_json(env: Env, *args: str) -> Any:
    proc = apc(env, *args)
    if proc.returncode != 0:
        raise RuntimeError(f"`apc {' '.join(args)}` exited {proc.returncode}: {proc.stderr}")
    return json.loads(proc.stdout)


def records(env: Env, *, source: str | None = None) -> list[dict[str, Any]]:
    args = ["list", "--json", "--limit", "200"]
    if source:
        args += ["--source", source]
    return apc_json(env, *args)["prompts"]


def iso(offset_minutes: float = 0.0, offset_seconds: float = 0.0) -> str:
    dt = datetime.now(UTC) + timedelta(minutes=offset_minutes, seconds=offset_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def contains_raw_secret(blob: str) -> list[str]:
    return [s for s in RAW_SECRETS if s in blob]


# --------------------------------------------------------------------------
# step 1: install
# --------------------------------------------------------------------------


def step_install(env: Env) -> None:
    R.step("1. install + doctor")

    outputs: dict[str, list[str]] = {}
    digests: dict[str, list[str]] = {}
    targets = ("claude-code", "codex", "opencode")
    watched = {
        "claude-code": env.settings_json,
        "codex": env.codex_hooks_json,
        "opencode": env.opencode_plugin,
    }

    for target in targets:
        outputs[target] = []
        digests[target] = []
        for _ in range(3):
            proc = apc(env, "install", target)
            if proc.returncode != 0:
                R.check(f"apc install {target} exits 0", False, proc.stderr.strip())
                return
            outputs[target].append(proc.stdout)
            path = watched[target]
            digests[target].append(path.read_bytes().hex() if path.exists() else "<missing>")
        R.check(f"apc install {target} exits 0", True)

    # -- the hook config that landed on disk
    settings = json.loads(env.settings_json.read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {})
    for event in ("UserPromptSubmit", "Stop"):
        rendered = json.dumps(hooks.get(event, []))
        R.check(
            f"~/.claude/settings.json {event} -> `apc capture claude-code`",
            "apc capture claude-code" in rendered,
            rendered[:200],
        )

    codex = json.loads(env.codex_hooks_json.read_text(encoding="utf-8"))
    codex_hooks = codex.get("hooks", {})
    for event in ("UserPromptSubmit", "Stop"):
        rendered = json.dumps(codex_hooks.get(event, []))
        R.check(
            f"~/.codex/hooks.json has {event} -> `apc capture codex`",
            "apc capture codex" in rendered,
            rendered[:200],
        )

    R.check(
        "OpenCode plugin installed in ~/.config/opencode/plugin/",
        env.opencode_plugin.is_file(),
        str(env.opencode_plugin),
    )
    R.check(
        "the installed plugin is the packaged opencode-plugin/agent-prompt-capture.js",
        env.opencode_plugin.read_bytes()
        == (REPO / "opencode-plugin" / "agent-prompt-capture.js").read_bytes(),
    )

    # -- idempotency
    for target in targets:
        R.check(
            f"apc install {target} is idempotent on disk (runs 1/2/3 byte-identical)",
            len(set(digests[target])) == 1,
            f"digests differ across runs for {watched[target]}",
        )
        R.check(
            f"apc install {target} output is stable once installed (run 2 == run 3)",
            outputs[target][1] == outputs[target][2],
            f"run2={outputs[target][1]!r} run3={outputs[target][2]!r}",
        )
        if outputs[target][0] != outputs[target][1]:
            R.warn(
                f"`apc install {target}` prints a different line on the first run "
                f"({outputs[target][0].strip()!r}) than on re-runs "
                f"({outputs[target][1].strip()!r}); the files it writes are identical, so "
                "this is a reporting difference, not a non-idempotent write"
            )

    doctor = apc(env, "doctor")
    R.check("apc doctor exits 0", doctor.returncode == 0, doctor.stderr.strip())
    for needle in ("claude-code hooks", "codex hooks", "opencode plugin"):
        R.check(
            f"apc doctor reports {needle} as [ok]",
            any(
                line.strip().startswith("[ok]") and needle in line
                for line in doctor.stdout.splitlines()
            ),
            needle,
        )


# --------------------------------------------------------------------------
# step 2: claude code hook path
# --------------------------------------------------------------------------


def step_claude_code(env: Env) -> None:
    R.step("2. claude_code hook path")

    prompt_ts = iso(offset_minutes=-90)
    stop_ts = iso(offset_minutes=-90, offset_seconds=3)
    prompt_payload = {
        "session_id": CC_SESSION,
        "transcript_path": f"/Users/alice/.claude/projects/proj/{CC_SESSION}.jsonl",
        "cwd": SECRET_PATH,
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": (
            f"Ping {SECRET_EMAIL} about the rotated key {SECRET_KEY} "
            f"and then refactor the parser under {SECRET_PATH}/src"
        ),
        "ts": prompt_ts,
    }
    stop_payload = {
        "session_id": CC_SESSION,
        "transcript_path": f"/Users/alice/.claude/projects/proj/{CC_SESSION}.jsonl",
        "cwd": SECRET_PATH,
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Refactor complete.",
        "ts": stop_ts,
    }

    for label, payload in (("UserPromptSubmit", prompt_payload), ("Stop", stop_payload)):
        proc = apc(env, "capture", "claude-code", stdin_text=json.dumps(payload))
        R.check(f"apc capture claude-code ({label}) exits 0", proc.returncode == 0)
        R.check(
            f"apc capture claude-code ({label}) writes nothing to stdout",
            proc.stdout == "",
            repr(proc.stdout),
        )
        R.check(
            f"apc capture claude-code ({label}) writes nothing to stderr",
            proc.stderr == "",
            repr(proc.stderr),
        )

    blob = apc(env, "list", "--json").stdout
    payload = json.loads(blob)
    R.check(
        "apc list --json shows exactly one record", payload["count"] == 1, json.dumps(payload)[:300]
    )
    if payload["count"] != 1:
        return
    rec = payload["prompts"][0]

    R.check("record source is claude_code", rec["source"] == "claude_code", rec["source"])
    R.check("record session_id survived", rec["session_id"] == CC_SESSION, str(rec["session_id"]))
    R.check(
        "turn_end_ts is set from the Stop hook",
        rec["turn_end_ts"] == stop_ts,
        str(rec["turn_end_ts"]),
    )
    R.check("prompt was scrubbed: [EMAIL_1]", "[EMAIL_1]" in rec["prompt"], rec["prompt"])
    R.check("prompt was scrubbed: [API_KEY_1]", "[API_KEY_1]" in rec["prompt"], rec["prompt"])
    R.check(
        "cwd was scrubbed to /Users/[USER]/dev/proj",
        rec["cwd"] == "/Users/[USER]/dev/proj",
        str(rec["cwd"]),
    )
    R.check(
        "pii_findings records email + api_key + home_path",
        {"email", "api_key", "home_path"} <= set(rec["pii_findings"]),
        json.dumps(rec["pii_findings"]),
    )
    leaks = contains_raw_secret(blob)
    R.check("no raw email / key / home path anywhere in the JSON", not leaks, f"leaked: {leaks}")


# --------------------------------------------------------------------------
# step 3: codex hook + legacy notify paths
# --------------------------------------------------------------------------


def step_codex(env: Env) -> None:
    R.step("3. codex_cli hook + legacy notify")

    prompt_ts = iso(offset_minutes=-60)
    stop_ts = iso(offset_minutes=-60, offset_seconds=8)
    hook_prompt = "Rename `foo` to `bar` and update the callsites in the tokenizer"
    notify_prompt = "Audit the retry backoff and write a regression test"

    submit = {
        "session_id": CX_SESSION,
        "turn_id": "turn-1",
        "transcript_path": "/Users/alice/.codex/sessions/2026/09/19/rollout.jsonl",
        "cwd": SECRET_PATH,
        "model": "gpt-5.1-codex",
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": hook_prompt,
        "ts": prompt_ts,
    }
    stop = {
        "session_id": CX_SESSION,
        "turn_id": "turn-1",
        "cwd": SECRET_PATH,
        "hook_event_name": "Stop",
        "ts": stop_ts,
    }
    for label, payload in (("UserPromptSubmit", submit), ("Stop", stop)):
        proc = apc(env, "capture", "codex", stdin_text=json.dumps(payload))
        R.check(
            f"apc capture codex ({label}) exits 0 and is silent",
            proc.returncode == 0 and proc.stdout == "" and proc.stderr == "",
            f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}",
        )

    # Legacy notify: JSON as the LAST ARGV ARGUMENT, with stdin closed.
    notify = {
        "type": "agent-turn-complete",
        "thread-id": CX_NOTIFY_SESSION,
        "turn-id": "turn-42",
        "cwd": SECRET_PATH,
        "client": "codex-tui",
        "input-messages": ["ignore me", notify_prompt],
        "last-assistant-message": "Test added and passing.",
    }
    proc = apc(env, "capture", "codex", json.dumps(notify), stdin_devnull=True)
    R.check(
        "apc capture codex (legacy notify, argv payload, stdin=DEVNULL) exits 0 and is silent",
        proc.returncode == 0 and proc.stdout == "" and proc.stderr == "",
        f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}",
    )

    rows = records(env, source="codex_cli")
    R.check("both codex payloads were stored", len(rows) == 2, f"got {len(rows)}")
    by_session = {r["session_id"]: r for r in rows}

    hook_row = by_session.get(CX_SESSION)
    R.check("the hooks UserPromptSubmit prompt is stored", hook_row is not None)
    if hook_row:
        R.check("hooks prompt text matches", hook_row["prompt"] == hook_prompt, hook_row["prompt"])
        R.check(
            "hooks Stop set turn_end_ts",
            hook_row["turn_end_ts"] == stop_ts,
            str(hook_row["turn_end_ts"]),
        )
        R.check(
            "hooks metadata carries turn_id",
            hook_row["metadata"].get("turn_id") == "turn-1",
            json.dumps(hook_row["metadata"]),
        )

    notify_row = by_session.get(CX_NOTIFY_SESSION)
    R.check("the legacy notify prompt is stored", notify_row is not None)
    if notify_row:
        R.check(
            "notify keeps the LAST input-message",
            notify_row["prompt"] == notify_prompt,
            notify_row["prompt"],
        )
        R.check(
            "notify is also a turn end",
            bool(notify_row["turn_end_ts"]),
            str(notify_row["turn_end_ts"]),
        )
        R.check(
            "notify metadata type is agent-turn-complete",
            notify_row["metadata"].get("type") == "agent-turn-complete",
            json.dumps(notify_row["metadata"]),
        )
    R.check(
        "codex cwd was scrubbed",
        all(r["cwd"] == "/Users/[USER]/dev/proj" for r in rows),
        json.dumps([r["cwd"] for r in rows]),
    )


# --------------------------------------------------------------------------
# step 4: opencode plugin path (the real installed plugin file)
# --------------------------------------------------------------------------


def step_opencode(env: Env) -> None:
    R.step("4. opencode plugin path")

    driver = REPO / "scripts" / "opencode_driver.mjs"
    R.check("scripts/opencode_driver.mjs exists", driver.is_file(), str(driver))
    if not driver.is_file():
        return
    if not env.opencode_plugin.is_file():
        R.check("the installed plugin file exists", False, str(env.opencode_plugin))
        return

    created_ms = int((datetime.now(UTC) - timedelta(minutes=30)).timestamp() * 1000)
    proc = subprocess.run(  # noqa: S603
        [
            "node",
            str(driver),
            str(env.opencode_plugin),
            "--session",
            OC_SESSION,
            "--created",
            str(created_ms),
            "--text",
            OC_REAL_TEXT,
            "--synthetic",
            OC_SYNTHETIC_TEXT,
            # `chat.message` and `event` each spawn a *detached* `apc capture`; fire
            # them back to back and the turn-end child can beat the prompt insert.
            # In real use a whole agent turn separates them.
            "--idle-delay-ms",
            "2000",
        ],
        capture_output=True,
        text=True,
        env=env.vars,
        cwd=str(REPO),
        timeout=60,
        check=False,
    )
    R.check(
        "the node driver ran the installed plugin",
        proc.returncode == 0,
        f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}",
    )
    if proc.stdout.strip():
        for line in proc.stdout.strip().splitlines():
            print(f"        driver: {line}")
    if proc.returncode != 0:
        return

    ok = wait_for(lambda: len(records(env, source="opencode")) >= 1, timeout=5.0)
    R.check("a record appeared within 5s (plugin spawns `apc` detached)", ok)
    rows = records(env, source="opencode")
    if not rows:
        return
    rec = rows[0]

    R.check("prompt keeps the real text part", OC_REAL_TEXT in rec["prompt"], rec["prompt"])
    R.check(
        "prompt EXCLUDES the synthetic text part",
        OC_SYNTHETIC_TEXT not in rec["prompt"],
        rec["prompt"],
    )
    R.check(
        "session_id came from chat.message input.sessionID",
        rec["session_id"] == OC_SESSION,
        str(rec["session_id"]),
    )
    R.check("project came from the plugin input", rec["project"] == "proj", str(rec["project"]))
    R.check("cwd (directory) was scrubbed", rec["cwd"] == "/Users/[USER]/dev/proj", str(rec["cwd"]))
    R.check(
        "metadata.model is the provider/model pair",
        rec["metadata"].get("model") == "anthropic/claude-sonnet-4",
        json.dumps(rec["metadata"]),
    )
    R.check(
        "metadata.attachments == 1 (one file part)",
        str(rec["metadata"].get("attachments")) == "1",
        f"metadata={json.dumps(rec['metadata'])}",
    )

    ended = wait_for(
        lambda: bool((records(env, source="opencode") or [{}])[0].get("turn_end_ts")), timeout=5.0
    )
    R.check(
        "session.idle set turn_end_ts within 5s",
        ended,
        str(records(env, source="opencode")[0].get("turn_end_ts")),
    )


# --------------------------------------------------------------------------
# step 5a: the real apc serve listener
# --------------------------------------------------------------------------


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body or "null")
        except json.JSONDecodeError:
            return exc.code, body


def _post(url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "null")
        except json.JSONDecodeError:
            return exc.code, raw


def _get_with_host(port: int, path: str, host_header: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host_header)
        conn.putheader("Accept", "*/*")
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def start_listener(env: Env) -> subprocess.Popen[str] | None:
    proc = subprocess.Popen(  # noqa: S603
        [*APC, "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env.vars,
        cwd=str(REPO),
    )
    base = f"http://127.0.0.1:{env.port}"

    def ready() -> bool:
        if proc.poll() is not None:
            return True  # died; reported below
        try:
            return _get(f"{base}/v1/health")[0] == 200
        except OSError:
            return False

    up = wait_for(ready, timeout=30.0, interval=0.2)
    if proc.poll() is not None:
        out, err = proc.communicate(timeout=5)
        R.check("apc serve started", False, f"exited {proc.returncode}\nstdout={out}\nstderr={err}")
        return None
    R.check("apc serve started and answers GET /v1/health", up, base)
    if not up:
        return None
    return proc


def step_listener(env: Env, token: str) -> None:
    base = f"http://127.0.0.1:{env.port}"

    status, body = _get(f"{base}/v1/health")
    R.check(
        "GET /v1/health -> 200 {ok:true}",
        status == 200 and isinstance(body, dict) and body.get("ok") is True,
        f"{status} {body}",
    )

    status, body = _get(f"{base}/v1/config", {"X-APC-Token": token})
    R.check(
        "GET /v1/config with the real token -> 200",
        status == 200 and isinstance(body, dict) and body.get("allowed_accounts_count") == 1,
        f"{status} {body}",
    )
    R.check(
        "GET /v1/config never returns the allowlisted email",
        ACCOUNT not in json.dumps(body),
        json.dumps(body),
    )

    status, body = _get(f"{base}/v1/config", {"X-APC-Token": "not-the-token"})
    R.check("GET /v1/config with a bad token -> 401", status == 401, f"{status} {body}")

    allowed_payload = {
        "source": "chatgpt_web",
        "prompt": f"draft the release notes, cc {LISTENER_EMAIL}, key {LISTENER_KEY}",
        "account": ACCOUNT,
        "conversation_id": "conv-e2e-1",
        "url": "https://chatgpt.com/c/conv-e2e-1?utm_source=leak#frag",
        "title": "Release notes",
        "ts": iso(offset_minutes=-20),
        "client_version": "0.1.0",
    }
    status, body = _post(f"{base}/v1/prompts", allowed_payload, {"X-APC-Token": token})
    R.check(
        "POST /v1/prompts (allowed account) -> 202 stored:true",
        status == 202 and isinstance(body, dict) and body.get("stored") is True,
        f"{status} {body}",
    )

    denied_payload = {
        **allowed_payload,
        "account": DENIED_ACCOUNT,
        "prompt": "this must never be stored",
        "conversation_id": "conv-e2e-2",
    }
    status, body = _post(f"{base}/v1/prompts", denied_payload, {"X-APC-Token": token})
    R.check(
        "POST /v1/prompts (account not on the allowlist) -> 202 stored:false",
        status == 202
        and isinstance(body, dict)
        and body.get("stored") is False
        and body.get("reason") == "account_not_allowed",
        f"{status} {body}",
    )

    status, body = _post(f"{base}/v1/prompts", allowed_payload, {"X-APC-Token": "wrong"})
    R.check("POST /v1/prompts with a bad token -> 401", status == 401, f"{status} {body}")

    # DNS-rebinding: a page on http://evil.example.com that resolves to 127.0.0.1.
    rebind = _get_with_host(env.port, "/v1/health", "evil.example.com")
    if 200 <= rebind < 300:
        # Contract: a warning, never a hard failure (the listener may not reject it yet).
        R.warn(
            "the listener answered a GET /v1/health carrying Host: evil.example.com with "
            f"{rebind}. It does not validate the Host header, so a page on "
            "http://evil.example.com resolving to 127.0.0.1 can reach it (DNS rebinding)."
        )
    R.check(
        "GET /v1/health with Host: evil.example.com is rejected (DNS rebinding)",
        not (200 <= rebind < 300),
        f"status {rebind}",
    )
    rebind_post = _get_with_host(env.port, "/v1/config", "evil.example.com")
    R.check(
        "GET /v1/config with Host: evil.example.com is rejected too",
        not (200 <= rebind_post < 300),
        f"status {rebind_post}",
    )

    stored = records(env, source="chatgpt_web")
    R.check("only the allowlisted POST was stored", len(stored) == 1, f"got {len(stored)}")
    if stored:
        rec = stored[0]
        R.check(
            "listener-side scrub replaced the email", "[EMAIL_1]" in rec["prompt"], rec["prompt"]
        )
        R.check(
            "listener-side scrub replaced the api key",
            "[API_KEY_1]" in rec["prompt"],
            rec["prompt"],
        )
        R.check(
            "account is stored as the configured alias",
            rec["account"] == ACCOUNT_ALIAS,
            str(rec["account"]),
        )
        R.check(
            "metadata.url dropped the query string and fragment",
            rec["metadata"].get("url") == "https://chatgpt.com/c/conv-e2e-1",
            json.dumps(rec["metadata"]),
        )
    leaks = contains_raw_secret(json.dumps(stored))
    R.check("no raw secrets in the stored browser record", not leaks, f"leaked: {leaks}")


# --------------------------------------------------------------------------
# step 5b: the Chrome extension, driven against the real listener
# --------------------------------------------------------------------------


def step_extension(env: Env, token: str) -> bool:
    R.step("5b. chrome extension -> real listener")

    if os.environ.get("APC_E2E_SKIP_EXTENSION") == "1":
        R.skip(
            "extension Playwright smoke against the real listener",
            "APC_E2E_SKIP_EXTENSION=1 is set",
        )
        return False

    smoke = REPO / "extension" / "scripts" / "smoke.mjs"
    child_env = env.vars
    child_env["APC_E2E_LISTENER_URL"] = f"http://127.0.0.1:{env.port}"
    child_env["APC_E2E_TOKEN"] = token
    # Only point Playwright at the shared browser pool when it really is there;
    # on a CI runner the browsers live wherever `playwright install` put them and
    # an invented path would hide them. smoke.mjs defaults the same way.
    if "PLAYWRIGHT_BROWSERS_PATH" not in child_env and Path("/opt/pw-browsers").is_dir():
        child_env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"

    print(f"  ...  running {smoke.relative_to(REPO)} against the real listener (this takes ~1 min)")
    proc = subprocess.run(  # noqa: S603
        ["node", str(smoke)],
        capture_output=True,
        text=True,
        env=child_env,
        cwd=str(REPO),
        timeout=600,
        check=False,
    )
    for line in (proc.stdout + proc.stderr).splitlines():
        print(f"        smoke: {line}")
    R.check(
        "extension smoke test passes against the real apc serve",
        proc.returncode == 0,
        f"exit {proc.returncode}",
    )
    if proc.returncode != 0:
        return False

    R.check(
        "the smoke run targeted the real listener (not the mock)",
        "APC_E2E: extension will target the real listener" in proc.stdout,
        "smoke.mjs did not report E2E mode",
    )

    rows = records(env, source="chatgpt_web")
    from_browser = [r for r in rows if SMOKE_PROMPT_HEAD in r["prompt"]]
    R.check(
        "a chatgpt_web record from the extension is in the DB",
        len(from_browser) == 1,
        f"chatgpt_web rows: {json.dumps(rows)[:400]}",
    )
    if from_browser:
        rec = from_browser[0]
        R.check(
            "the browser record is stored under the account alias",
            rec["account"] == ACCOUNT_ALIAS,
            str(rec["account"]),
        )
        R.check(
            "the extension pre-scrub placeholder survived",
            "[EMAIL]" in rec["prompt"],
            rec["prompt"],
        )
        R.check(
            "no raw address from the page reached the DB",
            "leaked.person@example.com" not in rec["prompt"],
            rec["prompt"],
        )
        R.check(
            "conversation_id became the session id",
            rec["session_id"] == "11111111-2222-3333-4444-555555555555",
            str(rec["session_id"]),
        )

    web = records(env, source="claude_code_web")
    R.check(
        "the claude.ai/code phase also landed as claude_code_web", len(web) >= 1, f"got {len(web)}"
    )
    leaks = contains_raw_secret(json.dumps(rows + web))
    R.check("no raw secrets in any browser-sourced record", not leaks, f"leaked: {leaks}")
    return True


# --------------------------------------------------------------------------
# step 6: the MCP server
# --------------------------------------------------------------------------


def _tool_json(result: Any) -> Any:
    texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
    if not texts:
        raise AssertionError(f"tool returned no text content: {result!r}")
    return json.loads("".join(texts))


async def _mcp_probe(env: Env) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
    from mcp.client.stdio import stdio_client  # noqa: PLC0415

    params = StdioServerParameters(
        command=APC[0], args=[*APC[1:], "mcp"], env=env.vars, cwd=str(REPO)
    )
    out: dict[str, Any] = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            out["server_name"] = getattr(getattr(init, "server_info", None), "name", None)
            tools = await session.list_tools()
            out["tools"] = sorted(t.name for t in tools.tools)
            calls = {
                "time_summary": {"since": "7d", "group_by": "project"},
                "daily_digest": {},
                "search_prompts": {"query": "tokenizer"},
                "list_sources": {},
                "activity_timeline": {"since": "24h", "bucket": "hour"},
                "prompt_stats": {"group_by": "source"},
            }
            out["results"] = {}
            out["raw"] = {}
            for name, args in calls.items():
                result = await asyncio.wait_for(session.call_tool(name, args), timeout=60)
                out["raw"][name] = "".join(
                    c.text for c in result.content if getattr(c, "type", "") == "text"
                )
                out["results"][name] = _tool_json(result)
    return out


def step_mcp(env: Env, expected_sources: set[str]) -> None:
    R.step("6. stdio MCP server")

    try:
        import importlib.metadata as md  # noqa: PLC0415

        version = md.version("mcp")
    except Exception as exc:  # noqa: BLE001
        version = f"unknown ({exc})"
    print(f"  ...  mcp SDK version {version}; using mcp.client.stdio.stdio_client + ClientSession")

    try:
        probe = asyncio.run(_mcp_probe(env))
    except Exception as exc:  # noqa: BLE001
        R.check("the MCP server initialised over stdio", False, f"{type(exc).__name__}: {exc}")
        return

    R.check(
        "initialize() returned the server name",
        probe["server_name"] == "agent-prompt-capture",
        str(probe["server_name"]),
    )

    expected_tools = {
        "list_prompts",
        "search_prompts",
        "get_prompt",
        "prompt_stats",
        "list_sources",
        "list_sessions",
        "time_summary",
        "activity_timeline",
        "daily_digest",
    }
    got_tools = set(probe["tools"])
    R.check(
        f"list_tools() exposes all 9 ARCHITECTURE.md tools ({len(expected_tools)})",
        got_tools == expected_tools,
        f"missing={sorted(expected_tools - got_tools)} extra={sorted(got_tools - expected_tools)}",
    )

    res = probe["results"]

    sources = {s["source"] for s in res["list_sources"]["sources"]}
    R.check(
        "list_sources() == exactly the sources this run captured",
        sources == expected_sources,
        f"got={sorted(sources)} expected={sorted(expected_sources)}",
    )

    ts = res["time_summary"]
    R.check(
        "time_summary(since=7d, group_by=project) has groups",
        bool(ts["groups"]),
        json.dumps(ts)[:300],
    )
    R.check(
        "time_summary total_active_minutes > 0",
        ts["total_active_minutes"] > 0,
        str(ts["total_active_minutes"]),
    )
    R.check(
        "time_summary groups the 'proj' project",
        any(g["key"] == "proj" for g in ts["groups"]),
        json.dumps([g["key"] for g in ts["groups"]]),
    )
    R.check(
        "time_summary counts every prompt exactly once",
        sum(g["prompt_count"] for g in ts["groups"]) == len(records(env)),
        f"{sum(g['prompt_count'] for g in ts['groups'])} vs {len(records(env))}",
    )
    R.check(
        "time_summary reports agent time from the turn_end_ts pairs",
        ts["groups"] and any(g["agent_minutes"] > 0 for g in ts["groups"]),
        json.dumps([g["agent_minutes"] for g in ts["groups"]]),
    )

    digest = res["daily_digest"]
    R.check(
        "daily_digest() returns today's date",
        digest["date"] == datetime.now().strftime("%Y-%m-%d"),
        str(digest["date"]),
    )
    R.check(
        "daily_digest() found activity sessions", bool(digest["sessions"]), json.dumps(digest)[:300]
    )
    R.check(
        "daily_digest() active_minutes > 0",
        digest["active_minutes"] > 0,
        str(digest["active_minutes"]),
    )

    search = res["search_prompts"]
    R.check(
        "search_prompts(query='tokenizer') matches the prompts that contain it",
        search["count"] >= 1 and all("tokenizer" in r["prompt"].lower() for r in search["results"]),
        json.dumps(search)[:400],
    )

    timeline = res["activity_timeline"]
    R.check(
        "activity_timeline(since=24h, bucket=hour) returns buckets",
        bool(timeline["buckets"]),
        json.dumps(timeline)[:300],
    )
    R.check(
        "activity_timeline buckets account for every prompt in the window",
        sum(b["prompt_count"] for b in timeline["buckets"])
        >= len(records(env, source="claude_code")),
        json.dumps([b["prompt_count"] for b in timeline["buckets"]]),
    )
    R.check(
        "activity_timeline credits active minutes",
        any(b["active_minutes"] > 0 for b in timeline["buckets"]),
        json.dumps([b["active_minutes"] for b in timeline["buckets"]]),
    )

    stats = res["prompt_stats"]
    R.check(
        "prompt_stats(group_by=source) agrees with list_sources()",
        {g["key"] for g in stats["groups"]} == sources,
        json.dumps(stats)[:300],
    )

    all_text = "\n".join(probe["raw"].values())
    leaks = contains_raw_secret(all_text)
    R.check("no raw PII in any MCP response text", not leaks, f"leaked: {leaks}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    print("agent-prompt-capture: end-to-end integration test")
    print(f"repo      {REPO}")
    print(f"apc       {' '.join(APC)}")

    port = free_port()
    root = Path(tempfile.mkdtemp(prefix="apc-e2e-"))
    listener: subprocess.Popen[str] | None = None
    env = Env(root, port)
    print(f"temp HOME {env.home}")
    print(f"listener  http://127.0.0.1:{port}")

    try:
        step_install(env)
        step_claude_code(env)
        step_codex(env)
        step_opencode(env)

        R.step("5a. apc serve listener")
        token = apc(env, "token").stdout.strip()
        R.check("apc token printed a shared secret", len(token) >= 16, f"{len(token)} chars")
        listener = start_listener(env)
        expected_sources = {"claude_code", "codex_cli", "opencode"}
        if listener is not None:
            step_listener(env, token)
            expected_sources.add("chatgpt_web")
            if step_extension(env, token):
                expected_sources.add("claude_code_web")
        else:
            R.step("5b. chrome extension -> real listener")
            R.skip("extension Playwright smoke", "the listener did not start")

        step_mcp(env, expected_sources)
    except KeyboardInterrupt:
        R.check("run completed", False, "interrupted")
    except Exception as exc:  # noqa: BLE001
        import traceback  # noqa: PLC0415

        traceback.print_exc()
        R.check(
            "run completed without an unhandled exception", False, f"{type(exc).__name__}: {exc}"
        )
    finally:
        R.step("7. cleanup")
        if listener is not None and listener.poll() is None:
            listener.terminate()
            try:
                listener.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                listener.kill()
                listener.wait(timeout=5)
        R.check("listener stopped", listener is None or listener.poll() is not None)
        if os.environ.get("APC_E2E_KEEP") == "1":
            R.skip("temporary tree removed", f"APC_E2E_KEEP=1, kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
            R.check("temporary HOME tree removed", not root.exists(), str(root))

    return R.summary()


if __name__ == "__main__":
    sys.exit(main())
