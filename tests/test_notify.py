"""Webhook notification after a sync (spec 5.6, R-N*)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from conftest import START, Env
from admentum_mcp.config import ConfigError, NotifyConfig, parse_config
from admentum_mcp.notify import Webhook, notify_after_sync, notify_status, sign, webhook_from_env

SECRET = "whsec_" + base64.b64encode(b"test-secret-key").decode()


def verify(secret: str, headers: httpx.Headers | dict, body: bytes) -> bool:
    """Standard Webhooks verification, written independently of notify.sign."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(p == f"v1,{expected}" for p in headers["webhook-signature"].split())


class Receiver:
    """An httpx transport that records events and answers with scripted status codes."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.statuses: list[int] = []  # consumed per request; empty means 200
        self.raise_error = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error:
            raise httpx.ConnectError("refused")
        assert verify(SECRET, request.headers, request.content)
        status = self.statuses.pop(0) if self.statuses else 200
        if 200 <= status < 300:
            self.events.append(json.loads(request.content))
        return httpx.Response(status)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


@pytest.fixture
def rx() -> Receiver:
    return Receiver()


@pytest.fixture
def hook(rx: Receiver) -> Webhook:
    return Webhook("https://hooks.example/admentum", SECRET, transport=httpx.MockTransport(rx.handler),
                   sleep=lambda s: None)


def notify_sync(env: Env, hook: Webhook):
    return env.sync(notify=lambda store, res: notify_after_sync(store, hook, res, env.clock))


def add(env: Env, thread: str, msg: str, minutes: int, **kw) -> None:
    if thread not in env.source.threads:
        env.source.add_thread(thread, f"Subject {thread}")
    env.source.add_message(thread, msg, START + timedelta(minutes=minutes), f"<p>Hemlig text {msg}</p>",
                           sender_name="Anna Lärare", **kw)


def test_new_items_are_announced_once_with_ids_only(env: Env, hook: Webhook, rx: Receiver) -> None:
    """R-N1, R-N3."""
    add(env, "1", "a", 0)
    add(env, "2", "b", 1)
    res = notify_sync(env, hook)
    assert res["notify"] == {"sent": ["items.new"], "failed": []}
    (event,) = rx.events
    assert event["type"] == "items.new" and event["new_items"] == 2
    assert sorted(event["item_ids"]) == ["a", "b"] and event["pending_total"] == 2
    assert set(event) == {"type", "new_items", "item_ids", "pending_total", "sync_run", "sent_at"}
    raw = rx.requests[0].content.decode()
    assert "Hemlig" not in raw and "Subject" not in raw and "Anna" not in raw

    env.clock.advance(minutes=15)
    assert notify_sync(env, hook)["notify"] == {"sent": [], "failed": []}  # nothing new, nothing sent
    add(env, "1", "c", 20)
    env.clock.advance(minutes=15)
    notify_sync(env, hook)
    assert rx.events[-1]["item_ids"] == ["c"] and rx.events[-1]["pending_total"] == 3


def test_own_and_noise_items_are_not_announced(env: Env, hook: Webhook, rx: Receiver) -> None:
    add(env, "1", "mine", 0, is_mine=True)
    notify_sync(env, hook)
    assert rx.events == []


def test_failed_delivery_is_retried_next_run(env: Env, hook: Webhook, rx: Receiver) -> None:
    """R-N2: the watermark moves only after a 2xx; the same event ID is reused."""
    add(env, "1", "a", 0)
    rx.statuses = [500, 500, 500]
    res = notify_sync(env, hook)
    assert res["notify"] == {"sent": [], "failed": ["items.new"]} and len(rx.requests) == 3
    first_id = rx.requests[0].headers["webhook-id"]

    env.clock.advance(minutes=15)
    add(env, "2", "b", 10)
    res = notify_sync(env, hook)
    assert res["notify"]["sent"] == ["items.new"]
    assert sorted(rx.events[0]["item_ids"]) == ["a", "b"]

    rx2 = Receiver()
    rx2.statuses = [500, 500, 500]
    env.clock.advance(minutes=15)
    add(env, "3", "c", 20)
    notify_sync(env, replace(hook, transport=httpx.MockTransport(rx2.handler)))
    notify_sync(env, replace(hook, transport=httpx.MockTransport(rx2.handler)))
    assert rx2.requests[0].headers["webhook-id"] == rx2.requests[3].headers["webhook-id"] != first_id


def test_client_errors_are_not_retried_and_connection_errors_are(env: Env, hook: Webhook, rx: Receiver) -> None:
    add(env, "1", "a", 0)
    rx.statuses = [401]
    assert notify_sync(env, hook)["notify"]["failed"] == ["items.new"] and len(rx.requests) == 1
    rx.raise_error = True
    env.clock.advance(minutes=1)
    assert notify_sync(env, hook)["notify"]["failed"] == ["items.new"] and len(rx.requests) == 4


def test_a_sync_without_notify_marks_items_announced(env: Env, hook: Webhook, rx: Receiver) -> None:
    """R-N2: the agent's own sync (MCP tool, plain CLI) suppresses the webhook for what it fetched."""
    add(env, "1", "a", 0)
    env.sync()
    env.clock.advance(minutes=15)
    notify_sync(env, hook)
    assert rx.events == []
    add(env, "1", "b", 5)
    env.clock.advance(minutes=15)
    notify_sync(env, hook)
    assert rx.events[0]["item_ids"] == ["b"]


