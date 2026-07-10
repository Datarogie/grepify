"""GRP-22: YAKE fallback extractor - offline, deterministic, never raises."""

from __future__ import annotations

from grepify.extract.fallback import YakeFallbackExtractor
from grepify.models import Item, SourceKind


def _item(item_id: str, *, title: str, summary: str | None = "") -> Item:
    return Item(
        item_id=item_id,
        source_id="src-1",
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://example.com/{item_id}",
        title=title,
        summary=summary,
        published_at="2026-07-08T09:00:00+00:00",
        fetched_at="2026-07-08T10:00:00+00:00",
        content_hash=f"hash-{item_id}",
    )


def test_extracts_keywords_from_title_and_summary() -> None:
    extractor = YakeFallbackExtractor()
    item = _item(
        "a",
        title="OpenAI releases GPT-5.2 with major reasoning gains",
        summary="Agentic coding workflows benefit from the new model.",
    )
    result = extractor.extract([item])
    assert result["a"]
    assert all(isinstance(kw, str) for kw in result["a"])


def test_multiple_items_each_get_their_own_keywords() -> None:
    extractor = YakeFallbackExtractor()
    items = [
        _item("a", title="Anthropic ships Claude updates for agentic coding"),
        _item("b", title="dbt Labs announces new analytics engineering features"),
    ]
    result = extractor.extract(items)
    assert set(result) == {"a", "b"}
    assert result["a"] != result["b"]


def test_respects_max_keywords() -> None:
    extractor = YakeFallbackExtractor(max_keywords=2)
    item = _item(
        "a",
        title="OpenAI Anthropic Google Meta Microsoft Nvidia race for AI supremacy",
        summary="Funding, chips, models, agents, tooling, and infrastructure all in play.",
    )
    result = extractor.extract([item])
    assert len(result["a"]) <= 2


# --- "must not raise" safety net (F-EXT-02 sanity bar mirrored from GRP-21) ---


def test_empty_title_and_summary_yields_no_keywords() -> None:
    extractor = YakeFallbackExtractor()
    item = _item("a", title="", summary=None)
    assert extractor.extract([item]) == {"a": []}


def test_blank_whitespace_only_text_yields_no_keywords() -> None:
    extractor = YakeFallbackExtractor()
    item = _item("a", title="   ", summary="   ")
    assert extractor.extract([item]) == {"a": []}


def test_stopwords_only_yields_no_keywords() -> None:
    extractor = YakeFallbackExtractor()
    item = _item("a", title="the a of", summary=None)
    assert extractor.extract([item]) == {"a": []}


def test_url_only_title_yields_no_url_keywords() -> None:
    extractor = YakeFallbackExtractor()
    item = _item("a", title="https://example.com/some/path?query=1", summary=None)
    for keyword in extractor.extract([item])["a"]:
        assert "://" not in keyword
        assert not keyword.lower().startswith("www.")


def test_emoji_and_unicode_text_does_not_raise() -> None:
    extractor = YakeFallbackExtractor()
    item = _item("a", title="🎉🎉🎉 emoji only test 🎉", summary="日本語のテキスト")
    result = extractor.extract([item])
    assert "a" in result


def test_no_item_produces_keyword_longer_than_60_chars() -> None:
    extractor = YakeFallbackExtractor()
    item = _item("a", title="a" * 500, summary=None)
    for keyword in extractor.extract([item])["a"]:
        assert 2 <= len(keyword) <= 60


def test_empty_items_list_returns_empty_mapping() -> None:
    extractor = YakeFallbackExtractor()
    assert extractor.extract([]) == {}
