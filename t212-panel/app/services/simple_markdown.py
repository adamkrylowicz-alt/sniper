"""
app/services/simple_markdown.py
================================
Minimalny konwerter markdown->HTML, WYŁĄCZNIE pod docs/help_pl.md i
docs/help_en.md (10.08.2026) - obsługuje dokładnie te konstrukcje, których
te dwa pliki używają: # / ## nagłówki, **pogrubienie**, `kod`, listy
punktowane (- ) i numerowane (1. ), akapity. Świadomie NIE `pip install
markdown` - jedna nowa zależność na PyPI dla dwóch statycznych,
kontrolowanych przez nas plików to za duży koszt (instalacja na
Python 3.8 na NAS-ie, dev+prod) względem tego jak mało trzeba sparsować.
"""
from __future__ import annotations

import html
import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`(.+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_OL_RE = re.compile(r"^\d+\.\s+(.*)$")


def _inline(text: str) -> str:
    text = html.escape(text)
    text = _LINK_RE.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    return text


def render(markdown_text: str) -> str:
    parts: list[str] = []
    list_tag: str | None = None  # "ul" albo "ol", gdy aktualnie w liście

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = None

    for raw_line in markdown_text.split("\n"):
        line = raw_line.strip()
        if not line:
            # Pusta linia NIE zamyka listy - w naszych plikach oddziela
            # kolejne punkty tej samej listy (czytelniejsze w edytorze),
            # a nie różne listy - lista zamyka się dopiero na nagłówku albo
            # akapicie zwykłego tekstu (patrz gałęzie niżej).
            continue
        if line.startswith("### "):
            close_list()
            parts.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            close_list()
            parts.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            close_list()
            parts.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("- "):
            if list_tag != "ul":
                close_list()
                parts.append("<ul>")
                list_tag = "ul"
            parts.append(f"<li>{_inline(line[2:])}</li>")
        elif (m := _OL_RE.match(line)):
            if list_tag != "ol":
                close_list()
                parts.append("<ol>")
                list_tag = "ol"
            parts.append(f"<li>{_inline(m.group(1))}</li>")
        else:
            close_list()
            parts.append(f"<p>{_inline(line)}</p>")
    close_list()
    return "\n".join(parts)
