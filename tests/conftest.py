"""Shared test helpers."""

from __future__ import annotations

import textwrap
from pathlib import Path

from grepify.models import Item, ItemKeyword, SourceKind

VALID_SETTINGS = textwrap.dedent(
    """
    llm:
      active_profile: gemini-free
      max_items_per_call: 25
      profiles:
        gemini-free:
          endpoint: openai-compat
          model: gemini-3.1-flash-lite
          max_calls_per_run: 40
    windows:
      cloud_days: 7
    limits:
      transcript_max_chars: 60000
      transcript_langs: [en]
    timezone: America/Edmonton
    """
).strip()

VALID_KEYWORDS = textwrap.dedent(
    """
    aliases:
      "gen ai": genai
    mute:
      - webinar
    pin:
      - anthropic
    """
).strip()


def write_config(
    root: Path,
    *,
    groups: dict[str, str] | None = None,
    settings: str = VALID_SETTINGS,
    keywords: str = VALID_KEYWORDS,
) -> Path:
    """Materialize a config tree under ``root``; returns the config root."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.yml").write_text(settings, encoding="utf-8")
    (root / "keywords.yml").write_text(keywords, encoding="utf-8")
    groups_dir = root / "groups"
    groups_dir.mkdir(exist_ok=True)
    for filename, body in (groups or {}).items():
        (groups_dir / filename).write_text(textwrap.dedent(body).strip(), encoding="utf-8")
    return root


def make_item(item_id: str, *, published_at: str = "2026-07-07T10:00:00+00:00") -> Item:
    return Item(
        item_id=item_id,
        source_id="src-1",
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://example.com/{item_id}",
        title=f"title {item_id}",
        summary="a summary",
        published_at=published_at,
        fetched_at="2026-07-07T11:00:00+00:00",
        content_hash=f"hash-{item_id}",
    )


def make_keyword(item_id: str, keyword: str, rank: int = 1) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword=keyword,
        rank=rank,
        method="llm",
        model="test-model",
        extracted_at="2026-07-07T12:00:00+00:00",
    )
