"""The sync algorithm (spec 5.2)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from . import prune as prune_mod
from .clean import html_to_text
from .config import Config
from .extract import extract, is_supported
from .files import DataDir, make_dir, safe_name, sha256_file, write_json, write_text
from .source import (
    AttachmentTooLarge,
    AuthError,
    Message,
    SchoolSource,
    SchoolSourceError,
    SessionError,
    ThreadSummary,
)
from .store import AttachmentRow, ItemRow, Store, from_iso, to_iso, utcnow

log = logging.getLogger(__name__)

BAD_ATTACHMENT = ("failed", "download_failed")


def is_bad(a: AttachmentRow) -> bool:
    """Counts as a failure (R-X4), except a scan without text, which the agent can view (R-V6)."""
    return a.extract_status in BAD_ATTACHMENT and a.error != "no_text_layer"

COUNT_KEYS = (
    "new_threads",
    "new_items_in_new_threads",
    "new_replies",
    "changed_threads",
    "auto_seen",
    "edited",
    "deleted_at_source",
    "attachment_failures",
    "unsupported_attachments",
    "thread_errors",
    "pruned_items",
)


def run_sync(
    cfg: Config,
    source: SchoolSource,
    *,
    dry_run: bool = False,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run one sync. Returns counts (R-Y5) or {"result": "already_running"} (R-Y1)."""
    data = DataDir(cfg.data_dir)
    data.ensure()
    lock = FileLock(str(data.lock), timeout=0, mode=0o600)
    try:
        lock.acquire()
    except Timeout:
        log.info("sync skipped: already running")
        return {"result": "already_running"}
    try:
        store = Store.open(data.state_db)
        try:
            return _Sync(cfg, source, store, data, clock, sleep, dry_run).run()
        finally:
            store.close()
    finally:
        lock.release()


def kind_for(cfg: Config, thread_id: str, subject: str | None, root_sender: str | None) -> str:
    """R-D4: the ID list wins over the rules."""
    if thread_id in cfg.feeds.thread_ids:
        return "feed"
    if any(r.matches(subject, root_sender) for r in cfg.feeds.rules):
        return "feed"
    return "conversation"


def _groups_json(s: ThreadSummary) -> str | None:
    return json.dumps(list(s.groups), ensure_ascii=False) if s.groups else None


