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

The first two are programming/environment faults that stop the run;
:class:`FetchError` is an isolated per-source hiccup that does not.
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
