"""Ingestion core (E1): the fetcher contract (GRP-10) and normalizer/dedup (GRP-14).

Public surface every E1/E2/E5 issue builds on:

- Fetch contract: :class:`RawItem`, :class:`Fetcher`, :class:`FetcherRegistry`,
  and the shipped :class:`FakeFetcher` test double.
- Normalize + identity: :func:`normalize`, :func:`normalize_batch`,
  :func:`compute_item_id`, :func:`canonicalize_url`, :func:`dedup_within_batch`.
- Near-dup layer: :func:`compute_content_hash`, :func:`hamming_distance`,
  :func:`group_near_duplicates`.

Concrete fetchers (RSS/YouTube/Reddit/X) and the orchestrator live in later E1/E5
issues and plug into these contracts without changing them.

Failure modes
-------------
None of its own — this is a pure re-export aggregator. See each submodule's
docstring for its failure modes (``base``/``registry``/``fake`` for the fetch
contract, ``normalize``/``dedup`` for identity + near-dup).
"""

from __future__ import annotations

from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.dedup import compute_content_hash, group_near_duplicates, hamming_distance
from grepify.ingest.fake import FakeFetcher
from grepify.ingest.normalize import (
    canonicalize_url,
    compute_item_id,
    dedup_within_batch,
    normalize,
    normalize_batch,
)
from grepify.ingest.registry import FetcherRegistry

__all__ = [
    "FakeFetcher",
    "Fetcher",
    "FetcherRegistry",
    "RawItem",
    "canonicalize_url",
    "compute_content_hash",
    "compute_item_id",
    "dedup_within_batch",
    "group_near_duplicates",
    "hamming_distance",
    "normalize",
    "normalize_batch",
]
