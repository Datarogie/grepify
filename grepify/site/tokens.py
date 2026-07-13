"""Design tokens for the site identity ("the morning wire", docs/design/identity-mock.html).

The single source of truth for the site's palette, spacing, radius, and type
scale. Tokens are rendered into CSS custom properties (``:root { --… }``) by
``templates/style.css.jinja`` so there is exactly one place a colour or spacing
value is defined - templates reference ``var(--…)`` and never hard-code values.

The identity is terminal-leaning-editorial: a wire-service masthead (League
Gothic, embedded in the stylesheet as an inline ``@font-face`` data URI - no
network fetch, PRD §8 F-SIT-07) over a serif reading face and a mono utility
face. The identity device is grep's match highlight: an amber wash marking
what's rising.

Themes: **dark is the default on first load**; :data:`LIGHT_STYLE_TOKENS`
re-grounds the same token names inside a ``:root[data-theme="light"]`` block,
so components never know which theme is active (the viewer toggles the
``data-theme`` attribute, ``static/theme.js``).

Contrast: every text-grade token was verified WCAG AA (>= 4.5:1) against its
ground in both themes.

Determinism: :data:`STYLE_TOKENS` and :data:`LIGHT_STYLE_TOKENS` are ordered
``dict``s (insertion order is part of the Python language contract), so
iterating them yields the same CSS byte sequence every build (F-SIT-08).
Callers must not sort or reorder them - the authored order *is* the intended
CSS order.

Failure modes
-------------
None - this module is pure data (module-level constants). Nothing here performs
I/O or raises.
"""

from __future__ import annotations

# Authored order is the emitted order (F-SIT-08); callers must not reorder. Dark
# is the default theme; name (without the leading `--`) maps to value.
STYLE_TOKENS: dict[str, str] = {
    # ground (dark = default)
    "bg": "#131519",
    "bg-elevated": "#1b1e23",
    "bg-sunken": "#0d0f12",
    "border": "#2a2e35",
    "border-strong": "#3a3f47",
    # text
    "text": "#e7e6e2",
    "text-muted": "#a9aca6",
    "text-faint": "#7c8078",
    # identity - grep's match highlight
    "match": "#f5a83c",  # text-grade amber: links, rising, active states
    "match-ink": "#131519",  # text on a match block
    "match-wash": "rgba(245, 168, 60, 0.13)",
    "mark": "#cf7f14",  # mark-grade amber: sparkline stroke
    "mark-fill": "rgba(207, 127, 20, 0.16)",
    "rise": "#4cc38a",  # up-deltas, ok status
    "error": "#ef6f5f",  # error status (6.19:1 on bg, 5.66:1 on bg-elevated)
    "link": "var(--match)",
    # type - display face embedded as a data URI in the stylesheet (F-SIT-07:
    # no external font fetch), body + mono from system stacks
    "font-display": "'League Gothic', 'Arial Narrow', 'Helvetica Neue', sans-serif",
    "font-body": "'Charter', 'Bitstream Charter', 'Sitka Text', Cambria, Georgia, serif",
    "font-mono": (
        "ui-monospace, 'SF Mono', 'Cascadia Mono', 'JetBrains Mono', Menlo, Consolas, monospace"
    ),
    "fs-xs": "0.75rem",
    "fs-s": "0.875rem",
    "fs-m": "1rem",
    "fs-body": "1.0625rem",
    "fs-l": "1.375rem",
    "fs-xl": "1.875rem",
    "fs-2xl": "2.75rem",
    "fs-masthead": "clamp(2.75rem, 10vw, 4rem)",
    "lh-body": "1.65",
    "lh-tight": "1.1",
    "measure": "65ch",  # readable prose measure
    "page-width": "44rem",  # overall page column (mobile-first, capped on desktop)
    # cloud size steps (log buckets w1..w5, see render.cloud_weight_bucket)
    "cloud-1": "1.0625rem",
    "cloud-2": "1.375rem",
    "cloud-3": "1.8125rem",
    "cloud-4": "2.375rem",
    "cloud-5": "3.125rem",
    # space
    "sp-1": "0.25rem",
    "sp-2": "0.5rem",
    "sp-3": "0.75rem",
    "sp-4": "1rem",
    "sp-5": "1.5rem",
    "sp-6": "2.25rem",
    "sp-7": "3.5rem",
    # shape + focus
    "r-s": "2px",
    "r-m": "4px",
    "focus": "2px solid var(--match)",
}

# Light theme: the same token names, re-grounded values only - rendered into a
# `:root[data-theme="light"]` block. Dark is the default on first load; light
# is an explicit, viewer-toggled state (static/theme.js). Tokens not listed
# here (type, space, shape) are theme-independent and inherit from the dark
# block above.
LIGHT_STYLE_TOKENS: dict[str, str] = {
    "bg": "#f4f4f1",
    "bg-elevated": "#ecece7",
    "bg-sunken": "#eaeae4",
    "border": "#d4d4cc",
    "border-strong": "#b8b8ae",
    "text": "#22241f",
    "text-muted": "#5a5e56",
    "text-faint": "#6b6f65",
    "match": "#9a5a08",
    "match-ink": "#f4f4f1",
    "match-wash": "rgba(179, 105, 10, 0.14)",
    "mark": "#b3690a",
    "mark-fill": "rgba(179, 105, 10, 0.14)",
    "rise": "#116b46",
    "error": "#ad2f24",  # error status (5.94:1 on bg, 5.52:1 on bg-elevated)
}

# Keyword-cloud weight buckets (log-scaled, F-SIT-01): CSS classes w1..wN mapped
# to the cloud-1..cloud-N size tokens; render.cloud_weight_bucket assigns each
# term by log(count).
CLOUD_BUCKETS = 5
