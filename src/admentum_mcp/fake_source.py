"""Scriptable in-memory SchoolSource for tests and local development (spec 5.1)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .source import (
    AttachmentFile,
    AttachmentTooLarge,
    AuthError,
    Message,
    SchoolSourceError,
    SourceError,
    ThreadSummary,
)


@dataclass
class FakeAttachment:
    filename: str
    mime: str
    data: bytes


@dataclass
class FakeMessage:
    id: str | None
    sent_at: datetime
    sender_id: str
    sender_name: str
    html: str
    is_mine: bool = False
    attachment_refs: list[str] = field(default_factory=list)


@dataclass
class FakeThread:
    id: str
    title: str
    messages: list[FakeMessage] = field(default_factory=list)
    groups: tuple[str, ...] = ()


class FakeSource:
    """Mutate between syncs with add_thread/add_message/edit_message/delete_message.

    Failures are injected per call: fail_thread(id, n) makes the next n get_thread calls for
    that thread raise; fail_attachment(ref, n) does the same for downloads; fail_login makes
    every call raise AuthError until cleared.
    """

    def __init__(self, *, report_activity: bool = True, report_reply_count: bool = False) -> None:
        self.threads: dict[str, FakeThread] = {}
        self.attachments: dict[str, FakeAttachment] = {}
        self.report_activity = report_activity
        self.report_reply_count = report_reply_count
        self.fail_login = False
        self._thread_failures: Counter[str] = Counter()
        self._attachment_failures: Counter[str] = Counter()
        self._attachment_errors: dict[str, type[SchoolSourceError]] = {}
        self.calls: list[tuple[str, str]] = []

    # scripting

    def add_thread(self, thread_id: str, title: str, groups: tuple[str, ...] = ()) -> FakeThread:
        t = FakeThread(thread_id, title, groups=groups)
        self.threads[thread_id] = t
        return t

    def add_message(
        self,
        thread_id: str,
        message_id: str | None,
        sent_at: datetime,
        html: str,
        *,
        sender_id: str = "teacher-1",
        sender_name: str = "Teacher One",
        is_mine: bool = False,
        attachments: list[tuple[str, FakeAttachment]] | None = None,
    ) -> FakeMessage:
        refs = []
        for ref, att in attachments or []:
            self.attachments[ref] = att
            refs.append(ref)
        m = FakeMessage(message_id, sent_at, sender_id, sender_name, html, is_mine, refs)
        self.threads[thread_id].messages.append(m)
        return m

    def edit_message(self, thread_id: str, message_id: str, html: str) -> None:
        self._find(thread_id, message_id).html = html

    def delete_message(self, thread_id: str, message_id: str) -> None:
        t = self.threads[thread_id]
        t.messages = [m for m in t.messages if m.id != message_id]

    def remove_thread(self, thread_id: str) -> None:
        del self.threads[thread_id]

    def fail_thread(self, thread_id: str, times: int = 1) -> None:
        self._thread_failures[thread_id] += times

    def fail_attachment(self, ref: str, times: int = 1, error: type[SchoolSourceError] = SourceError) -> None:
        self._attachment_failures[ref] += times
        self._attachment_errors[ref] = error

    def fetches(self, thread_id: str) -> int:
        return sum(1 for kind, tid in self.calls if kind == "get_thread" and tid == thread_id)

    def _find(self, thread_id: str, message_id: str) -> FakeMessage:
        for m in self.threads[thread_id].messages:
            if m.id == message_id:
                return m
        raise KeyError(message_id)

    # SchoolSource

    def list_threads(self) -> list[ThreadSummary]:
        self._check_login()
        self.calls.append(("list_threads", ""))
        out = []
        for t in self.threads.values():
            latest = max((m.sent_at for m in t.messages), default=None)
            out.append(
                ThreadSummary(
                    id=t.id,
                    title=t.title,
                    last_activity_at=latest if self.report_activity else None,
                    reply_count=max(len(t.messages) - 1, 0) if self.report_reply_count else None,
                    unread=None,
                    raw={"id": t.id, "title": t.title},
                    groups=t.groups,
                )
            )
        return out

    def get_thread(self, thread_id: str) -> list[Message]:
        self._check_login()
        self.calls.append(("get_thread", thread_id))
        if self._thread_failures[thread_id] > 0:
            self._thread_failures[thread_id] -= 1
            raise SourceError(f"injected failure for thread {thread_id}")
        t = self.threads.get(thread_id)
        if t is None:
            raise SourceError(f"thread {thread_id} not found")
        return [
            Message(
                id=m.id,
                sent_at=m.sent_at,
                sender_id=m.sender_id,
                sender_name=m.sender_name,
                is_mine=m.is_mine,
                html=m.html,
                attachment_refs=list(m.attachment_refs),
                raw={"id": m.id, "created_at": m.sent_at.isoformat(), "content": m.html},
            )
            for m in t.messages
        ]

    def download_attachment(self, ref: str, dest_dir: Path, max_bytes: int) -> AttachmentFile:
        self._check_login()
        self.calls.append(("download_attachment", ref))
        if self._attachment_failures[ref] > 0:
            self._attachment_failures[ref] -= 1
            raise self._attachment_errors[ref](f"injected failure for attachment {ref}")
        att = self.attachments.get(ref)
        if att is None:
            raise SourceError(f"attachment {ref} not found")
        if len(att.data) > max_bytes:
            raise AttachmentTooLarge(f"{len(att.data)} bytes")
        path = dest_dir / "download"
        path.write_bytes(att.data)
        return AttachmentFile(att.filename, att.mime, len(att.data), path)

    def _check_login(self) -> None:
        if self.fail_login:
            raise AuthError("injected login failure")


def demo_source() -> FakeSource:
    """A small fixture scenario for `--source fake`: a weekly-letter feed and a conversation."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    src = FakeSource()
    src.add_thread("demo-feed", "Veckobrev klass 3B", groups=("3B",))
    src.add_message("demo-feed", "demo-root", now - timedelta(days=21), "<p>Här kommer veckobreven.</p>")
    for i, week in enumerate((1, 2, 3)):
        src.add_message(
            "demo-feed", f"demo-week-{week}", now - timedelta(days=14 - 7 * i), f"<p>Veckobrev {week}</p>",
            attachments=[(f"demo-file-{week}", FakeAttachment(f"Veckobrev {week}.txt", "text/plain",
                                                              f"Demo-veckobrev {week}".encode()))],
        )
    src.add_thread("demo-conv", "Fråga om utflykt", groups=("3B", "Fritids"))
    src.add_message("demo-conv", "demo-q", now - timedelta(days=2), "<p>Vad ska barnen ta med?</p>")
    src.add_message("demo-conv", "demo-a", now - timedelta(days=1), "<p>Matsäck.</p>", is_mine=True)
    return src
