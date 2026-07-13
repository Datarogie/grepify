# Grepify - UI redesign brief (hand-off)

A self-contained brief for a design pass on the grepify static site. A designer
should be able to work from this alone, without reading the codebase. The goal
is a **much stronger visual identity and reading experience** for the pages Kyle
opens every morning, delivered **within** the locked v1 constraints below so it
drops straight into the existing Jinja templates.

## 1. Product, audience, job

- **Grepify** is a personal, self-hosted news/trend aggregator (AI + data-eng
  categories). It ingests RSS / YouTube / Reddit, extracts keywords with a
  cheap LLM, computes trends, and writes daily/weekly digests. Inspired by
  trendcloud.io.
- **Audience: one person (Kyle), primarily on a phone.** The site is a morning
  read, not a dashboard operated all day. Mobile-first is the default, not an
  afterthought; desktop is the widened case.
- **The page's job, ranked:** (1) read today's digest; (2) scan the keyword
  cloud and tap into a keyword; (3) browse/filter recent items. Everything else
  (sources, health) is utilitarian.

## 2. Hard constraints (non-negotiable - the redesign must honor all of these)

These come from PRD §5 and the E3 build contract. A design that breaks them
cannot be merged.

- **Static Jinja SSG.** Output is plain HTML + one CSS file + a few tiny vanilla
  JS files. No React/Vue/Svelte, no build step, no Tailwind/CDN.
- **All styling flows from design tokens.** Colors, spacing, type scale, radius
  live in one place (`grepify/site/tokens.py`) and render into
  `:root { --token: value }` CSS custom properties. Templates reference
  `var(--token)`; they never hard-code values. A redesign changes the **token
  values** and the **CSS rules for existing class names**, not the Python.
- **No external fonts, no CDNs, no trackers, no network at runtime.** System
  font stack, OR a face embedded as an inline `@font-face` `data:` URI in the
  stylesheet. No `<link>` to Google Fonts, no analytics.
- **Byte-stable output (snapshot-tested).** The CSS/HTML must be a pure function
  of the data: no `Math.random()`, no time-based values, no per-build variation.
  Animations are fine (CSS keyframes), but the emitted bytes must be identical
  for identical input. Honor `prefers-reduced-motion`.
- **Dark, mobile-first is the current default.** A light theme is welcome as an
  *addition* (see open questions), not a replacement. If added, do it at the
  token level so components don't care which theme is active.
- **Accessibility floor:** visible keyboard focus, semantic markup, `aria-current`
  on the active nav item, alt/aria labels on the sparkline. Body text passes
  WCAG AA on the chosen ground.
- **Interactivity stays tiny and vanilla.** Existing JS: items filter, digest
  daily/weekly filter, keyword-page tabs, and `<details>` "n similar" expanders.
  A redesign may restyle these but should not add a framework.

## 3. Page inventory + what each page shows

The design needs to cover these seven page types. Real content shapes are given
so mockups use realistic data, never lorem.

| Page | URL | Key content |
|---|---|---|
| Home | `/` | Stats row (items/sources/keywords/mentions/top-keyword/top-source, for a 7d window); **keyword cloud** (log-scaled font sizes, each links to a keyword page, some carry a green `+N` / faint `-N` delta and a "rising" state); Latest digests (up to 5); Latest items (10, each: title link, source · kind · date, keyword tags); Top sources. |
| Digest index | `/digest/` | List of digests with a daily/weekly filter control; each row: title, `kind · category · date`, keyword chips with counts. |
| Digest detail | `/digest/<kind>/<slug>/` | Title; `kind · category · period`; keyword chips (link to keyword pages); the narrative body (a **TL;DR** bullet list + 2-4 short paragraphs). This is the marquee reading surface - give it the best typography. |
| Keyword detail | `/keyword/<slug>/` | `#keyword` heading; stat pair (mentions / sources for a 30d window); a **mention timeline sparkline**; related keywords (chips with co-occurrence counts); "Latest content" **tabbed by kind** (Articles / YouTube / Reddit / X), each a list of items. |
| Items browser | `/items/` (+ `/items/page-N/`) | Filter bar (kind select, source select, keyword search, clear); paginated list (20/page) of items; near-duplicate items collapse under a `<details>` "n similar"; prev/next pager. |
| Sources | `/sources/` | Grouped tables (per source group): name, kind, enabled, feed link. |
| Health | `/health/` | Per-source status table: source, last status (ok/empty/error/skipped), last attempt, attempts, consecutive failures; flagged rows stand out. |

