"""Grepify exception hierarchy.

Failure modes
-------------
All raised errors derive from :class:`GrepifyError` so callers can catch the
whole family at pipeline boundaries. Subclasses are intentionally coarse:

- :class:`ConfigError` — config could not be loaded/parsed (missing dir, bad
  YAML, schema violation). Raised by the config layer; surfaced by ``validate``.
- :class:`RepositoryError` — storage could not satisfy a request (unreadable
  JSONL truth, cache rebuild failure). Raised by the repository layer.
- :class:`FetchError` — a *single* source failed to fetch (timeout, HTTP error,
  malformed feed, auth challenge, rate limit). Unlike the two above it is **not**
  a systemic fault: the ingest orchestrator catches it, records an ``error``
  ``fetch_log`` row, and continues the run — one dead feed never fails the run
  (PRD §9). Raised by :class:`~grepify.ingest.base.Fetcher` implementations.
- :class:`LlmError` — an LLM call could not be completed (transport failure,
  non-retryable HTTP error, retries exhausted, unsupported endpoint). Like
  :class:`FetchError` it is **not** systemic: the extract batcher (GRP-21)
  catches it and degrades that batch to the deterministic fallback extractor —
  the LLM failing never blocks the run or the site build (PRD §9).
- :class:`BudgetExceededError` — the per-run LLM budget circuit breaker
  (``max_calls_per_run``) refused a call *before* any network I/O (PRD §5, the
  CSR retry-loop lesson: bounded, no unbounded loops, ever). A subclass of
  :class:`LlmError`, so the batcher's fallback path catches it too; it is caught
  distinctly to stop issuing LLM calls for the rest of the run.

The first two are programming/environment faults that stop the run;
:class:`FetchError`, :class:`LlmError`, and :class:`BudgetExceededError` are
isolated degradations that do not.
"""

from __future__ import annotations


class GrepifyError(Exception):
    """Base class for all grepify errors."""


class ConfigError(GrepifyError):
    """Configuration could not be loaded or is invalid."""


class RepositoryError(GrepifyError):
    """A storage operation failed."""


class FetchError(GrepifyError):
    """A single source failed to fetch. Non-fatal: the orchestrator logs it to
    ``fetch_log`` and continues the run (PRD §9), so it never fails the run."""


class LlmError(GrepifyError):
    """An LLM call could not be completed. Non-fatal: the extract batcher
    (GRP-21) degrades the affected batch to the deterministic fallback extractor
    rather than failing the run (PRD §9)."""


class BudgetExceededError(LlmError):
    """The per-run LLM budget circuit breaker refused a call before any network
    I/O (``max_calls_per_run``, PRD §5). A subclass of :class:`LlmError` so the
    batcher's fallback path catches it, distinguished so callers can stop
    issuing LLM calls for the remainder of the run."""


class DataQualityError(GrepifyError):
    """A post-extract data-quality assertion (PRD §10.7) was violated —
    systemic, not isolated: unlike :class:`LlmError`, this stops the run
    rather than degrading, because it signals a bug in extraction/normalization
    itself rather than an unreachable LLM (PRD §10.7: "Violations fail the run
    loudly - no silent behavior changes")."""
