"""GRP-23: keyword normalization + alias/mute application — pure, table-driven."""

from __future__ import annotations

import pytest

from grepify.config.schemas import KeywordsConfig
from grepify.keywords import KeywordRules, apply_to_keyword, normalize_keyword
from grepify.models import ItemKeyword

# --- normalize_keyword: lowercase, trim, collapse whitespace, strip trailing punct ---

_NORMALIZE_CASES = [
    ("genai", "genai"),
    ("GenAI", "genai"),
    ("  Gen AI  ", "gen ai"),
    ("gen   ai", "gen ai"),
    ("AI.", "ai"),
    ("AI!!", "ai"),
    ("AI?", "ai"),
    ('"quoted"', '"quoted'),  # only trailing punctuation is stripped
    ("dbt,", "dbt"),
    ("already normal", "already normal"),
    ("", ""),
    ("   ", ""),
]


@pytest.mark.parametrize(("raw", "expected"), _NORMALIZE_CASES)
def test_normalize_keyword(raw: str, expected: str) -> None:
    assert normalize_keyword(raw) == expected


# --- KeywordRules.apply: normalize -> alias -> mute ----------------------------

_APPLY_CASES = [
    # (aliases, mutes, raw, expected)
    ({}, [], "genai", "genai"),
    ({"gen ai": "genai"}, [], "Gen AI", "genai"),
    ({"gen ai": "genai"}, [], "  gen   ai  ", "genai"),
    ({"gen ai": "genai"}, [], "unrelated", "unrelated"),
    ({}, ["webinar"], "Webinar", None),
    ({}, ["webinar"], "webinars", "webinars"),  # exact match only, no stemming
    ({"gen ai": "genai"}, ["genai"], "gen ai", None),  # mute applies after alias
    ({"gen ai": "genai"}, ["gen ai"], "genai", "genai"),  # mute on the alias, not the target
    ({}, [], "  Sponsored!  ", "sponsored"),
]


@pytest.mark.parametrize(("aliases", "mutes", "raw", "expected"), _APPLY_CASES)
def test_keyword_rules_apply(
    aliases: dict[str, str], mutes: list[str], raw: str, expected: str | None
) -> None:
    rules = KeywordRules.from_config(KeywordsConfig(aliases=aliases, mute=mutes))
    assert rules.apply(raw) == expected


def test_alias_and_mute_config_are_normalized_at_construction() -> None:
    rules = KeywordRules.from_config(
        KeywordsConfig(aliases={"  GEN AI  ": "GenAI"}, mute=["WEBINAR"])
    )
    assert rules.alias_map == {"gen ai": "genai"}
    assert rules.mute_set == frozenset({"webinar"})


# --- apply_to_keyword: applies rules to a stored ItemKeyword row --------------


def _row(keyword: str) -> ItemKeyword:
    return ItemKeyword(
        item_id="item-1",
        keyword=keyword,
        rank=1,
        method="llm",
        model="test-model",
        extracted_at="2026-07-08T12:00:00+00:00",
    )


def test_apply_to_keyword_replaces_keyword_field() -> None:
    rules = KeywordRules.from_config(KeywordsConfig(aliases={"gen ai": "genai"}, mute=[]))
    row = _row("Gen AI")
    result = apply_to_keyword(row, rules)
    assert result is not None
    assert result.keyword == "genai"
    assert result.item_id == row.item_id
    assert result.rank == row.rank
    assert result.method == row.method
    assert result.model == row.model
    assert result.extracted_at == row.extracted_at


def test_apply_to_keyword_returns_none_when_muted() -> None:
    rules = KeywordRules.from_config(KeywordsConfig(aliases={}, mute=["webinar"]))
    assert apply_to_keyword(_row("Webinar"), rules) is None


def test_apply_to_keyword_does_not_mutate_original_row() -> None:
    rules = KeywordRules.from_config(KeywordsConfig(aliases={"gen ai": "genai"}, mute=[]))
    row = _row("Gen AI")
    apply_to_keyword(row, rules)
    assert row.keyword == "Gen AI"
