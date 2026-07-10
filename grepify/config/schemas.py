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
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from grepify.models import SourceKind

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
    """One source within a group file. Exactly one locator field per kind."""

    id: str
    kind: SourceKind
    name: str | None = None
    enabled: bool = True
    added_at: str | None = None
    config: dict[str, Any] | None = None  # per-source overrides -> items.config_json
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

    @property
    def canonical_url(self) -> str:
        """Canonical feed identity - the same feed always hashes the same way."""
        if self.kind is SourceKind.RSS:
            assert self.url is not None
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


class SettingsConfig(_ConfigModel):
    """Top-level settings (PRD §7 settings.yml)."""

    llm: LlmSettings
    windows: Windows = Windows()
    limits: Limits = Limits()
    digest: DigestSettings = DigestSettings()
    timezone: str = "America/Edmonton"
