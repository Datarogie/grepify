"""Post-extract data-quality gate (PRD §10.7, GRP-25).

Two of §10.7's three in-pipeline assertions are this module's scope (the third
- "digest references only existing keywords" - is E4, not extraction):

- No stored keyword exceeds 60 chars. Both the LLM path
  (:mod:`grepify.extract.batcher`) and the fallback path
  (:mod:`grepify.extract.fallback`) already enforce this at the point they
  produce a keyword, and :func:`grepify.keywords.normalize_keyword` only ever
  shrinks text (trim/collapse/strip trailing punctuation) - so for *raw*
  extractor output this is a defensive gate against an upstream regression,
  not an expected case. It is not purely defensive end to end, though: alias
  substitution (also applied in :mod:`grepify.extract.pipeline`, ahead of
  this gate) can *lengthen* a keyword, since ``keywords.yml``'s alias targets
  aren't length-checked at config-validation time - a misconfigured alias
  mapping to a >60-char canonical string legitimately trips this gate, and
  correctly so (PRD §10.7: "Violations fail the run loudly").
- Every item fed into this run's extraction ends up with at least one keyword
  row, or is explicitly recorded as having none. F-EXT-02 says an empty
  keyword list is a legitimate LLM/fallback result (nothing salient found, or
  every candidate keyword got muted downstream in this same run); the *silent*
  failure mode this guards against is an item dropping out of the batch
  pipeline entirely (a chunking/writing bug) with no trace of it anywhere.
  :func:`assert_data_quality` always classifies every input item into one of
  the two buckets, so the caller (the ``extract`` CLI command) can surface the
  "no keywords" bucket loudly in the run manifest instead of it vanishing.

Failure modes
-------------
:func:`assert_data_quality` raises :class:`~grepify.errors.DataQualityError`
only for the over-length case - a systemic fault that should stop the run
(PRD §10.7: "Violations fail the run loudly"). It never raises for the
zero-keyword case; that is a valid outcome the report surfaces, not a
violation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from grepify.errors import DataQualityError
from grepify.models import Item, ItemKeyword

MAX_KEYWORD_LEN = 60  # PRD §10.7 / F-EXT-02


@dataclass(frozen=True)
class DataQualityReport:
    """Outcome of :func:`assert_data_quality` (feeds the ``extract`` run manifest)."""

    no_keywords_item_ids: list[str]


def assert_data_quality(
    items: Sequence[Item], keywords: Sequence[ItemKeyword]
) -> DataQualityReport:
    """Enforce PRD §10.7's extraction-time checks over one run's ``items`` and
    the ``keywords`` rows produced for them.

    ``items`` is the exact set of items this run selected for extraction;
    ``keywords`` is the (already normalized) rows about to be written for
    them. Raises :class:`~grepify.errors.DataQualityError` if any keyword
    exceeds :data:`MAX_KEYWORD_LEN` chars. Never raises for items with zero
    keyword rows - see the module docstring.
    """
    over_length = [row for row in keywords if len(row.keyword) > MAX_KEYWORD_LEN]
    if over_length:
        offenders = ", ".join(f"{row.item_id}:{row.keyword!r}" for row in over_length[:5])
        raise DataQualityError(
            f"{len(over_length)} keyword row(s) exceed {MAX_KEYWORD_LEN} chars "
            f"after normalization: {offenders}"
        )

    tagged_ids = {row.item_id for row in keywords}
    no_keywords_item_ids = sorted(item.item_id for item in items if item.item_id not in tagged_ids)
    return DataQualityReport(no_keywords_item_ids=no_keywords_item_ids)
