"""GRP-23/GRP-57: keyword normalization + alias/mute/pin application - pure, table-driven."""

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


# --- KeywordRules.is_pinned --------------------------------------------------

_PIN_CASES = [
    # (pins, canonical, expected)
    ([], "anthropic", False),
    (["anthropic"], "anthropic", True),
    (["Anthropic"], "anthropic", True),  # pin config is normalized too
    (["  DBT  "], "dbt", True),
    (["anthropic"], "dbt", False),
]


@pytest.mark.parametrize(("pins", "canonical", "expected"), _PIN_CASES)
def test_is_pinned(pins: list[str], canonical: str, expected: bool) -> None:
    rules = KeywordRules.from_config(KeywordsConfig(pin=pins))
    assert rules.is_pinned(canonical) is expected


def test_pin_config_is_normalized_at_construction() -> None:
    rules = KeywordRules.from_config(KeywordsConfig(pin=["  Anthropic  ", "DBT"]))
    assert rules.pin_set == frozenset({"anthropic", "dbt"})


def test_is_pinned_not_resolved_through_alias_map() -> None:
    # pin_set is checked verbatim, not alias-resolved (same as mute_set):
    # pinning the alias's surface form ("gen ai") does not pin the canonical
    # target ("genai") it maps to.
    rules = KeywordRules.from_config(KeywordsConfig(aliases={"gen ai": "genai"}, pin=["gen ai"]))
    assert rules.is_pinned("genai") is False
    assert rules.is_pinned("gen ai") is True  # the raw pin entry itself still matches


def test_muted_and_pinned_keyword_never_reaches_is_pinned() -> None:
    # Mute wins over pin. `apply` drops a muted keyword before any caller has a
    # canonical form left to test against `is_pinned`, the way a real caller
    # (TrendQueries._merged_counts) uses it.
    rules = KeywordRules.from_config(KeywordsConfig(mute=["anthropic"], pin=["anthropic"]))
    assert rules.apply("Anthropic") is None
    # pinned in isolation, but apply never returns it, so the pin can never
    # resurface a muted keyword
    assert rules.is_pinned("anthropic") is True


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
