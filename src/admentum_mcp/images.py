"""Images and PDF pages for a vision-capable agent (spec 5.4, R-V1 to R-V3)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from .extract import PDF, file_type

MAX_SIDE = 1568  # px, long side
JPEG_QUALITY = 85
MAX_PIXELS = 50_000_000  # refuse decompression bombs
MAX_PDF_SCALE = 4.0

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ImageError(Exception):
    """The attachment cannot be shown as an image."""


@dataclass(frozen=True)
class Rendered:
    jpeg: bytes
    page: int
    pages: int
    width: int  # of the returned image
    height: int


def kind(mime: str | None, filename: str | None) -> str | None:
    """'image', 'pdf', or None if the attachment is not viewable (R-V1)."""
    m = (mime or "").split(";")[0].strip().lower()
    if m in IMAGE_TYPES or (m in ("", "application/octet-stream") and Path(filename or "").suffix.lower() in _IMAGE_EXTENSIONS):
        return "image"
    if file_type(mime, filename) == PDF:
        return "pdf"
    return None


def page_count(path: Path, mime: str | None, filename: str | None) -> int:
    k = kind(mime, filename)
    if k == "image":
        return 1
    if k == "pdf":
        import pypdfium2 as pdfium

        try:
            with _open_pdf(path) as pdf:
                return len(pdf)
        except pdfium.PdfiumError as e:
            raise ImageError(f"cannot open PDF: {e}") from None
    raise ImageError("not a viewable type")


def render(path: Path, mime: str | None, filename: str | None, page: int = 1) -> Rendered:
    """One image, normalized per R-V3: oriented, metadata dropped, flattened, scaled, JPEG."""
    k = kind(mime, filename)
    if k == "image":
        if page != 1:
            raise ImageError("an image has only page 1")
        return _encode(_load_image(path), 1, 1)
    if k == "pdf":
        return _render_pdf(path, page)
    raise ImageError("not a viewable type")


def _load_image(path: Path):  # -> PIL.Image.Image
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        im = Image.open(path)
    except (UnidentifiedImageError, OSError) as e:
        raise ImageError(f"cannot read image: {type(e).__name__}") from None
    w, h = im.size
    if w * h > MAX_PIXELS:
        raise ImageError(f"image too large ({w}x{h} px)")
    im.seek(0)  # first frame of an animation
    im = ImageOps.exif_transpose(im)  # apply orientation before EXIF is dropped
    return im


def _render_pdf(path: Path, page: int) -> Rendered:
    import pypdfium2 as pdfium

    try:
        with _open_pdf(path) as pdf:
            pages = len(pdf)
            if not 1 <= page <= pages:
                raise ImageError(f"page must be between 1 and {pages}")
            p = pdf[page - 1]
            w, h = p.get_size()  # points
            scale = min(MAX_SIDE / max(w, h, 1), MAX_PDF_SCALE)
            im = p.render(scale=scale).to_pil()
    except pdfium.PdfiumError as e:
        raise ImageError(f"cannot render PDF: {e}") from None
    return _encode(im, page, pages)


class _open_pdf:
    """pypdfium2 documents must be closed explicitly."""

    def __init__(self, path: Path) -> None:
        import pypdfium2 as pdfium

        self.pdf = pdfium.PdfDocument(str(path))

    def __enter__(self):  # -> pdfium.PdfDocument
        return self.pdf

    def __exit__(self, *exc: object) -> None:
        self.pdf.close()


def _encode(im, page: int, pages: int) -> Rendered:  # im: PIL.Image.Image
    from PIL import Image

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    # A fresh RGB image saved without exif/icc arguments carries no metadata (GPS included).
    clean = Image.new("RGB", im.size)
    clean.paste(im)
    clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return Rendered(out.getvalue(), page, pages, clean.width, clean.height)
