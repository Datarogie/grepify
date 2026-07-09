"""Design tokens for the v1 site (GRP-30, PRD §5 "Frontend (locked)").

The single source of truth for the site's palette, spacing, radius, and type
scale. Tokens are rendered into CSS custom properties (``:root { --… }``) by
``templates/style.css.jinja`` so there is exactly one place a colour or spacing
value is defined — templates reference ``var(--…)`` and never hard-code values.

The theme is **dark, mobile-first**, with a **system font stack only** — no
external fonts, no web-font fetch, no trackers (PRD §8 F-SIT-07).

Determinism: :data:`STYLE_TOKENS` is an ordered ``dict`` (insertion order is
part of the Python language contract), so iterating it yields the same CSS byte
sequence every build (F-SIT-08). Callers must not sort or reorder it — the
authored order *is* the intended CSS order.

Failure modes
-------------
None — this module is pure data (module-level constants). Nothing here performs
I/O or raises.
"""

from __future__ import annotations

# CSS custom properties, name (without the leading `--`) → value. Authored order
# is the emitted order; grouped by concern for readability. Colours are a dark
# palette; the accent is a desaturated cyan that stays legible on the near-black
# background and passes WCAG AA for body text.
STYLE_TOKENS: dict[str, str] = {
    # palette (dark)
    "bg": "#0d1117",
    "bg-elevated": "#161b22",
    "bg-sunken": "#010409",
    "border": "#30363d",
    "text": "#e6edf3",
    "text-muted": "#9da7b3",
    "text-faint": "#6e7681",
    "accent": "#4dd0e1",
    "accent-strong": "#22b8cf",
    "link": "#79c0ff",
    "ok": "#3fb950",
    "warn": "#d29922",
    "error": "#f85149",
    # typography — system stack only (no external fonts, F-SIT-07)
    "font-sans": (
        "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, "
        "sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji'"
    ),
    "font-mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
    "text-sm": "0.85rem",
    "text-base": "1rem",
    "text-lg": "1.25rem",
    "text-xl": "1.6rem",
    "line-height": "1.55",
    # spacing scale
    "space-1": "0.25rem",
    "space-2": "0.5rem",
    "space-3": "0.75rem",
    "space-4": "1rem",
    "space-6": "1.5rem",
    "space-8": "2rem",
    # shape
    "radius": "8px",
    "radius-sm": "4px",
    "measure": "48rem",  # readable max content width (mobile-first, capped on desktop)
}

# Keyword-cloud sizing (log-scaled, PRD §8 F-SIT-01). The smallest/largest font
# a cloud term can render at, in rem — the home page interpolates between them by
# log(count). Kept here so the size range is a design token, not a magic number
# buried in a template or query.
CLOUD_MIN_REM = 0.85
CLOUD_MAX_REM = 2.4
