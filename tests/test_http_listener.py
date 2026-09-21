"""HTTP listener: auth, CORS, payload validation, limits."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_prompt_capture.config import Config
from agent_prompt_capture.http_listener import MAX_BODY_BYTES, make_server

BROWSER = {
    "source": "claude_web",
    "prompt": "hello from the browser",
    "account": "me@work.com",
    "conversation_id": "conv-1",
    "url": "https://claude.ai/chat/uuid?x=1",
    "title": "a title",
}


@pytest.fixture
def server(apc_home, store):
    config = Config(home=apc_home, allowed_accounts=["me@work.com"])
    httpd = make_server(config, store, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield {
        "url": f"http://{host}:{port}",
        "token": config.get_token(),
        "config": config,
        "store": store,
        "httpd": httpd,
    }
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def request(server, path, *, method="GET", body=None, token=None, origin=None, headers=None):
    if isinstance(body, bytes):
        data = body
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(server["url"] + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-APC-Token", token)
    if origin:
        req.add_header("Origin", origin)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # noqa: S310
            payload = response.read()
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_health_needs_no_auth(server):
    status, _, body = request(server, "/v1/health")
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert json.loads(body)["version"]


def test_post_stores_a_prompt(server):
    status, _, body = request(
        server, "/v1/prompts", method="POST", body=BROWSER, token=server["token"]
    )
    assert status == 202
    payload = json.loads(body)
    assert payload["stored"] is True
    assert server["store"].get(payload["id"]) is not None


def test_post_requires_the_token(server):
    status, _, body = request(server, "/v1/prompts", method="POST", body=BROWSER)
    assert status == 401
    assert "error" in json.loads(body)


def test_post_rejects_a_wrong_token(server):
    status, _, _ = request(server, "/v1/prompts", method="POST", body=BROWSER, token="nope")
    assert status == 401


def test_account_not_allowed(server):
    payload = {**BROWSER, "account": "stranger@example.com"}
    status, _, body = request(
        server, "/v1/prompts", method="POST", body=payload, token=server["token"]
    )
    assert status == 202
    assert json.loads(body) == {"stored": False, "reason": "account_not_allowed"}


def test_deduped(server):
    request(server, "/v1/prompts", method="POST", body=BROWSER, token=server["token"])
    status, _, body = request(
        server, "/v1/prompts", method="POST", body=BROWSER, token=server["token"]
    )
    assert status == 202
    assert json.loads(body) == {"stored": False, "reason": "deduped"}


def test_source_disabled(apc_home, store):
    config = Config(
        home=apc_home, allowed_accounts=["me@work.com"], disabled_sources=["claude_web"]
    )
    httpd = make_server(config, store, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        ctx = {"url": f"http://{host}:{port}", "token": config.get_token()}
        status, _, body = request(
            ctx, "/v1/prompts", method="POST", body=BROWSER, token=ctx["token"]
        )
        assert status == 202
        assert json.loads(body) == {"stored": False, "reason": "source_disabled"}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_bad_json(server):
    status, _, body = request(
        server, "/v1/prompts", method="POST", body=b"{not json", token=server["token"]
    )
    assert status == 400


def test_empty_body(server):
    status, _, _ = request(server, "/v1/prompts", method="POST", body=b"", token=server["token"])
    assert status == 400


def test_non_object_body(server):
    status, _, _ = request(
        server, "/v1/prompts", method="POST", body=[1, 2, 3], token=server["token"]
    )
    assert status == 400


def test_unknown_source(server):
    status, _, _ = request(
        server,
        "/v1/prompts",
        method="POST",
        body={**BROWSER, "source": "banana"},
        token=server["token"],
    )
    assert status == 400


def test_cli_source_is_rejected(server):
    status, _, _ = request(
        server,
        "/v1/prompts",
        method="POST",
        body={**BROWSER, "source": "claude_code"},
        token=server["token"],
    )
    assert status == 400


def test_payload_too_large(server):
    oversized = json.dumps({**BROWSER, "prompt": "x" * (MAX_BODY_BYTES + 100)}).encode()
    status, _, _ = request(
        server, "/v1/prompts", method="POST", body=oversized, token=server["token"]
    )
    assert status == 413


def test_cors_allows_extension_origins(server):
    origin = "chrome-extension://abcdefghijklmnop"
    status, headers, _ = request(server, "/v1/health", origin=origin)
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") == origin


def test_cors_rejects_other_origins(server):
    status, headers, _ = request(server, "/v1/health", origin="https://evil.example.com")
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers


def test_options_preflight(server):
    origin = "chrome-extension://abcdefghijklmnop"
    status, headers, _ = request(server, "/v1/prompts", method="OPTIONS", origin=origin)
    assert status == 204
    assert headers.get("Access-Control-Allow-Origin") == origin
    assert "X-APC-Token" in headers.get("Access-Control-Allow-Headers", "")


def test_config_endpoint_requires_auth_and_hides_emails(server):
    status, _, _ = request(server, "/v1/config")
    assert status == 401
    status, _, body = request(server, "/v1/config", token=server["token"])
    assert status == 200
    payload = json.loads(body)
    assert payload["allowed_accounts_count"] == 1
    assert "me@work.com" not in body.decode()
    assert "claude_web" in payload["sources"]


def test_unknown_routes(server):
    assert request(server, "/nope")[0] == 404
    assert request(server, "/nope", method="POST", token=server["token"])[0] == 404


def test_refuses_non_loopback_bind(apc_home, store):
    config = Config(home=apc_home)
    with pytest.raises(ValueError, match="loopback"):
        make_server(config, store, host="0.0.0.0", port=0)  # noqa: S104


def test_allow_remote_override(apc_home, store):
    config = Config(home=apc_home)
    httpd = make_server(config, store, host="127.0.0.1", port=0, allow_remote=True)
    httpd.server_close()


# ---------------------------------------------------------------------------
# regressions: DNS rebinding, token handling
# ---------------------------------------------------------------------------


def raw_request(server, raw: bytes) -> tuple[int, bytes]:
    """Speak HTTP by hand so we can forge the Host header urllib always sets."""
    import socket

    host, port = server["httpd"].server_address[:2]
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(raw)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\r\n\r\n" in b"".join(chunks) and len(b"".join(chunks)) > 40:
                break
    response = b"".join(chunks)
    status = int(response.split(b" ", 2)[1])
    return status, response


def forge(server, host_header: str, *, method="GET", path="/v1/health", token=None) -> int:
    headers = [f"{method} {path} HTTP/1.1", f"Host: {host_header}", "Connection: close"]
    if token:
        headers.append(f"X-APC-Token: {token}")
    body = b""
    if method == "POST":
        body = json.dumps(BROWSER).encode()
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode() + body
    return raw_request(server, raw)[0]


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",
        "attacker.test:47821",
        "apc.localhost.evil.com",
        "localhost.evil.com",
        "127.0.0.1.evil.com",
        "0.0.0.0",
        "192.168.1.10:47821",
        "",
    ],
)
def test_a_foreign_host_header_is_rejected(server, host):
    """DNS rebinding: the socket is reachable from any page whose name resolves here."""
    assert forge(server, host) == 403
    assert forge(server, host, method="POST", path="/v1/prompts", token=server["token"]) == 403
    assert forge(server, host, method="OPTIONS", path="/v1/prompts") == 403
    assert server["store"].count() == 0


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:47821",
        "localhost",
        "localhost:47821",
        "[::1]",
        "[::1]:47821",
        "127.0.0.2:9",
        "LOCALHOST",
    ],
)
def test_our_own_names_are_accepted(server, host):
    assert forge(server, host) == 200


def test_a_missing_host_header_is_rejected(server):
    status, _ = raw_request(server, b"GET /v1/health HTTP/1.1\r\nConnection: close\r\n\r\n")
    assert status == 403


def test_allow_remote_accepts_the_configured_host(apc_home, store):
    """`--allow-remote` is an explicit opt-in, so the bound address is a valid name."""
    from agent_prompt_capture.http_listener import host_allowed

    assert host_allowed("10.1.2.3:47821", extra="10.1.2.3")
    assert host_allowed("10.1.2.3", extra="10.1.2.3")
    assert not host_allowed("10.1.2.4", extra="10.1.2.3")
    assert not host_allowed("evil.example", extra="10.1.2.3")
    assert not host_allowed(None, extra="10.1.2.3")


def test_the_token_comparison_is_constant_time():
    """`hmac.compare_digest`, on bytes: a non-ASCII header must not raise."""
    import inspect

    from agent_prompt_capture import http_listener

    source = inspect.getsource(http_listener._Handler._authorised)
    assert "hmac.compare_digest" in source
    assert "==" not in source.split("compare_digest")[0].split("def ")[1]


def test_a_non_ascii_token_header_is_a_401_not_a_crash(server):
    """Header values decode as latin-1, and compare_digest rejects non-ASCII str."""
    status, _ = raw_request(
        server,
        "POST /v1/prompts HTTP/1.1\r\nHost: 127.0.0.1\r\nX-APC-Token: tökén\r\n"
        "Content-Length: 2\r\nConnection: close\r\n\r\n{}".encode("latin-1"),
    )
    assert status == 401
    assert request(server, "/v1/health")[0] == 200, "the server survived"


def test_a_missing_token_file_yields_401(server, apc_home):
    """Deleting the token mid-run must answer 401, never a traceback."""
    (apc_home / "token").unlink()
    status, _, _ = request(
        server, "/v1/prompts", method="POST", body=BROWSER, token=server["token"]
    )
    assert status == 401
    assert request(server, "/v1/health")[0] == 200


def test_an_unreadable_token_yields_401(server, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(server["config"], "get_token", boom)
    status, _, _ = request(
        server, "/v1/prompts", method="POST", body=BROWSER, token=server["token"]
    )
    assert status == 401


def test_cors_still_only_answers_extension_origins(server):
    for origin in ("https://claude.ai", "https://evil.example", "null", "http://localhost:3000"):
        _, headers, _ = request(server, "/v1/health", origin=origin)
        assert "Access-Control-Allow-Origin" not in headers, origin
    for origin in ("chrome-extension://abc", "moz-extension://abc"):
        _, headers, _ = request(server, "/v1/health", origin=origin)
        assert headers.get("Access-Control-Allow-Origin") == origin
