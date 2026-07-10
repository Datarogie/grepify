"""Inline-SVG sparkline helper (GRP-30, PRD §5 "sparklines = inline SVG …in Python").

A pure function that turns a series of numbers (e.g. a keyword's daily mention
timeline, E4/GRP-44) into a self-contained ``<svg>`` string - no JavaScript, no
external requests, no image files. The output is embedded directly in a page and
styled via ``currentColor`` so it inherits the surrounding text colour (works in
the dark theme without a hard-coded fill).

Determinism (F-SIT-08 / S8)
---------------------------
Coordinates are rounded to a fixed number of decimals (:data:`_PRECISION`) and
emitted in input order, so the same ``values`` always yield byte-identical SVG.
Nothing here reads a clock, a locale, or any process-varying state.

Failure modes
-------------
- Empty ``values`` → an empty-but-valid ``<svg>`` (no polyline); the caller
  needs no special-case.
- A non-positive ``width``/``height``/``stroke_width`` raises ``ValueError``
  (a programming error, not runtime data).
- A flat series (all equal, incl. a single point) renders a horizontal line at
  the vertical midpoint - no divide-by-zero.
"""

from __future__ import annotations

from collections.abc import Sequence

_PRECISION = 2  # decimal places for coordinates → byte-stable output


def _fmt(value: float) -> str:
    """Fixed-precision coordinate, with a trailing ``.0`` and ``-0`` stripped."""
    text = f"{value:.{_PRECISION}f}"
    # normalise "-0.00" → "0.00" so a rounding sign flip can't change bytes
    if set(text) <= {"-", "0", "."}:
        text = text.lstrip("-")
    return text


def sparkline_svg(
    values: Sequence[float],
    *,
    width: float = 120.0,
    height: float = 24.0,
    stroke_width: float = 1.5,
    pad: float = 2.0,
) -> str:
    """Render ``values`` as an inline SVG sparkline (polyline).

    The polyline is scaled to fill ``[pad, width-pad] x [pad, height-pad]``: x is
    evenly spaced across the points, y maps ``min(values)`` to the bottom and
    ``max(values)`` to the top. ``role="img"`` + ``aria-label`` carry an
    accessible label (F-SIT-07 leans on semantic, tracker-free markup).
    """
    if width <= 0 or height <= 0 or stroke_width <= 0:
        raise ValueError("width, height and stroke_width must be positive")

    open_tag = (
        f'<svg class="sparkline" viewBox="0 0 {_fmt(width)} {_fmt(height)}" '
        f'width="{_fmt(width)}" height="{_fmt(height)}" '
        f'preserveAspectRatio="none" role="img" '
        f'aria-label="sparkline of {len(values)} points">'
    )
    if not values:
        return f"{open_tag}</svg>"

    lo = min(values)
    hi = max(values)
    span = hi - lo
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    n = len(values)

    points: list[str] = []
    for i, value in enumerate(values):
        x = pad if n == 1 else pad + inner_w * i / (n - 1)
        # invert y (SVG y grows downward); flat series → midline
        frac = 0.5 if span == 0 else (value - lo) / span
        y = pad + inner_h * (1.0 - frac)
        points.append(f"{_fmt(x)},{_fmt(y)}")

    polyline = (
        f'<polyline fill="none" stroke="currentColor" '
        f'stroke-width="{_fmt(stroke_width)}" stroke-linecap="round" '
        f'stroke-linejoin="round" points="{" ".join(points)}"/>'
    )
    return f"{open_tag}{polyline}</svg>"
