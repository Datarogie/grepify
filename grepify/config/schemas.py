"""Pydantic schemas for the YAML config (PRD §7).

These validate the *shape* of ``sources/groups/*.yml``, ``keywords.yml``, and
``settings.yml``. They are the parse/validation layer; the
:class:`~grepify.config.provider.ConfigProvider` converts group/source specs
into backend-neutral :mod:`grepify.models` domain objects for the repository.

``extra="forbid"`` is deliberate: an unknown key is almost always a typo, and
silent-drop would be a silent behavior change (PRD §10). Validation surfaces it.

Failure modes
-------------
A malformed file raises ``pydantic.ValidationError`` (unknown kind, missing
``category``, wrong locator field for a kind). The provider translates these into
:class:`~grepify.errors.ConfigError` (fail-fast) or into a
:class:`~grepify.config.provider.ValidationReport` (lenient, for ``validate``).
Schema shape alone cannot catch a valid :class:`~grepify.models.SourceKind`
with no registered fetcher (``kind: x`` passes this layer - it has a locator
rule below - even though no fetcher is wired for it); that coverage check is
``registered_kinds``-aware and lives one layer up, in
:meth:`~grepify.config.provider.ConfigProvider.validate` (GRP-56).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from grepify.models import SourceKind, SourceStatus

# Which locator field each kind requires (PRD §7). The value resolves to the
# canonical URL that becomes the source identity (url + url_hash).
_LOCATOR_FIELD: dict[SourceKind, str] = {
    SourceKind.RSS: "url",
    SourceKind.YOUTUBE: "channel_id",
    SourceKind.REDDIT: "subreddit",
    SourceKind.X: "handle",
}


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceSpec(_ConfigModel):
    """One source within a group file. Exactly one locator field per kind.

    Lifecycle (ADR 0002): ``status`` is authoritative and ``enabled`` is derived
    from it (``active``/``degraded`` -> enabled, ``paywalled``/``dead`` ->
    disabled). For back-compat a file may still carry ``enabled`` alone, absent
    ``status``: ``enabled: true`` maps to ``active`` and ``enabled: false`` to
    ``dead``. A ``gone`` source is never represented here - it is removed from
    the group file entirely, so an explicit ``status: gone`` is rejected. When
    both ``status`` and ``enabled`` are set explicitly they must agree.
    """

    id: str
    kind: SourceKind
    name: str | None = None
    enabled: bool = True
    added_at: str | None = None
    config: dict[str, Any] | None = None  # per-source overrides -> items.config_json
    status: SourceStatus | None = None
    evidence: str | None = None  # required for dead (offline validate check)
    message: str | None = None  # required for paywalled; rendered on the sources page
    ladder: list[str] | None = None  # explicit opt-in to higher rungs (3/4)
    active_url: str | None = None  # a fallback/mirror rung that served (degraded evidence)
    # Kind-specific locators (exactly one required, matching `kind`):
    url: str | None = None  # rss
    channel_id: str | None = None  # youtube
    subreddit: str | None = None  # reddit
    handle: str | None = None  # x

    @model_validator(mode="after")
    def _check_locator(self) -> SourceSpec:
        required = _LOCATOR_FIELD[self.kind]
        present = {
            name
            for name in ("url", "channel_id", "subreddit", "handle")
            if getattr(self, name) is not None
        }
        if present != {required}:
            raise ValueError(
                f"source {self.id!r} of kind {self.kind} must set exactly "
                f"{required!r} (got {sorted(present) or 'nothing'})"
            )
        return self

    @model_validator(mode="after")
    def _check_lifecycle(self) -> SourceSpec:
        if self.status is SourceStatus.GONE:
            raise ValueError(
                f"source {self.id!r}: status 'gone' must not appear in a group file - "
                "a gone source is removed from the file (its reasoning lives in the "
                "removing commit)"
            )
        explicit_enabled = "enabled" in self.model_fields_set
        if self.status is not None and explicit_enabled and self.enabled != self.status.is_enabled:
            raise ValueError(
                f"source {self.id!r}: status {self.status.value!r} implies "
                f"enabled={self.status.is_enabled}, but enabled={self.enabled} is set explicitly"
            )
        return self

    @property
    def effective_status(self) -> SourceStatus:
        """The authoritative lifecycle class: explicit ``status`` if set, else
        derived from ``enabled`` (``true`` -> active, ``false`` -> dead)."""
        if self.status is not None:
            return self.status
        return SourceStatus.ACTIVE if self.enabled else SourceStatus.DEAD

    @property
    def effective_enabled(self) -> bool:
        return self.effective_status.is_enabled

    @property
    def canonical_url(self) -> str:
        """Canonical feed identity - the same feed always hashes the same way."""
        if self.kind is SourceKind.RSS:
            # S101: type-narrowing for mypy; `_check_locator` already enforces url.
            assert self.url is not None  # noqa: S101
            return self.url
        if self.kind is SourceKind.YOUTUBE:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={self.channel_id}"
        if self.kind is SourceKind.REDDIT:
            return f"https://www.reddit.com/r/{self.subreddit}/new.json"
        # x
        return f"https://x.com/{self.handle}"

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.canonical_url.encode("utf-8")).hexdigest()

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def config_json(self) -> str | None:
        """Deterministic JSON serialization of per-source overrides, or None."""
        if self.config is None:
            return None
        return json.dumps(self.config, sort_keys=True, separators=(",", ":"))


class GroupFile(_ConfigModel):
    """A curated source-group bundle (PRD §7)."""

    group: str
    name: str
    category: str
    enabled: bool = True
    builtin: bool = False
    sources: list[SourceSpec] = []


class KeywordsConfig(_ConfigModel):
    """Aliases / mutes / pins (PRD §7 keywords.yml)."""

    aliases: dict[str, str] = {}
    mute: list[str] = []
    pin: list[str] = []


class LlmProfile(_ConfigModel):
    endpoint: str
    model: str | None = None
    cmd: str | None = None
    max_calls_per_run: int | None = None


class LlmSettings(_ConfigModel):
    active_profile: str
    max_items_per_call: int = 25
    profiles: dict[str, LlmProfile]

    @model_validator(mode="after")
    def _active_exists(self) -> LlmSettings:
        if self.active_profile not in self.profiles:
            raise ValueError(f"active_profile {self.active_profile!r} is not defined in profiles")
        return self


class Windows(_ConfigModel):
    cloud_days: int = 7
    digest_daily_hours: int = 24
    digest_weekly: str = "iso_week"
    keyword_days: int = 30  # trailing window for keyword detail pages (F-SIT-04)
    keyword_min_mentions: int = 3  # a keyword gets a page at >= this many mentions in the window
    coverage_quiet_days: int = 30  # a live source with no item this recently is quiet (GRP-70)


class DigestSettings(_ConfigModel):
    """Rising-detection + digest-shaping knobs (PRD §8 F-TRD-03 / F-DIG-01/03)."""

    enabled: bool = True  # pause switch: when false, `digest` no-ops (no LLM calls, no files)
    daily_lookback_days: int = 7  # catch-up: daily digest backfills missing days over this window
    rising_min_count: int = 3  # F-TRD-03: a keyword needs >= this count to be "rising"
    rising_ratio: float = 3.0  # F-TRD-03: and count/previous-count >= this ratio
    min_items: int = 10  # F-DIG-03: skip (not fail) a category digest below this
    daily_top_keywords: int = 12  # top-N keywords fed to the daily digest prompt
    weekly_top_keywords: int = 20  # weekly is slightly longer (PRD §8 F-DIG-02)
    items_per_keyword: int = 3  # top item titles/summaries per keyword in the prompt


class Limits(_ConfigModel):
    transcript_max_chars: int = 60000
    transcript_langs: list[str] = ["en"]


class IngestSettings(_ConfigModel):
    """Best-effort source-kind policy (T6, GRP-31 - Reddit strategy option ii).

    ``min_interval_hours`` is how long, per :class:`~grepify.models.SourceKind`,
    the ingest orchestrator (:mod:`grepify.ingest.cadence`) waits between real
    fetch attempts for a source of that kind; a kind absent from the mapping
    (or mapped to ``<= 0``) is fetched every run, unchanged from pre-T6
    behavior. Reddit defaults to 20 hours so it is fetched about once a day
    against the pipeline's roughly-every-6-12h cron cadence, rather than on
    every run.

    ``quiet_kinds`` lists the kinds whose consecutive fetch failures never set
    the health-snapshot ``flagged`` bit (:mod:`grepify.health`) - the count is
    still computed and shown, only the boolean is suppressed, so the ~26
    currently-blocked Reddit sources stop reading as red/error noise on the
    health page without losing auditability.
    """

    min_interval_hours: dict[SourceKind, int] = {SourceKind.REDDIT: 20}
    quiet_kinds: list[SourceKind] = [SourceKind.REDDIT]


class SettingsConfig(_ConfigModel):
    """Top-level settings (PRD §7 settings.yml)."""

    llm: LlmSettings
    windows: Windows = Windows()
    limits: Limits = Limits()
    digest: DigestSettings = DigestSettings()
    ingest: IngestSettings = IngestSettings()
    timezone: str = "America/Edmonton"
