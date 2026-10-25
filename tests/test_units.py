from __future__ import annotations

import re
from pathlib import Path

import pytest

from admentum_mcp.clean import html_to_text
from admentum_mcp.config import ConfigError, load_config, load_dotenv, parse_config
from admentum_mcp.files import safe_id, safe_name


def test_html_to_text_keeps_structure_and_decodes_entities() -> None:
    html = "<p>Hej&nbsp;alla!</p><p>Ta med:<br>matsäck</p><ul><li>stövlar</li><li>regnkläder</li></ul>&amp; tack"
    assert html_to_text(html) == "Hej alla!\n\nTa med:\nmatsäck\n\n- stövlar\n- regnkläder\n\n& tack"


def test_html_to_text_drops_scripts_and_comments() -> None:
    assert html_to_text("<style>p{}</style><!-- x --><script>alert(1)</script><p>ok</p>") == "ok"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Veckobrev v.46.pdf", "Veckobrev v.46.pdf"),
        ("..", "attachment"),
        (".", "attachment"),
        (".hidden.pdf", "hidden.pdf"),
        ("trailing. .", "trailing"),
        ("a\x00b\x1f.pdf", "a_b_.pdf"),
        ("dir/sub\\file.pdf", "dir_sub_file.pdf"),
    ],
)
def test_safe_name(name: str, expected: str) -> None:
    assert safe_name(name, set()) == expected


def test_safe_name_limits_length_and_avoids_collisions_case_insensitively() -> None:
    taken: set[str] = set()
    long = safe_name("ö" * 300 + ".pdf", taken)
    assert len(long.encode()) <= 200 and long.endswith(".pdf")
    assert safe_name("A.pdf", taken) == "A.pdf"
    assert safe_name("a.pdf", taken) == "a (2).pdf"


def test_safe_id() -> None:
    assert safe_id("12345") == "12345"
    assert safe_id("../x").startswith("h") and "/" not in safe_id("../x")


def test_config_defaults_when_default_file_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("admentum_mcp.config.DEFAULT_CONFIG_PATH", tmp_path / "nope.toml")
    cfg = load_config()
    assert cfg.retention_days == 365 and cfg.sync.initial_lookback_days == 365
    assert cfg.feeds.rules == ()  # no built-in guesses about message titles


def test_lookback_defaults_to_retention_unless_set() -> None:
    assert parse_config({"retention_days": 90}).sync.initial_lookback_days == 90
    cfg = parse_config({"retention_days": 90, "sync": {"initial_lookback_days": 30}})
    assert cfg.sync.initial_lookback_days == 30


def test_config_parses_full_example(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        """
data_dir = "~/elsewhere"
retention_days = 90
[sync]
thread_delay_ms = 0
[feeds]
thread_ids = [123, "456"]
rules = [ { subject = "(?i)nyhetsbrev", sender = "" } ]
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.data_dir == Path.home() / "elsewhere"
    assert cfg.feeds.thread_ids == ("123", "456")
    assert cfg.feeds.rules[0].matches("Nyhetsbrev", "x") and not cfg.feeds.rules[0].matches("Veckobrev", "x")


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"retention_days": "180"}, "retention_days"),
        ({"retention_days": True}, "retention_days"),
        ({"sync": {"thread_delay": 1}}, "sync.thread_delay"),
        ({"bogus": 1}, "bogus"),
        ({"feeds": {"rules": [{"subject": "("}]}}, "feeds.rules[0].subject"),
        ({"children": {"rules": []}}, "children"),  # removed setting: unknown key
        ({"limits": {"max_attempts": 0}}, "limits.max_attempts"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
    ],
)
def test_invalid_config_names_the_key(data: dict, key: str) -> None:
    with pytest.raises(ConfigError, match=key.replace("[", r"\[").replace("]", r"\]")):
        parse_config(data)


def test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")


def test_load_dotenv_does_not_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADMENTUM_USERNAME", "from-env")
    monkeypatch.delenv("ADMENTUM_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text("# c\nADMENTUM_USERNAME=from-file\nADMENTUM_PASSWORD='p w'\n")
    load_dotenv(env)
    import os
    assert os.environ["ADMENTUM_USERNAME"] == "from-env"
    assert os.environ["ADMENTUM_PASSWORD"] == "p w"


def test_database_with_another_schema_version_is_refused(tmp_path: Path) -> None:
    """No automatic migrations: an old database is never changed silently."""
    import sqlite3

    from admentum_mcp.store import Store

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema version 1, this server needs"):
        Store.open(db)
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1  # untouched
    conn.close()


# config template (R-S3)


def _uncomment(template: str) -> str:
    """Activate every commented setting line ('# key = value'), leaving prose comments alone."""
    return re.sub(r"(?m)^# (?=[a-z_]+ = )", "", template)


def test_config_template_documents_every_setting_with_its_default() -> None:
    import tomllib

    from admentum_mcp.config import CONFIG_TEMPLATE, Config, FeedsConfig, LimitsConfig, SyncConfig

    assert parse_config(tomllib.loads(CONFIG_TEMPLATE)) == Config()  # as written: all defaults
    active = tomllib.loads(_uncomment(CONFIG_TEMPLATE))
    assert parse_config(active) == Config()  # uncommented: still exactly the defaults
    expected = {
        "data_dir", "retention_days", "timezone",
        *(f"sync.{f}" for f in SyncConfig.__dataclass_fields__),
        *(f"limits.{f}" for f in LimitsConfig.__dataclass_fields__),
        *(f"feeds.{f}" for f in FeedsConfig.__dataclass_fields__),
        "noise.rules",
    }
    present = {f"{k}.{kk}" if isinstance(v, dict) else k for k, v in active.items() for kk in (v if isinstance(v, dict) else [None])}
    assert present == expected


def test_default_config_is_created_once_and_never_overwritten(tmp_path: Path) -> None:
    import stat

    from admentum_mcp.config import CONFIG_TEMPLATE, ensure_default_config

    path = tmp_path / "home" / ".admentum-mcp" / "config.toml"
    assert ensure_default_config(path) is True
    assert path.read_text() == CONFIG_TEMPLATE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.write_text("retention_days = 30\n")
    assert ensure_default_config(path) is False
    assert path.read_text() == "retention_days = 30\n"
