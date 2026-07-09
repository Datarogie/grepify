"""YAKE fallback extractor (GRP-22) — the deterministic, offline safety net.

Implements the :class:`~grepify.extract.batcher.FallbackExtractor` Protocol
with `YAKE <https://github.com/LIAAD/yake>`_, a statistical (no model, no
network) keyword extractor. The batcher (GRP-21) calls this whenever the LLM
is down, over budget, or keeps returning malformed responses (PRD §5
"deterministic fallback ... site still builds").

Unlike the LLM path, the batcher does **not** sanity-check fallback output
(:func:`~grepify.extract.batcher._fallback_rows` trusts it directly), so this
module applies the same trim + length + no-URL rules GRP-21 applies to LLM
keywords (F-EXT-02's sanity bar), keeping ``item_keywords`` rows uniform
regardless of ``method``.

Failure modes
-------------
:meth:`YakeFallbackExtractor.extract` must not raise for ordinary items (it is
the last line of defense once the LLM has already failed). An item with no
extractable text (empty title/summary, or text YAKE finds nothing salient in —
verified offline against short/blank/emoji-only/URL-only/degenerate input)
simply maps to an empty keyword list, same as an LLM response that legitimately
finds nothing (F-EXT-02: "an empty keyword list for an item is valid").
"""

from __future__ import annotations

from collections.abc import Sequence

import yake

from grepify.extract.batcher import DEFAULT_MAX_KEYWORDS
from grepify.models import Item

_MIN_KEYWORD_LEN = 2
_MAX_KEYWORD_LEN = 60
_DEFAULT_LANGUAGE = "en"
_DEFAULT_NGRAM_SIZE = 3  # short phrases ("agentic coding"), matching prompt-v1's style


class YakeFallbackExtractor:
    """Deterministic keyword extraction over an item's title + summary.

    ``language``/``ngram_size`` are fixed at construction (v1 is English-only,
    PRD §7 ``limits.transcript_langs: [en]``); per-item language switching is
    not implemented — out of scope until a non-English source exists.
    """

    def __init__(
        self,
        *,
        max_keywords: int = DEFAULT_MAX_KEYWORDS,
        language: str = _DEFAULT_LANGUAGE,
        ngram_size: int = _DEFAULT_NGRAM_SIZE,
    ) -> None:
        self._max_keywords = max_keywords
        self._yake = yake.KeywordExtractor(lan=language, n=ngram_size, top=max_keywords)

    def extract(self, items: Sequence[Item]) -> dict[str, list[str]]:
        return {item.item_id: self._extract_one(item) for item in items}

    def _extract_one(self, item: Item) -> list[str]:
        text = _combined_text(item)
        if not text:
            return []
        scored = self._yake.extract_keywords(text)
        cleaned = [sane for keyword, _score in scored if (sane := _clean(keyword)) is not None]
        return cleaned[: self._max_keywords]


def _combined_text(item: Item) -> str:
    if item.summary:
        return f"{item.title}\n{item.summary}"
    return item.title


def _clean(keyword: str) -> str | None:
    """Trim + sanity-check one YAKE keyword; ``None`` if it fails the bar."""
    trimmed = keyword.strip()
    if not (_MIN_KEYWORD_LEN <= len(trimmed) <= _MAX_KEYWORD_LEN):
        return None
    lowered = trimmed.lower()
    if "://" in lowered or lowered.startswith("www."):
        return None
    return trimmed
