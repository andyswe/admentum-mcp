from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from admentum_mcp.config import Config, SyncConfig
from admentum_mcp.fake_source import FakeSource
from admentum_mcp.files import DataDir
from admentum_mcp.store import Store
from admentum_mcp.sync import run_sync

START = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)  # Monday of week 44


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


class Env:
    """A data dir, a fake source and a clock, with helpers to sync and inspect."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.source = FakeSource()
        self.clock = Clock()
        self.data = DataDir(cfg.data_dir)

    def sync(self, **kw):
        return run_sync(self.cfg, self.source, clock=self.clock, sleep=lambda s: None, **kw)

    def store(self) -> Store:
        return Store.open(self.data.state_db)

    def query(self, sql: str, *args):
        s = self.store()
        try:
            return s.conn.execute(sql, args).fetchall()
        finally:
            s.close()

    def pending_ids(self) -> list[str]:
        return [r["id"] for r in self.query(
            "SELECT id FROM items WHERE seen_at IS NULL AND downloaded_at IS NOT NULL ORDER BY received_at, id")]

    def mark_all_seen(self) -> None:
        s = self.store()
        try:
            with s.tx():
                for r in s.pending():
                    s.mark_seen(r["id"], "agent", "2026-01-01T00:00:00.000000Z")
        finally:
            s.close()

    def item_dir(self, item_id: str) -> Path:
        (row,) = self.query("SELECT path FROM items WHERE id = ?", item_id)
        return self.data.data / row["path"]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return replace(Config(), data_dir=tmp_path / ".admentum-mcp", sync=SyncConfig(thread_delay_ms=0))


@pytest.fixture
def env(cfg: Config) -> Env:
    return Env(cfg)
