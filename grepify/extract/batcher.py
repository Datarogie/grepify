"""Extract batcher + response validation (GRP-21).

Turns a list of items into :class:`~grepify.models.ItemKeyword` rows by batching
them into strict-JSON LLM calls, validating each response, retrying a malformed
batch once, and falling back to a deterministic extractor when the LLM can't
deliver. :func:`run_extract` is the entrypoint the pipeline wiring (GRP-25) will
drive; it never raises for LLM problems - extraction always yields rows so the
trend surface has data and the site builds (PRD §5/§9).

Batching (F-EXT-01)
-------------------
Items are chunked to ``max_items_per_call`` (25 per ``settings.yml``). Each batch
is one :meth:`~grepify.llm.client.LlmClient.complete` call (one budget unit).

Validation (F-EXT-02), batch-level
-----------------------------------
A response is valid iff it parses as JSON, is a list of ``{item_id, keywords}``
objects, its item_id set **equals** the batch's (echo check - no unknown ids, no
duplicates, no omissions), and every keyword passes sanity (2-60 chars after
trim, contains no URL). Each item's keyword list is then truncated to
``max_keywords`` (the count cap is a truncation, not a validation gate, per
F-EXT-02's explicit list). An empty keyword list for an item is valid (the
item simply gets no LLM rows); the "every item ≥1 keyword / ``no_keywords``
flag" data-quality rule is GRP-25. Full normalization + alias/mute is GRP-23 -
this module only trims and sanity-checks.

Retry-then-fallback (F-EXT-02)
------------------------------
A malformed batch is retried **once** with the same prompt; still malformed → it
falls back. A :class:`~grepify.errors.BudgetExceededError` or exhausted-retries
:class:`~grepify.errors.LlmError` short-circuits that batch straight to fallback;
once budget is exhausted, every remaining batch goes to fallback with no further
LLM calls. Malformed-retries are **bounded in total** (``max_malformed_retries``,
default = number of batches) so a pathological run cannot retry without limit.

Failure modes
-------------
This module raises nothing of its own for LLM behavior - every LLM failure mode
degrades to the fallback extractor. A misbehaving fallback extractor (GRP-22) or
an invalid :class:`~grepify.models.ItemKeyword` field would surface its own
exception (``pydantic.ValidationError``); that is a programming fault, not an
expected degradation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from grepify.clock import Clock, to_iso
from grepify.errors import BudgetExceededError, LlmError
from grepify.extract.prompt import TranscriptReader, build_messages
from grepify.llm import ChatMessage, LlmClient
from grepify.models import ExtractionMethod, Item, ItemKeyword

_MIN_KEYWORD_LEN = 2
_MAX_KEYWORD_LEN = 60
DEFAULT_MAX_KEYWORDS = 8  # F-EXT-01 (max 8 keywords per item)
DEFAULT_MAX_ITEMS_PER_CALL = 25  # settings.yml llm.max_items_per_call


class FallbackExtractor(Protocol):
    """Deterministic, offline keyword extractor used when the LLM can't deliver.

    Implemented by GRP-22 (YAKE). Returns ``item_id -> ordered keywords`` for the
    items it can handle; an item absent from the mapping simply gets no fallback
    rows. Must not raise for ordinary items (it is the safety net).
    """

    def extract(self, items: Sequence[Item]) -> dict[str, list[str]]: ...


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of an extraction run (feeds the run manifest / data-quality, GRP-25)."""

    keywords: list[ItemKeyword]
    batches_total: int
    batches_llm: int
    batches_fallback: int
    llm_retries: int
    budget_exhausted: bool


class _MalformedResponseError(Exception):
    """Internal: an LLM response failed validation (never escapes this module)."""


