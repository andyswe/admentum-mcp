"""config.toml and environment loading with validation (spec 7.1)."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DATA_DIR = Path.home() / ".admentum-mcp"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.toml"


class ConfigError(Exception):
    """Invalid configuration; the message names the offending key."""


@dataclass(frozen=True)
class Rule:
    """Matches when every non-empty pattern matches. A rule with no patterns matches nothing."""

    subject: re.Pattern[str] | None = None
    sender: re.Pattern[str] | None = None

    def matches(self, subject: str | None, sender: str | None) -> bool:
        if self.subject is None and self.sender is None:
            return False
        if self.subject is not None and not self.subject.search(subject or ""):
            return False
        if self.sender is not None and not self.sender.search(sender or ""):
            return False
        return True


@dataclass(frozen=True)
class SyncConfig:
    initial_lookback_days: int = 365  # parse_config defaults it to retention_days
    thread_delay_ms: int = 200
    full_recheck_days: int = 30
    stale_recheck_every: int = 10


@dataclass(frozen=True)
class LimitsConfig:
    max_attachment_bytes: int = 100 * 1024 * 1024
    max_extracted_chars: int = 200_000
    max_attempts: int = 5


@dataclass(frozen=True)
class FeedsConfig:
    thread_ids: tuple[str, ...] = ()
    rules: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class Config:
    data_dir: Path = DEFAULT_DATA_DIR
    retention_days: int = 365
    timezone: str = "Europe/Stockholm"  # for dates shown to the agent (R-F5)
    sync: SyncConfig = field(default_factory=SyncConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    feeds: FeedsConfig = field(default_factory=FeedsConfig)
    noise: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


CONFIG_TEMPLATE = """\
# admentum-mcp configuration. Created on first start and never overwritten.
# Every setting is listed with its default, commented out. To change one, remove
# the leading "# " and edit the value, then restart the server.
# `admentum-mcp config` prints this template, including settings added later.
# Credentials never go here: set ADMENTUM_USERNAME and ADMENTUM_PASSWORD in the
# environment of the server.

# Where state.db, logs and synced files live. This file stays in
# ~/.admentum-mcp even if the data is moved.
# data_dir = "~/.admentum-mcp"

# Days to keep the files of seen messages. Unseen messages are never pruned.
# retention_days = 365

# Time zone for dates shown to the agent.
# timezone = "Europe/Stockholm"

[sync]
# How far back the first sync reaches, in days. Default: retention_days.
# initial_lookback_days = 365
# Pause between fetching conversations, in milliseconds.
# thread_delay_ms = 200
# Only for sources without activity times: conversations active within this
# many days are checked on every sync, older ones every stale_recheck_every syncs.
# full_recheck_days = 30
# stale_recheck_every = 10

[limits]
# Attachments larger than this are not downloaded (bytes; 100 MB).
# max_attachment_bytes = 104857600
# Extracted text longer than this is cut (characters).
# max_extracted_chars = 200000
# Attempts for a failed download or extraction before giving up.
# max_attempts = 5

[feeds]
# Conversations treated as feeds, where each reply is a bulletin that stands
# alone: by conversation ID, or by rules matching the subject and/or the sender
# of the first message (regular expressions). The ID list wins over the rules.
# Example: rules = [ { subject = "(?i)veckobrev", sender = "" } ]
# thread_ids = []
# rules = []

[noise]
# Messages matching these rules (same shape as feeds.rules, using the
# conversation subject and the message sender) are marked seen automatically.
# rules = []
"""


def ensure_default_config(path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Create the commented template at the default path if it is missing (R-S3). Never overwrites."""
    if path.exists():
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(CONFIG_TEMPLATE)
    return True


def load_config(path: Path | None = None) -> Config:
    """Load config.toml. A missing file at the default path means all defaults."""
    if path is None:
        path = DEFAULT_CONFIG_PATH
        if not path.exists():
            return Config()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    return parse_config(data)


def parse_config(data: dict[str, Any]) -> Config:
    t = _Table(data, "")
    cfg = Config(
        data_dir=Path(t.get_str("data_dir", str(DEFAULT_DATA_DIR))).expanduser(),
        retention_days=(retention := t.get_int("retention_days", Config.retention_days)),
        timezone=_timezone(t),
        sync=_sync(t.table("sync"), retention),
        limits=_limits(t.table("limits")),
        feeds=_feeds(t.table("feeds")),
        noise=_noise(t.table("noise")),
    )
    t.done()
    return cfg


