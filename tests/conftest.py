"""Shared test helpers."""

from __future__ import annotations

import json
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from grepify.ingest.http import HttpResponse
from grepify.models import Item, ItemKeyword, Source, SourceKind

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_bytes(*parts: str) -> bytes:
    """Read raw bytes from ``tests/fixtures/<parts...>``."""
    return (FIXTURES_DIR.joinpath(*parts)).read_bytes()


def fixture_response(
    *parts: str, status: int = 200, headers: dict[str, str] | None = None
) -> HttpResponse:
    """Build an :class:`~grepify.ingest.http.HttpResponse` from a fixture file,
    for scripting a :class:`ScriptedTransport` in fetcher tests."""
    return HttpResponse(
        status_code=status, content=fixture_bytes(*parts), headers=dict(headers or {})
    )


class ScriptedTransport:
    """Test double for :class:`~grepify.ingest.http.Transport`.

    Returns pre-scripted responses/exceptions in call order, one per
    :meth:`get` call - the way fetcher unit tests drive fetchers with recorded
    fixtures and no network (PRD §9/§10.2). Popping past the script is a test
    bug (``IndexError``), not a fetcher concern.
    """

    def __init__(self, script: list[HttpResponse | Exception]) -> None:
        self.script: list[HttpResponse | Exception] = list(script)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
        self.calls.append((url, dict(headers)))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


def make_item(
    item_id: str,
    *,
    published_at: str = "2026-07-07T10:00:00+00:00",
    content_hash: str | None = None,
) -> Item:
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
        content_hash=content_hash if content_hash is not None else f"hash-{item_id}",
    )


def make_source(
    source_id: str, *, kind: SourceKind = SourceKind.RSS, url: str | None = None
) -> Source:
    return Source(
        source_id=source_id,
        name=source_id.upper(),
        kind=kind,
        url=url if url is not None else f"https://example.com/{source_id}/feed",
        url_hash=f"urlhash-{source_id}",
        group_id="g1",
        added_at="2026-07-07T00:00:00+00:00",
    )


def openai_envelope(
    text: str, *, prompt_tokens: int | None = 11, completion_tokens: int | None = 7
) -> bytes:
    """Wrap ``text`` as an OpenAI-compatible chat-completions response body."""
    usage: dict[str, int] = {}
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage["completion_tokens"] = completion_tokens
    payload: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage:
        payload["usage"] = usage
    return json.dumps(payload).encode("utf-8")


def envelope_response(text: str, **usage: int | None) -> HttpResponse:
    """A 200 :class:`HttpResponse` carrying an OpenAI-compat body for ``text``."""
    return HttpResponse(status_code=200, content=openai_envelope(text, **usage), headers={})


class ScriptedCompletionTransport:
    """Test double for :class:`~grepify.llm.transport.CompletionTransport`.

    Returns pre-scripted responses/exceptions in call order (one per
    ``post_json`` call) and records each request as ``(url, headers, payload)``
    on ``.posts`` — so a test can assert that a budget-refused call sent nothing.
    Popping past the script is a test bug (``IndexError``), not client behavior.
    """

    def __init__(self, script: list[HttpResponse | Exception]) -> None:
        self.script: list[HttpResponse | Exception] = list(script)
        self.posts: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> HttpResponse:
        self.posts.append((url, dict(headers), payload))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeFallbackExtractor:
    """Deterministic stand-in for the GRP-22 YAKE fallback extractor.

    Returns ``canned`` keywords for the item_ids it knows, records the batches
    it was asked to handle on ``.calls``, and (by default) yields a single
    per-item keyword so a fallback batch always produces rows.
    """

    def __init__(self, canned: dict[str, list[str]] | None = None) -> None:
        self.canned = canned
        self.calls: list[list[str]] = []

    def extract(self, items: Sequence[Item]) -> dict[str, list[str]]:
        self.calls.append([item.item_id for item in items])
        if self.canned is not None:
            return {item.item_id: self.canned.get(item.item_id, []) for item in items}
        return {item.item_id: [f"fallback-{item.item_id}"] for item in items}


def make_keyword(item_id: str, keyword: str, rank: int = 1) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword=keyword,
        rank=rank,
        method="llm",
        model="test-model",
        extracted_at="2026-07-07T12:00:00+00:00",
    )
