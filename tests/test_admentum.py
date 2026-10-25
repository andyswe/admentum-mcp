"""AdmentumSource against a mock Admentum built from the probe's shapes (never real content)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote

import httpx
import pytest

from admentum_mcp.admentum import AdmentumSource
from admentum_mcp.config import Config, Credentials
from admentum_mcp.source import AttachmentTooLarge, AuthError, ProtocolError, SessionError, SourceError

USER_ID = 1001
CREDS = Credentials("parent@example.org", "s3cret-pw")
LOGIN_FORM = '<form><input name="csrfmiddlewaretoken" value="csrf-auth"><input name="password"></form>'
NEXT = "/openid/authorize?response_type=code&client_id=42&redirect_uri=https%3A//skola.admentum.se/user_token/"
LOGIN_URL = "https://auth.admentum.se/?next=" + quote(NEXT, safe="")


class FakeAdmentum:
    """Just enough of auth/skola/messages/files to exercise the connector."""

    def __init__(self) -> None:
        self.conversations = [self.conv(i) for i in range(150)]
        self.threads: dict[str, list[dict]] = {}
        self.files: dict[str, tuple[dict, bytes]] = {}
        self.valid_tokens: set[str] = set()
        self.token_seq = 0
        self.counts: dict[str, int] = {}
        self.login_page_html = LOGIN_FORM
        self.app_page_html = f'<script>window.app = {{"current_user_id": {USER_ID}, "school": 9}};</script>'
        self.fail_token = 0
        self.reject_api = 0
        self.api_status: list[int] = []
        self.files_html = 0
        self.files_403 = 0

    @staticmethod
    def conv(i: int) -> dict:
        return {
            "id": f"c{i}",
            "title": f"Thread {i}",
            "is_unread": False,
            "flag": 0,
            "creator": {"id": 7, "first_name": "T", "last_name": "Eacher"},
            "latest_message": {"id": f"m{i}", "created_at": "2026-10-20 08:15:00.123456+02:00", "meta_id": ""},
            "recipients": [],
        }

    def hit(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "auth.admentum.se" and req.method == "GET":
            self.hit("login_page")
            return httpx.Response(200, html=self.login_page_html,
                                  headers={"set-cookie": "csrftoken=a; Domain=auth.admentum.se; Path=/"})
        if host == "auth.admentum.se" and req.method == "POST":
            self.hit("login_post")
            form = parse_qs(req.content.decode())
            assert form.get("next") == [NEXT]
            if form.get("password") != [CREDS.password] or form.get("csrfmiddlewaretoken") != ["csrf-auth"]:
                return httpx.Response(200, html=LOGIN_FORM)
            return httpx.Response(302, headers={
                "location": "https://skola.admentum.se/overview",
                "set-cookie": "sessionid=sess; Domain=.admentum.se; Path=/",
            })
        if host == "skola.admentum.se" and path in ("/overview", "/messages/"):
            if "sessionid=sess" not in req.headers.get("cookie", ""):
                return httpx.Response(302, headers={"location": LOGIN_URL})
            return httpx.Response(200, html=self.app_page_html,
                                  headers={"set-cookie": "csrftoken=k; Domain=skola.admentum.se; Path=/"})
        if host == "skola.admentum.se" and path == "/user_token/":
            self.hit("token")
            if self.fail_token:
                self.fail_token -= 1
                return httpx.Response(403, html="forbidden")
            if "sessionid=sess" not in req.headers.get("cookie", "") or req.headers.get("x-csrftoken") != "k":
                return httpx.Response(403, html="forbidden")
            self.token_seq += 1
            tok = f"tok{self.token_seq}"
            self.valid_tokens.add(tok)
            return httpx.Response(200, json={"status": 200, "data": {"id_token": tok, "access_token": "x",
                                                                       "refresh_token": "y"}})
        if host == "messages.admentum.se":
            return self.messages_api(req)
        if host == "files.admentum.se":
            return self.files_api(req)
        return httpx.Response(404)

    def messages_api(self, req: httpx.Request) -> httpx.Response:
        self.hit("api")
        assert req.url.path.startswith(f"/api/users/{USER_ID}/")
        if self.api_status:
            return httpx.Response(self.api_status.pop(0), text="err")
        if self.reject_api:
            self.reject_api -= 1
            return httpx.Response(401, json={"error": "unauthorized"})
        if req.headers.get("user-info") not in self.valid_tokens:
            return httpx.Response(401, json={"error": "unauthorized"})
        rest = req.url.path.removeprefix(f"/api/users/{USER_ID}/")
        q = {k: int(v) for k, v in req.url.params.items() if k in ("page", "per_page")}
        page, per = q["page"], q["per_page"]
        if rest == "conversations":
            batch = self.conversations[(page - 1) * per: page * per]
            return httpx.Response(200, json={"conversations": batch, "page": page,
                                             "has_next_page": page * per < len(self.conversations),
                                             "timestamp": "x"})
        tid = rest.removeprefix("conversations/")
        msgs = self.threads[tid][(page - 1) * per: page * per]
        return httpx.Response(200, json={"conversation": {"id": tid, "title": "t", "page": page, "per_page": per,
                                                          "messages": msgs, "recipients": []}})

    def files_api(self, req: httpx.Request) -> httpx.Response:
        uuid = req.url.params["uuid"]
        if self.files_403:
            self.files_403 -= 1
            return httpx.Response(403, json={"error": "forbidden"})
        if "sessionid=sess" not in req.headers.get("cookie", ""):
            return httpx.Response(200, html="<!DOCTYPE html><html>login</html>")
        if uuid not in self.files:
            return httpx.Response(404, html="<!DOCTYPE html><html>Not found</html>")
        meta, data = self.files[uuid]
        if req.url.path == "/meta":
            return httpx.Response(200, json=meta)
        self.hit("file_get")
        if self.files_html:
            self.files_html -= 1
            return httpx.Response(200, content=b"<!DOCTYPE html>\n<html><body>Logga in</body></html>")
        return httpx.Response(200, content=data)  # Admentum sends no content-type here


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def server() -> FakeAdmentum:
    return FakeAdmentum()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def src(server: FakeAdmentum, clock: Clock):
    s = AdmentumSource(Config(), CREDS, transport=httpx.MockTransport(server), clock=clock, sleep=lambda s: None)
    yield s
    s.close()


def msg(i: int, *, creator_id: int = 7, meta_id: str = "") -> dict:
    return {
        "id": f"msg{i}",
        "created_at": f"2026-10-{1 + i % 28:02d} 10:00:00.000000+02:00",
        "formatted_created_at": "x",
        "message_type": 4 if meta_id else 1,
        "creator": {"id": creator_id, "first_name": "Anna", "last_name": "Lärare"},
        "content": f"<p>{i}</p>",
        "meta_id": meta_id,
    }


def test_list_threads_follows_pagination_and_parses_activity(src: AdmentumSource, server: FakeAdmentum) -> None:
    threads = src.list_threads()
    assert [t.id for t in threads] == [f"c{i}" for i in range(150)]
    assert threads[0].last_activity_at == datetime(2026, 10, 20, 6, 15, 0, 123456, tzinfo=UTC)
    assert threads[0].reply_count is None and threads[0].unread is False
    assert server.counts["login_post"] == 1 and server.counts["token"] == 1


def test_get_thread_follows_pagination_and_maps_messages(src: AdmentumSource, server: FakeAdmentum) -> None:
    server.threads["c1"] = [msg(i) for i in range(60)] + [msg(60, creator_id=USER_ID, meta_id="uuid-1")]
    messages = src.get_thread("c1")
    assert len(messages) == 61
    first, last = messages[0], messages[-1]
    assert first.sender_name == "Anna Lärare" and first.sender_id == "7" and not first.is_mine
    assert first.attachment_refs == []
    assert last.is_mine and last.attachment_refs == ["uuid-1"]
    assert first.sent_at.utcoffset().total_seconds() == 0


def test_rejected_login_raises_auth_error(src: AdmentumSource) -> None:
    src._creds = Credentials(CREDS.username, "wrong")
    with pytest.raises(AuthError) as e:
        src.list_threads()
    assert "wrong" not in str(e.value) and CREDS.username not in str(e.value)


def test_login_page_without_csrf_is_protocol_error(src: AdmentumSource, server: FakeAdmentum) -> None:
    server.login_page_html = "<html>maintenance</html>"
    with pytest.raises(ProtocolError):
        src.list_threads()


def test_old_token_is_renewed_without_logging_in_again(src: AdmentumSource, server: FakeAdmentum, clock: Clock) -> None:
    src.list_threads()
    clock.t += 46 * 60
    src.list_threads()
    assert server.counts["token"] == 2 and server.counts["login_post"] == 1


def test_401_renews_token_and_retries(src: AdmentumSource, server: FakeAdmentum) -> None:
    src.list_threads()
    server.reject_api = 1
    src.list_threads()
    assert server.counts["token"] == 2 and server.counts["login_post"] == 1


def test_401_after_renewal_logs_in_again(src: AdmentumSource, server: FakeAdmentum) -> None:
    src.list_threads()
    server.reject_api = 2
    src.list_threads()
    assert server.counts["login_post"] == 2


def test_failed_renewal_logs_in_again(src: AdmentumSource, server: FakeAdmentum, clock: Clock) -> None:
    src.list_threads()
    clock.t += 46 * 60
    server.fail_token = 1
    src.list_threads()
    assert server.counts["login_post"] == 2


def test_persistent_401_raises_session_error(src: AdmentumSource, server: FakeAdmentum) -> None:
    src.list_threads()
    server.reject_api = 99
    with pytest.raises(SessionError):
        src.list_threads()


def test_server_errors_are_retried_then_typed(src: AdmentumSource, server: FakeAdmentum) -> None:
    src.list_threads()
    server.api_status = [503, 502]
    assert len(src.list_threads()) == 150
    server.api_status = [500, 500, 500]
    with pytest.raises(SourceError):
        src.list_threads()


def test_non_json_is_protocol_error(src: AdmentumSource, server: FakeAdmentum) -> None:
    src.list_threads()
    server.api_status = [200]
    with pytest.raises(ProtocolError):
        src.list_threads()


def test_network_failure_is_source_error(server: FakeAdmentum, clock: Clock) -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=req)

    s = AdmentumSource(Config(), CREDS, transport=httpx.MockTransport(down), clock=clock, sleep=lambda s: None)
    with pytest.raises(SourceError):
        s.list_threads()


def test_user_id_is_discovered_at_login(src: AdmentumSource, server: FakeAdmentum) -> None:
    src.list_threads()
    assert src._user_id == str(USER_ID)


def test_app_page_without_user_id_is_protocol_error(src: AdmentumSource, server: FakeAdmentum) -> None:
    server.app_page_html = "<html>changed layout</html>"
    with pytest.raises(ProtocolError, match="current_user_id"):
        src.list_threads()


def test_download_attachment(src: AdmentumSource, server: FakeAdmentum, tmp_path: Path) -> None:
    server.files["u1"] = ({"originalName": "Veckobrev v.44.pdf", "mimetype": "application/pdf", "size": 9},
                          b"%PDF-1.4\n")
    f = src.download_attachment("u1", tmp_path, max_bytes=1000)
    assert (f.filename, f.mime, f.size) == ("Veckobrev v.44.pdf", "application/pdf", 9)
    assert f.path.read_bytes() == b"%PDF-1.4\n"


def test_html_page_instead_of_file_logs_in_again_then_fails(src: AdmentumSource, server: FakeAdmentum,
                                                             tmp_path: Path) -> None:
    server.files["u1"] = ({"originalName": "a.pdf", "mimetype": "application/pdf"}, b"%PDF-1.4\n")
    server.files_html = 1
    assert src.download_attachment("u1", tmp_path, max_bytes=1000).size == 9
    assert server.counts["login_post"] == 2

    server.files_html = 2
    with pytest.raises(SourceError, match="HTML"):
        src.download_attachment("u1", tmp_path, max_bytes=1000)


def test_too_large_attachment(src: AdmentumSource, server: FakeAdmentum, tmp_path: Path) -> None:
    server.files["big"] = ({"originalName": "a.pdf", "mimetype": "application/pdf", "size": 5000}, b"x" * 5000)
    with pytest.raises(AttachmentTooLarge):
        src.download_attachment("big", tmp_path, max_bytes=1000)
    assert "file_get" not in server.counts  # rejected from metadata, never downloaded
    server.files["nosize"] = ({"originalName": "a.pdf", "mimetype": "application/pdf"}, b"x" * 5000)
    with pytest.raises(AttachmentTooLarge):
        src.download_attachment("nosize", tmp_path, max_bytes=1000)


def test_missing_attachment_is_source_error(src: AdmentumSource, tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        src.download_attachment("nope", tmp_path, max_bytes=1000)


def test_attachment_metadata_defaults(src: AdmentumSource, server: FakeAdmentum, tmp_path: Path) -> None:
    server.files["u2"] = ({}, b"data")
    f = src.download_attachment("u2", tmp_path, max_bytes=1000)
    assert (f.filename, f.mime) == ("u2", "application/octet-stream")


def test_credentials_repr_is_redacted() -> None:
    assert CREDS.password not in repr(CREDS) and CREDS.username not in repr(CREDS)


def test_files_403_fails_without_logging_in_again(src: AdmentumSource, server: FakeAdmentum, tmp_path: Path) -> None:
    server.files["u1"] = ({"originalName": "a.pdf", "mimetype": "application/pdf"}, b"%PDF-1.4\n")
    src.list_threads()
    server.files_403 = 1
    with pytest.raises(SourceError, match="403"):
        src.download_attachment("u1", tmp_path, max_bytes=1000)
    assert server.counts["login_post"] == 1
    assert src.download_attachment("u1", tmp_path, max_bytes=1000).size == 9


def test_files_requests_are_paced(server: FakeAdmentum, tmp_path: Path) -> None:
    t = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s

    s = AdmentumSource(Config(), CREDS, transport=httpx.MockTransport(server), clock=lambda: t[0], sleep=sleep)
    server.files["u1"] = ({"originalName": "a.pdf", "mimetype": "application/pdf"}, b"%PDF-1.4\n")
    s.download_attachment("u1", tmp_path, max_bytes=1000)
    s.download_attachment("u1", tmp_path, max_bytes=1000)
    assert len(slept) == 3 and all(x == pytest.approx(0.5) for x in slept)  # meta, get, meta, get


def test_deleted_file_404_fails_without_logging_in_again(src: AdmentumSource, server: FakeAdmentum,
                                                          tmp_path: Path) -> None:
    src.list_threads()
    with pytest.raises(SourceError, match="404"):
        src.download_attachment("gone", tmp_path, max_bytes=1000)
    assert server.counts["login_post"] == 1


def test_recipient_group_names_only(src: AdmentumSource, server: FakeAdmentum) -> None:
    server.conversations[0]["json_recipients"] = {
        "all": ["x"], "is_information": True,
        "names": {"primary_groups": {"45009": "3C"}, "leisure_groups": {"443": "Fritids Ekorren", "444": "3C"}},
        "parents": {"primary_groups": [45009]},
    }
    server.conversations[0]["recipients"] = [{"id": 1, "first_name": "Other", "last_name": "Parent"}]
    threads = src.list_threads()
    assert threads[0].groups == ("3C", "Fritids Ekorren")
    assert threads[1].groups == ()
