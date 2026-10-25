"""Per-item pruning with tombstones (spec 5.4)."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from typing import Any

from .files import DataDir
from .store import Store, to_iso

log = logging.getLogger(__name__)


def prune(store: Store, data: DataDir, older_than_days: int, now: datetime, *, dry_run: bool = True) -> list[dict[str, Any]]:
    """Delete the files of seen items older than the cutoff and keep their rows (R-R1, R-R2).

    Unseen items are never pruned (R-R3).
    """
    cutoff = to_iso(now - timedelta(days=older_than_days))
    rows = store.conn.execute(
        """SELECT id, thread_id, received_at, path FROM items
           WHERE pruned_at IS NULL AND downloaded_at IS NOT NULL AND seen_at IS NOT NULL AND received_at < ?
           ORDER BY received_at, id""",
        (cutoff,),
    ).fetchall()
    out = [{"id": r["id"], "thread_id": r["thread_id"], "received_at": r["received_at"]} for r in rows]
    if dry_run:
        return out
    root = data.data.resolve()
    for r in rows:
        if r["path"]:
            target = (data.data / r["path"]).resolve()
            if target.is_relative_to(root) and target != root:
                shutil.rmtree(target, ignore_errors=True)
        with store.tx():
            store.update_item(r["id"], pruned_at=to_iso(now))
    if rows:
        log.info("pruned items=%d", len(rows))
    return out


def unseen_past_retention(store: Store, retention_days: int, now: datetime) -> int:
    cutoff = to_iso(now - timedelta(days=retention_days))
    return store.conn.execute(
        "SELECT COUNT(*) FROM items WHERE seen_at IS NULL AND downloaded_at IS NOT NULL AND received_at < ?",
        (cutoff,),
    ).fetchone()[0]
