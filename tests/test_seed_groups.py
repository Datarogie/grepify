"""GRP-07: guard the committed seed group files (sources/groups/*.yml).

These assert the *real* repo config - not a fixture - so a future edit that
breaks the schema, duplicates an id/url_hash, drops a PRD-mandated seed, or
sneaks in a forbidden category fails the gate loudly (PRD §2 no-crypto, §7
group/category schema, §14 seeds).

Failure modes
-------------
If ``sources/`` is moved or a group file is malformed, these fail with the
provider's aggregated errors - the same signal ``grepify validate`` gives in CI.
"""

from __future__ import annotations

from pathlib import Path

from grepify.config.filesystem import FilesystemConfigProvider
from grepify.models import SourceKind, SourceStatus

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOURCES = _REPO_ROOT / "sources"

# The five trendcloud-derived AI groups, the data-engineering group (PRD §7),
# and the long-form ai-voices group.
_EXPECTED_GROUPS = {
    "ai-research",
    "ai-business",
    "ai-tooling-dev",
    "youtube-ai",
    "reddit-ai",
    "data-engineering",
    "ai-voices",
}
_TRENDCLOUD_GROUPS = _EXPECTED_GROUPS - {"data-engineering", "ai-voices"}
# Only these two categories launch (PRD §14); crypto is excluded entirely (PRD §2).
_ALLOWED_CATEGORIES = {"ai", "data-eng"}


def _provider() -> FilesystemConfigProvider:
    return FilesystemConfigProvider(_SOURCES)


def test_committed_config_validates() -> None:
    report = _provider().validate()
    assert report.ok, report.errors
    assert report.group_count == len(_EXPECTED_GROUPS)


def test_expected_groups_present() -> None:
    groups = {g.group_id for g in _provider().groups()}
    assert groups == _EXPECTED_GROUPS


def test_trendcloud_scrape_has_all_118_sources() -> None:
    # The trendcloud.io /sources AI list was 118 sources (83 rss, 9 youtube, 26
    # reddit); clarifai-blog was removed as `gone` (#66, ADR 0002: HTTP 404
    # 16/16 is a moved/dead URL, not a WAF block), leaving 117 (82 rss).
    sources = [s for s in _provider().sources() if s.group_id in _TRENDCLOUD_GROUPS]
    assert len(sources) == 117, len(sources)
    by_kind = {k: 0 for k in SourceKind}
    for s in sources:
        by_kind[s.kind] += 1
    assert by_kind[SourceKind.RSS] == 82
    assert by_kind[SourceKind.YOUTUBE] == 9
    assert by_kind[SourceKind.REDDIT] == 26


def test_no_forbidden_category() -> None:
    for group in _provider().groups():
        assert group.category in _ALLOWED_CATEGORIES, group.category
        # Belt-and-suspenders on the PRD §2 hard rule.
        assert "crypto" not in group.category


def test_data_engineering_category() -> None:
    by_id = {g.group_id: g for g in _provider().groups()}
    assert by_id["data-engineering"].category == "data-eng"


def test_ai_voices_group_is_rss_ai_category() -> None:
    # ai-voices feeds are the long-form RSS/Atom feeds of AI figures (no auth,
    # richer than tweets).
    by_id = {g.group_id: g for g in _provider().groups()}
    voices = by_id["ai-voices"]
    assert voices.category == "ai"
    sources = [s for s in _provider().sources() if s.group_id == "ai-voices"]
    assert sources, "expected ai-voices feeds"
    assert all(s.kind is SourceKind.RSS for s in sources)
    assert {s.source_id for s in sources} >= {"simon-willison", "latent-space"}


def test_prd_mandated_seeds_present() -> None:
    by_id = {s.source_id: s for s in _provider().sources()}
    # PRD §14 data-eng seeds given verbatim.
    assert by_id["benn-substack"].url == "https://benn.substack.com/feed"
    assert by_id["getdbt-roundup"].url == "https://roundup.getdbt.com/feed"
    # PRD §14 Q3 reddit seeds (LocalLLaMA/MachineLearning scraped into reddit-ai;
    # dataengineering is Kyle's data-eng seed).
    for sid in ("r-localllama", "r-machinelearning", "r-dataengineering"):
        assert by_id[sid].kind is SourceKind.REDDIT


def test_dead_and_reprobe_sources_present_not_omitted() -> None:
    # ADR 0002/#119: runner-blocked-but-live sources stay present as degraded;
    # the two never-fetched data-eng seeds are
    # provisional-active re-probes (enabled) so the pipeline ladder resolves
    # their real feed instead of leaving them dark.
    by_id = {s.source_id: s for s in _provider().sources()}
    assert by_id["benn-substack"].status is SourceStatus.DEGRADED
    assert by_id["benn-substack"].enabled is True
    assert by_id["benn-substack"].evidence is not None
    assert by_id["benn-substack"].active_url is None
    assert by_id["dbt-developer-blog"].enabled is True
    assert by_id["snowflake-engineering"].enabled is True


def test_youtube_sources_have_channel_id_locator() -> None:
    yt = [s for s in _provider().sources() if s.kind is SourceKind.YOUTUBE]
    assert yt, "expected youtube seeds"
    # canonical url resolves to the keyless channel-RSS endpoint (PRD F-ING-02).
    assert all("channel_id=" in s.url for s in yt)


def test_every_source_has_unique_url_hash() -> None:
    sources = _provider().sources()
    hashes = [s.url_hash for s in sources]
    assert len(hashes) == len(set(hashes))