def test_seen_items_are_not_announced(env: Env, hook: Webhook, rx: Receiver) -> None:
    add(env, "1", "a", 0)
    env.sync(notify=lambda store, res: {"sent": [], "failed": []})  # a notifying run whose POST was lost
    env.mark_all_seen()
    notify_sync(env, hook)
    assert rx.events == []


def test_dry_run_and_already_running_send_nothing(env: Env, hook: Webhook, rx: Receiver) -> None:
    from filelock import FileLock

    add(env, "1", "a", 0)
    assert "notify" not in env.sync(dry_run=True, notify=lambda s, r: pytest.fail("called"))
    env.data.ensure()
    with FileLock(str(env.data.lock)):
        assert notify_sync(env, hook) == {"result": "already_running"}
    assert rx.requests == []


def test_sync_failure_is_reported_once_then_recovery(env: Env, hook: Webhook, rx: Receiver) -> None:
    """R-N6."""
    add(env, "1", "a", 0)
    notify_sync(env, hook)
    env.source.fail_login = True
    for _ in range(3):
        env.clock.advance(minutes=15)
        assert notify_sync(env, hook)["result"] == "error"
    assert rx.types() == ["items.new", "sync.failed"]
    assert rx.events[1]["error"] == "AuthError" and set(rx.events[1]) == {"type", "error", "sync_run", "sent_at"}

    env.source.fail_login = False
    add(env, "1", "b", 60)
    env.clock.advance(minutes=15)
    notify_sync(env, hook)
    assert rx.types() == ["items.new", "sync.failed", "sync.recovered", "items.new"]
    env.clock.advance(minutes=15)
    notify_sync(env, hook)
    assert len(rx.events) == 4


def test_undelivered_failure_event_is_retried(env: Env, hook: Webhook, rx: Receiver) -> None:
    env.source.fail_login = True
    rx.statuses = [503, 503, 503]
    assert notify_sync(env, hook)["notify"]["failed"] == ["sync.failed"]
    env.clock.advance(minutes=15)
    assert notify_sync(env, hook)["notify"]["sent"] == ["sync.failed"]
    env.clock.advance(minutes=15)
    assert notify_sync(env, hook)["notify"] == {"sent": [], "failed": []}


def test_status_reports_notification_state_without_the_url(env: Env, hook: Webhook, rx: Receiver) -> None:
    env.cfg = replace(env.cfg, notify=NotifyConfig(webhook_url="https://secret-host.example/x"))
    add(env, "1", "a", 0)
    env.source.fail_login = True
    notify_sync(env, hook)
    s = env.store()
    try:
        st = notify_status(env.cfg, s)
    finally:
        s.close()
    assert st["configured"] is True and st["last"]["event"] == "sync.failed" and st["last"]["result"] == "sent"
    assert "sync_failure_reported_since" in st and "secret-host" not in json.dumps(st)


