"""Admentum connector: the only Admentum-specific module (spec 9).

Mimics the web app's internal calls. Read-only: besides login and token requests it only
makes GET requests. Logs and errors carry IDs, status codes and paths, never content,
cookie values, tokens or credentials.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .config import Config, Credentials
from .source import (
    AttachmentFile,
    AttachmentTooLarge,
    AuthError,
    Message,
    ProtocolError,
    SessionError,
    SourceError,
    ThreadSummary,
)

log = logging.getLogger(__name__)

AUTH = "https://auth.admentum.se"
SKOLA = "https://skola.admentum.se"
FILES = "https://files.admentum.se"
MESSAGES = "https://messages.admentum.se/api/users"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) admentum-mcp"

TOKEN_MAX_AGE_S = 45 * 60
LIST_PER_PAGE = 100
THREAD_PER_PAGE = 50
MAX_PAGES = 1000
RETRY_ATTEMPTS = 3
FILES_MIN_INTERVAL_S = 0.5  # the files host rate-limits bursts with 403
_CSRF = re.compile(r'csrfmiddlewaretoken" value="([^"]+)"')
_CURRENT_USER = re.compile(r"""["']?current_user_id["']?\s*[:=]\s*["']?(\d+)""")


class AdmentumSource:
    """SchoolSource for Admentum. One instance per sync; call close() when done."""

    def __init__(
        self,
        cfg: Config,
        creds: Credentials,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._creds = creds
        self._user_id: str | None = None  # learned at login (current_user_id on the app page)
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
        )
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_at = 0.0
        self._files_last = float("-inf")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AdmentumSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # SchoolSource

    def list_threads(self) -> list[ThreadSummary]:
        out: list[ThreadSummary] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._api_json(f"conversations?per_page={LIST_PER_PAGE}&page={page}")
            batch = body.get("conversations")
            if not isinstance(batch, list):
                raise ProtocolError("conversation list: missing 'conversations'")
            for c in batch:
                out.append(self._summary(c))
            more = body.get("has_next_page")
            if not (more if isinstance(more, bool) else len(batch) >= LIST_PER_PAGE):
                return out
        raise ProtocolError("conversation list: too many pages")

    def get_thread(self, thread_id: str) -> list[Message]:
        msgs: list[Message] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._api_json(f"conversations/{quote(thread_id, safe='')}?page={page}&per_page={THREAD_PER_PAGE}")
            conv = body.get("conversation")
            if not isinstance(conv, dict) or not isinstance(conv.get("messages"), list):
                raise ProtocolError(f"conversation {thread_id}: missing 'conversation.messages'")
            batch = conv["messages"]
            msgs.extend(self._message(thread_id, m) for m in batch)
            if len(batch) < THREAD_PER_PAGE:
                return msgs
        raise ProtocolError(f"conversation {thread_id}: too many pages")

    def download_attachment(self, ref: str, dest_dir: Path, max_bytes: int) -> AttachmentFile:
        meta = self._files_meta(ref)
        filename = meta.get("originalName") if isinstance(meta.get("originalName"), str) else None
        mime = meta.get("mimetype") if isinstance(meta.get("mimetype"), str) else None
        size = meta.get("size")
        if isinstance(size, int) and size > max_bytes:
            raise AttachmentTooLarge(f"{size} bytes")
        mime = mime or "application/octet-stream"
        path = dest_dir / "download"
        for attempt in (1, 2):
            written = self._stream_file(ref, path, mime, max_bytes)
            if written is not None:
                return AttachmentFile(filename or ref, mime, written, path)
            if attempt == 1:
                log.info("files host returned HTML; logging in again ref=%s", ref)
                self._relogin()
        # R-X8: an HTML page instead of the file, most likely an expired session.
        raise SourceError(f"attachment {ref}: got an HTML page instead of the file")

    # mapping

    def _summary(self, c: Any) -> ThreadSummary:
        if not isinstance(c, dict) or not isinstance(c.get("id"), (str, int)):
            raise ProtocolError("conversation list: entry without id")
        latest = c.get("latest_message_at") or (c.get("latest_message") or {}).get("created_at")
        title = c.get("title")
        return ThreadSummary(
            id=str(c["id"]),
            title=title if isinstance(title, str) else None,
            last_activity_at=_parse_time(latest) if latest else None,
            reply_count=None,
            unread=c.get("is_unread") if isinstance(c.get("is_unread"), bool) else None,
            raw=c,
            groups=_group_names(c.get("json_recipients")),
        )

    def _message(self, thread_id: str, m: Any) -> Message:
        if not isinstance(m, dict) or not m.get("created_at"):
            raise ProtocolError(f"conversation {thread_id}: message without created_at")
        creator = m.get("creator") if isinstance(m.get("creator"), dict) else {}
        name = creator.get("name") or " ".join(
            p for p in (creator.get("first_name"), creator.get("last_name")) if isinstance(p, str) and p
        )
        sender_id = str(creator["id"]) if creator.get("id") is not None else None
        meta_id = m.get("meta_id")
        content = m.get("content")
        return Message(
            id=str(m["id"]) if m.get("id") else None,
            sent_at=_parse_time(m["created_at"]),
            sender_id=sender_id,
            sender_name=name or "Unknown",
            is_mine=sender_id == self._user_id,
            html=content if isinstance(content, str) else "",
            attachment_refs=[meta_id] if isinstance(meta_id, str) and meta_id else [],
            raw=m,
        )

    # session (spec 9.1)

    def _login(self) -> None:
        """Start where a browser does: the app redirects to the login page, whose `next`
        parameter carries the OpenID request (including the app's client_id)."""
        self._client.cookies.clear()
        self._token = None
        r = self._request("GET", f"{SKOLA}/messages/")
        m = _CSRF.search(r.text)
        next_param = parse_qs(urlsplit(str(r.url)).query).get("next", [None])[0]
        if r.status_code != 200 or not m or not next_param:
            raise ProtocolError(
                f"login page: status {r.status_code}, CSRF token {'found' if m else 'missing'}, "
                f"next {'found' if next_param else 'missing'}"
            )
        post_url = str(r.url)
        r = self._request(
            "POST",
            post_url,
            data={
                "csrfmiddlewaretoken": m.group(1),
                "username": self._creds.username,
                "password": self._creds.password,
                "next": next_param,
            },
            headers={"Referer": post_url},
            retry=False,
        )
        form_again = 'name="password"' in r.text and "csrfmiddlewaretoken" in r.text
        if form_again or not self._cookie("sessionid"):
            raise AuthError("login rejected")
        r = self._request("GET", f"{SKOLA}/messages/")
        uid = _CURRENT_USER.search(r.text)
        if not uid:
            raise ProtocolError("app page: current_user_id not found")
        self._user_id = uid.group(1)
        log.info("logged in")

    def _renew_token(self) -> None:
        csrf = self._cookie("csrftoken", "skola.admentum.se") or self._cookie("csrftoken")
        if not csrf:
            raise SessionError("no csrftoken cookie")
        r = self._request(
            "POST",
            f"{SKOLA}/user_token/",
            content=f"csrfmiddlewaretoken={csrf}",
            headers={
                "X-CSRFToken": csrf,
                "Referer": f"{SKOLA}/messages/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        token = None
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            try:
                data = r.json().get("data")
                token = data.get("id_token") if isinstance(data, dict) else None
            except ValueError:
                pass
        if not isinstance(token, str) or not token:
            raise SessionError(f"user_token: status {r.status_code}, no id_token")
        self._token = token
        self._token_at = self._clock()

    def _ensure_token(self) -> None:
        if self._token is None:
            self._login()
            self._renew_token()
        elif self._clock() - self._token_at > TOKEN_MAX_AGE_S:
            self._refresh_session()

    def _refresh_session(self) -> None:
        """Renew the token; if that fails, log in again; if that fails, SessionError (9.1 step 5)."""
        try:
            self._renew_token()
            return
        except SessionError:
            log.info("token renewal failed; logging in again")
        self._relogin()

    def _relogin(self) -> None:
        try:
            self._login()
            self._renew_token()
        except AuthError as e:
            raise SessionError(f"re-login failed: {e}") from None

    def _cookie(self, name: str, domain: str | None = None) -> str | None:
        for ck in self._client.cookies.jar:
            if ck.name == name and (domain is None or ck.domain.lstrip(".") == domain):
                return ck.value
        return None

    # HTTP

    def _api_json(self, path: str) -> dict[str, Any]:
        self._ensure_token()
        url = f"{MESSAGES}/{self._user_id}/{path}"  # _ensure_token has logged in
        for step in ("first", "renewed", "relogged"):
            r = self._request("GET", url, headers={"User-Info": self._token or ""})
            if r.status_code not in (401, 403):
                break
            log.info("messages API returned %d after %s attempt", r.status_code, step)
            if step == "first":
                self._refresh_session()
            elif step == "renewed":
                self._relogin()
            else:
                raise SessionError(f"messages API: status {r.status_code} after logging in again")
        if r.status_code != 200:
            raise SourceError(f"messages API {_path(path)}: status {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise ProtocolError(f"messages API {_path(path)}: not JSON") from None
        if not isinstance(body, dict):
            raise ProtocolError(f"messages API {_path(path)}: not an object")
        return body

    def _files_meta(self, ref: str) -> dict[str, Any]:
        self._ensure_token()
        for attempt in (1, 2):
            self._pace_files()
            r = self._request("GET", f"{FILES}/meta", params={"uuid": ref})
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                try:
                    body = r.json()
                except ValueError:
                    body = None
                if isinstance(body, dict):
                    return body
                raise ProtocolError(f"files meta {ref}: not a JSON object")
            # Only a 401 or a login page served with 200 means an expired session. A 403 is rate
            # limiting, and a 404 (an HTML error page) is a deleted file: neither needs a new login.
            expired = r.status_code == 401 or (r.status_code == 200 and _looks_html(r.headers, r.content[:512]))
            if attempt == 1 and expired:
                log.info("files meta returned %d; logging in again ref=%s", r.status_code, ref)
                self._relogin()
                continue
            raise SourceError(f"files meta {ref}: status {r.status_code}")
        raise SourceError(f"files meta {ref}: failed")  # pragma: no cover

    def _stream_file(self, ref: str, path: Path, mime: str, max_bytes: int) -> int | None:
        """Stream into `path`. Returns the size, or None if the response was an HTML page."""
        expect_html = "html" in mime.lower()
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            self._pace_files()
            try:
                with self._client.stream("GET", f"{FILES}/get", params={"uuid": ref}) as r:
                    if _retryable(r.status_code) and attempt < RETRY_ATTEMPTS:
                        self._backoff(attempt)
                        continue
                    if not 200 <= r.status_code < 300:
                        raise SourceError(f"files get {ref}: status {r.status_code}")
                    size = 0
                    first = b""
                    with path.open("wb") as f:
                        for chunk in r.iter_bytes():
                            if not first:
                                first = chunk[:512]
                            size += len(chunk)
                            if size > max_bytes:
                                raise AttachmentTooLarge(f"over {max_bytes} bytes")
                            f.write(chunk)
                    if not expect_html and _looks_html(r.headers, first):
                        path.unlink(missing_ok=True)
                        return None
                    return size
            except httpx.TransportError as e:
                if attempt == RETRY_ATTEMPTS:
                    raise SourceError(f"files get {ref}: {type(e).__name__}") from None
                self._backoff(attempt)
        raise SourceError(f"files get {ref}: failed after retries")

    def _request(self, method: str, url: str, *, retry: bool = True, **kw: Any) -> httpx.Response:
        """One request with retries on connection errors, 429 and 5xx (spec 7.3)."""
        attempts = RETRY_ATTEMPTS if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                r = self._client.request(method, url, **kw)
            except httpx.TransportError as e:
                if attempt == attempts:
                    raise SourceError(f"{method} {_host_path(url)}: {type(e).__name__}") from None
                self._backoff(attempt)
                continue
            if _retryable(r.status_code) and attempt < attempts:
                self._backoff(attempt)
                continue
            return r
        raise SourceError(f"{method} {_host_path(url)}: failed after retries")  # pragma: no cover

    def _pace_files(self) -> None:
        wait = self._files_last + FILES_MIN_INTERVAL_S - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._files_last = self._clock()

    def _backoff(self, attempt: int) -> None:
        self._sleep(2 ** (attempt - 1))


def _group_names(recipients: Any) -> tuple[str, ...]:
    """Recipient group names from json_recipients.names (group ID -> name, per group type).

    Only group names: the individual recipient lists hold other families' names and are never used.
    """
    names = recipients.get("names") if isinstance(recipients, dict) else None
    if not isinstance(names, dict):
        return ()
    out: list[str] = []
    for groups in names.values():
        if isinstance(groups, dict):
            out += [v for v in groups.values() if isinstance(v, str) and v and v not in out]
    return tuple(out)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _looks_html(headers: httpx.Headers, first: bytes) -> bool:
    if "text/html" in headers.get("content-type", "").lower():
        return True
    head = first.lstrip()[:64].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _parse_time(value: Any) -> datetime:
    """Admentum sends 'YYYY-MM-DD HH:MM:SS.ffffff+HH:MM'. Returned in UTC; naive values are taken as UTC."""
    if not isinstance(value, str):
        raise ProtocolError(f"timestamp is {type(value).__name__}, not a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProtocolError("unparseable timestamp") from None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _path(path: str) -> str:
    return path.split("?", 1)[0]


def _host_path(url: str) -> str:
    u = httpx.URL(url)
    return f"{u.host}{u.path}"