def _timezone(t: _Table) -> str:
    name = t.get_str("timezone", Config.timezone)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{t.key('timezone')}: unknown time zone {name!r}") from None
    return name


def _sync(t: _Table, retention_days: int) -> SyncConfig:
    d = SyncConfig()
    r = SyncConfig(
        initial_lookback_days=t.get_int("initial_lookback_days", retention_days),
        thread_delay_ms=t.get_int("thread_delay_ms", d.thread_delay_ms),
        full_recheck_days=t.get_int("full_recheck_days", d.full_recheck_days),
        stale_recheck_every=t.get_int("stale_recheck_every", d.stale_recheck_every, minimum=1),
    )
    t.done()
    return r


def _limits(t: _Table) -> LimitsConfig:
    d = LimitsConfig()
    r = LimitsConfig(
        max_attachment_bytes=t.get_int("max_attachment_bytes", d.max_attachment_bytes, minimum=1),
        max_extracted_chars=t.get_int("max_extracted_chars", d.max_extracted_chars, minimum=1),
        max_attempts=t.get_int("max_attempts", d.max_attempts, minimum=1),
    )
    t.done()
    return r


def _feeds(t: _Table) -> FeedsConfig:
    d = FeedsConfig()
    ids = t.raw("thread_ids", list(d.thread_ids))
    if not isinstance(ids, list) or not all(isinstance(i, (str, int)) and not isinstance(i, bool) for i in ids):
        raise ConfigError(f"{t.key('thread_ids')}: expected a list of thread IDs")
    r = FeedsConfig(
        thread_ids=tuple(str(i) for i in ids),
        rules=tuple(_rule(rt) for rt in t.tables("rules")),
    )
    t.done()
    return r


def _noise(t: _Table) -> tuple[Rule, ...]:
    r = tuple(_rule(rt) for rt in t.tables("rules"))
    t.done()
    return r


def _rule(t: _Table) -> Rule:
    r = Rule(subject=t.regex("subject"), sender=t.regex("sender"))
    t.done()
    return r


class _Table:
    """Typed access to one TOML table that tracks keys, so unknown keys can be rejected."""

    def __init__(self, data: Any, prefix: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{prefix or '<root>'}: expected a table")
        self._data = data
        self._prefix = prefix
        self._seen: set[str] = set()

    def key(self, name: str) -> str:
        return f"{self._prefix}.{name}" if self._prefix else name

    def has(self, name: str) -> bool:
        return name in self._data

    def raw(self, name: str, default: Any) -> Any:
        self._seen.add(name)
        return self._data.get(name, default)

    def get_str(self, name: str, default: str) -> str:
        v = self.raw(name, default)
        if not isinstance(v, str):
            raise ConfigError(f"{self.key(name)}: expected a string")
        return v

    def get_int(self, name: str, default: int, minimum: int = 0) -> int:
        v = self.raw(name, default)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"{self.key(name)}: expected an integer")
        if v < minimum:
            raise ConfigError(f"{self.key(name)}: must be at least {minimum}")
        return v

    def get_bool(self, name: str, default: bool) -> bool:
        v = self.raw(name, default)
        if not isinstance(v, bool):
            raise ConfigError(f"{self.key(name)}: expected true or false")
        return v

    def regex(self, name: str) -> re.Pattern[str] | None:
        v = self.get_str(name, "")
        if not v:
            return None
        try:
            return re.compile(v)
        except re.error as e:
            raise ConfigError(f"{self.key(name)}: invalid regex: {e}") from None

    def table(self, name: str) -> _Table:
        return _Table(self.raw(name, {}), self.key(name))

    def tables(self, name: str) -> list[_Table]:
        v = self.raw(name, [])
        if not isinstance(v, list):
            raise ConfigError(f"{self.key(name)}: expected a list of tables")
        return [_Table(item, f"{self.key(name)}[{i}]") for i, item in enumerate(v)]

    def done(self) -> None:
        unknown = sorted(set(self._data) - self._seen)
        if unknown:
            raise ConfigError(f"{self.key(unknown[0])}: unknown key")


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ. Already-set variables win. Dev convenience only."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def credentials_from_env() -> Credentials:
    """Credentials are only ever read from the environment, never logged."""
    missing = [k for k in ("ADMENTUM_USERNAME", "ADMENTUM_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise ConfigError(f"missing environment variable: {', '.join(missing)}")
    return Credentials(os.environ["ADMENTUM_USERNAME"], os.environ["ADMENTUM_PASSWORD"])
