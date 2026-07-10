"""Digest prompt v1 (E4, GRP-41): the strict-JSON narrative contract.

Builds the chat messages for one category's digest from a
:class:`~grepify.digest.assemble.DigestInput`. The model sees **only** data
derived from stored items - keyword counts, rising flags, and a few item
titles/summaries per keyword - and is told not to browse or invent (F-DIG-04).
It must return exactly ``{"title", "tldr": [...], "body_md"}`` so the parser
(:mod:`grepify.digest.generate`) and the prompt agree; the top-keyword chips are
rendered from the deterministic assembler, not asked of the model.

The prompt is versioned by the :data:`PROMPT_VERSION` module constant, bumped on
any wording change (PRD §5). It is recorded per digest: the stored
:class:`~grepify.models.Digest` carries both ``model`` and ``prompt_version``
(PRD §6 schema, F-DIG-04), so a mixed-provenance archive is auditable.

Failure modes
-------------
Pure string/JSON assembly - never raises for normal input. Item summaries are
truncated to :data:`SUMMARY_CHAR_CAP` for token hygiene (they are already <=2k
from the normalizer).
"""

from __future__ import annotations

import json

from grepify.digest.assemble import DigestInput
from grepify.llm import ChatMessage
from grepify.models import DigestKind

PROMPT_VERSION = "digest-v1"
SUMMARY_CHAR_CAP = 300

_SYSTEM_TEMPLATE = (
    "You write a short {cadence} news digest for the '{category}' category of a "
    "trend tracker.\n"
    "You are given JSON: the period, a total item count, and the top keywords "
    "with their mention counts, whether each is 'rising', and a few example item "
    "titles/summaries.\n"
    "\n"
    "Write about what mattered and why, grounded ONLY in the supplied data. Do "
    "not browse, do not invent facts, do not cite anything not present in the "
    "input.\n"
    "\n"
    "Output ONLY a JSON object - no prose, no markdown code fences. Use exactly "
    "this shape:\n"
    '{{"title": "<=80 chars", "tldr": ["3-5 short bullets"], '
    '"body_md": "<{paragraphs} short markdown paragraphs>"}}\n'
    "\n"
    "Rules:\n"
    "- title: a specific, plain headline; no date needed.\n"
    "- tldr: 3 to 5 terse bullets, each a single line, no leading dash.\n"
    "- body_md: {paragraphs} paragraphs of plain markdown prose separated by "
    "blank lines; lead with rising topics if any.\n"
    "- Mention concrete keywords from the input; do not list raw counts as a "
    "table.\n"
    "- Output nothing except the JSON object."
)


def build_messages(digest_input: DigestInput) -> list[ChatMessage]:
    """Build the (system, user) chat messages for one category digest."""
    weekly = digest_input.kind is DigestKind.WEEKLY
    payload = {
        "category": digest_input.category,
        "cadence": "weekly" if weekly else "daily",
        "period_start": digest_input.period.start,
        "period_end": digest_input.period.end,
        "item_count": digest_input.item_count,
        "keywords": [
            {
                "keyword": brief.keyword,
                "mentions": brief.count,
                "rising": brief.rising,
                "examples": [
                    {"title": item.title, "summary": (item.summary or "")[:SUMMARY_CHAR_CAP]}
                    for item in brief.items
                ],
            }
            for brief in digest_input.keywords
        ],
    }
    system = _SYSTEM_TEMPLATE.format(
        cadence="weekly" if weekly else "daily",
        category=digest_input.category,
        paragraphs="3 to 4" if weekly else "2 to 3",
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]
