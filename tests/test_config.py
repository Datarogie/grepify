"""Config layer tests (GRP-04): schema validation + cross-file checks."""

from __future__ import annotations

import textwrap
from pathlib import Path

from grepify.config.filesystem import FilesystemConfigProvider
from grepify.models import SourceKind
from tests.conftest import write_config

_GROUP_OK = """
    group: ai-research
    name: AI Research
    category: ai
    enabled: true
    sources:
      - id: ahead-of-ai
        kind: rss
        url: https://magazine.sebastianraschka.com/feed
      - id: yt-two-minute
        kind: youtube
        channel_id: UCabc123
      - id: r-localllama
        kind: reddit
        subreddit: LocalLLaMA
      - id: x-karpathy
        kind: x
        handle: karpathy
"""


def _provider(tmp_path: Path, groups: dict[str, str]) -> FilesystemConfigProvider:
    root = write_config(tmp_path / "sources", groups=groups)
    return FilesystemConfigProvider(root)


def test_valid_config_passes(tmp_path: Path) -> None:
    provider = _provider(tmp_path, {"ai-research.yml": _GROUP_OK})
    report = provider.validate()
    assert report.ok, report.errors
    assert report.group_count == 1
    assert report.source_count == 4


def test_sources_resolve_canonical_urls(tmp_path: Path) -> None:
    provider = _provider(tmp_path, {"ai-research.yml": _GROUP_OK})
    by_id = {s.source_id: s for s in provider.sources()}

    assert by_id["yt-two-minute"].kind is SourceKind.YOUTUBE
    assert "channel_id=UCabc123" in by_id["yt-two-minute"].url
    assert by_id["r-localllama"].url == "https://www.reddit.com/r/LocalLLaMA/new.json"
    assert by_id["x-karpathy"].url == "https://x.com/karpathy"
    # url_hash is stable + populated for every source
    assert all(s.url_hash for s in by_id.values())


def test_duplicate_source_id_is_rejected(tmp_path: Path) -> None:
    dupe = """
        group: g2
        name: G2
        category: ai
        sources:
          - id: ahead-of-ai
            kind: rss
            url: https://other.example.com/feed
    """
    provider = _provider(tmp_path, {"ai-research.yml": _GROUP_OK, "g2.yml": dupe})
    report = provider.validate()
    assert not report.ok
    assert any("duplicate source id" in e for e in report.errors)


def test_duplicate_feed_url_is_rejected(tmp_path: Path) -> None:
    same_feed = """
        group: g2
        name: G2
        category: ai
        sources:
          - id: ahead-of-ai-mirror
            kind: rss
            url: https://magazine.sebastianraschka.com/feed
    """
    provider = _provider(tmp_path, {"ai-research.yml": _GROUP_OK, "g2.yml": same_feed})
    report = provider.validate()
    assert not report.ok
    assert any("duplicate feed" in e for e in report.errors)


def test_bad_kind_is_rejected(tmp_path: Path) -> None:
    bad = """
        group: g
        name: G
        category: ai
        sources:
          - id: x1
            kind: mastodon
            url: https://example.com/feed
    """
    report = _provider(tmp_path, {"g.yml": bad}).validate()
    assert not report.ok
    assert any("g.yml" in e for e in report.errors)


def test_missing_category_is_rejected(tmp_path: Path) -> None:
    bad = """
        group: g
        name: G
        sources: []
    """
    report = _provider(tmp_path, {"g.yml": bad}).validate()
    assert not report.ok
    assert any("category" in e.lower() for e in report.errors)


def test_wrong_locator_for_kind_is_rejected(tmp_path: Path) -> None:
    bad = """
        group: g
        name: G
        category: ai
        sources:
          - id: rss-without-url
            kind: rss
            handle: oops
    """
    report = _provider(tmp_path, {"g.yml": bad}).validate()
    assert not report.ok


def test_active_profile_must_exist(tmp_path: Path) -> None:
    settings = """
        llm:
          active_profile: nonexistent
          profiles:
            gemini-free:
              endpoint: openai-compat
              model: m
    """
    root = write_config(tmp_path / "sources", settings=textwrap.dedent(settings).strip())
    report = FilesystemConfigProvider(root).validate()
    assert not report.ok
    assert any("active_profile" in e for e in report.errors)


def test_empty_groups_dir_is_valid_with_warning(tmp_path: Path) -> None:
    report = _provider(tmp_path, {}).validate()
    assert report.ok
    assert report.warnings


def test_keywords_and_settings_parse(tmp_path: Path) -> None:
    provider = _provider(tmp_path, {"ai-research.yml": _GROUP_OK})
    assert provider.keywords().aliases == {"gen ai": "genai"}
    assert provider.settings().llm.active_profile == "gemini-free"
    assert provider.settings().timezone == "America/Edmonton"


# --- kind-coverage check ------------------------------------------------------
#
# `kind: x` passes schema validation on its own (schemas.py has a locator rule
# for it) even though the production registry registers no fetcher for it -
# that gap is exactly what these tests guard. Without `registered_kinds`,
# `validate()` does not look at fetcher coverage at all (test_valid_config_passes,
# above, relies on that: `_GROUP_OK` includes an `x` source and still passes).

_RSS_ONLY = frozenset({SourceKind.RSS, SourceKind.YOUTUBE, SourceKind.REDDIT})


def test_enabled_source_with_unregistered_kind_is_rejected(tmp_path: Path) -> None:
    report = _provider(tmp_path, {"ai-research.yml": _GROUP_OK}).validate(
        registered_kinds=_RSS_ONLY
    )
    assert not report.ok
    matches = [e for e in report.errors if "x-karpathy" in e]
    assert len(matches) == 1
    assert "kind 'x'" in matches[0]
    assert "no registered fetcher" in matches[0]


def test_source_with_registered_kind_is_not_rejected(tmp_path: Path) -> None:
    all_kinds = frozenset(SourceKind)
    report = _provider(tmp_path, {"ai-research.yml": _GROUP_OK}).validate(
        registered_kinds=all_kinds
    )
    assert report.ok, report.errors


def test_disabled_source_with_unregistered_kind_is_not_rejected(tmp_path: Path) -> None:
    group = """
        group: g
        name: G
        category: ai
        sources:
          - id: x-off
            kind: x
            handle: someone
            enabled: false
    """
    report = _provider(tmp_path, {"g.yml": group}).validate(registered_kinds=_RSS_ONLY)
    assert report.ok, report.errors


def test_unregistered_kind_in_disabled_group_is_not_rejected(tmp_path: Path) -> None:
    group = """
        group: g
        name: G
        category: ai
        enabled: false
        sources:
          - id: x-off
            kind: x
            handle: someone
    """
    report = _provider(tmp_path, {"g.yml": group}).validate(registered_kinds=_RSS_ONLY)
    assert report.ok, report.errors
