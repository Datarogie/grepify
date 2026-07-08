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

CI workflows (added in GRP-06) call `make ingest extract build` on a schedule
and deploy the static site. Fork → add secrets → enable the scheduled workflow
and you have a running clone.

## Data & storage

Truth is append-only JSONL under `data/` (committed, human-readable diffs). The
SQLite cache (`data/grepify.db`) is rebuilt from JSONL each run and is
gitignored — never commit it.
