"""Stable url slugs for digest + keyword pages (E4, GRP-43/44).

Two pure helpers shared by every place that links to a digest or keyword page -
the home lists, the digest index, the keyword cloud, and the digest chips - so a
link and the page it points at always agree. Both are deterministic (no clock,
no random state) and filesystem/url safe.

- :func:`digest_slug` strips a digest_id's redundant ``<kind>-`` prefix
  (``daily-ai-2026-07-07`` -> ``ai-2026-07-07``); ``kind`` is a fixed-enum
  prefix, so the strip is unambiguous even when the category contains a hyphen
  (``daily-data-eng-2026-07-07`` -> ``data-eng-2026-07-07``).
- :func:`keyword_slug` renders a keyword to a readable base plus a short blake2b
  suffix, so symbol-heavy or otherwise-colliding keywords (``c++``/``c#``) still
  get distinct, stable page paths.

Failure modes
-------------
Pure string transforms - never raise or perform I/O.
"""

from __future__ import annotations

import hashlib
import re

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_KEYWORD_HASH_LEN = 6  # hex chars of the disambiguating blake2b suffix


def digest_slug(digest_id: str, kind: str) -> str:
    """Return the page-path segment for a digest (``digest_id`` minus ``<kind>-``)."""
    prefix = f"{kind}-"
    return digest_id[len(prefix) :] if digest_id.startswith(prefix) else digest_id


def keyword_slug(keyword: str) -> str:
    """Return a stable, unique, url-safe slug for a keyword.

    ``slugify(keyword) + "-" + blake2b(keyword)[:6]``; the hash is taken over the
    exact keyword text so two keywords that slugify the same still differ.
    """
    base = _NON_SLUG.sub("-", keyword.lower()).strip("-") or "kw"
    digest = hashlib.blake2b(keyword.encode("utf-8"), digest_size=4).hexdigest()[:_KEYWORD_HASH_LEN]
    return f"{base}-{digest}"
