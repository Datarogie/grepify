"""GRP-25: PRD §10.7 post-extract data-quality gate."""

from __future__ import annotations

import pytest

from grepify.errors import DataQualityError
from grepify.extract.quality import assert_data_quality
from tests.conftest import make_item, make_keyword


def test_every_item_with_a_keyword_row_is_not_flagged() -> None:
    items = [make_item("a"), make_item("b")]
    keywords = [make_keyword("a", "genai"), make_keyword("b", "llm")]
    report = assert_data_quality(items, keywords)
    assert report.no_keywords_item_ids == []


def test_item_with_no_keyword_rows_is_flagged_not_raised() -> None:
    items = [make_item("a"), make_item("b")]
    keywords = [make_keyword("a", "genai")]  # "b" got nothing (F-EXT-02: valid)
    report = assert_data_quality(items, keywords)
    assert report.no_keywords_item_ids == ["b"]


def test_no_items_no_keywords_is_a_clean_no_op() -> None:
    report = assert_data_quality([], [])
    assert report.no_keywords_item_ids == []


def test_keyword_over_60_chars_raises_data_quality_error() -> None:
    items = [make_item("a")]
    keywords = [make_keyword("a", "x" * 61)]
    with pytest.raises(DataQualityError, match="exceed 60 chars"):
        assert_data_quality(items, keywords)


def test_keyword_at_exactly_60_chars_is_fine() -> None:
    items = [make_item("a")]
    keywords = [make_keyword("a", "x" * 60)]
    report = assert_data_quality(items, keywords)
    assert report.no_keywords_item_ids == []


def test_multiple_over_length_keywords_are_named_in_the_error() -> None:
    items = [make_item("a"), make_item("b")]
    keywords = [make_keyword("a", "x" * 61), make_keyword("b", "y" * 70)]
    with pytest.raises(DataQualityError) as exc_info:
        assert_data_quality(items, keywords)
    assert "a:" in str(exc_info.value)
    assert "b:" in str(exc_info.value)