def run_extract(  # noqa: PLR0913 - items+client+run context+fallback+tuning are all distinct inputs
    items: Sequence[Item],
    client: LlmClient,
    *,
    run_id: str,
    clock: Clock,
    fallback: FallbackExtractor,
    max_items_per_call: int = DEFAULT_MAX_ITEMS_PER_CALL,
    max_keywords: int = DEFAULT_MAX_KEYWORDS,
    max_malformed_retries: int | None = None,
    transcript_reader: TranscriptReader | None = None,
) -> ExtractResult:
    """Extract keywords for ``items``; see the module docstring for the contract.

    ``items`` are taken as-is (untagged-item selection and ``--force`` are GRP-25
    wiring). ``max_malformed_retries`` defaults to the number of batches, so at
    most one retry per batch and never unbounded. ``transcript_reader`` (GRP-53)
    is passed through to the prompt so youtube items with a stored transcript get
    a <=1500-char excerpt in their payload; ``None`` leaves the prompt unchanged.
    """
    batches = _chunk(list(items), max_items_per_call)
    retry_budget = len(batches) if max_malformed_retries is None else max_malformed_retries
    extracted_at = to_iso(clock.now())

    rows: list[ItemKeyword] = []
    batches_llm = 0
    batches_fallback = 0
    retries_used = 0
    budget_exhausted = False

    for batch in batches:
        if budget_exhausted:
            rows.extend(_fallback_rows(batch, fallback, extracted_at, max_keywords))
            batches_fallback += 1
            continue

        batch_ids = [item.item_id for item in batch]
        messages = build_messages(
            batch, max_keywords=max_keywords, transcript_reader=transcript_reader
        )
        parsed: dict[str, list[str]] | None = None
        try:
            parsed = _call_and_validate(
                client, messages, batch_ids, run_id=run_id, max_keywords=max_keywords
            )
            if parsed is None and retries_used < retry_budget:
                # One bounded retry with the same prompt (F-EXT-02). Count it only
                # after the call returns: a retry refused by the budget breaker
                # raises BudgetExceededError here (no network) and must not inflate
                # the retry metric.
                parsed = _call_and_validate(
                    client, messages, batch_ids, run_id=run_id, max_keywords=max_keywords
                )
                retries_used += 1
        except BudgetExceededError:
            # Hard budget stop: this batch and all remaining ones go to fallback
            # with no further LLM calls (PRD §5).
            budget_exhausted = True
            parsed = None
        except LlmError:
            # Network exhausted for this batch - degrade just this batch.
            parsed = None

        if parsed is None:
            rows.extend(_fallback_rows(batch, fallback, extracted_at, max_keywords))
            batches_fallback += 1
        else:
            rows.extend(_llm_rows(batch, parsed, client.model, extracted_at))
            batches_llm += 1

    return ExtractResult(
        keywords=rows,
        batches_total=len(batches),
        batches_llm=batches_llm,
        batches_fallback=batches_fallback,
        llm_retries=retries_used,
        budget_exhausted=budget_exhausted,
    )


def _call_and_validate(
    client: LlmClient,
    messages: Sequence[ChatMessage],
    batch_ids: Sequence[str],
    *,
    run_id: str,
    max_keywords: int,
) -> dict[str, list[str]] | None:
    """One LLM call + validation. Returns the validated mapping, or ``None`` if
    the response was malformed. Lets :class:`~grepify.errors.LlmError` /
    :class:`~grepify.errors.BudgetExceededError` propagate to the caller (→ fallback).
    """
    completion = client.complete(
        messages,
        run_id=run_id,
        purpose="extract",
        input_items=len(batch_ids),
    )
    try:
        return _validate_response(completion.text, batch_ids, max_keywords=max_keywords)
    except _MalformedResponseError:
        return None


