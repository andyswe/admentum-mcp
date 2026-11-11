"""Webhook notification after a sync (spec 5.6).

Events carry IDs and counts only, never message content (R-N3), and are signed per the
Standard Webhooks spec (R-N4). Delivery is at least once: the watermark moves only after a 2xx (R-N2).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config, ConfigError
from .store import Store, to_iso, utcnow

log = logging.getLogger(__name__)

SECRET_ENV = "ADMENTUM_WEBHOOK_SECRET"
MAX_IDS = 100
RETRY_ATTEMPTS = 3
META_THROUGH = "notify_through"  # newest downloaded_at already announced
META_FAILING = "notify_sync_failing_since"  # set once sync.failed was delivered
META_LAST = "notify_last"  # JSON: at, event, result


@dataclass
class Webhook:
    url: str
    secret: str
    timeout: float = 10
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time

    def __repr__(self) -> str:
        return "Webhook(<redacted>)"

    def deliver(self, msg_id: str, payload: dict[str, Any]) -> str | None:
        """POST one event. Returns None on a 2xx, else a short error (never the URL or the secret)."""
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        error = "not sent"
        with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                ts = str(int(self.now()))
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "admentum-mcp",
                    "webhook-id": msg_id,
                    "webhook-timestamp": ts,
                    "webhook-signature": sign(self.secret, msg_id, ts, body),
                }
                try:
                    r = client.post(self.url, content=body, headers=headers)
                except httpx.HTTPError as e:
                    error, retry = type(e).__name__, True
                else:
                    if 200 <= r.status_code < 300:
                        log.info("webhook delivered event=%s status=%d attempt=%d", payload["type"], r.status_code, attempt)
                        return None
                    error, retry = f"http {r.status_code}", r.status_code == 429 or r.status_code >= 500
                log.warning("webhook failed event=%s error=%s attempt=%d", payload["type"], error, attempt)
                if not retry or attempt == RETRY_ATTEMPTS:
                    break
                self.sleep(2 ** (attempt - 1))
        return error


def webhook_from_env(cfg: Config) -> Webhook:
    """The configured webhook; the secret comes only from the environment (R-N5)."""
    if not cfg.notify.webhook_url:
        raise ConfigError("notify.webhook_url is not set")
    secret = os.environ.get(SECRET_ENV, "")
    if not secret.strip():
        raise ConfigError(f"missing environment variable: {SECRET_ENV}")
    sign(secret, "check", "0", b"")  # a malformed whsec_ secret fails here, before syncing
    return Webhook(cfg.notify.webhook_url, secret, timeout=cfg.notify.timeout_seconds)


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """Standard Webhooks: "v1," + base64 HMAC-SHA256 of "{id}.{timestamp}.{body}".

    A "whsec_" secret is base64-decoded to the key; any other secret is used as raw bytes.
    """
    key = secret.encode()
    if secret.startswith("whsec_"):
        try:
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        except binascii.Error:
            raise ConfigError(f"{SECRET_ENV}: invalid whsec_ secret") from None
    mac = hmac.new(key, msg_id.encode() + b"." + timestamp.encode() + b"." + body, hashlib.sha256)
    return "v1," + base64.b64encode(mac.digest()).decode()


def advance(store: Store) -> None:
    """Mark everything downloaded so far as announced (a sync that does not notify, R-N2)."""
    newest = store.conn.execute("SELECT MAX(downloaded_at) FROM items").fetchone()[0]
    if newest is not None:
        with store.tx():
            store.set_meta(META_THROUGH, newest)


def notify_after_sync(
    store: Store, hook: Webhook, result: dict[str, Any], clock: Callable[[], Any] = utcnow
) -> dict[str, list[str]]:
    """Send the events this sync calls for (R-N1, R-N6). Runs under the sync lock."""
    sent: list[str] = []
    failed: list[str] = []
    now_iso = to_iso(clock())
    run = store.conn.execute("SELECT MAX(id) FROM sync_runs").fetchone()[0]
    failing_since = store.get_meta(META_FAILING)
    sync_ok = result.get("result") in ("ok", "partial")

    def send(event: str, msg_id: str, payload: dict[str, Any]) -> bool:
        payload = {"type": event, **payload, "sync_run": run, "sent_at": now_iso}
        error = hook.deliver(msg_id, payload)
        (failed if error else sent).append(event)
        with store.tx():
            store.set_meta(META_LAST, json.dumps(
                {"at": now_iso, "event": event, "result": error or "sent"}, separators=(",", ":")))
        return error is None

    if sync_ok and failing_since is not None:
        if send("sync.recovered", f"admentum-sync-recovered-{run}", {"failing_since": failing_since}):
            with store.tx():
                store.conn.execute("DELETE FROM meta WHERE key = ?", (META_FAILING,))

    through = store.get_meta(META_THROUGH)
    rows = store.conn.execute(
        """SELECT id, downloaded_at FROM items
           WHERE seen_at IS NULL AND downloaded_at IS NOT NULL AND pruned_at IS NULL AND downloaded_at > ?
           ORDER BY downloaded_at, id""",
        (through or "",),
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        digest = hashlib.sha256("\x1f".join(ids).encode()).hexdigest()[:32]
        payload: dict[str, Any] = {"new_items": len(ids), "item_ids": ids[:MAX_IDS],
                                   "pending_total": store.pending_count()}
        if send("items.new", f"admentum-items-{digest}", payload):
            with store.tx():
                store.set_meta(META_THROUGH, rows[-1]["downloaded_at"])

    if result.get("result") == "error" and failing_since is None:
        error_type = (result.get("error") or "error").split(":", 1)[0]  # class name only, never the message
        if send("sync.failed", f"admentum-sync-failed-{run}", {"error": error_type}):
            with store.tx():
                store.set_meta(META_FAILING, now_iso)

    return {"sent": sent, "failed": failed}


def notify_status(cfg: Config, store: Store) -> dict[str, Any]:
    """For `status`: never the URL."""
    out: dict[str, Any] = {"configured": bool(cfg.notify.webhook_url)}
    last = store.get_meta(META_LAST)
    if last:
        out["last"] = json.loads(last)
    failing = store.get_meta(META_FAILING)
    if failing:
        out["sync_failure_reported_since"] = failing
    return out
