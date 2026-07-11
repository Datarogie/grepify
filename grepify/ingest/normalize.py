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

Entity-encoded tags (T5 audit, GRP-30)
---------------------------------------
A source that HTML-escapes a whole element in its ``<description>`` (e.g.
``&lt;div class=&quot;alert&quot;&gt;Breaking news&lt;/div&gt;``) used to leak
that markup verbatim into ``item.summary``: the first tag-strip pass runs
before entities are unescaped, so an encoded tag is invisible to it, and by
the time ``html.unescape`` reveals the literal ``<div>...</div>`` there was no
further stripping pass. :func:`_strip_html` now runs a second, conservative
pass after unescaping to close that gap - conservative because it only
treats a **matched open/close pair of the same tag name** as markup (the
shape a genuinely encoded HTML element takes), stripping the tags but keeping
the inner text, same as the first pass. A bare, unpaired tag-shaped fragment
revealed by unescaping - e.g. the ``<b>`` in ``"a &lt;b&gt; c and c &gt; a"``,
a comparison operator, not markup - has no matching close tag and is left as
plain text, so genuinely plain-text ``&lt;x&gt;``-shaped content (comparison
operators, code snippets, common in this aggregator's dev/AI-research feeds)
still is not mistaken for a tag. Entity-*double*-encoded markup (e.g.
``&amp;lt;div&amp;gt;``) still only unwinds one level per :func:`clean_summary`
call, for the same reason: a single ``html.unescape`` pass cannot see through
a second layer of escaping without also risking that plain-text tradeoff.

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

# Same tag-strip regex feedutil.clean_title applies to titles - summaries never
# got the same treatment, so raw markup (<div>, class="...", <span>, ...) from
# feed descriptions/reddit selftext was landing in item.summary and leaking
# into the YAKE fallback as spurious "keywords".
_TAG_RE = re.compile(r"<[^>]+>")
# A generic tag-strip removes <script>/<style> tags but not their bodies, so
# without this a feed embedding a <script> or <style> element would leak its
# code/CSS text as if it were prose (same symptom as the reported bug).
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
    """Replace a matched open/close tag pair (post-unescape) with its inner
    text, padded with spaces so adjacent words don't glue together; any tag
    nested inside is real markup too (it only exists because it is inside a
    confirmed pair), so it gets the plain tag-strip as well."""
    return f" {_TAG_RE.sub(' ', match.group(2))} "


def _strip_html(text: str) -> str:
    """Drop script/style bodies, strip remaining markup, unescape entities,
    then run a second conservative strip pass for tags an entity-encoded
    source only reveals *after* unescaping - collapsing whitespace last. The
    same treatment ``feedutil.clean_title`` gives titles (plus the
    script/style + second-pass steps), applied here so every summary
    (RSS/Atom description, reddit selftext) gets it too regardless of source
    kind. See the module docstring's "Summary cleaning" and "Entity-encoded
    tags" sections for the known regex-vs-parser limitations."""
    without_script_style = _SCRIPT_STYLE_RE.sub(" ", text)
    without_tags = _TAG_RE.sub(" ", without_script_style)
    unescaped = html.unescape(without_tags)
    # Second pass, deliberately after unescaping: a source that entity-encoded
    # a whole <script>/<style> element, or any other paired tag, only becomes
    # literal markup at this point (see "Entity-encoded tags" above).
    revealed_script_style = _SCRIPT_STYLE_RE.sub(" ", unescaped)
    revealed_tags = _PAIRED_TAG_RE.sub(_strip_revealed_pairs, revealed_script_style)
    return " ".join(revealed_tags.split())


def clean_summary(text: str) -> str:
    """Strip markup + entities and truncate to the stored-excerpt limit.

    The one place summary cleaning is defined. :func:`normalize` applies it to a
    fresh :class:`RawItem` summary at ingest; the ``renormalize`` maintenance
    command (GRP-60) re-applies the *same* function to already-stored summaries,
    so a re-extract can never diverge from what a fresh ingest would produce.
    Idempotent: cleaning an already-clean summary returns it unchanged (up to the
    documented regex-vs-parser fixed point). The trailing ``rstrip`` after the
    truncation is load-bearing for that: without it, truncating exactly on a
    whitespace boundary would leave a trailing space that a second pass strips,
    so ``renormalize`` would rewrite (and re-extract) the same long item forever.
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
