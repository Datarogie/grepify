"""SSG skeleton tests (GRP-30): golden snapshots, determinism, sparkline, tokens.

Snapshots (F-SIT-08 / S8): the base layout and the tokenised stylesheet are
rendered with fixed inputs and compared against committed goldens under
``tests/fixtures/site/`` - any rendering change is an explicit snapshot update
in the diff. Determinism is asserted by rendering twice in a row and requiring
byte-identical output (the "passes twice in CI" rule).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grepify.site import (
    PageContext,
    SiteMeta,
    cloud_weight_bucket,
    create_environment,
    render_page,
    render_stylesheet,
    sparkline_svg,
)
from grepify.site.tokens import LIGHT_STYLE_TOKENS, STYLE_TOKENS

GOLDEN = Path(__file__).parent / "fixtures" / "site"


def _meta() -> SiteMeta:
    return SiteMeta(
        title="grepify",
        base_path="/grepify/",
        generated_at="2026-07-09T12:00:00+00:00",
        run_id="20260709T120000Z-abc123",
    )


# --- golden snapshots + determinism -----------------------------------------


def test_base_page_matches_golden() -> None:
    env = create_environment()
    html = render_page(env, "base.html", PageContext(meta=_meta(), active="home"))
    assert html == (GOLDEN / "base.html").read_text(encoding="utf-8")


def test_stylesheet_matches_golden() -> None:
    env = create_environment()
    css = render_stylesheet(env)
    assert css == (GOLDEN / "style.css").read_text(encoding="utf-8")


def test_hidden_attribute_is_authoritative() -> None:
    # The HTML `hidden` attribute must beat component `display:` rules. Several
    # components set `display: flex` on their base class (the tablist, .filters,
    # .topic-follow, .topic-chips), so without a global
    # `[hidden] { display: none !important }` an element stays visible while its
    # `hidden` attribute is set.
    css = render_stylesheet(create_environment())
    assert "[hidden] { display: none !important; }" in css


def test_render_is_deterministic_twice_in_a_row() -> None:
    env = create_environment()
    ctx = PageContext(meta=_meta(), active="items")
    first = render_page(env, "base.html", ctx)
    second = render_page(env, "base.html", ctx)
    assert first == second
    assert render_stylesheet(env) == render_stylesheet(env)


def test_active_nav_marks_current_page() -> None:
    env = create_environment()
    html = render_page(env, "base.html", PageContext(meta=_meta(), active="sources"))
    assert html.count('aria-current="page"') == 1
    assert '/grepify/sources/" aria-current="page"' in html


def test_base_path_prefixes_every_internal_link() -> None:
    env = create_environment()
    html = render_page(env, "base.html", PageContext(meta=_meta(), active="home"))
    assert '<link rel="stylesheet" href="/grepify/static/style.css">' in html
    assert '<script src="/grepify/static/theme.js"></script>' in html
    brand = '<a class="brand" href="/grepify/">grepify<span class="caret"'
    assert brand in html


def test_asset_url_falls_back_to_bare_when_unversioned() -> None:
    # A SiteMeta with no registered versions (the pure-render path) emits the
    # plain base-path URL, so render-only snapshots stay unversioned.
    meta = _meta()
    assert meta.asset("digests.js") == "/grepify/static/digests.js"


def test_asset_url_appends_version_when_known() -> None:
    meta = SiteMeta(
        title="grepify",
        base_path="/grepify/",
        generated_at="2026-07-09T12:00:00+00:00",
        run_id="20260709T120000Z-abc123",
        asset_versions={"digests.js": "deadbeef"},
    )
    assert meta.asset("digests.js") == "/grepify/static/digests.js?v=deadbeef"
    # an asset with no registered version still degrades to the bare URL
    assert meta.asset("style.css") == "/grepify/static/style.css"


def test_no_external_fonts_or_trackers() -> None:
    # No third-party requests. The one display face (League Gothic) is embedded
    # as an inline data URI, never fetched.
    css = render_stylesheet(create_environment())
    # scheme-prefixed URLs only: the base64 font payload may legitimately
    # contain the bare substring "http"
    assert "fonts.googleapis" not in css and "@import" not in css
    assert "http://" not in css and "https://" not in css
    assert 'src: url("data:font/woff2;base64,' in css
    assert "ui-monospace" in css  # body/mono faces stay on system stacks


# --- style tokens ------------------------------------------------------------


def test_tokens_render_as_css_custom_properties() -> None:
    css = render_stylesheet(create_environment())
    for name, value in STYLE_TOKENS.items():
        assert f"  --{name}: {value};" in css


def test_light_theme_is_a_token_level_block() -> None:
    # the light theme re-grounds the same token names inside a data-theme
    # block - components never know which theme is active
    css = render_stylesheet(create_environment())
    assert ':root[data-theme="light"]' in css
    for name, value in LIGHT_STYLE_TOKENS.items():
        assert name in STYLE_TOKENS  # same names, re-grounded values only
        assert f"  --{name}: {value};" in css


# --- sparkline ---------------------------------------------------------------


def test_sparkline_is_deterministic() -> None:
    values = [1.0, 3.0, 2.0, 5.0, 4.0]
    assert sparkline_svg(values) == sparkline_svg(values)


def test_sparkline_empty_is_valid_svg() -> None:
    svg = sparkline_svg([])
    assert svg.startswith("<svg") and svg.endswith("</svg>") and "polyline" not in svg


def test_sparkline_single_point_no_divide_by_zero() -> None:
    svg = sparkline_svg([7.0])
    assert "polyline" in svg
    # single point sits at the left pad, vertical midline
    assert 'points="2.00,12.00"' in svg


def test_sparkline_flat_series_is_midline() -> None:
    svg = sparkline_svg([4.0, 4.0, 4.0], width=100.0, height=20.0)
    # all y at the vertical midpoint (height/2)
    assert svg.count(",10.00") == 3


def test_sparkline_scales_min_to_bottom_max_to_top() -> None:
    svg = sparkline_svg([0.0, 10.0], width=100.0, height=20.0, pad=2.0)
    # min → bottom (y = height-pad = 18), max → top (y = pad = 2)
    assert "2.00,18.00" in svg
    assert "98.00,2.00" in svg


def test_sparkline_rejects_nonpositive_dimensions() -> None:
    with pytest.raises(ValueError, match="positive"):
        sparkline_svg([1.0, 2.0], width=0.0)


# --- cloud weight buckets ----------------------------------------------------


def test_cloud_weight_bucket_monotonic_and_bounded() -> None:
    small = cloud_weight_bucket(1, min_count=1, max_count=100)
    big = cloud_weight_bucket(100, min_count=1, max_count=100)
    mid = cloud_weight_bucket(10, min_count=1, max_count=100)
    assert small <= mid <= big
    assert small == 1  # min count → lightest bucket (w1)
    assert big == 5  # max count → heaviest bucket (w5, CLOUD_BUCKETS)


def test_cloud_weight_bucket_single_count_is_middle() -> None:
    # every term the same count → middle bucket, no divide-by-zero
    assert cloud_weight_bucket(5, min_count=5, max_count=5) == 3


def test_cloud_weight_bucket_covers_every_bucket() -> None:
    buckets = {cloud_weight_bucket(n, min_count=1, max_count=100) for n in range(1, 101)}
    assert buckets == {1, 2, 3, 4, 5}
