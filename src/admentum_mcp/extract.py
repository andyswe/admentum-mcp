"""Attachment text extraction (spec 5.3): PDF and DOCX via MarkItDown, plain text directly."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from .config import Config

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT = "text/plain"

_BY_EXTENSION = {".pdf": PDF, ".docx": DOCX, ".txt": TEXT}
_GENERIC = {"", "application/octet-stream", "binary/octet-stream", "application/x-download"}


@dataclass(frozen=True)
class Extracted:
    status: str  # ok | failed | unsupported
    text: str = ""
    truncated: bool = False
    error: str | None = None


def file_type(mime: str | None, filename: str | None) -> str | None:
    """The extractor type for a file, or None if unsupported. Generic MIME types fall back to the extension."""
    m = (mime or "").split(";")[0].strip().lower()
    if m in (PDF, DOCX, TEXT):
        return m
    if m in _GENERIC:
        return _BY_EXTENSION.get(Path(filename or "").suffix.lower())
    return None


def is_supported(mime: str | None, filename: str | None) -> bool:
    return file_type(mime, filename) is not None


def extract(path: Path, mime: str | None, cfg: Config, filename: str | None = None) -> Extracted:
    kind = file_type(mime, filename or path.name)
    if kind is None:
        ext = Path(filename or path.name).suffix.lower() or "no extension"
        return Extracted("unsupported", error=f"no extractor for {mime or 'unknown type'} ({ext})")
    try:
        if kind == TEXT:
            text = path.read_bytes().decode("utf-8", errors="replace")
        else:
            text = _markitdown(path, kind)
    except Exception as e:  # any extractor failure is recorded, never fatal (R-X4)
        return Extracted("failed", error=f"{type(e).__name__}: {e}"[:300])
    text = unicodedata.normalize("NFC", text).strip()
    if kind == PDF and not re.search(r"\w", text):
        # R-X5: no text layer (a scan). Not retried; the agent can view it as images (R-V1).
        return Extracted("failed", error="no_text_layer")
    limit = cfg.limits.max_extracted_chars
    if len(text) > limit:
        return Extracted("ok", text[:limit], truncated=True)
    return Extracted("ok", text)


def _markitdown(path: Path, kind: str) -> str:
    """Run only the converter for this type. MarkItDown's dispatcher falls back to reading
    the file as plain text when the real converter fails, which would store raw bytes as text."""
    from markitdown import StreamInfo

    ext = {PDF: ".pdf", DOCX: ".docx"}[kind]
    with path.open("rb") as f:
        result: Any = _converters()[kind].convert(f, StreamInfo(mimetype=kind, extension=ext))
    return result.markdown or ""


@cache
def _converters() -> dict[str, Any]:
    from markitdown.converters import DocxConverter, PdfConverter

    return {PDF: PdfConverter(), DOCX: DocxConverter()}
