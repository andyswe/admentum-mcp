"""The SchoolSource protocol, its data classes and typed errors (spec 5.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class SchoolSourceError(Exception):
    """Base class. Sources never report failures as empty results."""


class AuthError(SchoolSourceError):
    """Login was rejected."""


class SessionError(SchoolSourceError):
    """The session or token could not be renewed."""


class SourceError(SchoolSourceError):
    """Network or HTTP failure after retries."""


class ProtocolError(SchoolSourceError):
    """The response had an unexpected shape."""


class AttachmentTooLarge(SourceError):
    """The attachment exceeds the size limit and was not downloaded (R-X3)."""


@dataclass(frozen=True)
class ThreadSummary:
    id: str
    title: str | None
    last_activity_at: datetime | None
    reply_count: int | None
    unread: bool | None
    raw: dict[str, Any] = field(default_factory=dict)
    groups: tuple[str, ...] = ()  # recipient group names (a class, an after-school group), as the source reports them


@dataclass(frozen=True)
class Message:
    id: str | None  # None only for sources without message IDs (R-D2)
    sent_at: datetime  # timezone-aware
    sender_id: str | None
    sender_name: str | None
    is_mine: bool
    html: str
    attachment_refs: list[str]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttachmentFile:
    filename: str  # original name
    mime: str
    size: int
    path: Path


class SchoolSource(Protocol):
    def list_threads(self) -> list[ThreadSummary]: ...

    def get_thread(self, thread_id: str) -> list[Message]:
        """All messages of the thread, every page."""
        ...

    def download_attachment(self, ref: str, dest_dir: Path, max_bytes: int) -> AttachmentFile:
        """Download into dest_dir (which exists and is empty).

        Raises AttachmentTooLarge if the file is over max_bytes.
        """
        ...
