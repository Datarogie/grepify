"""GRP-14: normalizer + identity (item_id, canonical_url, unique-index handling)."""

from __future__ import annotations

import pytest

from grepify.ingest import (
    RawItem,
    canonicalize_url,
    compute_item_id,
    dedup_within_batch,
    normalize,
    normalize_batch,
)
from grepify.models import SourceKind
from tests.conftest import make_source

_FETCHED = "2026-07-08T12:00:00+00:00"


def _raw(**kw: object) -> RawItem:
    base: dict[str, object] = {"url": "https://example.com/a", "title": "Hello World"}
    base.update(kw)
    return RawItem(**base)  # type: ignore[arg-type]


# --- canonicalize_url -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),  # bare root keeps its slash
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8080/a", "https://example.com:8080/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&id=5&fbclid=9", "https://example.com/a?id=5"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("/relative/path", "/relative/path"),
        ("mailto:foo@bar.com", "mailto:foo@bar.com"),
    ],
)
def test_canonicalize_url(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_canonicalize_preserves_remaining_query_order() -> None:
    assert canonicalize_url("https://e.com/a?b=2&a=1&utm_x=z") == "https://e.com/a?b=2&a=1"


# --- item_id identity rule --------------------------------------------------


def test_item_id_uses_external_id_when_present() -> None:
    # Same external_id + kind -> same id, even from different urls.
    a = compute_item_id(SourceKind.RSS, "https://x.com/1", "guid-42")
    b = compute_item_id(SourceKind.RSS, "https://y.com/2", "guid-42")
    assert a == b


def test_item_id_falls_back_to_canonical_url_without_external_id() -> None:
    a = compute_item_id(SourceKind.RSS, "https://x.com/1", None)
    b = compute_item_id(SourceKind.RSS, "https://x.com/1", None)
    c = compute_item_id(SourceKind.RSS, "https://x.com/2", None)
    assert a == b
    assert a != c


def test_item_id_is_kind_scoped() -> None:
    rss = compute_item_id(SourceKind.RSS, "https://x.com/1", "id1")
    yt = compute_item_id(SourceKind.YOUTUBE, "https://x.com/1", "id1")
    assert rss != yt


def test_item_id_stable_across_fetched_at() -> None:
    source = make_source("s1")
    raw = _raw(external_id="guid-1")
    i1 = normalize(raw, source, fetched_at="2026-07-08T00:00:00+00:00")
    i2 = normalize(raw, source, fetched_at="2026-07-09T23:59:00+00:00")
    assert i1.item_id == i2.item_id  # identity independent of when we fetched


# --- unique-index handling (empty external_id -> None) ----------------------


def test_blank_external_id_coerced_to_none() -> None:
    source = make_source("s1")
    for blank in ("", "   ", None):
        item = normalize(_raw(external_id=blank), source, fetched_at=_FETCHED)
        assert item.external_id is None  # so (kind, external_id) NULLs stay distinct


def test_blank_external_id_items_dedup_on_url_not_blank_key() -> None:
    source = make_source("s1")
    # Two DIFFERENT urls, both blank external_id: distinct items (NULLs distinct),
    # keyed on canonical_url -> different item_ids, no false unique-index clash.
    a = normalize(_raw(url="https://e.com/a", external_id=""), source, fetched_at=_FETCHED)
    b = normalize(_raw(url="https://e.com/b", external_id=""), source, fetched_at=_FETCHED)
    assert a.external_id is None and b.external_id is None
    assert a.item_id != b.item_id


# --- field hygiene ----------------------------------------------------------


def test_summary_truncated_to_2000_chars() -> None:
    source = make_source("s1")
    item = normalize(_raw(summary="x" * 5000), source, fetched_at=_FETCHED)
    assert item.summary is not None
    assert len(item.summary) == 2000


def test_missing_published_at_falls_back_to_fetched_at() -> None:
    source = make_source("s1")
    item = normalize(_raw(published_at=None), source, fetched_at=_FETCHED)
    assert item.published_at == _FETCHED


def test_published_at_preserved_when_present() -> None:
    source = make_source("s1")
    item = normalize(_raw(published_at="2026-01-01T00:00:00+00:00"), source, fetched_at=_FETCHED)
    assert item.published_at == "2026-01-01T00:00:00+00:00"


def test_normalize_copies_source_identity_and_kind() -> None:
    source = make_source("src-x", kind=SourceKind.REDDIT)
    item = normalize(_raw(), source, fetched_at=_FETCHED)
    assert item.source_id == "src-x"
    assert item.kind is SourceKind.REDDIT
    assert item.fetched_at == _FETCHED
    assert item.canonical_url == "https://example.com/a"
    assert item.content_hash  # populated


# --- batch helpers ----------------------------------------------------------


def test_normalize_batch_is_one_to_one() -> None:
    source = make_source("s1")
    raws = [_raw(url=f"https://e.com/{n}", external_id=f"g{n}") for n in range(3)]
    items = normalize_batch(raws, source, fetched_at=_FETCHED)
    assert len(items) == 3
    assert len({i.item_id for i in items}) == 3


def test_dedup_within_batch_keeps_first_per_item_id() -> None:
    source = make_source("s1")
    # Same entry listed twice in one feed -> same item_id.
    raws = [
        _raw(external_id="dup"),
        _raw(external_id="dup"),
        _raw(url="https://e.com/z", external_id="z"),
    ]
    items = normalize_batch(raws, source, fetched_at=_FETCHED)
    deduped = dedup_within_batch(items)
    assert len(deduped) == 2
    assert deduped[0].external_id == "dup"
    assert deduped[1].external_id == "z"