def test_signature_matches_standard_webhooks_reference() -> None:
    """Example from the Standard Webhooks reference implementation."""
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    body = b'{"test": 2432232314}'
    assert sign(secret, "msg_p5jXN8AQM9LWM0D4loKWxJek", "1614265330", body) == \
        "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    raw = sign("plain-secret", "id", "1", b"{}")
    expected = base64.b64encode(hmac.new(b"plain-secret", b"id.1.{}", hashlib.sha256).digest()).decode()
    assert raw == f"v1,{expected}"


@pytest.mark.parametrize("url", ["ftp://x/y", "http://hooks.example/x", "http://10.0.0.5:8644/x", "nonsense"])
def test_webhook_url_must_be_https_or_loopback(url: str) -> None:
    with pytest.raises(ConfigError, match="notify.webhook_url"):
        parse_config({"notify": {"webhook_url": url}})


@pytest.mark.parametrize("url", ["https://hooks.example/x", "http://127.0.0.1:8644/webhooks/a",
                                 "http://localhost:8644/x", "http://[::1]:8644/x"])
def test_accepted_webhook_urls(url: str) -> None:
    assert parse_config({"notify": {"webhook_url": url}}).notify.webhook_url == url


def test_webhook_needs_url_and_secret(env: Env, monkeypatch) -> None:
    monkeypatch.delenv("ADMENTUM_WEBHOOK_SECRET", raising=False)
    with pytest.raises(ConfigError, match="webhook_url"):
        webhook_from_env(env.cfg)
    cfg = replace(env.cfg, notify=NotifyConfig(webhook_url="https://hooks.example/x"))
    with pytest.raises(ConfigError, match="ADMENTUM_WEBHOOK_SECRET"):
        webhook_from_env(cfg)
    monkeypatch.setenv("ADMENTUM_WEBHOOK_SECRET", "whsec_not base64!")
    with pytest.raises(ConfigError, match="whsec_"):
        webhook_from_env(cfg)
    monkeypatch.setenv("ADMENTUM_WEBHOOK_SECRET", SECRET)
    assert "test-secret" not in repr(webhook_from_env(cfg))


# the CLI against a real local HTTP receiver


class _Handler(BaseHTTPRequestHandler):
    received: list[tuple[dict, bytes]] = []
    status = 202

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server():
    _Handler.received = []
    _Handler.status = 202
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _cli_config(tmp_path: Path, url: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path / "data"}"\n[sync]\nthread_delay_ms = 0\n'
                    f'[notify]\nwebhook_url = "{url}"\ntimeout_seconds = 5\n')
    return path


def test_cli_sync_and_notify(tmp_path: Path, server, monkeypatch, capsys) -> None:
    from admentum_mcp.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMENTUM_WEBHOOK_SECRET", SECRET)
    url = f"http://127.0.0.1:{server.server_port}/webhooks/admentum"
    cfg = _cli_config(tmp_path, url)
    assert main(["--config", str(cfg), "--source", "fake", "sync-and-notify"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["result"] == "ok" and out["notify"] == {"sent": ["items.new"], "failed": []}
    ((headers, body),) = _Handler.received
    assert verify(SECRET, headers, body) and json.loads(body)["new_items"] == out["pending_total"]

    assert main(["--config", str(cfg), "--source", "fake", "sync-and-notify"]) == 0  # nothing new
    assert len(_Handler.received) == 1

    _Handler.status = 400
    assert main(["--config", str(cfg), "--source", "fake", "reset", "--yes"]) == 0
    assert main(["--config", str(cfg), "--source", "fake", "sync-and-notify"]) == 1  # delivery failed


def test_cli_sync_and_notify_config_errors_exit_2(tmp_path: Path, monkeypatch, capsys) -> None:
    from admentum_mcp.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADMENTUM_WEBHOOK_SECRET", raising=False)
    cfg = _cli_config(tmp_path, "https://hooks.example/x")
    assert main(["--config", str(cfg), "--source", "fake", "sync-and-notify"]) == 2
    assert "ADMENTUM_WEBHOOK_SECRET" in capsys.readouterr().err
    assert not (tmp_path / "data" / "state.db").exists()  # refused before syncing