def _validate_response(
    text: str, batch_ids: Sequence[str], *, max_keywords: int
) -> dict[str, list[str]]:
    """Parse + validate one response into ``item_id -> keywords`` (F-EXT-02).

    Raises :class:`_MalformedResponseError` on any structural, echo, or sanity
    violation. Keyword lists are truncated to ``max_keywords`` after sanity
    checks pass.
    """
    payload = _parse_json_array(text)
    expected = set(batch_ids)
    result: dict[str, list[str]] = {}
    for entry in payload:
        if not isinstance(entry, dict) or "item_id" not in entry or "keywords" not in entry:
            raise _MalformedResponseError("entry is not a {item_id, keywords} object")
        item_id = entry["item_id"]
        raw_keywords = entry["keywords"]
        if not isinstance(item_id, str) or not isinstance(raw_keywords, list):
            raise _MalformedResponseError("item_id must be a string and keywords a list")
        if item_id in result:
            raise _MalformedResponseError(f"duplicate item_id {item_id!r}")
        if item_id not in expected:
            raise _MalformedResponseError(f"unknown item_id {item_id!r} not in batch")
        result[item_id] = _clean_keywords(raw_keywords, max_keywords=max_keywords)

    if set(result) != expected:
        missing = sorted(expected - set(result))
        raise _MalformedResponseError(f"response omitted item_ids: {missing}")
    return result


def _parse_json_array(text: str) -> list[object]:
    stripped = _strip_code_fences(text)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise _MalformedResponseError(f"not valid json: {exc}") from exc
    if not isinstance(payload, list):
        raise _MalformedResponseError("top-level json is not an array")
    return payload


def _strip_code_fences(text: str) -> str:
    """Tolerate a model that wraps the array in a ```json fence despite the
    prompt. Anything more exotic than a fenced block stays as-is and fails the
    JSON parse (→ malformed → retry/fallback)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped[3:]
        if stripped[:4].lower() == "json":
            stripped = stripped[4:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _clean_keywords(raw_keywords: list[object], *, max_keywords: int) -> list[str]:
    cleaned: list[str] = []
    for keyword in raw_keywords:
        if not isinstance(keyword, str):
            raise _MalformedResponseError("keyword is not a string")
        trimmed = keyword.strip()
        if not (_MIN_KEYWORD_LEN <= len(trimmed) <= _MAX_KEYWORD_LEN):
            raise _MalformedResponseError(f"keyword {trimmed!r} fails length sanity (2-60)")
        if _looks_like_url(trimmed):
            raise _MalformedResponseError(f"keyword {trimmed!r} looks like a url")
        cleaned.append(trimmed)
    # Count cap is truncation, not a validation gate (F-EXT-02).
    return cleaned[:max_keywords]


def _looks_like_url(keyword: str) -> bool:
    lowered = keyword.lower()
    return "://" in lowered or lowered.startswith("www.")


def _llm_rows(
    batch: Sequence[Item],
    parsed: dict[str, list[str]],
    model: str,
    extracted_at: str,
) -> list[ItemKeyword]:
    # `parsed` keyword lists are already trimmed, sanity-checked, and truncated
    # to max_keywords by `_clean_keywords` during validation - no re-capping here.
    rows: list[ItemKeyword] = []
    for item in batch:
        for rank, keyword in enumerate(parsed.get(item.item_id, []), start=1):
            rows.append(
                ItemKeyword(
                    item_id=item.item_id,
                    keyword=keyword,
                    rank=rank,
                    method=ExtractionMethod.LLM,
                    model=model,
                    extracted_at=extracted_at,
                )
            )
    return rows


def _fallback_rows(
    batch: Sequence[Item],
    fallback: FallbackExtractor,
    extracted_at: str,
    max_keywords: int,
) -> list[ItemKeyword]:
    produced = fallback.extract(batch)
    rows: list[ItemKeyword] = []
    for item in batch:
        for rank, keyword in enumerate(produced.get(item.item_id, [])[:max_keywords], start=1):
            rows.append(
                ItemKeyword(
                    item_id=item.item_id,
                    keyword=keyword,
                    rank=rank,
                    method=ExtractionMethod.FALLBACK,
                    model=None,
                    extracted_at=extracted_at,
                )
            )
    return rows


def _chunk(items: list[Item], size: int) -> list[list[Item]]:
    if size < 1:
        raise ValueError("max_items_per_call must be >= 1")
    return [items[i : i + size] for i in range(0, len(items), size)]