def item_id_for(thread_id: str, m: Message) -> str:
    """R-D2: the source ID, else a content hash."""
    if m.id:
        return str(m.id)
    key = "\x1f".join([thread_id, m.sender_id or m.sender_name or "", to_iso(m.sent_at), m.html[:200]])
    return "sha-" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def body_hash(html: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", html).encode("utf-8")).hexdigest()


class _Sync:
    def __init__(
        self,
        cfg: Config,
        source: SchoolSource,
        store: Store,
        data: DataDir,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        dry_run: bool,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.store = store
        self.data = data
        self.clock = clock
        self.sleep = sleep
        self.dry = dry_run
        self.counts: dict[str, int] = dict.fromkeys(COUNT_KEYS, 0)
        if dry_run:
            self.counts["attachments_to_download"] = 0
        self.now = clock()
        self.now_iso = to_iso(self.now)

    def run(self) -> dict[str, Any]:
        with self.store.tx():
            run_id = self.store.start_run(self.now_iso)
            previous_runs = self.store.run_count()
        log.info("sync started run=%d dry_run=%s", run_id, self.dry)
        error: str | None = None
        try:
            if not self.dry:
                self.data.clear_tmp()
                self._retry_attachments()
            cutoff = self._cutoff()
            fetch = self._reconcile_threads(self.source.list_threads(), previous_runs, cutoff)
            for i, (summary, is_new) in enumerate(fetch):
                if i and self.cfg.sync.thread_delay_ms:
                    self.sleep(self.cfg.sync.thread_delay_ms / 1000)
                self._sync_thread(summary, is_new, cutoff)
            if not self.dry:
                pruned = prune_mod.prune(self.store, self.data, self.cfg.retention_days, self.clock(), dry_run=False)
                self.counts["pruned_items"] = len(pruned)
        except SchoolSourceError as e:
            # R-Y4: auth and list failures fail the whole sync; never "no new messages".
            error = f"{type(e).__name__}: {e}"
            log.error("sync failed run=%d error=%s", run_id, type(e).__name__)
        except Exception as e:
            self._finish(run_id, "error", f"internal error: {type(e).__name__}")
            raise
        if error:
            result = "error"
        elif self.dry:
            result = "dry_run"
        elif self.counts["thread_errors"]:
            result = "partial"
        else:
            result = "ok"
        return self._finish(run_id, result, error)

    def _finish(self, run_id: int, result: str, error: str | None) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.counts)
        out["pending_total"] = self.store.pending_count()
        out["result"] = result
        if error:
            out["error"] = error
        with self.store.tx():
            self.store.finish_run(run_id, to_iso(self.clock()), result, out, error)
        log.info("sync finished run=%d result=%s counts=%s", run_id, result, self.counts)
        return out

    def _cutoff(self) -> datetime:
        """Ingest cutoff: first sync time minus initial_lookback_days."""
        first = self.store.get_meta("first_sync_at")
        if first is None:
            first = self.now_iso
            if not self.dry:
                with self.store.tx():
                    self.store.set_meta("first_sync_at", first)
        return from_iso(first) - timedelta(days=self.cfg.sync.initial_lookback_days)

    # threads

    def _reconcile_threads(
        self, summaries: list[ThreadSummary], previous_runs: int, cutoff: datetime
    ) -> list[tuple[ThreadSummary, bool]]:
        known = {row["id"]: row for row in self.store.all_threads()}
        listed: set[str] = set()
        fetch: list[tuple[ThreadSummary, bool]] = []
        with self.store.tx():
            for s in summaries:
                listed.add(s.id)
                row = known.get(s.id)
                if row is None:
                    if s.last_activity_at is not None and s.last_activity_at < cutoff:
                        continue  # nothing to ingest; not stored at all, not even its title
                    self.counts["new_threads"] += 1
                    if not self.dry:
                        self.store.insert_thread(s.id, s.title, kind_for(self.cfg, s.id, s.title, None))
                        self.store.update_thread(s.id, groups=_groups_json(s))
                    fetch.append((s, True))
                    continue
                if not self.dry:
                    root = self._root_sender(s.id)
                    self.store.update_thread(
                        s.id,
                        subject=s.title,
                        kind=kind_for(self.cfg, s.id, s.title, root),
                        groups=_groups_json(s),
                        archived_at=None,
                    )
                if self._is_changed(s, row, previous_runs) and not self._before_cutoff(s, cutoff):
                    # A thread that never had a successful fetch still counts as new.
                    fetch.append((s, row["last_checked_at"] is None))
            if not self.dry:
                for thread_id, row in known.items():
                    if thread_id not in listed and row["archived_at"] is None:
                        self.store.update_thread(thread_id, archived_at=self.now_iso)
        return fetch

    def _before_cutoff(self, s: ThreadSummary, cutoff: datetime) -> bool:
        """A known thread whose latest activity is before the ingest cutoff has nothing to ingest.

        It is recorded as checked without fetching. (Unknown threads like this are not stored.)
        """
        if s.last_activity_at is None or s.last_activity_at >= cutoff:
            return False
        if not self.dry:
            self.store.update_thread(
                s.id,
                last_activity_at=to_iso(s.last_activity_at),
                reply_count=s.reply_count,
                last_checked_at=self.now_iso,
                last_error=None,
                last_error_at=None,
            )
        return True

    def _is_changed(self, s: ThreadSummary, row: Any, previous_runs: int) -> bool:
        """R-Y2 step 3."""
        if row["last_error"] is not None or row["last_checked_at"] is None:
            return True
        if s.last_activity_at is None and s.reply_count is None:
            last = self.store.last_item_at(s.id) or row["last_checked_at"]
            if from_iso(last) >= self.now - timedelta(days=self.cfg.sync.full_recheck_days):
                return True
            return previous_runs % self.cfg.sync.stale_recheck_every == 0
        if s.last_activity_at is not None and to_iso(s.last_activity_at) != row["last_activity_at"]:
            return True
        if s.reply_count is not None and s.reply_count != row["reply_count"]:
            return True
        return False

    def _root_sender(self, thread_id: str) -> str | None:
        items = self.store.thread_items(thread_id)
        return items[0]["sender"] if items else None

    def _sync_thread(self, s: ThreadSummary, is_new: bool, cutoff: datetime) -> None:
        try:
            messages = self.source.get_thread(s.id)
        except (AuthError, SessionError):
            raise
        except SchoolSourceError as e:
            # R-Y3: record and move on; last_activity_at stays unchanged so the thread is retried.
            self.counts["thread_errors"] += 1
            log.warning("thread fetch failed thread=%s error=%s", s.id, type(e).__name__)
            if not self.dry:
                with self.store.tx():
                    self.store.update_thread(s.id, last_error=f"{type(e).__name__}: {e}"[:500], last_error_at=self.now_iso)
            return
        self.counts["changed_threads"] += 1
        messages = sorted(messages, key=lambda m: m.sent_at)
        if self.dry:
            self._dry_thread(s, is_new, messages, cutoff)
            return

        thread_dir = self.data.data / self.data.thread_rel(s.id)
        make_dir(thread_dir)
        write_json(thread_dir / "thread.json", s.raw)

        known = {row["id"]: row for row in self.store.thread_items(s.id)}
        present: set[str] = set()
        for m in messages:
            item_id = item_id_for(s.id, m)
            present.add(item_id)
            row = known.get(item_id)
            if row is not None:
                self._check_known(row, m)
            elif m.sent_at >= cutoff:
                self._ingest(s, m, item_id, is_new)
        with self.store.tx():
            for item_id, row in known.items():
                if item_id not in present and row["source_deleted_at"] is None:
                    self.store.update_item(item_id, source_deleted_at=self.now_iso)
                    self.counts["deleted_at_source"] += 1
            self.store.renumber(s.id)
            root = messages[0].sender_name if messages else self._root_sender(s.id)
            self.store.update_thread(
                s.id,
                kind=kind_for(self.cfg, s.id, s.title, root),
                groups=_groups_json(s),
                last_activity_at=to_iso(s.last_activity_at) if s.last_activity_at else None,
                reply_count=s.reply_count,
                last_checked_at=self.now_iso,
                last_error=None,
                last_error_at=None,
            )

    def _dry_thread(self, s: ThreadSummary, is_new: bool, messages: list[Message], cutoff: datetime) -> None:
        known = {row["id"] for row in self.store.thread_items(s.id)}
        for m in messages:
            if item_id_for(s.id, m) in known or m.sent_at < cutoff:
                continue
            self.counts["new_items_in_new_threads" if is_new else "new_replies"] += 1
            self.counts["attachments_to_download"] += len(m.attachment_refs)

    def _check_known(self, row: Any, m: Message) -> None:
        """R-D7: keep the local copy, flag edits; clear a deletion flag if the item is back."""
        fields: dict[str, Any] = {}
        if row["source_deleted_at"] is not None:
            fields["source_deleted_at"] = None
        if row["source_edited_at"] is None and row["body_sha256"] != body_hash(m.html):
            fields["source_edited_at"] = self.now_iso
            self.counts["edited"] += 1
        if row["received_at"] != to_iso(m.sent_at):
            fields["received_at"] = to_iso(m.sent_at)
        if fields:
            with self.store.tx():
                self.store.update_item(row["id"], **fields)

    # items

    def _ingest(self, s: ThreadSummary, m: Message, item_id: str, is_new_thread: bool) -> None:
        """Stage the whole item, move it into place, then commit the row (R-F2)."""
        stage = self.data.new_stage()
        try:
            write_json(stage / "item.json", m.raw)
            status, error = "ok", None
            try:
                body = html_to_text(m.html)
            except Exception as e:
                body, status, error = "", "error", f"body: {type(e).__name__}"
            write_text(stage / "body.txt", body)

            attachments: list[AttachmentRow] = []
            if m.attachment_refs:
                att_dir = stage / "attachments"
                make_dir(att_dir)
                taken: set[str] = set()
                for ref in m.attachment_refs:
                    attachments.append(self._new_attachment(item_id, ref, att_dir, taken))
            if status == "ok" and any(is_bad(a) for a in attachments):
                status = "partial"

            rel = self.data.item_rel(s.id, item_id)
            self.data.commit_stage(stage, rel)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

        seen_by = None
        if m.is_mine:
            seen_by = "own"  # R-D5
        elif any(r.matches(s.title, m.sender_name) for r in self.cfg.noise):
            seen_by = "noise_rule"  # R-Y6
        item = ItemRow(
            id=item_id,
            thread_id=s.id,
            received_at=to_iso(m.sent_at),
            sender=m.sender_name,
            sender_id=m.sender_id,
            is_mine=m.is_mine,
            body_sha256=body_hash(m.html),
            body_chars=len(body),
            status=status,
            error=error,
            downloaded_at=to_iso(self.clock()),
            path=rel,
            seen_at=self.now_iso if seen_by else None,
            seen_by=seen_by,
        )
        with self.store.tx():
            self.store.insert_item(item, attachments)
        self.counts["new_items_in_new_threads" if is_new_thread else "new_replies"] += 1
        if seen_by:
            self.counts["auto_seen"] += 1
        for a in attachments:
            if is_bad(a):
                self.counts["attachment_failures"] += 1
            elif a.extract_status == "unsupported":
                self.counts["unsupported_attachments"] += 1
        log.info("item stored thread=%s item=%s attachments=%d status=%s", s.id, item_id, len(attachments), status)

    # attachments

    def _new_attachment(self, item_id: str, ref: str, att_dir: Path, taken: set[str]) -> AttachmentRow:
        row = AttachmentRow(
            item_id=item_id, source_ref=ref, filename=None, safe_name=None, mime=None, size_bytes=None,
            sha256=None, duplicate_of=None, text_chars=None, truncated=False,
            extract_status="download_failed", error=None, attempts=1,
        )
        self._download_into(row, att_dir, taken)
        return row

    def _download_into(self, row: AttachmentRow, att_dir: Path, taken: set[str]) -> None:
        """Download, store under a safe name and extract. Updates `row` in place."""
        dl = self.data.new_stage()
        try:
            try:
                f = self.source.download_attachment(row.source_ref, dl, self.cfg.limits.max_attachment_bytes)
            except AttachmentTooLarge:
                row.extract_status, row.error = "download_failed", "too_large"
                row.attempts = self.cfg.limits.max_attempts  # R-X3: not retried
                return
            except (AuthError, SessionError):
                raise
            except SchoolSourceError as e:
                row.extract_status, row.error = "download_failed", f"{type(e).__name__}: {e}"[:500]
                log.warning("attachment download failed item=%s ref=%s error=%s", row.item_id, row.source_ref, type(e).__name__)
                return
            row.filename = unicodedata.normalize("NFC", f.filename)
            row.mime = f.mime
            row.safe_name = safe_name(row.filename, taken)
            dest = att_dir / row.safe_name
            os.replace(f.path, dest)
            row.size_bytes = dest.stat().st_size
            row.sha256 = sha256_file(dest)
            row.error = None
            self._extract_into(row, dest)
        finally:
            shutil.rmtree(dl, ignore_errors=True)

    def _extract_into(self, row: AttachmentRow, dest: Path) -> None:
        txt = dest.with_name(dest.name + ".txt")
        dup = self.store.find_by_sha(row.sha256) if row.sha256 else None
        if dup is not None and (dup["item_id"], dup["source_ref"]) != (row.item_id, row.source_ref):
            src = self.data.data / dup["item_path"] / "attachments" / f"{dup['safe_name']}.txt"
            if src.is_file():
                # R-X7: reuse the text, keep the file per item.
                write_text(txt, src.read_text(encoding="utf-8"))
                row.duplicate_of = f"{dup['item_id']}/{dup['source_ref']}"
                row.extract_status = dup["extract_status"]
                row.text_chars = dup["text_chars"]
                row.truncated = bool(dup["truncated"])
                row.error = None
                return
        result = extract(dest, row.mime, self.cfg, filename=row.filename)
        row.extract_status = result.status
        row.error = result.error
        row.truncated = result.truncated
        if result.status == "ok":
            write_text(txt, result.text)
            row.text_chars = len(result.text)
        else:
            row.text_chars = None
            if result.status == "failed":
                log.warning("attachment extraction failed item=%s ref=%s", row.item_id, row.source_ref)

    def _retry_attachments(self) -> None:
        """R-Y2 step 1: re-extract `failed`, download `download_failed` again."""
        for r in self.store.retryable_attachments(self.cfg.limits.max_attempts):
            if r["extract_status"] == "failed" and r["error"] == "no_text_layer":
                continue  # R-X5
            self._retry_one(r)

    def reextract_on_startup(self) -> int:
        """R-Y8: re-extract `failed` attachments and `unsupported` ones whose type now has an extractor."""
        rows = [
            r for r in self.store.retryable_attachments(self.cfg.limits.max_attempts)
            if r["extract_status"] == "failed" and r["error"] != "no_text_layer"
        ]
        rows += [r for r in self.store.unsupported_attachments() if is_supported(r["mime"], r["filename"])]
        for r in rows:
            self._retry_one(r)
        return len(rows)

    def _retry_one(self, r: Any) -> None:
        att_dir = self.data.data / r["item_path"] / "attachments"
        row = AttachmentRow.from_row(r)
        row.attempts += 1
        if r["extract_status"] == "download_failed":
            make_dir(att_dir)
            taken = {a["safe_name"] for a in self.store.item_attachments(r["item_id"]) if a["safe_name"]}
            self._download_into(row, att_dir, taken)
        else:
            path = att_dir / (row.safe_name or "")
            if not row.safe_name or not path.is_file():
                row.error = "file_missing"
            else:
                self._extract_into(row, path)
        with self.store.tx():
            self.store.update_attachment(
                row.item_id, row.source_ref,
                filename=row.filename, safe_name=row.safe_name, mime=row.mime, size_bytes=row.size_bytes,
                sha256=row.sha256, duplicate_of=row.duplicate_of, text_chars=row.text_chars,
                truncated=int(row.truncated), extract_status=row.extract_status, error=row.error,
                attempts=row.attempts,
            )
            self.store.refresh_item_status(row.item_id)
        log.info("attachment retried item=%s ref=%s status=%s", row.item_id, row.source_ref, row.extract_status)


def run_startup(cfg: Config, *, clock: Callable[[], datetime] = utcnow) -> dict[str, Any]:
    """R-Y8 server start: empty data/.tmp and re-extract what can now be extracted.

    Skipped (and reported) when a sync holds the lock; that sync owns the staging area.
    """
    data = DataDir(cfg.data_dir)
    data.ensure()
    lock = FileLock(str(data.lock), timeout=0, mode=0o600)
    try:
        lock.acquire()
    except Timeout:
        return {"result": "already_running"}
    try:
        data.clear_tmp()
        store = Store.open(data.state_db)
        try:
            n = _Sync(cfg, _NoSource(), store, data, clock, time.sleep, dry_run=False).reextract_on_startup()
        finally:
            store.close()
    finally:
        lock.release()
    log.info("startup re-extracted attachments=%d", n)
    return {"result": "ok", "reextracted": n}


class _NoSource:
    """Startup only re-extracts local files; it never contacts the source."""

    def list_threads(self) -> list[ThreadSummary]:
        raise AssertionError("no source at startup")

    def get_thread(self, thread_id: str) -> list[Message]:
        raise AssertionError("no source at startup")

    def download_attachment(self, ref: str, dest_dir: Path, max_bytes: int) -> Any:
        raise AssertionError("no source at startup")
