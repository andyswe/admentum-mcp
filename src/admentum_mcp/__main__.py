"""CLI entry point (spec 7.2). Output is JSON on stdout; logs go to the log file and stderr."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_TEMPLATE,
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    credentials_from_env,
    ensure_default_config,
    load_config,
    load_dotenv,
)
from .files import DataDir, FILE_MODE


def setup_logging(cfg: Config, *, stderr: bool) -> None:
    data = DataDir(cfg.data_dir)
    data.ensure()
    log_file = data.logs / "admentum-mcp.log"
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    log_file.chmod(FILE_MODE)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger("admentum_mcp")
    root.setLevel(logging.INFO)
    root.handlers[:] = [handler]
    if stderr:
        err = logging.StreamHandler(sys.stderr)
        err.setFormatter(fmt)
        root.addHandler(err)


def make_source(cfg: Config, kind: str) -> Any:
    if kind == "fake":
        from .fake_source import demo_source

        return demo_source()
    from .admentum import AdmentumSource

    return AdmentumSource(cfg, credentials_from_env())


REQUIRED_MODULES = {  # import name -> package to install (R-S1)
    "mcp": "mcp",
    "httpx": "httpx",
    "bs4": "beautifulsoup4",
    "filelock": "filelock",
    "tzdata": "tzdata",
    "markitdown": "markitdown[pdf,docx]",
    "pdfminer": "markitdown[pdf,docx]",
    "mammoth": "markitdown[pdf,docx]",
}


def missing_packages() -> list[str]:
    import importlib.util

    return sorted({pkg for mod, pkg in REQUIRED_MODULES.items() if importlib.util.find_spec(mod) is None})


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    """MCP over stdio. stdout carries only the protocol (R-P8); logs go to the log file."""
    missing = missing_packages()
    if missing:
        print(f"missing required packages: {', '.join(missing)}", file=sys.stderr)
        return 1
    setup_logging(cfg, stderr=False)
    from .server import Inbox, build_server
    from .sync import run_startup

    run_startup(cfg)
    if args.source == "fake":
        fake = make_source(cfg, "fake")  # one instance, so its state persists across syncs
        factory = lambda: fake  # noqa: E731
    else:
        factory = lambda: make_source(cfg, "admentum")  # noqa: E731 - credentials are read per sync
    build_server(Inbox(cfg, factory)).run("stdio")
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    from .sync import run_sync

    source = make_source(cfg, args.source)
    try:
        result = run_sync(cfg, source, dry_run=args.dry_run)
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    print(json.dumps(result, indent=2))
    return 0 if result.get("result") in ("ok", "dry_run", "partial") else 1


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    from .server import Inbox

    print(json.dumps(Inbox(cfg, lambda: make_source(cfg, args.source)).status(), indent=2, ensure_ascii=False))
    return 0


def cmd_prune(cfg: Config, args: argparse.Namespace) -> int:
    from .server import Inbox

    result = Inbox(cfg, lambda: make_source(cfg, args.source)).prune(args.older_than_days, dry_run=not args.apply)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if result.get("result") == "already_running" else 0


def cmd_reset(cfg: Config, args: argparse.Namespace) -> int:
    """Delete state.db and data/ inside data_dir only; config.toml and logs stay (development aid)."""
    import shutil

    from filelock import FileLock, Timeout

    if not args.yes:
        print("reset deletes all synced messages and state; run again with --yes", file=sys.stderr)
        return 2
    data = DataDir(cfg.data_dir)
    if not data.root.exists():
        print(json.dumps({"result": "ok", "deleted": []}))
        return 0
    lock = FileLock(str(data.lock), timeout=0, mode=0o600)
    try:
        lock.acquire()
    except Timeout:
        print(json.dumps({"result": "already_running"}))
        return 1
    try:
        deleted = []
        for p in (data.state_db, data.state_db.with_name("state.db-wal"), data.state_db.with_name("state.db-shm")):
            if p.exists():
                p.unlink()
                deleted.append(p.name)
        if data.data.exists():
            shutil.rmtree(data.data)
            deleted.append("data/")
    finally:
        lock.release()
    print(json.dumps({"result": "ok", "deleted": deleted}))
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    ap = argparse.ArgumentParser(prog="admentum-mcp")
    ap.add_argument("--config", type=Path, help="path to config.toml")
    ap.add_argument("--source", choices=("admentum", "fake"), default="admentum")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    p_sync = sub.add_parser("sync", help="sync messages from the source")
    p_sync.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="counts and health, as JSON")
    p_prune = sub.add_parser("prune", help="list (or with --apply delete) files of old seen items")
    p_prune.add_argument("--older-than-days", type=int, default=None)
    p_prune.add_argument("--apply", action="store_true")
    p_reset = sub.add_parser("reset", help="delete state.db and data/ (development)")
    p_reset.add_argument("--yes", action="store_true")
    sub.add_parser("config", help="print the config template: every setting with its default")
    args = ap.parse_args(argv)

    if args.command == "config":
        print(CONFIG_TEMPLATE, end="")
        return 0
    # R-S3: only the default path is created; a missing --config file stays an error.
    if args.config is None and ensure_default_config(DEFAULT_CONFIG_PATH):
        print(f"created {DEFAULT_CONFIG_PATH} with every setting commented out", file=sys.stderr)
    load_dotenv(Path.cwd() / ".env")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.command in (None, "serve"):
        return cmd_serve(cfg, args)
    if args.command == "sync":
        setup_logging(cfg, stderr=True)
        try:
            return cmd_sync(cfg, args)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
    setup_logging(cfg, stderr=True)
    from mcp.server.mcpserver.exceptions import ToolError  # after the R-S1 check point, not at import

    commands = {"status": cmd_status, "prune": cmd_prune, "reset": cmd_reset}
    try:
        return commands[args.command](cfg, args)
    except ToolError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
