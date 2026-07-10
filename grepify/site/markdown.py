"""Minimal, deterministic markdown -> HTML for digest bodies (E4, GRP-43).

A digest's ``body_md`` (PRD §6) is composed markdown; the digest detail page has
to render it. Rather than take a heavyweight markdown dependency (and its
version-to-version output drift, which would break byte-stable snapshots,
F-SIT-08), this renders the *small, known* subset the digest generator emits:

- paragraphs (blank-line separated),
- unordered lists (lines starting ``- ``),
- inline ``**bold**``.

Everything is HTML-escaped first, so a keyword or model output can never inject
markup (the same XSS posture as the autoescaped templates). Output is a
byte-stable function of the input - no clock, no locale, no external state.

Failure modes
-------------
Pure string transform; never raises. Unrecognized markdown (headings, links,
images, code fences) is emitted as escaped paragraph text rather than
interpreted - a safe, lossy fallback, since the generator does not produce it.
"""

from __future__ import annotations

import re
from html import escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    """Escape, then apply inline ``**bold**`` (the only inline markup emitted)."""
    escaped = escape(text, quote=False)
    return _BOLD.sub(r"<strong>\1</strong>", escaped)


def render_markdown(body_md: str) -> str:
    """Render the digest markdown subset to a byte-stable HTML fragment."""
    blocks: list[str] = []
    for raw_block in re.split(r"\n\s*\n", body_md.strip()):
        block = raw_block.strip("\n")
        if not block.strip():
            continue
        lines = block.splitlines()
        if all(line.lstrip().startswith("- ") for line in lines if line.strip()):
            items = "".join(
                f"<li>{_inline(line.lstrip()[2:].strip())}</li>" for line in lines if line.strip()
            )
            blocks.append(f"<ul>{items}</ul>")
        else:
            paragraph = _inline(" ".join(line.strip() for line in lines))
            blocks.append(f"<p>{paragraph}</p>")
    return "\n".join(blocks)
