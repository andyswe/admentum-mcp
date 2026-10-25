"""SQLite schema and queries (spec 4). No automatic migrations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .files import FILE_MODE

# No automatic migrations: a schema change bumps SCHEMA_VERSION, and a database with another
# version is refused. Migrate it by hand or start over with `admentum-mcp reset --yes`.
SCHEMA_VERSION = 2

_SCHEMA = """
    CREATE TABLE threads(
      id TEXT PRIMARY KEY,
      subject TEXT,
      kind TEXT NOT NULL,
      groups TEXT,
      last_activity_at TEXT,
      reply_count INTEGER,
      last_checked_at TEXT,
      last_error TEXT, last_error_at TEXT,
      archived_at TEXT
    );
    CREATE TABLE items(
      id TEXT PRIMARY KEY,
      thread_id TEXT NOT NULL REFERENCES threads(id),
      position INTEGER NOT NULL,
      received_at TEXT NOT NULL,
      sender TEXT, sender_id TEXT,
      is_mine INTEGER NOT NULL DEFAULT 0,
      body_sha256 TEXT, body_chars INTEGER,
      status TEXT NOT NULL,
      error TEXT,
      downloaded_at TEXT, seen_at TEXT, seen_by TEXT,
      pruned_at TEXT,
      source_edited_at TEXT, source_deleted_at TEXT,
      path TEXT
    );
    CREATE INDEX items_thread ON items(thread_id, received_at);
    CREATE INDEX items_unseen ON items(seen_at) WHERE seen_at IS NULL;
    CREATE TABLE attachments(
      item_id TEXT NOT NULL REFERENCES items(id),
      source_ref TEXT NOT NULL,
      filename TEXT,
      safe_name TEXT,
      mime TEXT, size_bytes INTEGER, sha256 TEXT,
      duplicate_of TEXT,
      text_chars INTEGER, truncated INTEGER NOT NULL DEFAULT 0,
      extract_status TEXT NOT NULL,
      error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (item_id, source_ref)
    );
    CREATE INDEX attachments_sha ON attachments(sha256);
    CREATE TABLE sync_runs(
      id INTEGER PRIMARY KEY,
      started_at TEXT, finished_at TEXT,
      result TEXT,
      counts_json TEXT, error TEXT
    );
    CREATE TABLE meta(
      key TEXT PRIMARY KEY,
      value TEXT
    );
    """


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Fixed-width UTC ISO 8601, so string order equals time order (R-F5)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class AttachmentRow:
    item_id: str
    source_ref: str
    filename: str | None
    safe_name: str | None
    mime: str | None
    size_bytes: int | None
    sha256: str | None
    duplicate_of: str | None
    text_chars: int | None
    truncated: bool
    extract_status: str
    error: str | None
    attempts: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> AttachmentRow:
        keys = set(r.keys())
        return cls(**{k: r[k] for k in cls.__dataclass_fields__ if k in keys})


@dataclass
class ItemRow:
    id: str
    thread_id: str
    received_at: str
    sender: str | None
    sender_id: str | None
    is_mine: bool
    body_sha256: str
    body_chars: int
    status: str
    error: str | None
    downloaded_at: str
    path: str
    seen_at: str | None = None
    seen_by: str | None = None


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> Store:
        new = not path.exists()
        conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        if new:
            path.chmod(FILE_MODE)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        store = cls(conn)
        store._init_schema()
        return store

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise RuntimeError(
                f"state.db has schema version {version}, this server needs {SCHEMA_VERSION}. "
                "Migrate it by hand, or start over with `admentum-mcp reset --yes`."
            )
        with self.tx():
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # meta

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # threads

    def get_thread(self, thread_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()

    def all_threads(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM threads").fetchall()

    def insert_thread(self, thread_id: str, subject: str | None, kind: str) -> None:
        self.conn.execute("INSERT INTO threads(id, subject, kind) VALUES (?, ?, ?)", (thread_id, subject, kind))

    def update_thread(self, thread_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE threads SET {cols} WHERE id = ?", (*fields.values(), thread_id))

    def last_item_at(self, thread_id: str) -> str | None:
        row = self.conn.execute("SELECT MAX(received_at) FROM items WHERE thread_id = ?", (thread_id,)).fetchone()
        return row[0]

    # items

    def get_item(self, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def thread_items(self, thread_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE thread_id = ? ORDER BY received_at, id", (thread_id,)
        ).fetchall()

    def insert_item(self, item: ItemRow, attachments: list[AttachmentRow]) -> None:
        """Insert an item with its attachments. Position is set by renumber()."""
        self.conn.execute(
            """INSERT INTO items(id, thread_id, position, received_at, sender, sender_id, is_mine,
                 body_sha256, body_chars, status, error, downloaded_at, seen_at, seen_by, path)
               VALUES (?, ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.id, item.thread_id, item.received_at, item.sender, item.sender_id, int(item.is_mine),
                item.body_sha256, item.body_chars, item.status, item.error,
                item.downloaded_at, item.seen_at, item.seen_by, item.path,
            ),
        )
        for a in attachments:
            self.insert_attachment(a)

    def update_item(self, item_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))

    def renumber(self, thread_id: str) -> None:
        """Positions follow sent time, never download time (R-D3)."""
        rows = self.conn.execute(
            "SELECT id, position FROM items WHERE thread_id = ? ORDER BY received_at, id", (thread_id,)
        ).fetchall()
        for pos, row in enumerate(rows):
            if row["position"] != pos:
                self.conn.execute("UPDATE items SET position = ? WHERE id = ?", (pos, row["id"]))

    def mark_seen(self, item_id: str, by: str, at: str) -> str:
        row = self.get_item(item_id)
        if row is None:
            raise KeyError(item_id)
        if row["seen_at"] is not None:
            return "already_seen"
        self.update_item(item_id, seen_at=at, seen_by=by)
        return "ok"

    def pending(self, limit: int | None = None, offset: int = 0, newest_first: bool = False) -> list[sqlite3.Row]:
        direction = "DESC" if newest_first else "ASC"
        sql = f"""SELECT * FROM items WHERE seen_at IS NULL AND downloaded_at IS NOT NULL
                  ORDER BY received_at {direction}, id {direction}"""
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        return self.conn.execute(sql).fetchall()

    def pending_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE seen_at IS NULL AND downloaded_at IS NOT NULL"
        ).fetchone()[0]

    # attachments

    def insert_attachment(self, a: AttachmentRow) -> None:
        self.conn.execute(
            """INSERT INTO attachments(item_id, source_ref, filename, safe_name, mime, size_bytes, sha256,
                 duplicate_of, text_chars, truncated, extract_status, error, attempts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                a.item_id, a.source_ref, a.filename, a.safe_name, a.mime, a.size_bytes, a.sha256,
                a.duplicate_of, a.text_chars, int(a.truncated), a.extract_status, a.error, a.attempts,
            ),
        )

    def update_attachment(self, item_id: str, source_ref: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE attachments SET {cols} WHERE item_id = ? AND source_ref = ?",
            (*fields.values(), item_id, source_ref),
        )

    def item_attachments(self, item_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM attachments WHERE item_id = ? ORDER BY rowid", (item_id,)
        ).fetchall()

    def find_by_sha(self, sha256: str) -> sqlite3.Row | None:
        """The first extracted attachment with this content, for duplicate reuse (R-X7)."""
        return self.conn.execute(
            """SELECT a.*, i.path AS item_path FROM attachments a JOIN items i ON i.id = a.item_id
               WHERE a.sha256 = ? AND a.duplicate_of IS NULL AND a.extract_status = 'ok'
                 AND i.pruned_at IS NULL
               ORDER BY a.rowid LIMIT 1""",
            (sha256,),
        ).fetchone()

    def retryable_attachments(self, max_attempts: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.*, i.thread_id, i.path AS item_path FROM attachments a JOIN items i ON i.id = a.item_id
               WHERE a.extract_status IN ('failed', 'download_failed') AND a.attempts < ?
                 AND i.pruned_at IS NULL
               ORDER BY a.rowid""",
            (max_attempts,),
        ).fetchall()

    def unsupported_attachments(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.*, i.thread_id, i.path AS item_path FROM attachments a JOIN items i ON i.id = a.item_id
               WHERE a.extract_status = 'unsupported' AND i.pruned_at IS NULL
               ORDER BY a.rowid"""
        ).fetchall()

    def refresh_item_status(self, item_id: str) -> None:
        """An item is partial while any attachment failed (R-X4); a scan is viewable, so it does not count (R-V6)."""
        bad = self.conn.execute(
            """SELECT COUNT(*) FROM attachments WHERE item_id = ?
               AND extract_status IN ('failed', 'download_failed') AND COALESCE(error, '') != 'no_text_layer'""",
            (item_id,),
        ).fetchone()[0]
        row = self.get_item(item_id)
        if row is not None and row["status"] != "error":
            self.update_item(item_id, status="partial" if bad else "ok")

    # sync runs

    def start_run(self, started_at: str) -> int:
        cur = self.conn.execute("INSERT INTO sync_runs(started_at) VALUES (?)", (started_at,))
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, finished_at: str, result: str, counts: dict[str, Any], error: str | None) -> None:
        self.conn.execute(
            "UPDATE sync_runs SET finished_at = ?, result = ?, counts_json = ?, error = ? WHERE id = ?",
            (finished_at, result, json.dumps(counts), error, run_id),
        )

    def run_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sync_runs WHERE result != 'dry_run'").fetchone()[0]
