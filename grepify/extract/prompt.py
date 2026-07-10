"""Extraction prompt v1 (GRP-21): the strict-JSON keyword-extraction contract.

Builds the chat messages for one batch of items. The prompt asks for exactly the
shape the validator (:mod:`grepify.extract.batcher`) enforces -
``[{"item_id": ..., "keywords": [...]}]`` and nothing else - so the model's job
and the acceptance test agree. The prompt is versioned (:data:`PROMPT_VERSION`)
because a wording change is a behavior change the eval harness (GRP-24) must
re-score (PRD §5: model + prompt version recorded for auditability).

Transcript excerpts (E5, GRP-53)
--------------------------------
For a YouTube item that has a stored transcript, a smart-cut excerpt
(<=1500 chars, F-EXT-01) is added to that item's payload as a ``transcript``
field and the system prompt gains one line describing it. This augmentation is
**purely additive and conditional**: a batch with no transcript-bearing item
(every batch today, and every non-youtube batch) gets the byte-identical v1
prompt, and the required *output* contract (``[{item_id, keywords}]``) is
unchanged either way - so :data:`PROMPT_VERSION` stays ``extract-v1`` (it names
the output contract the validator enforces, which did not change). The
transcript excerpt is read through an injected ``transcript_reader`` so this
module never touches storage directly.

Failure modes
-------------
Pure string/JSON assembly - never raises for normal :class:`~grepify.models.Item`
input. Summaries are truncated to :data:`SUMMARY_CHAR_CAP` for token hygiene
(they are already ≤2k from the normalizer, F-ING-04); titles are sent whole. A
``transcript_reader`` that returns ``None``/empty for a ref simply omits the
transcript field (best-effort, PRD §13).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from grepify.ingest.transcript import excerpt_transcript
from grepify.llm import ChatMessage
from grepify.models import Item, SourceKind

PROMPT_VERSION = "extract-v1"

# Summaries are capped in the prompt purely to bound tokens/cost; the stored
# summary (≤2k) is unaffected. Titles carry most of the signal, so this is safe.
SUMMARY_CHAR_CAP = 500

# source_id-independent reader: transcript_ref -> transcript text (or None).
# Matches TranscriptStore.read; injected so the prompt never imports storage I/O.
TranscriptReader = Callable[[str], str | None]

_SYSTEM_TEMPLATE = (
    "You extract topical keywords from short content items for a news trend "
    "tracker.\n"
    "You are given a JSON array of items, each with an item_id, title, and "
    "summary.\n"
    "{transcript_intro}"
    "For each item, extract the salient topical keywords or short key phrases, "
    "most salient first.\n"
    "\n"
    "Output ONLY a JSON array - no prose, no explanation, no markdown code "
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

_TRANSCRIPT_INTRO = (
    "Some items also include a transcript excerpt (the start of a video's "
    "captions); use it as additional context for those items.\n"
)


def build_messages(
    items: Sequence[Item],
    *,
    max_keywords: int,
    transcript_reader: TranscriptReader | None = None,
) -> list[ChatMessage]:
    """Build the (system, user) chat messages for one extraction batch.

    ``transcript_reader`` (optional, GRP-53) resolves a youtube item's
    ``transcript_ref`` to its text; when it yields text, a <=1500-char smart-cut
    excerpt is added to that item's payload (see the module docstring). Absent
    or yielding nothing, the batch is the byte-identical v1 prompt.
    """
    payload: list[dict[str, Any]] = []
    any_transcript = False
    for item in items:
        entry: dict[str, Any] = {
            "item_id": item.item_id,
            "title": item.title,
            "summary": (item.summary or "")[:SUMMARY_CHAR_CAP],
        }
        excerpt = _transcript_excerpt(item, transcript_reader)
        if excerpt is not None:
            entry["transcript"] = excerpt
            any_transcript = True
        payload.append(entry)

    transcript_intro = _TRANSCRIPT_INTRO if any_transcript else ""
    system = _SYSTEM_TEMPLATE.format(max_keywords=max_keywords, transcript_intro=transcript_intro)
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


def _transcript_excerpt(item: Item, reader: TranscriptReader | None) -> str | None:
    if reader is None or item.kind is not SourceKind.YOUTUBE or not item.transcript_ref:
        return None
    text = reader(item.transcript_ref)
    if not text:
        return None
    return excerpt_transcript(text)
