"""Spec 5.4: images and PDF pages for a vision-capable agent."""

from __future__ import annotations

import base64
import io
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client
from PIL import Image

from conftest import START, Env
from docs import make_pdf
from admentum_mcp import images
from admentum_mcp.extract import PDF
from admentum_mcp.fake_source import FakeAttachment
from admentum_mcp.server import Inbox, build_server


def png(size: tuple[int, int], mode: str = "RGB", color=(200, 30, 30), **save) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format="PNG", **save)
    return out.getvalue()


def jpeg_with_gps_rotated(size: tuple[int, int]) -> bytes:
    im = Image.new("RGB", size, (10, 120, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
    exif[0x8825] = {1: "N", 2: (59.0, 20.0, 0.0), 3: "E", 4: (18.0, 4.0, 0.0)}  # GPS
    out = io.BytesIO()
    im.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def opened(r: images.Rendered) -> Image.Image:
    return Image.open(io.BytesIO(r.jpeg))


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_kind() -> None:
    assert images.kind("image/png", "a.png") == "image"
    assert images.kind("application/octet-stream", "foto.JPG") == "image"
    assert images.kind(PDF, "a.pdf") == "pdf"
    assert images.kind("image/heic", "a.heic") is None
    assert images.kind("application/vnd.ms-excel", "a.xls") is None


def test_large_image_is_scaled_down_and_encoded_as_jpeg(tmp_path: Path) -> None:
    r = images.render(write(tmp_path, "big.png", png((4000, 3000))), "image/png", "big.png")
    assert (r.width, r.height) == (1568, 1176) and (r.page, r.pages) == (1, 1)
    im = opened(r)
    assert im.format == "JPEG" and im.mode == "RGB"


def test_small_image_is_not_upscaled(tmp_path: Path) -> None:
    r = images.render(write(tmp_path, "s.png", png((300, 200))), "image/png", "s.png")
    assert (r.width, r.height) == (300, 200)


def test_transparency_is_flattened_onto_white(tmp_path: Path) -> None:
    r = images.render(write(tmp_path, "t.png", png((50, 50), "RGBA", (0, 0, 0, 0))), "image/png", "t.png")
    assert opened(r).getpixel((25, 25)) == (255, 255, 255)


def test_orientation_applied_and_all_metadata_dropped(tmp_path: Path) -> None:
    r = images.render(write(tmp_path, "p.jpg", jpeg_with_gps_rotated((400, 200))), "image/jpeg", "p.jpg")
    assert (r.width, r.height) == (200, 400)  # rotated per EXIF before it was dropped
    im = opened(r)
    assert len(im.getexif()) == 0 and "exif" not in im.info and "icc_profile" not in im.info


def test_decompression_bomb_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(images, "MAX_PIXELS", 10_000)
    with pytest.raises(images.ImageError, match="too large"):
        images.render(write(tmp_path, "b.png", png((200, 200))), "image/png", "b.png")


def test_corrupt_image_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(images.ImageError):
        images.render(write(tmp_path, "c.png", b"\x89PNG not really"), "image/png", "c.png")


def test_pdf_pages_render_at_max_side(tmp_path: Path) -> None:
    p = write(tmp_path, "a.pdf", make_pdf(["Vecka 46"]))
    assert images.page_count(p, PDF, "a.pdf") == 1
    r = images.render(p, PDF, "a.pdf", page=1)
    assert max(r.width, r.height) == 1568 and (r.page, r.pages) == (1, 1)
    with pytest.raises(images.ImageError, match="between 1 and 1"):
        images.render(p, PDF, "a.pdf", page=2)


def test_image_has_only_page_one(tmp_path: Path) -> None:
    with pytest.raises(images.ImageError):
        images.render(write(tmp_path, "s.png", png((10, 10))), "image/png", "s.png", page=2)


# through the MCP tool


def call_image(env: Env, args: dict) -> object:
    server = build_server(Inbox(env.cfg, lambda: env.source, clock=env.clock))

    async def run():
        async with Client(server) as client:
            return await client.call_tool("get_attachment_image", args)

    return anyio.run(run)


@pytest.fixture
def with_attachments(env: Env) -> Env:
    env.source.add_thread("t", "Bilder")
    env.source.add_message("t", "m", START, "<p>Se bilagor</p>", attachments=[
        ("i", FakeAttachment("Affisch höstfest.png", "image/png", png((2000, 1000)))),
        ("s", FakeAttachment("Inskannat brev.pdf", PDF, make_pdf([]))),
        ("h", FakeAttachment("bild.heic", "image/heic", b"heic")),
    ])
    from admentum_mcp.sync import run_sync

    run_sync(env.cfg, env.source, clock=env.clock, sleep=lambda s: None)
    return env


def test_tool_returns_note_and_jpeg(with_attachments: Env) -> None:
    result = call_image(with_attachments, {"id": "m", "filename": "Affisch höstfest.png"})
    assert not result.is_error
    text, image = result.content
    assert text.type == "text" and "Affisch höstfest.png" in text.text and "untrusted" in text.text
    assert image.type == "image" and image.mime_type == "image/jpeg"
    im = Image.open(io.BytesIO(base64.b64decode(image.data)))
    assert im.size == (1568, 784)


def test_tool_renders_scanned_pdf_page(with_attachments: Env) -> None:
    result = call_image(with_attachments, {"id": "m", "filename": "Inskannat brev.pdf", "page": 1})
    assert not result.is_error and result.content[1].type == "image"


def test_tool_errors(with_attachments: Env) -> None:
    r = call_image(with_attachments, {"id": "m", "filename": "nope.png"})
    assert r.is_error and "Affisch höstfest.png" in r.content[0].text  # lists the real names
    r = call_image(with_attachments, {"id": "m", "filename": "bild.heic"})
    assert r.is_error and "cannot be shown as an image" in r.content[0].text
    r = call_image(with_attachments, {"id": "m", "filename": "Inskannat brev.pdf", "page": 5})
    assert r.is_error and "between 1 and 1" in r.content[0].text


def test_item_with_only_a_scan_is_not_partial(env: Env) -> None:
    env.source.add_thread("t", "Brev")
    env.source.add_message("t", "m", START - timedelta(hours=1), "",
                           attachments=[("s", FakeAttachment("brev.pdf", PDF, make_pdf([])))])
    from admentum_mcp.sync import run_sync

    r = run_sync(env.cfg, env.source, clock=env.clock, sleep=lambda s: None)
    assert r["attachment_failures"] == 0
    (item,) = env.query("SELECT status FROM items")
    assert item["status"] == "ok"
