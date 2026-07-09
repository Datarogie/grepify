"""SSG skeleton tests (GRP-30): golden snapshots, determinism, sparkline, tokens.

Snapshots (F-SIT-08 / S8): the base layout and the tokenised stylesheet are
rendered with fixed inputs and compared against committed goldens under
``tests/fixtures/site/`` — any rendering change is an explicit snapshot update
in the diff. Determinism is asserted by rendering twice in a row and requiring
byte-identical output (the "passes twice in CI" rule).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grepify.site import (
    PageContext,
    SiteMeta,
    cloud_font_rem,
    create_environment,
    render_page,
    render_stylesheet,
    sparkline_svg,
)
from grepify.site.tokens import STYLE_TOKENS

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
    # exactly one nav link is aria-current, and it's Sources
    assert html.count('aria-current="page"') == 1
    assert '/grepify/sources/" aria-current="page"' in html


def test_base_path_prefixes_every_internal_link() -> None:
    env = create_environment()
    html = render_page(env, "base.html", PageContext(meta=_meta(), active="home"))
    assert '<link rel="stylesheet" href="/grepify/static/style.css">' in html
    assert '<a class="brand" href="/grepify/">grepify</a>' in html


def test_no_external_fonts_or_trackers() -> None:
    # F-SIT-07: system font stack only, no third-party requests.
    css = render_stylesheet(create_environment())
    assert "fonts.googleapis" not in css and "@import" not in css and "http" not in css
    assert "-apple-system" in css


# --- style tokens ------------------------------------------------------------


def test_tokens_render_as_css_custom_properties() -> None:
    css = render_stylesheet(create_environment())
    for name, value in STYLE_TOKENS.items():
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
    assert "2.00,18.00" in svg  # first point (min) at bottom
    assert "98.00,2.00" in svg  # last point (max) at top


def test_sparkline_rejects_nonpositive_dimensions() -> None:
    with pytest.raises(ValueError, match="positive"):
        sparkline_svg([1.0, 2.0], width=0.0)


# --- cloud font sizing -------------------------------------------------------


def test_cloud_font_rem_monotonic_and_bounded() -> None:
    small = cloud_font_rem(1, min_count=1, max_count=100)
    big = cloud_font_rem(100, min_count=1, max_count=100)
    mid = cloud_font_rem(10, min_count=1, max_count=100)
    assert small < mid < big
    assert small == pytest.approx(0.85, abs=0.01)  # CLOUD_MIN_REM
    assert big == pytest.approx(2.4, abs=0.01)  # CLOUD_MAX_REM


def test_cloud_font_rem_single_count_is_midpoint() -> None:
    # every term the same count → midpoint size, no divide-by-zero
    assert cloud_font_rem(5, min_count=5, max_count=5) == pytest.approx(1.625, abs=0.001)
