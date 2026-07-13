# grepify

> grep the firehose. A personal, configurable news/video/social aggregator:
> ingest RSS / YouTube / Reddit, extract keywords with a cheap LLM, compute
> trends, generate daily/weekly digests, and render a static site.

Grepify is a **cloneable template**: fork it, add your secrets, enable the cron,
and it self-hosts on free CI + static hosting. See [`docs/setup.md`](docs/setup.md).

## Quick start

```sh
make install     # sync the dev environment (uv)
make check       # lint + typecheck + test  (definition-of-done gate)
grepify --help   # single-entrypoint CLI
```

## How it works

Append-only JSONL is the **source of truth**, committed to a dedicated
**`data` branch** (not `main`) at the repo root under `logs/`, `items/`,
`digests/`, `runs/`. A SQLite file is a **derived query cache** rebuilt from
that JSONL in CI each run and never committed anywhere. Everything
deterministic is plain Python/SQL over local storage; the LLM is used only for
keyword extraction and digest prose. No server, no LLM in the serving path.

Full architecture: [`docs/architecture.md`](docs/architecture.md). Product spec
and issue plan: [`docs/prd.md`](docs/prd.md) (the source of truth). Build order:
[`docs/playbook.md`](docs/playbook.md).

## Repo layout

```
grepify/          core package (models, repository, config, cli)
sources/          your config: groups/, keywords.yml, settings.yml
data/             grepify.db cache only (gitignored, rebuilt each run)
docs/             prd.md, architecture.md, setup.md, epics/
tests/            pytest suite + fixtures
```
