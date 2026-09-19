"""``apc serve``: a loopback HTTP listener for the Chrome extension.

Stdlib only: :class:`http.server.ThreadingHTTPServer`. No third-party web framework.
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import Config, get_logger
from .ingest import IngestResult, ingest
from .models import BROWSER_SOURCES, Source
from .store import Store

__all__ = ["make_server", "serve", "MAX_BODY_BYTES", "ALLOWED_ORIGIN_PREFIXES"]

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB
DRAIN_LIMIT_BYTES = 16 * 1024 * 1024  # how much of an oversized body we will discard
ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")

_log = get_logger()


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


class _Handler(BaseHTTPRequestHandler):
    server_version = f"apc/{__version__}"
    protocol_version = "HTTP/1.1"

    # injected by make_server
    config: Config
    store: Store

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        _log.debug("http %s - %s", self.address_string(), fmt % args)

    def _origin_allowed(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin and origin.startswith(ALLOWED_ORIGIN_PREFIXES):
            return origin
        return None

    def _cors(self) -> None:
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-APC-Token")
            self.send_header("Access-Control-Max-Age", "600")

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:  # pragma: no cover
            pass

    def _error(self, status: int, message: str) -> None:
        self._respond(status, {"error": message})

    def _authorised(self) -> bool:
        presented = self.headers.get("X-APC-Token") or ""
        try:
            expected = self.config.get_token()
        except OSError:  # pragma: no cover - unwritable home
            return False
        return hmac.compare_digest(presented.strip(), expected.strip())

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "invalid Content-Length")
            return None
        if length > MAX_BODY_BYTES:
            # Drain what the client already committed to sending before answering,
            # otherwise the response races the client's write and it sees a reset
            # instead of the 413. Then close: we are not reading the rest.
            self._drain(min(length, DRAIN_LIMIT_BYTES))
            self.close_connection = True
            self._error(413, "payload too large")
            return None
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _drain(self, length: int) -> None:
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    # -- routes --------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/v1/health":
            self._respond(200, {"ok": True, "version": __version__})
            return
        if path == "/v1/config":
            if not self._authorised():
                self._error(401, "bad token")
                return
            self._respond(
                200,
                {
                    "allowed_accounts_count": len(self.config.allowed_accounts),
                    "sources": [s.value for s in Source if not self.config.is_source_disabled(s)],
                },
            )
            return
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/v1/prompts":
            self._error(404, "not found")
            return
        if not self._authorised():
            self._error(401, "bad token")
            return

        raw = self._read_body()
        if raw is None:
            return
        if not raw:
            self._error(400, "empty body")
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid JSON")
            return
        if not isinstance(payload, dict):
            self._error(400, "expected a JSON object")
            return

        declared = payload.get("source")
        try:
            source = Source(str(declared))
        except ValueError:
            self._error(400, f"unknown source {declared!r}")
            return
        if source not in BROWSER_SOURCES:
            self._error(400, f"source {source.value!r} is not a browser source")
            return

        if self.config.is_source_disabled(source):
            self._respond(202, {"stored": False, "reason": IngestResult.SOURCE_DISABLED})
            return
        if not self.config.is_account_allowed(payload.get("account")):
            self._respond(202, {"stored": False, "reason": IngestResult.ACCOUNT_NOT_ALLOWED})
            return

        try:
            record = ingest(source, payload, config=self.config, store=self.store)
        except ValueError as exc:
            self._error(400, str(exc))
            return
        except Exception:  # pragma: no cover - never leak a traceback to the browser
            _log.exception("ingest failed")
            self._error(400, "ingest failed")
            return

        if record is None:
            self._respond(202, {"stored": False, "reason": IngestResult.DEDUPED})
            return
        self._respond(202, {"stored": True, "id": record.id})


def make_server(
    config: Config,
    store: Store,
    *,
    host: str | None = None,
    port: int | None = None,
    allow_remote: bool = False,
) -> ThreadingHTTPServer:
    """Build (but do not start) the listener. Pass ``port=0`` in tests."""
    bind_host = host if host is not None else config.host
    bind_port = config.port if port is None else int(port)

    if not _is_loopback(bind_host) and not allow_remote:
        raise ValueError(
            f"refusing to bind non-loopback address {bind_host!r}; pass --allow-remote to override"
        )

    handler = type("_BoundHandler", (_Handler,), {"config": config, "store": store})
    server = ThreadingHTTPServer((bind_host, bind_port), handler)
    server.daemon_threads = True
    return server


def serve(
    config: Config,
    store: Store,
    *,
    host: str | None = None,
    port: int | None = None,
    allow_remote: bool = False,
) -> None:
    """Run the listener until interrupted."""
    server = make_server(config, store, host=host, port=port, allow_remote=allow_remote)
    bound_host, bound_port = server.server_address[:2]
    _log.info("listening on http://%s:%s", bound_host, bound_port)
    print(f"apc: listening on http://{bound_host}:{bound_port} (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
