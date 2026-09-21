"""``apc``: the argparse entry point.

``apc capture`` is the hot path: it must never print, never raise and always exit 0,
so anything expensive (the MCP SDK, the HTTP listener) is imported lazily.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__

__all__ = ["main", "build_parser"]

_CAPTURE_TARGETS = ("claude-code", "codex", "opencode")
_INSTALL_TARGETS = ("claude-code", "codex", "opencode")


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apc",
        description="Capture, scrub and query the prompts you send to your coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"apc {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="read hook JSON on stdin and store it")
    capture.add_argument("target", choices=_CAPTURE_TARGETS)
    capture.add_argument(
        "payload",
        nargs="?",
        help=(
            "the payload as a trailing argument. Codex's legacy `notify` program "
            "delivers its JSON this way, with stdin closed."
        ),
    )

    serve = sub.add_parser("serve", help="run the local HTTP listener for the extension")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--allow-remote", action="store_true")

    sub.add_parser("mcp", help="run the stdio MCP server")

    install = sub.add_parser("install", help="write the hook config for an agent")
    install.add_argument("target", choices=_INSTALL_TARGETS)
    install.add_argument("--dry-run", action="store_true")
    install.add_argument(
        "--legacy",
        action="store_true",
        help="codex only: use the deprecated notify program instead of hooks.json",
    )
    install.add_argument(
        "--no-trust",
        action="store_true",
        help=(
            "codex only: do not write the hook trust entries into config.toml. "
            'Codex then ignores the hooks until you start `codex` and choose "Trust all '
            'and continue".'
        ),
    )

    uninstall = sub.add_parser("uninstall", help="remove the hook config for an agent")
    uninstall.add_argument("target", choices=_INSTALL_TARGETS)

    token = sub.add_parser("token", help="print (or rotate) the listener token")
    token.add_argument("--rotate", action="store_true")

    listing = sub.add_parser("list", help="list captured prompts")
    listing.add_argument("--source")
    listing.add_argument("--since")
    listing.add_argument("--until")
    listing.add_argument("--project")
    listing.add_argument("--session-id")
    listing.add_argument("--account")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="full text search over captured prompts")
    search.add_argument("query")
    search.add_argument("--source")
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")

    stats = sub.add_parser("stats", help="prompt counts grouped by a dimension")
    stats.add_argument(
        "--group-by",
        default="source",
        choices=["source", "day", "week", "project", "account", "session"],
    )
    stats.add_argument("--since")
    stats.add_argument("--until")
    stats.add_argument("--json", action="store_true")

    time_cmd = sub.add_parser("time", help="where your time went")
    time_cmd.add_argument("--since", default="7d")
    time_cmd.add_argument("--until")
    time_cmd.add_argument(
        "--group-by",
        default="project",
        choices=["project", "source", "day", "hour_of_day", "weekday", "session"],
    )
    time_cmd.add_argument("--json", action="store_true")

    digest = sub.add_parser("digest", help="a digest of one local day")
    digest.add_argument("date", nargs="?")
    digest.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="dump captured prompts")
    export.add_argument("--format", default="jsonl", choices=["jsonl", "csv"])
    export.add_argument("--source")
    export.add_argument("--since")
    export.add_argument("--until")
    export.add_argument("--limit", type=int, default=100000)
    export.add_argument("--output", "-o")

    purge = sub.add_parser("purge", help="delete captured prompts")
    purge.add_argument("--before")
    purge.add_argument("--source")
    purge.add_argument("--yes", action="store_true")

    sub.add_parser("doctor", help="check the install")

    return parser


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load():
    from .config import load_config  # noqa: PLC0415
    from .store import Store  # noqa: PLC0415

    config = load_config()
    return config, Store(config.db_path)


def _truncate(text: str, width: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= width:
        return flat
    return flat[: max(width - 1, 0)] + "…"


def _table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    cells = [[str(c) if c is not None else "" for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for index, value in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(value))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in cells:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)).rstrip())
    return "\n".join(lines)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def _payload_text(args: argparse.Namespace) -> str:
    """The hook payload: the trailing argv argument if it is a JSON object, else stdin.

    Codex's legacy ``notify`` program receives its JSON as the final argv argument with
    stdin closed (``docs/research/hook-specs.md`` §2.5), so argv is checked first.
    """
    candidate = (getattr(args, "payload", None) or "").strip()
    if candidate.startswith("{"):
        try:
            if isinstance(json.loads(candidate), dict):
                return candidate
        except json.JSONDecodeError:
            pass
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    return raw if raw.strip() else ""


def cmd_capture(args: argparse.Namespace) -> int:
    """Never prints, never raises, always exits 0."""
    try:
        from .config import load_config, setup_logging  # noqa: PLC0415

        config = load_config()
        log = setup_logging(config.home)
        raw = _payload_text(args)
        if not raw:
            log.debug("capture %s: no payload on argv or stdin, nothing to do", args.target)
            return 0

        from .ingest import SOURCE_ALIASES, ingest  # noqa: PLC0415
        from .store import Store  # noqa: PLC0415

        source = SOURCE_ALIASES.get(str(args.target).lower())
        if source is None:
            log.warning("capture: unknown target %r", args.target)
            return 0

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("capture %s: stdin is not valid JSON", args.target)
            return 0

        store = Store(config.db_path)
        try:
            record = ingest(source, payload, config=config, store=store)
        finally:
            store.close()
        if record is not None:
            log.info("captured %s prompt %s (%d chars)", source.value, record.id, record.char_count)
    except Exception:  # noqa: BLE001 - a hook must never fail the user's turn
        try:
            from .config import get_logger  # noqa: PLC0415

            get_logger().exception("capture failed")
        except Exception:  # noqa: BLE001,S110 - nothing left to do
            pass
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .http_listener import serve  # noqa: PLC0415

    config, store = _load()
    config.get_token()
    try:
        serve(
            config,
            store,
            host=args.host,
            port=args.port,
            allow_remote=args.allow_remote,
        )
    except ValueError as exc:
        print(f"apc: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


def cmd_mcp(_args: argparse.Namespace) -> int:
    from .mcp_server import run  # noqa: PLC0415

    run()
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from .installer import install  # noqa: PLC0415

    print(install(args.target, dry_run=args.dry_run, legacy=args.legacy, trust=not args.no_trust))
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from .installer import uninstall  # noqa: PLC0415

    print(uninstall(args.target))
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    from .config import load_config  # noqa: PLC0415

    config = load_config()
    print(config.rotate_token() if args.rotate else config.get_token())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config, store = _load()
    try:
        records = store.list(
            source=args.source,
            since=args.since,
            until=args.until,
            project=args.project,
            session_id=args.session_id,
            account=args.account,
            limit=args.limit,
            offset=args.offset,
        )
        if args.json:
            _emit({"prompts": [r.to_dict() for r in records], "count": len(records)})
            return 0
        if not records:
            print("no prompts captured yet")
            return 0
        rows = [[r.ts, r.source.value, r.project or "-", _truncate(r.prompt, 68)] for r in records]
        print(_table(rows, ["ts", "source", "project", "prompt"]))
    finally:
        store.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config, store = _load()
    try:
        hits = store.search(
            args.query,
            source=args.source,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
        if args.json:
            _emit(
                {
                    "results": [{**r.to_dict(), "rank": rank} for r, rank in hits],
                    "count": len(hits),
                }
            )
            return 0
        if not hits:
            print(f"no matches for {args.query!r}")
            return 0
        rows = [[r.ts, r.source.value, f"{rank:.2f}", _truncate(r.prompt, 62)] for r, rank in hits]
        print(_table(rows, ["ts", "source", "rank", "prompt"]))
    finally:
        store.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config, store = _load()
    try:
        rows = store.stats(since=args.since, until=args.until, group_by=args.group_by)
        if args.json:
            _emit({"group_by": args.group_by, "groups": rows})
            return 0
        if not rows:
            print("no prompts captured yet")
            return 0
        print(
            _table(
                [[r["key"], r["prompt_count"], r["chars"]] for r in rows],
                [args.group_by, "prompts", "chars"],
            )
        )
    finally:
        store.close()
    return 0


def cmd_time(args: argparse.Namespace) -> int:
    from .timeline import time_summary  # noqa: PLC0415

    config, store = _load()
    try:
        summary = time_summary(
            store,
            config=config,
            since=args.since,
            until=args.until,
            group_by=args.group_by,
        )
        if args.json:
            _emit(summary)
            return 0
        groups = summary["groups"]
        if not groups:
            print("no activity in that window")
            return 0
        rows = [
            [
                g["key"],
                f"{g['active_minutes']:.0f}m",
                g["prompt_count"],
                f"{g['agent_minutes']:.0f}m",
                "-" if g["avg_think_seconds"] is None else f"{g['avg_think_seconds']:.0f}s",
            ]
            for g in groups
        ]
        print(_table(rows, [args.group_by, "active", "prompts", "agent", "avg think"]))
        print()
        print(
            f"total active: {summary['total_active_minutes']:.0f} min"
            f"   context switches: {summary['context_switches']}"
        )
    finally:
        store.close()
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from .timeline import daily_digest  # noqa: PLC0415

    config, store = _load()
    try:
        digest = daily_digest(store, config=config, date=args.date)
        if args.json:
            _emit(digest)
            return 0
        print(f"digest for {digest['date']}")
        print(
            f"  {digest['prompt_count']} prompts, {digest['active_minutes']:.0f} active minutes,"
            f" {digest['context_switches']} context switches"
        )
        print(f"  first: {digest['first_activity']}   last: {digest['last_activity']}")
        if digest["sessions"]:
            print()
            rows = [
                [
                    s["project"] or "-",
                    s["source"],
                    s["start"],
                    f"{s['active_minutes']:.0f}m",
                    s["prompt_count"],
                ]
                for s in digest["sessions"]
            ]
            print(_table(rows, ["project", "source", "start", "active", "prompts"]))
        if digest["top_terms"]:
            print()
            print("top terms: " + ", ".join(t["term"] for t in digest["top_terms"]))
    finally:
        store.close()
    return 0


_EXPORT_FIELDS = (
    "id",
    "ts",
    "source",
    "prompt",
    "prompt_hash",
    "session_id",
    "account",
    "cwd",
    "project",
    "char_count",
    "turn_end_ts",
)


def cmd_export(args: argparse.Namespace) -> int:
    config, store = _load()
    try:
        records = store.list(
            source=args.source,
            since=args.since,
            until=args.until,
            limit=args.limit,
            order="asc",
        )
        handle = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
        try:
            if args.format == "csv":
                writer = csv.DictWriter(handle, fieldnames=list(_EXPORT_FIELDS))
                writer.writeheader()
                for record in records:
                    data = record.to_dict()
                    writer.writerow({k: data.get(k) for k in _EXPORT_FIELDS})
            else:
                for record in records:
                    handle.write(json.dumps(record.to_dict(), default=str) + "\n")
        finally:
            if args.output:
                handle.close()
    finally:
        store.close()
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    config, store = _load()
    try:
        if not args.before and not args.source:
            print(
                "apc: refusing to purge everything; pass --before and/or --source",
                file=sys.stderr,
            )
            return 2
        if not args.yes:
            if not sys.stdin.isatty():
                print("apc: pass --yes to purge non-interactively", file=sys.stderr)
                return 2
            target = f"source={args.source}" if args.source else ""
            window = f"before={args.before}" if args.before else ""
            answer = input(f"delete prompts ({' '.join(x for x in (target, window) if x)})? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        removed = store.delete(source=args.source, before=args.before)
        print(f"deleted {removed} prompt(s)")
    finally:
        store.close()
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    from .config import load_config  # noqa: PLC0415
    from .installer import (  # noqa: PLC0415
        claude_settings_path,
        codex_config_path,
        codex_hooks_path,
        opencode_plugin_path,
    )
    from .store import Store  # noqa: PLC0415

    config = load_config()
    checks: list[tuple[bool, str]] = []

    checks.append((config.home.exists(), f"APC_HOME {config.home}"))
    checks.append((config.config_path.exists(), f"config.toml {config.config_path}"))

    try:
        store = Store(config.db_path)
        total = store.count()
        version = store.schema_version
        store.close()
        checks.append((True, f"database {config.db_path} (schema v{version}, {total} prompts)"))
    except Exception as exc:  # noqa: BLE001
        checks.append((False, f"database {config.db_path}: {exc}"))

    try:
        token = config.get_token()
        checks.append((bool(token), f"token {config.token_path} ({len(token)} chars)"))
    except OSError as exc:
        checks.append((False, f"token {config.token_path}: {exc}"))

    checks.append((config.log_path.exists(), f"log {config.log_path}"))
    checks.append(
        (
            bool(config.allowed_accounts),
            f"browser allowlist: {len(config.allowed_accounts)} account(s)",
        )
    )
    checks.append((_listener_alive(config), f"listener http://{config.host}:{config.port}"))

    settings = claude_settings_path()
    checks.append((_claude_hooks_installed(settings), f"claude-code hooks in {settings}"))
    hooks_json = codex_hooks_path()
    codex_hooks = _codex_hooks_installed(hooks_json)
    checks.append((codex_hooks, f"codex hooks in {hooks_json}"))
    codex_toml = codex_config_path()
    codex_trusted = _codex_hooks_trusted(codex_toml)
    if codex_hooks and codex_trusted:
        checks.append((True, f"codex hooks trusted in {codex_toml}"))
    codex_notify = _codex_notify_installed(codex_toml)
    if codex_notify:
        checks.append((True, f"codex notify (legacy) in {codex_toml}"))
    elif not codex_hooks:
        checks.append((False, f"codex hooks or notify in {codex_toml}"))
    plugin = opencode_plugin_path()
    checks.append((plugin.exists(), f"opencode plugin {plugin}"))

    for ok, label in checks:
        print(f"  {'[ok]  ' if ok else '[--]  '}{label}")

    warnings: list[str] = []
    if codex_hooks and not codex_trusted:
        warnings.append(
            "codex hooks installed but NOT trusted: run 'apc install codex' again, or "
            "start 'codex' and choose \"Trust all and continue\""
        )
    if codex_hooks and codex_notify:
        warnings.append(
            "codex is configured twice (hooks.json AND a notify line): every turn will be "
            f"captured twice and the two records will not dedupe. Remove the notify line "
            f"from {codex_toml}, or run `apc uninstall codex` and reinstall."
        )
    if not config.allowed_accounts:
        warnings.append(
            "the browser allowlist is empty, so nothing is captured from claude.ai or "
            "chatgpt.com. Add emails under [capture].allowed_accounts in config.toml."
        )
    for warning in warnings:
        print(f"  [warn] {warning}")

    print()
    print("  note: Claude Code on the web (claude.ai/code) does not run user hooks, so those")
    print("        prompts are captured as `claude_code_web` by the Chrome extension, not by")
    print("        the `apc capture claude-code` hook.")
    return 0


def _listener_alive(config: Any) -> bool:
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = f"http://{config.host}:{config.port}/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:  # noqa: S310
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _claude_hooks_installed(path: Path) -> bool:
    from .installer import CLAUDE_HOOK_EVENTS, HOOK_COMMAND  # noqa: PLC0415

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False
    return all(HOOK_COMMAND in json.dumps(hooks.get(event, [])) for event in CLAUDE_HOOK_EVENTS)


def _codex_hooks_installed(path: Path) -> bool:
    from .installer import CODEX_HOOK_COMMAND  # noqa: PLC0415

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return isinstance(hooks, dict) and CODEX_HOOK_COMMAND in json.dumps(hooks)


def _codex_hooks_trusted(config_path: Path) -> bool:
    """Does ``config.toml`` hold a matching ``trusted_hash`` for every hook we installed?

    The keys are derived from the hooks.json that is on disk right now, so a moved
    ``CODEX_HOME`` (keys pointing at the old path) reads as untrusted, which is exactly
    what Codex itself would conclude.
    """
    import tomllib  # noqa: PLC0415

    from .installer import codex_expected_trust  # noqa: PLC0415

    expected = codex_expected_trust()
    if not expected:
        return False
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    hooks = data.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    if not isinstance(state, dict):
        return False
    for key, digest in expected.items():
        entry = state.get(key)
        if not isinstance(entry, dict) or entry.get("trusted_hash") != digest:
            return False
    return True


def _codex_notify_installed(path: Path) -> bool:
    import re  # noqa: PLC0415

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"^\s*notify\s*=\s*\[\s*[\"']apc[\"']", text, re.MULTILINE))


_COMMANDS = {
    "capture": cmd_capture,
    "serve": cmd_serve,
    "mcp": cmd_mcp,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "token": cmd_token,
    "list": cmd_list,
    "search": cmd_search,
    "stats": cmd_stats,
    "time": cmd_time,
    "digest": cmd_digest,
    "export": cmd_export,
    "purge": cmd_purge,
    "doctor": cmd_doctor,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = _COMMANDS[args.command]
    if args.command == "capture":
        return cmd_capture(args)
    try:
        return handler(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except ValueError as exc:
        print(f"apc: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
