"""URL slug helper tests (GRP-43/44): digest + keyword page slugs."""

from __future__ import annotations

from grepify.site.urls import digest_slug, keyword_slug


def test_digest_slug_strips_kind_prefix() -> None:
    assert digest_slug("daily-ai-2026-07-07", "daily") == "ai-2026-07-07"
    assert digest_slug("weekly-ai-2026-W27", "weekly") == "ai-2026-W27"


def test_digest_slug_keeps_hyphenated_category() -> None:
    # only the leading `<kind>-` is stripped; a hyphen in the category survives
    assert digest_slug("daily-data-eng-2026-07-07", "daily") == "data-eng-2026-07-07"


def test_keyword_slug_is_readable_and_unique() -> None:
    slug = keyword_slug("gen ai")
    assert slug.startswith("gen-ai-")
    # deterministic
    assert keyword_slug("gen ai") == slug


def test_keyword_slug_disambiguates_colliding_symbol_keywords() -> None:
    # "c++" and "c#" both slugify to base "c" but must not collide (hash suffix)
    assert keyword_slug("c++") != keyword_slug("c#")
    assert keyword_slug("c++").startswith("c-")


def test_keyword_slug_url_safe() -> None:
    slug = keyword_slug("GPT-5 / o3!!")
    assert all(ch.isalnum() or ch == "-" for ch in slug)


def test_keyword_slug_empty_base_falls_back() -> None:
    slug = keyword_slug("+++")
    assert slug.startswith("kw-")
