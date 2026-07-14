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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2

from grepify.site.markdown import render_markdown
from grepify.site.published_url import safe_published_url
from grepify.site.sparkline import sparkline_svg
from grepify.site.tokens import CLOUD_BUCKETS, LIGHT_STYLE_TOKENS, STYLE_TOKENS
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

    ``asset_versions`` maps a ``static/`` asset filename (``digests.js``,
    ``style.css``, ...) to a short content hash. Templates route every static
    reference through :meth:`asset`, which appends ``?v=<hash>`` so a redeploy
    of changed JS/CSS lands on a fresh URL and GitHub Pages cannot keep serving
    a stale cached copy. The hash is a pure function of the asset bytes, so
    identical content yields an identical URL and byte-stable output; only a
    real content change moves the query string. It defaults to empty so pure
    ``render_page`` snapshots (which do not build the static tree) fall back to
    the bare, unversioned URL.
    """

    title: str
    base_path: str
    generated_at: str
    run_id: str
    asset_versions: Mapping[str, str] = field(default_factory=dict)

    def asset(self, name: str) -> str:
        """Base-path-prefixed ``static/`` URL for ``name``, cache-busted if known.

        Appends ``?v=<hash>`` when a version is registered for the asset,
        otherwise returns the bare ``{base_path}static/{name}`` URL.
        """
        version = self.asset_versions.get(name)
        suffix = f"?v={version}" if version else ""
        return f"{self.base_path}static/{name}{suffix}"


@dataclass(frozen=True)
class PageContext:
    """Per-page context: which nav entry is active + the shared meta."""

    meta: SiteMeta
    active: str
    nav: tuple[NavLink, ...] = NAV


def cloud_weight_bucket(count: int, *, min_count: int, max_count: int) -> int:
    """Log-scaled cloud weight bucket, 1..:data:`CLOUD_BUCKETS` (PRD §8 F-SIT-01).

    The cloud template renders each term with a ``w<bucket>`` class mapped to
    the ``cloud-<bucket>`` size tokens, so the size steps live in the design
    tokens instead of inline styles. Interpolates by ``log(count)`` so a
    handful of high-count terms don't dwarf the rest. A single distinct count
    (min == max) renders every term at the middle bucket. Integer output is
    byte-stable by construction.
    """
    if max_count <= min_count:
        frac = 0.5
    elif count <= min_count:
        frac = 0.0
    else:
        lo = math.log(min_count + 1)
        hi = math.log(max_count + 1)
        frac = (math.log(count + 1) - lo) / (hi - lo)
    return min(CLOUD_BUCKETS, 1 + int(frac * CLOUD_BUCKETS))


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
    env.globals["cloud_weight_bucket"] = cloud_weight_bucket
    env.globals["digest_slug"] = digest_slug
    env.globals["keyword_slug"] = keyword_slug
    env.globals["render_markdown"] = render_markdown
    env.globals["safe_published_url"] = safe_published_url
    return env


def render_page(
    env: jinja2.Environment,
    template: str,
    ctx: PageContext,
    **data: Any,
) -> str:
    """Render ``template`` with the page context + page-specific ``data``."""
    return env.get_template(template).render(ctx=ctx, meta=ctx.meta, nav=ctx.nav, **data)


def render_stylesheet(
    env: jinja2.Environment,
    tokens: Mapping[str, str] = STYLE_TOKENS,
    light_tokens: Mapping[str, str] = LIGHT_STYLE_TOKENS,
) -> str:
    """Render ``style.css`` from the design tokens (single source of truth).

    ``tokens`` fills the default (dark) ``:root`` block; ``light_tokens`` fills
    the ``:root[data-theme="light"]`` re-grounding block.
    """
    return env.get_template("style.css.jinja").render(tokens=tokens, light_tokens=light_tokens)