## 4. Current design tokens (the starting point to improve on)

Dark palette, GitHub-ish. The redesign should treat these as a baseline to
**replace with something more distinctive**, not a constraint.

- Ground: `bg #0d1117`, `bg-elevated #161b22`, `bg-sunken #010409`, `border #30363d`
- Text: `text #e6edf3`, `text-muted #9da7b3`, `text-faint #6e7681`
- Accent: `accent #4dd0e1` (cyan), `accent-strong #22b8cf`, `link #79c0ff`
- Semantic: `ok #3fb950`, `warn #d29922`, `error #f85149`
- Type: system sans + system mono; scale `0.85 / 1 / 1.25 / 1.6 rem`; line-height 1.55
- Space scale: `0.25 / 0.5 / 0.75 / 1 / 1.5 / 2 rem`; radius `8px` / `4px`;
  readable measure `48rem`
- Cloud font range: `0.85rem` to `2.4rem` (log-scaled by mention count)

## 5. The exact style hooks (so a designer restyles in CSS only)

The templates already emit these class names. A redesign is mostly: new token
values + new CSS rules for these selectors. Renaming a class means editing a
template too, which is fine but note it in the hand-back.

- Chrome: `.site-header`, `.brand`, `.site-nav` (+ `a[aria-current="page"]`),
  `.site-main`, `.site-footer`, `.skip-link`
- Headings: `h1`, `h2`
- Home: `.stats` (`.stat-value`, `.stat-label`); `.cloud` (`.delta-up`,
  `.delta-down`); `.feed` (`.meta`); `.tag` (`.count`)
- Digest: `.digest` (index rows), `.digest-detail`, `.prose` (`p`, `ul`, `li`)
- Keyword: `.keyword-detail`, `.timeline` (`.sparkline`), `.tabs` (`.tab`,
  `[aria-selected="true"]`), `.tab-panel`
- Items: `.filters` (select/input/button), `.item`, `.similar` (`<details>`),
  `.pager`
- Tables (sources/health): `table`, `th`, `td`, `.status-ok/-warn/-error`
- Utility: `.card`, `.muted`

## 6. Where the design has real room (spend the effort here)

- **The keyword cloud is the hero.** Today it's plain sized links. It can carry
  the identity: weighting, rhythm, how "rising" reads (a badge? a glow? a mark?),
  how deltas show. Make it feel alive without animation-for-its-own-sake.
- **Digest reading typography.** The digest body is the thing Kyle actually
  reads. Editorial-quality type: measure, scale, TL;DR treatment, chip design.
- **The sparkline + keyword page.** Give the timeline an area fill, a faint
  baseline, an emphasized endpoint; make the tabs feel like a real control.
- **Density + hierarchy on mobile.** Stats and lists should scan in a thumb's
  reach; the cloud shouldn't push the digest below the fold on a phone.

## 7. What "done" looks like + how it lands back in the repo

Deliverable from the designer (or Fable): a concrete visual direction - ideally
a single self-contained HTML/CSS mock of Home + Digest detail + Keyword page,
theme-aware, using the real content shapes above. From that, the repo change is:

1. Update token values in `grepify/site/tokens.py` (and add any light-theme
   token block).
2. Rewrite `grepify/site/templates/style.css.jinja` (the CSS rules for the hooks
   in §5). If a display face is used, embed it as an inline `@font-face data:`
   URI here.
3. Minor template tweaks only if new class hooks/markup are needed (the seven
   templates in `grepify/site/templates/`).
4. Regenerate the snapshot goldens (the build is byte-stable, so this is a
   deliberate one-time diff) and run `make check`.
5. One PR titled as a site-refresh; it does not touch the pipeline, storage, or
   LLM code.

This is a **styling iteration inside the locked GRP-30 decisions** (still Jinja,
still tokens, still dark/mobile-first/no-fonts), so it needs no PRD/architecture
change. If the direction wants to break a constraint (e.g. a webfont, a JS
framework, a light-only theme), that's a PRD §5 discussion first.

## 8. Open questions for Kyle / the designer

1. **Light theme?** Add one (token-level, viewer-toggleable) or stay dark-only?
2. **Typography:** stay on the system stack (zero bytes, safe) or embed one
   display face as a data-URI for identity (adds ~20-80 KB to the CSS)?
3. **Personality:** what should grepify *feel* like - terminal/hacker (leans
   into "grep the firehose"), editorial/newspaper, or clean-utility? This is the
   single biggest steer for palette + type.
4. **Motion:** any ambient/entrance motion wanted, or keep it static and fast?
