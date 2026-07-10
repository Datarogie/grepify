"""Digest generation (E4, GRP-41/42): one LLM call per category, with fallbacks.

Turns a :class:`~grepify.digest.assemble.DigestInput` into a stored
:class:`~grepify.models.Digest`, or ``None`` when the category is below the
minimum-item threshold (F-DIG-03: skipped, not failed). The narrative is the
epic's only LLM call (``purpose='digest'``, one per category); everything else -
the top-keyword chips, the skip decision, the template fallback - is
deterministic.

Three outcomes for one category:

- **skip** (``item_count < min_items``): return ``None``; the caller logs it.
- **llm**: one ``client.complete()``; on a valid ``{title, tldr, body_md}``
  reply the digest is ``model=<profile model>``.
- **template** (LLM down / over budget / malformed reply): a deterministic
  digest built from the assembler data alone, ``model='template'`` (PRD §13
  "digest degrades to template"). The run continues and the site still builds
  (PRD §9).

Provenance (F-DIG-04)
---------------------
Both ``model`` and ``prompt_version`` are recorded per digest. On the LLM path
that is the profile model plus :data:`~grepify.digest.prompt.PROMPT_VERSION`; on
the degraded template path it is :data:`TEMPLATE_MODEL` plus
:data:`TEMPLATE_PROMPT_VERSION` (``'none'`` - the template uses no LLM prompt).

Determinism (F-SIT-08 / S8)
---------------------------
``created_at`` is the injected clock; ``digest_id``, ``period``, chips, and the
template body are pure functions of the input. Offline tests inject a fake LLM
transport, so no network is touched (PRD §9/§10).

Failure modes
-------------
:class:`~grepify.errors.LlmError` / :class:`~grepify.errors.BudgetExceededError`
are caught and degrade to the template digest; a malformed but successful LLM
reply degrades the same way. This module raises nothing of its own.
"""

from __future__ import annotations

import json

from grepify.clock import Clock, to_iso
from grepify.digest.assemble import DigestInput
from grepify.digest.prompt import PROMPT_VERSION, build_messages
from grepify.errors import LlmError
from grepify.llm import LlmClient
from grepify.models import Digest, DigestKind

TEMPLATE_MODEL = "template"  # provenance marker for a degraded (non-LLM) digest
TEMPLATE_PROMPT_VERSION = "none"  # the template path uses no LLM prompt (F-DIG-04)


def digest_id_for(kind: DigestKind, category: str, period_key: str) -> str:
    """The stable digest id (PRD §6): ``<kind>-<category>-<period key>``."""
    return f"{kind.value}-{category}-{period_key}"


def generate_digest(
    digest_input: DigestInput,
    client: LlmClient,
    *,
    run_id: str,
    clock: Clock,
    min_items: int,
) -> Digest | None:
    """Generate (or skip) one category's digest. Returns ``None`` when skipped."""
    if digest_input.item_count < min_items:
        return None  # F-DIG-03: too little to say - skip, not fail (caller logs)

    created_at = to_iso(clock.now())
    top_keywords_json = _chips_json(digest_input)

    try:
        title, body_md = _generate_llm(digest_input, client, run_id=run_id)
        model = client.model
        prompt_version = PROMPT_VERSION
    except LlmError:
        # LLM down or over budget or malformed reply -> deterministic template.
        title, body_md = _template_digest(digest_input)
        model = TEMPLATE_MODEL
        prompt_version = TEMPLATE_PROMPT_VERSION

    return Digest(
        digest_id=digest_id_for(digest_input.kind, digest_input.category, digest_input.period.key),
        kind=digest_input.kind,
        category=digest_input.category,
        period_start=digest_input.period.start,
        period_end=digest_input.period.end,
        title=title,
        body_md=body_md,
        top_keywords=top_keywords_json,
        model=model,
        prompt_version=prompt_version,
        created_at=created_at,
    )


# --- llm path ----------------------------------------------------------------


def _generate_llm(digest_input: DigestInput, client: LlmClient, *, run_id: str) -> tuple[str, str]:
    """One ``complete()``; parse the strict-JSON reply into (title, body_md).

    A malformed reply is raised as :class:`~grepify.errors.LlmError` so the
    caller's ``except`` degrades it to the template, exactly like a transport
    failure - one degradation path, not two.
    """
    completion = client.complete(
        build_messages(digest_input),
        run_id=run_id,
        purpose="digest",
        input_items=len(digest_input.keywords),
    )
    title, tldr, narrative = _parse_reply(completion.text)
    return title, _compose_body(tldr, narrative)


def _parse_reply(text: str) -> tuple[str, list[str], str]:
    """Validate the ``{title, tldr, body_md}`` reply; raise ``LlmError`` if bad."""
    try:
        data = json.loads(text)
        title = data["title"]
        tldr = data["tldr"]
        body_md = data["body_md"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LlmError(f"malformed digest reply: {exc}") from exc
    if not (isinstance(title, str) and isinstance(body_md, str) and isinstance(tldr, list)):
        raise LlmError("digest reply fields have wrong types")
    if not all(isinstance(bullet, str) for bullet in tldr):
        raise LlmError("digest tldr must be a list of strings")
    if not title.strip() or not body_md.strip():
        raise LlmError("digest reply has an empty title or body")
    return title.strip(), [b.strip() for b in tldr if b.strip()], body_md.strip()


# --- template fallback (deterministic) ---------------------------------------


def _template_digest(digest_input: DigestInput) -> tuple[str, str]:
    """A deterministic digest from the assembler data alone (PRD §13)."""
    cadence = "Weekly" if digest_input.kind is DigestKind.WEEKLY else "Daily"
    title = f"{cadence} {digest_input.category} digest - {digest_input.period.key}"
    tldr = [f"{brief.keyword} ({brief.count})" for brief in digest_input.keywords[:5]]
    rising = digest_input.rising_keywords
    lead = f"Rising: {', '.join(rising)}. " if rising else "No rising keywords this period. "
    body = (
        f"{lead}Top keywords across {digest_input.item_count} items: "
        f"{', '.join(f'{b.keyword} ({b.count})' for b in digest_input.keywords) or 'none'}."
    )
    return title, _compose_body(tldr, body)


# --- shared helpers ----------------------------------------------------------


def _compose_body(tldr: list[str], narrative: str) -> str:
    """Compose the stored ``body_md``: a **TL;DR** bullet list then the narrative."""
    parts: list[str] = []
    if tldr:
        parts.append("**TL;DR**\n\n" + "\n".join(f"- {bullet}" for bullet in tldr))
    if narrative:
        parts.append(narrative)
    return "\n\n".join(parts)


def _chips_json(digest_input: DigestInput) -> str:
    """The ``top_keywords`` json (``[{keyword, count}]``) - deterministic order."""
    chips = [{"keyword": brief.keyword, "count": brief.count} for brief in digest_input.keywords]
    return json.dumps(chips, ensure_ascii=False, sort_keys=True)
