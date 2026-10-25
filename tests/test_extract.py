"""Build step 3: extraction, limits and failures."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import START, Env
from docs import make_docx, make_pdf
from admentum_mcp.config import Config, LimitsConfig
from admentum_mcp.extract import DOCX, PDF, extract, is_supported
from admentum_mcp.fake_source import FakeAttachment
from admentum_mcp.sync import run_startup

BULLETIN = ["Veckobrev", "Utflykt till Skansen på torsdag.", "Ta med matsäck och överdragskläder."]


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# extraction


def test_swedish_pdf(tmp_path: Path) -> None:
    r = extract(write(tmp_path, "a.pdf", make_pdf(BULLETIN)), PDF, Config())
    assert r.status == "ok" and not r.truncated
    assert "Utflykt till Skansen på torsdag." in r.text and "överdragskläder" in r.text


def test_swedish_docx(tmp_path: Path) -> None:
    r = extract(write(tmp_path, "a.docx", make_docx(["Läsläxa för åk 3", "Läs kapitel 4 till fredag."])), DOCX, Config())
    assert r.status == "ok" and "Läsläxa för åk 3" in r.text and "fredag" in r.text


def test_pdf_without_text_layer_fails_with_no_text_layer(tmp_path: Path) -> None:
    r = extract(write(tmp_path, "scan.pdf", make_pdf([])), PDF, Config())
    assert (r.status, r.error) == ("failed", "no_text_layer")


def test_broken_pdf_fails(tmp_path: Path) -> None:
    r = extract(write(tmp_path, "bad.pdf", b"%PDF-1.4 this is not a pdf"), PDF, Config())
    assert r.status == "failed" and r.error


def test_unsupported_type(tmp_path: Path) -> None:
    r = extract(write(tmp_path, "bild.heic", b"\0\0"), "image/heic", Config(), filename="bild.heic")
    assert r.status == "unsupported" and "image/heic" in r.error and ".heic" in r.error


def test_generic_mime_falls_back_to_extension(tmp_path: Path) -> None:
    p = write(tmp_path, "download", make_pdf(BULLETIN))
    assert is_supported("application/octet-stream", "Veckobrev.pdf")
    assert not is_supported("application/octet-stream", "Veckobrev")
    assert extract(p, "application/octet-stream", Config(), filename="Veckobrev.pdf").status == "ok"


def test_long_text_is_truncated(tmp_path: Path) -> None:
    cfg = replace(Config(), limits=LimitsConfig(max_extracted_chars=20))
    r = extract(write(tmp_path, "a.pdf", make_pdf(BULLETIN)), PDF, cfg)
    assert r.status == "ok" and r.truncated and len(r.text) == 20


# through sync


def pdf_att(name: str, lines: list[str]) -> FakeAttachment:
    return FakeAttachment(name, PDF, make_pdf(lines))


def test_pdf_attachment_text_is_stored_beside_the_file(env: Env) -> None:
    env.source.add_thread("1", "Veckobrev 3B")
    env.source.add_message("1", "a", START, "", attachments=[("f", pdf_att("Veckobrev.pdf", ["Vecka 46", *BULLETIN]))])
    env.sync()
    (att,) = env.query("SELECT * FROM attachments WHERE source_ref = 'f'")
    assert att["extract_status"] == "ok" and att["text_chars"] > 50
    assert "Skansen på torsdag" in (env.item_dir("a") / "attachments" / "Veckobrev.pdf.txt").read_text()


def test_body_is_kept_whole(env: Env) -> None:
    """No quote stripping: Admentum does not quote, and heuristics would cut real content."""
    body = "<p>Den här veckan skrev vi om:</p><ul><li>vikingar</li></ul><p>--</p><p>Mvh</p>"
    env.source.add_thread("1", "Info")
    env.source.add_message("1", "a", START, body)
    env.sync()
    assert (env.item_dir("a") / "body.txt").read_text() == "Den här veckan skrev vi om:\n\n- vikingar\n\n--\n\nMvh"


def test_scanned_pdf_is_not_retried_but_broken_pdf_is(env: Env) -> None:
    env.source.add_thread("1", "A")
    env.source.add_message("1", "a", START, "x", attachments=[
        ("scan", pdf_att("scan.pdf", [])),
        ("bad", FakeAttachment("bad.pdf", PDF, b"%PDF-1.4 broken")),
    ])
    r = env.sync()
    assert r["attachment_failures"] == 1  # the scan is viewable, so not a failure (R-V6)
    (item,) = env.query("SELECT status FROM items")
    assert item["status"] == "partial"  # because of the broken PDF
    for _ in range(3):
        env.clock.advance(hours=1)
        env.sync()
    att = {a["source_ref"]: a for a in env.query("SELECT * FROM attachments")}
    assert att["scan"]["error"] == "no_text_layer" and att["scan"]["attempts"] == 1
    assert att["bad"]["extract_status"] == "failed" and att["bad"]["attempts"] == 4


def test_startup_reextracts_unsupported_types_that_are_now_supported(env: Env, monkeypatch) -> None:
    import admentum_mcp.extract as ex

    monkeypatch.setattr(ex, "file_type", lambda mime, filename: None)  # as if no PDF extractor existed
    env.source.add_thread("1", "Veckobrev")
    env.source.add_message("1", "a", START, "", attachments=[("f", pdf_att("Brev v.48.pdf", BULLETIN))])
    r = env.sync()
    assert r["unsupported_attachments"] == 1
    monkeypatch.undo()

    assert run_startup(env.cfg, clock=env.clock) == {"result": "ok", "reextracted": 1}
    (att,) = env.query("SELECT extract_status, attempts FROM attachments")
    assert att["extract_status"] == "ok"
    assert run_startup(env.cfg, clock=env.clock)["reextracted"] == 0


def test_startup_skips_while_sync_holds_lock(env: Env) -> None:
    from filelock import FileLock

    env.data.ensure()
    with FileLock(str(env.data.lock)):
        assert run_startup(env.cfg) == {"result": "already_running"}
