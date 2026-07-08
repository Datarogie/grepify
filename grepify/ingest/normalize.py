"""Normalizer + identity: ``RawItem`` -> ``Item`` (GRP-14).

Turns a fetcher's :class:`~grepify.ingest.base.RawItem` into the PRD §6
:class:`~grepify.models.Item`, computing the one thing fetchers deliberately do
*not*: a stable identity. All identity rules live here so every source kind
dedups the same way.

Identity (PRD §6 ``item_id = kind + canonical_url|external_id``)
---------------------------------------------------------------
``item_id = sha256(kind + "\\0" + identity)`` where ``identity`` is the
``external_id`` when the entry carries one, else the ``canonical_url``. It is
independent of ``fetched_at`` and ``published_at`` fallbacks, so re-fetching the
same entry yields the same ``item_id`` and :meth:`Repository.add_items
<grepify.repository.base.Repository.add_items>` writes zero new rows — this is
what makes ingest idempotent (F-ING-07).

Unique-index handling
---------------------
The cache has ``unique index idx_items_dedup on items(kind, external_id)``. When
``external_id`` is present it *is* the identity, so two entries sharing
``(kind, external_id)`` collapse to one ``item_id`` before insert and the unique
index cannot be violated. Guid-less entries store ``external_id = NULL`` (SQLite
treats NULLs as distinct) and dedup on ``canonical_url`` via ``item_id``. An
empty / whitespace-only ``external_id`` is coerced to ``None`` so a blank id can
never masquerade as a shared non-null key.

Failure modes
-------------
Pure functions, no I/O. They can only raise ``pydantic.ValidationError``, and
only if a :class:`~grepify.models.Item` field constraint is violated (wrong type
from a malformed ``RawItem``); the field set produced here always satisfies the
model. Note the ``Item.title`` / ``Item.canonical_url`` columns are ``not null``
but *not* min-length — an empty string passes; fetchers own display-ready title
text. Malformed URLs do not raise: :func:`canonicalize_url` passes non-``http(s)``
/relative URLs through unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from grepify.ingest.base import RawItem
from grepify.ingest.dedup import compute_content_hash
from grepify.models import Item, Source, SourceKind

_SUMMARY_MAX_CHARS = 2000  # PRD §6 / F-ING-04: store a truncated excerpt only

# Tracking/analytics query params dropped during canonicalization: they vary per
# referral for the *same* article, so keeping them would defeat url-based dedup.
_TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "referrer",
        "_hsenc",
        "_hsmi",
    }
)


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith("utm_")


def canonicalize_url(url: str) -> str:
    """Return a conservative, deterministic canonical form of ``url``.

    Lowercases scheme + host, drops the default port and any userinfo, drops the
    fragment, strips tracking query params (``utm_*``, ``gclid``, ``fbclid``,
    ``ref``, …) while preserving the order of the rest, and strips a single
    trailing slash. A relative or non-``http(s)`` URL is returned stripped but
    otherwise unchanged (nothing to canonicalize safely).
    """
    stripped = url.strip()
    parts = urlsplit(stripped)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return stripped

    host = parts.hostname  # urlsplit.hostname lowercases the host (no punycode/IDNA change)
    port = parts.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))


def compute_item_id(kind: SourceKind, canonical_url: str, external_id: str | None) -> str:
    """Stable content-identity hash (PRD §6). See module docstring for the rule."""
    identity = external_id if external_id else canonical_url
    return hashlib.sha256(f"{kind.value}\x00{identity}".encode()).hexdigest()


def _clean_external_id(external_id: str | None) -> str | None:
    """Coerce an empty / whitespace-only external id to ``None`` (see module
    docstring — protects the ``(kind, external_id)`` unique index)."""
    if external_id is None:
        return None
    trimmed = external_id.strip()
    return trimmed or None


def normalize(raw: RawItem, source: Source, *, fetched_at: str) -> Item:
    """Normalize one :class:`RawItem` from ``source`` into an :class:`Item`.

    ``fetched_at`` is the run's fetch instant (ISO-8601, from the injected
    :class:`~grepify.clock.Clock`); it also backs ``published_at`` when the entry
    carried no date, so ``published_at`` is never null (PRD §6).
    """
    canonical = canonicalize_url(raw.url)
    external_id = _clean_external_id(raw.external_id)
    summary = raw.summary[:_SUMMARY_MAX_CHARS] if raw.summary is not None else None
    return Item(
        item_id=compute_item_id(source.kind, canonical, external_id),
        source_id=source.source_id,
        kind=source.kind,
        external_id=external_id,
        canonical_url=canonical,
        title=raw.title,
        summary=summary,
        author=raw.author,
        published_at=raw.published_at or fetched_at,
        fetched_at=fetched_at,
        content_hash=compute_content_hash(raw.title),
        transcript_ref=raw.transcript_ref,
        lang=raw.lang,
    )


def normalize_batch(raws: Sequence[RawItem], source: Source, *, fetched_at: str) -> list[Item]:
    """Normalize a fetcher's whole result for one source (1:1 with ``raws``)."""
    return [normalize(raw, source, fetched_at=fetched_at) for raw in raws]


def dedup_within_batch(items: Sequence[Item]) -> list[Item]:
    """Drop later items sharing an ``item_id`` (keep the first), preserving order.

    A single feed can list the same entry twice; this lets the orchestrator
    report an honest new-item count. Cross-run dedup is the repository's job
    (``add_items`` is idempotent by ``item_id``); this is the in-batch pass.
    """
    seen: set[str] = set()
    result: list[Item] = []
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        result.append(item)
    return result
