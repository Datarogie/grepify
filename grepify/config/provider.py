"""``ConfigProvider`` - the single config contract (PRD §5, §7).

All config access goes through this interface. v1 is filesystem-YAML
(:class:`~grepify.config.filesystem.FilesystemConfigProvider`); v2 is DB-backed
with a checkbox UI over the same group files (they become seed rows). Accessors
return backend-neutral domain models (:mod:`grepify.models`) and config schemas
(:mod:`grepify.config.schemas`) - no filesystem types leak into any signature.

Failure modes
-------------
- :meth:`groups` / :meth:`sources` / :meth:`keywords` / :meth:`settings` raise
  :class:`~grepify.errors.ConfigError` on malformed config (fail-fast; used by
  the pipeline, which must not run on bad config).
- :meth:`validate` never raises for config problems - it aggregates every issue
  into a :class:`ValidationReport` so ``grepify validate`` reports them all.
  When called with ``registered_kinds`` (GRP-56), it also flags any *enabled*
  source whose :class:`~grepify.models.SourceKind` has no entry in that set -
  the same gap that would otherwise only surface as a ``KeyError`` at ingest
  time (see :mod:`grepify.ingest.orchestrator`). Passing ``None`` (the
  default) skips that check, so callers that only care about schema/cross-file
  validity (most existing tests) are unaffected.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from grepify.config.schemas import KeywordsConfig, SettingsConfig
from grepify.models import Source, SourceGroup, SourceKind


class ValidationReport(BaseModel):
    """Outcome of :meth:`ConfigProvider.validate`."""

    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    group_count: int = 0
    source_count: int = 0

    def summary(self) -> str:
        head = "config ok" if self.ok else "config INVALID"
        return (
            f"{head}: {self.group_count} groups, {self.source_count} sources, "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings"
        )


class ConfigProvider(ABC):
    """Backend-neutral config contract."""

    @abstractmethod
    def groups(self) -> list[SourceGroup]:
        """Return all source groups as domain models."""

    @abstractmethod
    def sources(self) -> list[Source]:
        """Return all sources (flattened across groups) as domain models,
        with canonical url + url_hash resolved."""

    @abstractmethod
    def keywords(self) -> KeywordsConfig:
        """Return the aliases/mutes/pins config."""

    @abstractmethod
    def settings(self) -> SettingsConfig:
        """Return the settings config."""

    @abstractmethod
    def validate(
        self, *, registered_kinds: frozenset[SourceKind] | None = None
    ) -> ValidationReport:
        """Validate all config and aggregate every problem into a report.

        ``registered_kinds``, when given, is checked against every enabled
        source's ``kind`` (see the module docstring); omit it to validate
        shape/cross-file rules only.
        """
