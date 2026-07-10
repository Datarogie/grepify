"""Jinja rendering skeleton (GRP-30, PRD §5 "Jinja SSG").

Builds the deterministic Jinja environment every page render shares, plus the
small typed context objects (:class:`SiteMeta`, :class:`NavLink`,
:class:`PageContext`) that carry site chrome into templates. The base layout
(``templates/base.html``) and the tokenised stylesheet
(``templates/style.css.jinja``) live alongside this module; :func:`render_page`
and :func:`render_stylesheet` turn them into byte-stable strings.

Determinism (F-SIT-08 / S8)
---------------------------
- ``generated_at`` comes from the **injected clock** via the caller - this
  module never calls ``datetime.now()`` (PRD §5). Templates receive it as a
  string.
- The environment sets ``trim_blocks``/``lstrip_blocks``/``keep_trailing_newline``
  so whitespace is stable, and autoescaping is on for HTML (XSS-safe titles).
- Callers must hand templates **already-sorted** collections; nothing here
  reorders data, but nothing here relies on dict order for output either.

Failure modes
-------------
- A missing/misspelled template name → ``jinja2.TemplateNotFound`` (a
  programming error, surfaced loudly at build time).
- A template referencing an undefined variable renders it as empty (Jinja's
  default ``Undefined``); the build orchestrator (GRP-35) passes complete
  contexts, so this does not mask data gaps in practice.
- Pure rendering - no I/O here; writing ``public/`` is the orchestrator's job.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2

from grepify.site.markdown import render_markdown
from grepify.site.sparkline import sparkline_svg
from grepify.site.tokens import CLOUD_MAX_REM, CLOUD_MIN_REM, STYLE_TOKENS
from grepify.site.urls import digest_slug, keyword_slug

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class NavLink:
    """One primary-nav entry."""

    key: str
    label: str
    href: str


# The primary nav, in display order. Hrefs are root-relative *within* the site
# and are prefixed with the deploy base path at render time (see SiteMeta).
NAV: tuple[NavLink, ...] = (
    NavLink(key="home", label="Home", href=""),
    NavLink(key="digests", label="Digests", href="digest/"),
    NavLink(key="items", label="Items", href="items/"),
    NavLink(key="sources", label="Sources", href="sources/"),
    NavLink(key="health", label="Health", href="health/"),
)


@dataclass(frozen=True)
class SiteMeta:
    """Site-wide chrome shared by every page.

    ``base_path`` is the deploy sub-path (e.g. ``/grepify/`` for a project Pages
    site, ``/`` for a user/root deploy); it prefixes every internal link so the
    same build works under either. ``generated_at``/``run_id`` come from the
    injected clock + run manifest (provenance in the footer).
    """

    title: str
    base_path: str
    generated_at: str
    run_id: str


@dataclass(frozen=True)
class PageContext:
    """Per-page context: which nav entry is active + the shared meta."""

    meta: SiteMeta
    active: str
    nav: tuple[NavLink, ...] = NAV


def cloud_font_rem(count: int, *, min_count: int, max_count: int) -> float:
    """Log-scaled cloud font size in rem (PRD §8 F-SIT-01).

    Interpolates between :data:`CLOUD_MIN_REM` and :data:`CLOUD_MAX_REM` by
    ``log(count)`` so a handful of high-count terms don't dwarf the rest. A
    single distinct count (min == max) renders every term at the midpoint.
    Rounded to 3 decimals for byte-stable output.
    """
    if max_count <= min_count:
        frac = 0.5
    elif count <= min_count:
        frac = 0.0
    else:
        lo = math.log(min_count + 1)
        hi = math.log(max_count + 1)
        frac = (math.log(count + 1) - lo) / (hi - lo)
    return round(CLOUD_MIN_REM + (CLOUD_MAX_REM - CLOUD_MIN_REM) * frac, 3)


def create_environment() -> jinja2.Environment:
    """Build the shared, deterministic Jinja environment."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(enabled_extensions=("html",)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=jinja2.Undefined,
    )
    env.globals["sparkline_svg"] = sparkline_svg
    env.globals["cloud_font_rem"] = cloud_font_rem
    env.globals["digest_slug"] = digest_slug
    env.globals["keyword_slug"] = keyword_slug
    env.globals["render_markdown"] = render_markdown
    return env


def render_page(
    env: jinja2.Environment,
    template: str,
    ctx: PageContext,
    **data: Any,
) -> str:
    """Render ``template`` with the page context + page-specific ``data``."""
    return env.get_template(template).render(ctx=ctx, meta=ctx.meta, nav=ctx.nav, **data)


def render_stylesheet(env: jinja2.Environment, tokens: Mapping[str, str] = STYLE_TOKENS) -> str:
    """Render ``style.css`` from the design tokens (single source of truth)."""
    return env.get_template("style.css.jinja").render(tokens=tokens)
