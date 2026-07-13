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
<grepify.repository.base.Repository.add_items>` writes zero new rows - this is
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

Summary cleaning
----------------
``RawItem.summary`` is raw source text (RSS/Atom ``<description>`` HTML,
reddit ``selftext``) - fetchers deliberately don't clean it (E1 brief: fetchers
own title text via ``feedutil.clean_title``, everything else funnels through
here). :func:`normalize` drops ``<script>``/``<style>`` element bodies, strips
remaining tags, and unescapes entities the same way before truncating to
:data:`_SUMMARY_MAX_CHARS`, so ``item.summary`` never carries markup into
downstream consumers (the YAKE fallback extractor treats it as plain text,
PRD §7).

Known limitation, shared with ``feedutil.clean_title``: this is a regex
stripper, not a real HTML parser. A dangling/unclosed tag, or a tag whose
attribute value itself contains a literal ``>``, is not stripped correctly.
Likewise, a ``<script>`` body that itself contains the literal string
``</script>`` (some legacy tracking snippets escape the slash to avoid this
exact problem) can make the non-greedy body match end early and leak the
remainder of the script as text - the same regex-vs-parser tradeoff as the
rest of this list.

Entity-encoded tags
-------------------
A source that HTML-escapes a whole element in its ``<description>`` (e.g.
``&lt;div class=&quot;alert&quot;&gt;Breaking news&lt;/div&gt;``) only becomes
literal markup after ``html.unescape``, past the first tag-strip pass. So
:func:`_strip_html` runs a second, conservative pass after unescaping: it treats
only a **matched open/close pair of the same tag name** as markup (the shape a
genuinely encoded element takes), stripping the tags but keeping the inner text.
A bare, unpaired tag-shaped fragment - e.g. the ``<b>`` in
``"a &lt;b&gt; c and c &gt; a"``, a comparison operator - has no close tag and
stays plain text, so ``&lt;x&gt;``-shaped code snippets are not mistaken for
tags. Entity-*double*-encoded markup (``&amp;lt;div&amp;gt;``) unwinds only one
level per :func:`clean_summary` call, for the same reason: a single
``html.unescape`` cannot see through a second layer without risking that
plain-text tradeoff.

Failure modes
-------------
Pure functions, no I/O. They can only raise ``pydantic.ValidationError``, and
only if a :class:`~grepify.models.Item` field constraint is violated (wrong type
from a malformed ``RawItem``); the field set produced here always satisfies the
model. Note the ``Item.title`` / ``Item.canonical_url`` columns are ``not null``
but *not* min-length - an empty string passes; fetchers own display-ready title
text. Malformed URLs do not raise: :func:`canonicalize_url` passes non-``http(s)``
/relative URLs through unchanged.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from grepify.ingest.base import RawItem
from grepify.ingest.dedup import compute_content_hash
from grepify.models import Item, Source, SourceKind

_SUMMARY_MAX_CHARS = 2000  # PRD §6 / F-ING-04: store a truncated excerpt only

# Same tag-strip regex feedutil.clean_title applies to titles, so summary markup
# never leaks into item.summary (and the YAKE fallback) as spurious keywords.
_TAG_RE = re.compile(r"<[^>]+>")
# _TAG_RE strips <script>/<style> tags but not their bodies, so this removes the
# element whole - otherwise its code/CSS text would read as prose.
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
# Second-pass, post-unescape only (see module docstring "Entity-encoded tags"):
# a matched open/close pair of the *same* tag name is treated as a revealed
# tag; a bare unpaired fragment (comparison operators, code snippets) is not.
_PAIRED_TAG_RE = re.compile(r"<(\w+)(?:\s[^>]*)?>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)

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


def _strip_revealed_pairs(match: re.Match[str]) -> str:
    """Replace a matched tag pair with its inner text, space-padded so adjacent
    words don't glue together; a nested tag is real markup (it sits inside a
    confirmed pair), so it gets the plain tag-strip too."""
    return f" {_TAG_RE.sub(' ', match.group(2))} "


def _strip_html(text: str) -> str:
    """Drop script/style bodies, strip markup, unescape entities, then a second
    conservative pass for tags an entity-encoded source only reveals after
    unescaping, collapsing whitespace last. See the module docstring's "Summary
    cleaning" and "Entity-encoded tags" sections for the regex-vs-parser limits."""
    without_script_style = _SCRIPT_STYLE_RE.sub(" ", text)
    without_tags = _TAG_RE.sub(" ", without_script_style)
    unescaped = html.unescape(without_tags)
    # Second pass after unescaping: entity-encoded elements only become literal
    # markup here (see "Entity-encoded tags" above).
    revealed_script_style = _SCRIPT_STYLE_RE.sub(" ", unescaped)
    revealed_tags = _PAIRED_TAG_RE.sub(_strip_revealed_pairs, revealed_script_style)
    return " ".join(revealed_tags.split())


def clean_summary(text: str) -> str:
    """Strip markup + entities and truncate to the stored-excerpt limit.

    The one place summary cleaning is defined; ``renormalize`` re-applies the
    *same* function to stored summaries, so a re-extract never diverges from a
    fresh ingest. Idempotent: cleaning an already-clean summary returns it
    unchanged (up to the regex-vs-parser fixed point). The trailing ``rstrip``
    is load-bearing for that: without it, truncating exactly on a whitespace
    boundary leaves a trailing space a second pass would strip, so
    ``renormalize`` would rewrite the same long item forever.
    """
    return _strip_html(text)[:_SUMMARY_MAX_CHARS].rstrip()


def _clean_external_id(external_id: str | None) -> str | None:
    """Coerce an empty / whitespace-only external id to ``None`` (see module
    docstring - protects the ``(kind, external_id)`` unique index)."""
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
    summary = clean_summary(raw.summary) if raw.summary is not None else None
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
