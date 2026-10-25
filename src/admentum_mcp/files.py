"""Data directory layout, atomic writes, safe names and permissions (spec 3)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any

DIR_MODE = 0o700
FILE_MODE = 0o600
MAX_NAME_BYTES = 200  # leaves room for ".txt" and a collision suffix under the 255-byte limit

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")


class DataDir:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_db = root / "state.db"
        self.lock = root / "sync.lock"
        self.logs = root / "logs"
        self.data = root / "data"
        self.tmp = self.data / ".tmp"

    def ensure(self) -> None:
        for d in (self.root, self.logs, self.data, self.tmp):
            make_dir(d)

    def clear_tmp(self) -> None:
        """Remove leftovers from interrupted item writes (R-F2, R-Y8)."""
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        make_dir(self.tmp)

    def new_stage(self) -> Path:
        stage = self.tmp / uuid.uuid4().hex
        make_dir(stage)
        return stage

    def thread_rel(self, thread_id: str) -> str:
        return f"thread_{safe_id(thread_id)}"

    def item_rel(self, thread_id: str, item_id: str) -> str:
        return f"{self.thread_rel(thread_id)}/items/{safe_id(item_id)}"

    def commit_stage(self, stage: Path, rel: str) -> Path:
        """Move a fully written stage into place (R-F2).

        A directory already at the target has no committed row (rows are only committed after
        the move), so it is a leftover from a crash and is replaced.
        """
        final = self.data / rel
        make_dir(final.parent.parent)
        make_dir(final.parent)
        if final.exists():
            shutil.rmtree(final)
        secure_tree(stage)
        os.replace(stage, final)
        return final


def safe_id(value: str) -> str:
    """Directory name for a source ID. IDs that are not plain tokens are hashed."""
    if _SAFE_ID.match(value):
        return value
    return "h" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def safe_name(name: str, taken: set[str]) -> str:
    """On-disk file name (R-F3). Adds the result to `taken`."""
    name = unicodedata.normalize("NFC", name)
    name = "".join("_" if c == "/" or c == "\\" or unicodedata.category(c) == "Cc" else c for c in name)
    name = name.lstrip(".").rstrip(". ")
    if not name:
        name = "attachment"
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode("utf-8")) > 16:
        stem, ext = name, ""
    stem = _truncate_bytes(stem, MAX_NAME_BYTES - len(ext.encode("utf-8")) - 1)
    candidate = f"{stem}.{ext}" if ext else stem
    n = 2
    while candidate.casefold() in {t.casefold() for t in taken}:
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        n += 1
    taken.add(candidate)
    return candidate


def _truncate_bytes(s: str, limit: int) -> str:
    while len(s.encode("utf-8")) > limit:
        s = s[:-1]
    return s


def make_dir(path: Path) -> None:
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)


def secure_tree(root: Path) -> None:
    os.chmod(root, DIR_MODE)
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            os.chmod(os.path.join(dirpath, d), DIR_MODE)
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), FILE_MODE)


def write_text(path: Path, text: str) -> None:
    """Atomic UTF-8 write, NFC-normalized (R-F4)."""
    write_bytes(path, unicodedata.normalize("NFC", text).encode("utf-8"))


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))


def write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
