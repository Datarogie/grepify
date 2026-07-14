"""Domain records - the storage contract.

These pydantic models are the column set of PRD §6, expressed as backend-neutral
domain objects. They are what the :class:`~grepify.repository.base.Repository`
interface reads and writes; no backend (SQLite in v1, Postgres in v2) leaks
into them. Timestamps are ISO-8601 strings (see PRD §6 - text columns; keeps
JSONL diffs readable and is Postgres-swappable).

Failure modes
-------------
Construction validates types and enum membership; invalid data raises
``pydantic.ValidationError`` at the boundary rather than corrupting truth files.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceKind(StrEnum):
    RSS = "rss"
    YOUTUBE = "youtube"
    REDDIT = "reddit"
    X = "x"


class FetchStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    SKIPPED = "skipped"

    @property
    def is_real_attempt(self) -> bool:
        """Whether this status records a genuine fetch attempt.

        ``SKIPPED`` is the one non-attempt status (a cadence non-event, T6):
        the source was deliberately not dispatched, so it carries no fetch
        signal. Every other status is a real attempt. Both the cadence math
        (:func:`grepify.ingest.cadence.last_real_attempt_at`) and the health
        rollup (:func:`grepify.health.compute_health`) filter on this single
        definition so a future non-attempt status added here stays in sync
        across both instead of silently desyncing one from the other.
        """
        return self is not FetchStatus.SKIPPED


class SourceStatus(StrEnum):
    """A source's lifecycle class (ADR 0002), authoritative over ``enabled``.

    ``active``/``degraded`` are enabled (fetched); ``paywalled``/``dead`` are
    disabled (``dead`` still gets a slow re-check, ``paywalled`` is terminal).
    ``gone`` is a removal signal - a source is deleted from its group file, so
    it never reaches storage as a live class (validate rejects a ``gone`` entry
    left in a group file).
    """

    ACTIVE = "active"
    DEGRADED = "degraded"
    PAYWALLED = "paywalled"
    GONE = "gone"
    DEAD = "dead"

    @property
    def is_enabled(self) -> bool:
        return self in (SourceStatus.ACTIVE, SourceStatus.DEGRADED)


class Rung(StrEnum):
    """Which acquisition-ladder strategy served a fetch (ADR 0002 §1).

    Recorded on the ``fetch_log`` row so the classification layer can tell a
    primary-served feed (``direct`` -> active) from a fallback-served one
    (anything else -> degraded). ``direct`` is rung 0; the rest are the ordered
    fallbacks a source reaches only when the rung before it failed.
    """

    DIRECT = "direct"
    ALT_ENDPOINT = "alt_endpoint"
    AUTODISCOVERY = "autodiscovery"
    MIRROR = "mirror"
    THIRD_PARTY = "third_party"

    @property
    def is_fallback(self) -> bool:
        return self is not Rung.DIRECT


class DigestKind(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class ExtractionMethod(StrEnum):
    LLM = "llm"
    FALLBACK = "fallback"


class _Record(BaseModel):
    """Base for stored records: strict, extra fields forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceGroup(_Record):
    """A curated bundle of sources sharing a category (PRD §6 source_groups)."""

    group_id: str
    name: str
    category: str
    enabled: bool = True
    builtin: bool = False


class Source(_Record):
    """A single feed/channel/subreddit/handle (PRD §6 sources)."""

    source_id: str
    name: str
    kind: SourceKind
    url: str
    url_hash: str
    group_id: str
    enabled: bool = True
    added_at: str
    config_json: str | None = None
    status: SourceStatus = SourceStatus.ACTIVE
    evidence: str | None = None
    message: str | None = None
    active_url: str | None = None


class Item(_Record):
    """A normalized content item (PRD §6 items). Metadata only - no article body."""

    item_id: str
    source_id: str
    kind: SourceKind
    external_id: str | None = None
    canonical_url: str
    title: str
    summary: str | None = None
    author: str | None = None
    published_at: str
    fetched_at: str
    content_hash: str
    transcript_ref: str | None = None
    lang: str | None = None


class ItemKeyword(_Record):
    """An extracted keyword attached to an item (PRD §6 item_keywords)."""

    item_id: str
    keyword: str
    rank: int
    method: ExtractionMethod
    model: str | None = None
    extracted_at: str


class KeywordAlias(_Record):
    """User-curated merge map entry (PRD §6 keyword_aliases)."""

    alias: str
    canonical: str


class Digest(_Record):
    """A generated per-category digest (PRD §6 digests)."""

    digest_id: str
    kind: DigestKind
    category: str
    period_start: str
    period_end: str
    title: str
    body_md: str
    top_keywords: str  # json [{keyword, count}]
    model: str
    prompt_version: str  # prompt contract id, e.g. 'digest-v1'; 'none' when templated (F-DIG-04)
    created_at: str


class FetchLogEntry(_Record):
    """One per-source fetch attempt (PRD §6 fetch_log)."""

    source_id: str
    run_id: str
    started_at: str
    status: FetchStatus
    items_new: int = 0
    error: str | None = None
    duration_ms: int | None = None
    rung: Rung | None = None


class LlmLogEntry(_Record):
    """One LLM call, including failures (PRD §6 llm_log)."""

    run_id: str
    purpose: str  # 'extract' | 'digest'
    model: str
    input_items: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    status: str
    created_at: str


class RunManifest(_Record):
    """Per-run manifest written to data/runs/<run_id>.json (PRD §8 F-OPS-04).

    Powers the health page and phone debugging: counts, durations, budget usage.
    """

    run_id: str
    command: str
    started_at: str
    finished_at: str | None = None
    ok: bool = True
    counts: dict[str, int] = Field(default_factory=dict)
    durations_ms: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
