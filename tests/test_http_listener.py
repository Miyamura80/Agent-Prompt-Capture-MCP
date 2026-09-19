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
