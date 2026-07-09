"""GRP-21: strict-JSON response validation (F-EXT-02).

Exercises the validator directly for the full echo/sanity matrix; the
retry/fallback/budget *behavior* is covered in ``test_extract_batcher.py``.
"""

from __future__ import annotations

import json

import pytest

from grepify.extract.batcher import _MalformedResponseError, _strip_code_fences, _validate_response


def _text(mapping: dict[str, list[str]]) -> str:
    return json.dumps([{"item_id": k, "keywords": v} for k, v in mapping.items()])


def test_valid_response_returns_mapping() -> None:
    text = _text({"a": ["genai", "anthropic"], "b": ["dbt"]})
    result = _validate_response(text, ["a", "b"], max_keywords=8)
    assert result == {"a": ["genai", "anthropic"], "b": ["dbt"]}


def test_keywords_are_trimmed() -> None:
    text = _text({"a": ["  genai  "]})
    assert _validate_response(text, ["a"], max_keywords=8) == {"a": ["genai"]}


def test_empty_keyword_list_is_valid() -> None:
    text = _text({"a": []})
    assert _validate_response(text, ["a"], max_keywords=8) == {"a": []}


def test_more_than_max_keywords_is_truncated_not_rejected() -> None:
    text = _text({"a": [f"kw{i}" for i in range(12)]})
    result = _validate_response(text, ["a"], max_keywords=8)
    assert result["a"] == [f"kw{i}" for i in range(8)]


def test_code_fenced_array_is_tolerated() -> None:
    text = "```json\n" + _text({"a": ["genai"]}) + "\n```"
    assert _validate_response(text, ["a"], max_keywords=8) == {"a": ["genai"]}


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "{}",  # top-level not an array
        '[{"item_id": "a"}]',  # missing keywords
        '[{"keywords": ["x"]}]',  # missing item_id
        '[{"item_id": "a", "keywords": "genai"}]',  # keywords not a list
        '[{"item_id": 1, "keywords": ["x"]}]',  # item_id not a string
        '[{"item_id": "a", "keywords": [123]}]',  # keyword not a string
        '[["a", ["x"]]]',  # entry not an object
    ],
)
def test_structurally_malformed_responses_are_rejected(text: str) -> None:
    with pytest.raises(_MalformedResponseError):
        _validate_response(text, ["a"], max_keywords=8)


def test_unknown_item_id_is_rejected() -> None:
    text = _text({"a": ["xx"], "zzz": ["yy"]})
    with pytest.raises(_MalformedResponseError, match="unknown item_id"):
        _validate_response(text, ["a"], max_keywords=8)


def test_omitted_item_id_is_rejected() -> None:
    text = _text({"a": ["xx"]})
    with pytest.raises(_MalformedResponseError, match="omitted"):
        _validate_response(text, ["a", "b"], max_keywords=8)


def test_duplicate_item_id_is_rejected() -> None:
    text = '[{"item_id": "a", "keywords": ["xx"]}, {"item_id": "a", "keywords": ["yy"]}]'
    with pytest.raises(_MalformedResponseError, match="duplicate"):
        _validate_response(text, ["a"], max_keywords=8)


@pytest.mark.parametrize("keyword", ["a", "x" * 61])
def test_keyword_length_sanity(keyword: str) -> None:
    text = _text({"a": [keyword]})
    with pytest.raises(_MalformedResponseError, match="length sanity"):
        _validate_response(text, ["a"], max_keywords=8)


@pytest.mark.parametrize("keyword", ["https://evil.example", "http://x.io", "www.example.com"])
def test_url_like_keywords_are_rejected(keyword: str) -> None:
    text = _text({"a": [keyword]})
    with pytest.raises(_MalformedResponseError, match="url"):
        _validate_response(text, ["a"], max_keywords=8)


def test_strip_code_fences_passthrough_when_not_fenced() -> None:
    assert _strip_code_fences('[{"item_id": "a"}]') == '[{"item_id": "a"}]'
