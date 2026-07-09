# Grepify make targets.
#
# CI is just a scheduler: every pipeline step is a make target that also runs
# locally (PRD §5). `make check` is the definition-of-done gate for every MR.

.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install fmt lint typecheck test check \
        ingest extract trends digest build validate health backfill \
        digest-gate data-branch commit-data site eval

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Sync the dev environment
	$(UV) sync --group dev

fmt: ## Auto-format
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Lint + format check (no changes)
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck: ## Strict type-check the core package
	$(UV) run mypy

test: ## Run the test suite
	$(UV) run pytest

check: lint typecheck test ## Full DoD gate: lint + typecheck + test

# --- Pipeline entrypoints (PRD §8 F-OPS-01). Stubbed in E0; filled by later epics. ---

ingest: ## Fetch enabled sources (E1)
	$(UV) run grepify ingest

extract: ## LLM keyword extraction (E2)
	$(UV) run grepify extract

trends: ## Compute trend datasets (E3/E4)
	$(UV) run grepify trends

digest: ## Generate category digests (E4)
	$(UV) run grepify digest

build: ## Render the static site (E3)
	$(UV) run grepify build

validate: ## Schema-validate config (CI check on every MR)
	$(UV) run grepify validate

health: ## Print the latest run manifest
	$(UV) run grepify health

backfill: ## Re-extract method='fallback' rows through the real LLM (GRP-22); broader E6 modes are later work
	$(UV) run grepify backfill

eval: ## Score the extract prompt/model against the GRP-24 labeled set (PRD §10.5); manual, not part of `check`/CI
	$(UV) run python scripts/eval.py

# --- CI-only helpers (GRP-06). Kept as make targets, not inline workflow ---
# --- shell, per F-OPS-03 (GitLab portability). ---------------------------

digest-gate: ## Print daily=/weekly= flags: are digest steps due now? (coarse pre-GRP-45 placeholder)
	@bash scripts/digest-gate.sh

data-branch: ## Check out the dedicated `data` branch as a worktree at ./data (bootstraps it on first run)
	bash scripts/ensure-data-branch.sh

commit-data: ## Commit + push data/ changes to the `data` branch worktree with rebase-retry ([skip ci] loop guard)
	$(UV) run python scripts/commit_pipeline_data.py --repo-dir data --branch data

site: ## Assemble public/ for the Pages deploy (placeholder until GRP-35 emits the real SSG output)
	mkdir -p public
	cp -r site-placeholder/. public/
