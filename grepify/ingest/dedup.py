"""Near-duplicate detection: title simhash + Hamming clustering (GRP-14).

The exact-dedup layer is ``item_id`` (:mod:`grepify.ingest.normalize`). This is
the *second* layer (PRD §6 note 2): the same wire story reposted across sources
has different urls/guids - so different ``item_id``s - but near-identical titles.
:func:`compute_content_hash` gives each item a 64-bit **simhash** of its title;
titles that are near-duplicates land a small :func:`hamming_distance` apart.
:func:`group_near_duplicates` clusters them so the UI can collapse a group behind
an "n similar" expander (F-SIT-03). Grouping never deletes.

Determinism: features are hashed with :func:`hashlib.blake2b`, **not** Python's
built-in ``hash()`` (which is salted per process - it would make ``content_hash``
differ run to run and break byte-stable builds, F-SIT-08).

Failure modes
-------------
Pure functions, no I/O. :func:`hamming_distance` raises :class:`ValueError` if
handed two hashes of different width (a programming error - mixing hash schemes).
An empty / punctuation-only title yields the all-zero hash deterministically.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Sequence

from grepify.models import Item

_HASH_BITS = 64
_HASH_HEX_LEN = _HASH_BITS // 4  # 16 hex chars
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TAG_RE = re.compile(r"<[^>]+>")

# Near-dup Hamming threshold, calibrated on title-length text (not documents).
# A title has few tokens, so each simhash bit's vote margin is thin and a single
# word change flips more bits than the classic document threshold (k≈3) assumes.
# Measured on representative pairs: a minor headline edit sits ~6-12 bits apart,
# an unrelated headline ~29-35 - a wide gap. 12 catches the reposts while leaving
# a ~17-bit margin to unrelated content. Grouping is non-destructive (UI collapse,
# expandable - F-SIT-03), so favouring recall here is safe.
_DEFAULT_MAX_DISTANCE = 12


def _normalize_title(title: str) -> list[str]:
    """Lowercase, unescape entities, strip tags, tokenize to alnum unigrams."""
    text = html.unescape(title).lower()
    text = _TAG_RE.sub(" ", text)
    return _TOKEN_RE.findall(text)


def _feature_bits(token: str) -> int:
    """Stable 64-bit hash of one token (process-independent - see module docstring)."""
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def compute_content_hash(title: str) -> str:
    """Return the 64-bit simhash of ``title`` as a 16-char zero-padded hex string.

    Empty/punctuation-only titles hash to ``"0" * 16`` deterministically.
    """
    tokens = _normalize_title(title)
    if not tokens:
        return "0" * _HASH_HEX_LEN

    votes = [0] * _HASH_BITS
    for token in tokens:
        bits = _feature_bits(token)
        for i in range(_HASH_BITS):
            if (bits >> i) & 1:
                votes[i] += 1
            else:
                votes[i] -= 1

    fingerprint = 0
    for i in range(_HASH_BITS):
        if votes[i] > 0:
            fingerprint |= 1 << i
    return f"{fingerprint:0{_HASH_HEX_LEN}x}"


def hamming_distance(a: str, b: str) -> int:
    """Bit-difference count between two equal-width hex content hashes."""
    if len(a) != len(b):
        raise ValueError(f"hash width mismatch: {len(a)} vs {len(b)} hex chars")
    return (int(a, 16) ^ int(b, 16)).bit_count()


def group_near_duplicates(
    items: Sequence[Item], *, max_distance: int = _DEFAULT_MAX_DISTANCE
) -> list[list[Item]]:
    """Cluster ``items`` whose title simhashes are within ``max_distance`` bits.

    Returns clusters (each a list; singletons included) in deterministic order:
    items are first ordered by ``(published_at, item_id)``, greedily assigned to
    the first existing cluster within ``max_distance`` of any member, and clusters
    are returned in the order their first member appears.

    Intended for a **windowed / paginated** set (e.g. one items-browser page or a
    trend window), not the whole corpus: it is O(n²) in ``items`` (PRD §6 note 2,
    F-SIT-03). Grouping only - nothing is deleted.
    """
    ordered = sorted(items, key=lambda it: (it.published_at, it.item_id))
    clusters: list[list[Item]] = []
    for item in ordered:
        for cluster in clusters:
            if any(
                hamming_distance(item.content_hash, member.content_hash) <= max_distance
                for member in cluster
            ):
                cluster.append(item)
                break
        else:
            clusters.append([item])
    return clusters
