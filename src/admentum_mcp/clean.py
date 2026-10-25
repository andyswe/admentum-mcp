"""HTML to text (R-C1)."""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString, Tag

_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "blockquote", "pre", "table", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "dt", "dd", "hr", "address", "figure",
}
_SKIP = {"script", "style", "head", "title", "meta", "noscript"}


def html_to_text(html: str) -> str:
    """Convert HTML to plain text, keeping paragraphs, lists and line breaks."""
    soup = BeautifulSoup(html or "", "html.parser")
    parts: list[str] = []
    _walk(soup, parts)
    text = "".join(parts)
    lines = [re.sub(r"[ \t\f\v ]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return unicodedata.normalize("NFC", text)


def _walk(node: Tag, parts: list[str]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if type(child) is NavigableString:  # skips comments, doctypes and CDATA
                parts.append(re.sub(r"\s+", " ", str(child)))
            continue
        if not isinstance(child, Tag) or child.name in _SKIP:
            continue
        name = child.name
        if name == "br":
            parts.append("\n")
        elif name == "li":
            parts.append("\n- ")
            _walk(child, parts)
        elif name in ("td", "th"):
            _walk(child, parts)
            parts.append(" | ")
        elif name in _BLOCK:
            parts.append("\n\n")
            _walk(child, parts)
            parts.append("\n\n")
        else:
            _walk(child, parts)

