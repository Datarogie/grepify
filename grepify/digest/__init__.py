"""Digests + cron gating (E4): per-category daily/weekly digests (PRD §8).

Public surface the CLI (``grepify digest`` / ``grepify digest-gate``) and the
site build (E4 pages) consume:

- Digest input assembler (GRP-40) - :func:`assemble_digest_input` +
  :class:`DigestInput` / :class:`KeywordBrief`: deterministic, category-keyed.
  Rising detection (F-TRD-03) is :func:`grepify.windows.is_rising`, shared
  with the site's keyword cloud.
- Digest generation (GRP-41/42) - :func:`generate_digest` (skip-threshold,
  provenance, template fallback), :func:`digest_id_for`, :data:`PROMPT_VERSION`.
- Pipeline (GRP-41/42) - :func:`run_digest_pipeline` (catch-up + idempotent) +
  :class:`DigestRunResult`, :func:`periods_for`.
- Period math (GRP-41/42) - :func:`previous_day`, :func:`previous_iso_week`,
  :func:`recent_days`, :func:`trailing_days`, :func:`edmonton_date`,
  :class:`Period`, :data:`EDMONTON`.
- Cron gating (GRP-45) - :func:`digest_gate` + :class:`DigestGate`,
  :func:`format_gate`.

Determinism (F-SIT-08 / S8): the clock is injected everywhere (period + gate +
``created_at``); the one LLM call per category is offline-faked in tests.

Failure modes
-------------
None of its own - a re-export aggregator. See the submodules for module-level
failure modes.
"""

from __future__ import annotations

from grepify.digest.assemble import (
    DigestInput,
    KeywordBrief,
    assemble_digest_input,
)
from grepify.digest.gating import DigestGate, digest_gate, format_gate
from grepify.digest.generate import (
    TEMPLATE_MODEL,
    digest_id_for,
    generate_digest,
)
from grepify.digest.periods import (
    EDMONTON,
    Period,
    edmonton_date,
    previous_day,
    previous_iso_week,
    recent_days,
    trailing_days,
)
from grepify.digest.pipeline import (
    DigestRunResult,
    periods_for,
    run_digest_pipeline,
)
from grepify.digest.prompt import PROMPT_VERSION, build_messages

__all__ = [
    "EDMONTON",
    "PROMPT_VERSION",
    "TEMPLATE_MODEL",
    "DigestGate",
    "DigestInput",
    "DigestRunResult",
    "KeywordBrief",
    "Period",
    "assemble_digest_input",
    "build_messages",
    "digest_gate",
    "digest_id_for",
    "edmonton_date",
    "format_gate",
    "generate_digest",
    "periods_for",
    "previous_day",
    "previous_iso_week",
    "recent_days",
    "run_digest_pipeline",
    "trailing_days",
]
