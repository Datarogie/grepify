"""Grepify exception hierarchy.

Failure modes
-------------
All raised errors derive from :class:`GrepifyError` so callers can catch the
whole family at pipeline boundaries. Subclasses are intentionally coarse:

- :class:`ConfigError` — config could not be loaded/parsed (missing dir, bad
  YAML, schema violation). Raised by the config layer; surfaced by ``validate``.
- :class:`RepositoryError` — storage could not satisfy a request (unreadable
  JSONL truth, cache rebuild failure). Raised by the repository layer.

These are programming/environment faults, not per-source hiccups. A single
source failing to fetch is *not* an error here — that is logged to ``fetch_log``
and the run continues (PRD §9).
"""

from __future__ import annotations


class GrepifyError(Exception):
    """Base class for all grepify errors."""


class ConfigError(GrepifyError):
    """Configuration could not be loaded or is invalid."""


class RepositoryError(GrepifyError):
    """A storage operation failed."""
