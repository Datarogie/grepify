# Setup — self-hosting a grepify fork

Grepify is a cloneable template: no hardcoded personal paths, config lives in
`sources/`, and secrets live only in CI masked variables (PRD §5). This guide
gets a fork running.

## 1. Prerequisites

- Python 3.12 (pinned in `.python-version`)
- [`uv`](https://docs.astral.sh/uv/) for environment management
- `make`

## 2. Install & sanity-check

```sh
make install     # uv sync --group dev
make check       # lint + typecheck + test — should be green on a fresh clone
grepify --help
```

## 3. Configure your sources

Config is plain YAML under `sources/` (loaded by the `ConfigProvider`):

```
sources/
  settings.yml     # cadence, windows, LLM profiles, budgets  (copy from settings.example.yml)
  keywords.yml     # aliases / mutes / pins
  groups/*.yml     # curated source-group bundles (one file per group)
```

Copy the template and edit:

```sh
cp settings.example.yml sources/settings.yml
$EDITOR sources/settings.yml
```

Add or edit group files under `sources/groups/` (schema in PRD §7). Then:

```sh
grepify validate   # schema-validates config; rejects dup ids/url_hashes, bad kinds
```

`validate` also runs in CI on every MR — a config change that fails validation
never merges.

## 4. Secrets (CI only)

Set these as masked CI variables — **never commit them** (`.env` is gitignored):

| Secret | Used by | When |
|---|---|---|
| `LLM_API_KEY` | keyword extraction + digests | M2 |
| X session cookies | twscrape | M5 |
| Slack webhook / bot token | digest push | v1.5 |

## 5. Enable the cron

Two workflows live under `.github/workflows/`:

- `validate.yml` — `make check` + `make validate` on every PR (and on pushes
  to `main` that touch anything outside `data/`).
- `pipeline.yml` — cron (3x/day) running `make ingest extract` (+ `make
  digest` when `scripts/digest-gate.sh` says it's due), commits any new
  `data/` files back to the branch (`[skip ci]`, so it never re-triggers
  `validate`), then `make build site` and a GitHub Pages deploy.

One manual step outside the repo: **Settings → Pages → Source: GitHub
Actions** (can't be set from a workflow file). After that, both workflows run
as-is; `pipeline.yml` also has a `workflow_dispatch` trigger so you can run it
on demand from the Actions tab (including from a PR branch, e.g. from a
phone) instead of waiting for the schedule.

Until the real site build lands (GRP-35, M3), `make site` deploys the static
placeholder in `site-placeholder/`.

## Data & storage

Truth is append-only JSONL under `data/` (committed, human-readable diffs). The
SQLite cache (`data/grepify.db`) is rebuilt from JSONL each run and is
gitignored — never commit it.
