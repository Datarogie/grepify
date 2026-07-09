"""Extraction (E2): batch untagged items into LLM keyword calls (GRP-21).

Public surface later E2 issues (GRP-22 fallback, GRP-23 normalize, GRP-24 eval,
GRP-25 pipeline wiring) build on:

- :func:`run_extract` + :class:`ExtractResult` — the batcher: chunk items,
  call the LLM under its budget breaker, validate the strict-JSON response,
  retry a malformed batch once, then fall back deterministically.
- :class:`FallbackExtractor` — the Protocol the batcher calls when the LLM
  can't deliver; GRP-22 implements it with YAKE.
- :func:`build_messages` + :data:`PROMPT_VERSION` — prompt v1.

The LLM provider itself (client, budget breaker, retries, ``llm_log``) is
:mod:`grepify.llm` (GRP-20).

Failure modes
-------------
None of its own — a re-export aggregator. See :mod:`grepify.extract.batcher`
and :mod:`grepify.extract.prompt` for module-level failure modes.
"""

from __future__ import annotations

from grepify.extract.batcher import (
    DEFAULT_MAX_ITEMS_PER_CALL,
    DEFAULT_MAX_KEYWORDS,
    ExtractResult,
    FallbackExtractor,
    run_extract,
)
from grepify.extract.prompt import PROMPT_VERSION, build_messages

__all__ = [
    "DEFAULT_MAX_ITEMS_PER_CALL",
    "DEFAULT_MAX_KEYWORDS",
    "PROMPT_VERSION",
    "ExtractResult",
    "FallbackExtractor",
    "build_messages",
    "run_extract",
]
