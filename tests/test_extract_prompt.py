"""GRP-21: prompt v1 assembly."""

from __future__ import annotations

import json

from grepify.extract import PROMPT_VERSION, build_messages
from grepify.extract.prompt import SUMMARY_CHAR_CAP
from grepify.models import Item, SourceKind


def _item(item_id: str, *, summary: str | None = "s") -> Item:
    return Item(
        item_id=item_id,
        source_id="src",
        kind=SourceKind.RSS,
        canonical_url=f"https://example.com/{item_id}",
        title=f"Title {item_id}",
        summary=summary,
        published_at="2026-07-08T09:00:00+00:00",
        fetched_at="2026-07-08T10:00:00+00:00",
        content_hash="h",
    )


def test_prompt_version_is_stable() -> None:
    assert PROMPT_VERSION == "extract-v1"


def test_system_and_user_messages_built() -> None:
    system, user = build_messages([_item("a"), _item("b")], max_keywords=8)
    assert system.role == "system"
    assert "8 keywords" in system.content
    assert "JSON array" in system.content

    assert user.role == "user"
    payload = json.loads(user.content)
    assert payload == [
        {"item_id": "a", "title": "Title a", "summary": "s"},
        {"item_id": "b", "title": "Title b", "summary": "s"},
    ]


def test_summary_is_capped_and_none_becomes_empty() -> None:
    long_summary = "x" * (SUMMARY_CHAR_CAP + 500)
    _system, user = build_messages(
        [_item("a", summary=long_summary), _item("b", summary=None)], max_keywords=8
    )
    payload = json.loads(user.content)
    assert len(payload[0]["summary"]) == SUMMARY_CHAR_CAP
    assert payload[1]["summary"] == ""
