"""Extraction prompt v1 (GRP-21): the strict-JSON keyword-extraction contract.

Builds the chat messages for one batch of items. The prompt asks for exactly the
shape the validator (:mod:`grepify.extract.batcher`) enforces —
``[{"item_id": ..., "keywords": [...]}]`` and nothing else — so the model's job
and the acceptance test agree. The prompt is versioned (:data:`PROMPT_VERSION`)
because a wording change is a behavior change the eval harness (GRP-24) must
re-score (PRD §5: model + prompt version recorded for auditability).

Failure modes
-------------
Pure string/JSON assembly — never raises for normal :class:`~grepify.models.Item`
input. Summaries are truncated to :data:`SUMMARY_CHAR_CAP` for token hygiene
(they are already ≤2k from the normalizer, F-ING-04); titles are sent whole.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from grepify.llm import ChatMessage
from grepify.models import Item

PROMPT_VERSION = "extract-v1"

# Summaries are capped in the prompt purely to bound tokens/cost; the stored
# summary (≤2k) is unaffected. Titles carry most of the signal, so this is safe.
SUMMARY_CHAR_CAP = 500

_SYSTEM_TEMPLATE = (
    "You extract topical keywords from short content items for a news trend "
    "tracker.\n"
    "You are given a JSON array of items, each with an item_id, title, and "
    "summary.\n"
    "For each item, extract the salient topical keywords or short key phrases, "
    "most salient first.\n"
    "\n"
    "Output ONLY a JSON array — no prose, no explanation, no markdown code "
    "fences. Use exactly this shape:\n"
    '[{{"item_id": "<the item_id, echoed exactly>", "keywords": ["kw1", "kw2"]}}]\n'
    "\n"
    "Rules:\n"
    "- Include every item_id from the input exactly once; echo each id verbatim.\n"
    "- At most {max_keywords} keywords per item; fewer is fine.\n"
    "- Each keyword is 2 to 60 characters, lowercase, and contains no URL.\n"
    "- Keywords are topics/entities/technologies, not full sentences.\n"
    "- Output nothing except the JSON array."
)


def build_messages(items: Sequence[Item], *, max_keywords: int) -> list[ChatMessage]:
    """Build the (system, user) chat messages for one extraction batch."""
    payload = [
        {
            "item_id": item.item_id,
            "title": item.title,
            "summary": (item.summary or "")[:SUMMARY_CHAR_CAP],
        }
        for item in items
    ]
    return [
        ChatMessage(role="system", content=_SYSTEM_TEMPLATE.format(max_keywords=max_keywords)),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]
