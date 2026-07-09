"""Single-entrypoint CLI (PRD §8 F-OPS-01): ``grepify <subcommand>``.

Subcommands: ``ingest extract trends digest build validate health backfill``.
``ingest`` is wired to the E1 orchestrator (GRP-15/16); ``extract`` is wired to
the E2 pipeline (GRP-25: untagged-item selection, real LLM client, keyword
normalization, PRD §10.7 data-quality gate); ``validate`` is fully wired to
the config layer; ``backfill`` re-extracts ``method='fallback'`` rows through
a real LLM client (GRP-22). ``trends``/``digest``/``build`` remain stubs that
record a run manifest so the operator tooling (``health``) works end to
end — later epics replace each stub body without changing the CLI surface.
(``backfill``'s scope here is GRP-22's fallback-only re-extraction; broader
E6/GRP-60 maintenance modes — reindex, vacuum, prune — are later work behind
the same subcommand.)

Failure modes
-------------
- ``validate`` exits non-zero when config is invalid (CI gate on every MR).
- ``ingest`` never fails the process for a single dead source (isolated in the
  orchestrator, PRD §9); it only propagates systemic failures (bad config,
  unreadable truth).
- ``extract`` exits non-zero if ``LLM_BASE_URL`` is unset (nothing to call) —
  same convention as ``backfill``, below. A :class:`~grepify.errors.DataQualityError`
  (PRD §10.7 gate failed) propagates and fails the run loudly, writing no
  manifest for that run, same as a systemic config failure. Once running,
  per-batch LLM failures degrade to the fallback extractor (PRD §9) rather
  than failing the command.
- ``backfill`` exits non-zero if ``LLM_BASE_URL`` is unset (nothing to call);
  a misconfigured LLM profile (bad endpoint/missing model) propagates
  :class:`~grepify.errors.LlmError` as a systemic failure, same as ``ingest``
  propagates :class:`~grepify.errors.ConfigError` for bad config. Once running,
  per-batch LLM failures degrade to the fallback extractor (PRD §9) rather
  than failing the command.
- Pipeline stubs never fail the process; they write a manifest noting the
  not-yet-implemented epic and return 0.
- ``health`` with no recorded runs prints a friendly notice, exit 0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from grepify.clock import Clock, SystemClock, to_iso
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.extract import YakeFallbackExtractor, run_extract_pipeline, run_fallback_backfill
from grepify.health import write_health_snapshot
from grepify.ingest.orchestrator import IngestServices, build_registry, run_ingest
from grepify.keywords import KeywordRules
from grepify.llm import build_client
from grepify.models import RunManifest
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.run import latest_manifest, new_run_id, write_manifest

app = typer.Typer(add_completion=False, help="grep the firehose — grepify CLI.")


@dataclass
class AppState:
    config_root: Path
    data_root: Path
    clock: Clock


ConfigRootOpt = Annotated[Path, typer.Option(help="Config directory (sources/).")]
DataRootOpt = Annotated[Path, typer.Option(help="Data root (JSONL truth + cache).")]


@app.callback()
def main(
    ctx: typer.Context,
    config_root: ConfigRootOpt = Path("sources"),
    data_root: DataRootOpt = Path("data"),
) -> None:
    """Resolve shared paths for all subcommands."""
    ctx.obj = AppState(config_root=config_root, data_root=data_root, clock=SystemClock())


# --- wired pipeline commands -------------------------------------------------


@app.command()
def ingest(ctx: typer.Context) -> None:
    """Fetch enabled sources, normalize+dedup, and record health (E1, GRP-15/16)."""
    state: AppState = ctx.obj
    layout = DataLayout(state.data_root)
    run_id = new_run_id(state.clock)
    started_at = to_iso(state.clock.now())

    config = FilesystemConfigProvider(state.config_root)
    repository = JsonlSqliteRepository(state.data_root)
    try:
        summary = run_ingest(
            IngestServices(
                config=config,
                repository=repository,
                registry=build_registry(),
                clock=state.clock,
            ),
            run_id=run_id,
        )
        write_health_snapshot(
            repository.iter_fetch_log(),
            layout,
            run_id=run_id,
            generated_at=to_iso(state.clock.now()),
        )
    finally:
        repository.close()

    manifest = RunManifest(
        run_id=run_id,
        command="ingest",
        started_at=started_at,
        finished_at=to_iso(state.clock.now()),
        ok=True,
        counts={
            "sources_attempted": summary.sources_attempted,
            "sources_ok": summary.sources_ok,
            "sources_empty": summary.sources_empty,
            "sources_error": summary.sources_error,
            "items_new": summary.items_new,
        },
        durations_ms={"total_ms": summary.duration_ms},
        notes=[f"{r.source_id}: {r.error}" for r in summary.results if r.error],
    )
    write_manifest(layout, manifest)
    typer.echo(
        f"ingest: {summary.sources_ok} ok, {summary.sources_empty} empty, "
        f"{summary.sources_error} error, {summary.items_new} new items; run {run_id}"
    )


ForceOpt = Annotated[
    bool,
    typer.Option(
        "--force", help="Re-extract every item, including already-tagged ones (F-EXT-04)."
    ),
]


@app.command()
def extract(ctx: typer.Context, force: ForceOpt = False) -> None:
    """Extract keywords for untagged items via the active LLM profile (GRP-25).

    Selects items with no keyword rows at all, runs them through the extract
    batcher (budget-gated, degrading to the deterministic fallback extractor
    per PRD §9), normalizes + alias/mute-applies the result
    (:mod:`grepify.keywords`), and enforces the PRD §10.7 data-quality gate
    before writing to truth. Reads the LLM endpoint from ``settings.yml``'s
    active profile but resolves the deployment secrets from the environment
    (``LLM_BASE_URL`` required, ``LLM_API_KEY`` optional for keyless local
    endpoints), never from committed config (PRD §5) — same convention as
    ``backfill``. ``--force`` bypasses untagged-item selection entirely and
    re-extracts every item in truth (F-EXT-04) — a deliberate manual escape
    hatch (e.g. after a prompt/model change), not wired into the pipeline cron.
    """
    state: AppState = ctx.obj
    layout = DataLayout(state.data_root)
    run_id = new_run_id(state.clock)
    started_at = to_iso(state.clock.now())

    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url:
        typer.echo("extract: LLM_BASE_URL is not set; nothing to do", err=True)
        raise typer.Exit(code=1)

    config = FilesystemConfigProvider(state.config_root)
    repository = JsonlSqliteRepository(state.data_root)
    try:
        settings = config.settings()
        profile = settings.llm.profiles[settings.llm.active_profile]
        client = build_client(
            profile,
            api_key=os.environ.get("LLM_API_KEY") or None,
            base_url=base_url,
            log_sink=repository.log_llm,
            clock=state.clock,
        )
        rules = KeywordRules.from_config(config.keywords())
        items = list(repository.iter_items())
        existing_keywords = list(repository.iter_item_keywords())
        summary, new_keywords = run_extract_pipeline(
            items,
            existing_keywords,
            client,
            run_id=run_id,
            clock=state.clock,
            fallback=YakeFallbackExtractor(),
            rules=rules,
            force=force,
            max_items_per_call=settings.llm.max_items_per_call,
        )
        written = repository.add_item_keywords(new_keywords)
    finally:
        repository.close()

    notes = []
    if summary.no_keywords_item_ids:
        notes.append(
            "items with no keywords after extraction: " + ", ".join(summary.no_keywords_item_ids)
        )
    if summary.budget_exhausted:
        notes.append("llm budget exhausted before all untagged items were extracted")

    manifest = RunManifest(
        run_id=run_id,
        command="extract",
        started_at=started_at,
        finished_at=to_iso(state.clock.now()),
        ok=True,
        counts={
            "items_selected": summary.items_selected,
            "batches_total": summary.batches_total,
            "batches_llm": summary.batches_llm,
            "batches_fallback": summary.batches_fallback,
            "keywords_written": written,
            "keywords_muted": summary.muted_count,
            "items_no_keywords": len(summary.no_keywords_item_ids),
        },
        notes=notes,
    )
    write_manifest(layout, manifest)
    typer.echo(
        f"extract: {summary.items_selected} items, {summary.batches_llm} llm batches, "
        f"{summary.batches_fallback} fallback batches, {written} new keyword rows; run {run_id}"
    )


# --- pipeline stubs (E3/E4) ----------------------------------------------------


@app.command()
def trends(ctx: typer.Context) -> None:
    """Compute trend datasets (E3/E4)."""
    _record_stub(ctx, "trends", "E3")


@app.command()
def digest(ctx: typer.Context) -> None:
    """Generate per-category digests (E4)."""
    _record_stub(ctx, "digest", "E4")


@app.command()
def build(ctx: typer.Context) -> None:
    """Render the static site (E3)."""
    _record_stub(ctx, "build", "E3")


BackfillMaxCallsOpt = Annotated[
    int, typer.Option(help="Cap on real LLM calls this run (playbook S7 recommends 200).")
]


@app.command()
def backfill(ctx: typer.Context, max_calls: BackfillMaxCallsOpt = 200) -> None:
    """Re-extract items whose keywords are entirely ``method='fallback'`` (GRP-22).

    Manual/one-time command — not wired into the pipeline cron (GRP-25). Reads
    the LLM endpoint from ``settings.yml``'s active profile but resolves the
    deployment secrets from the environment (``LLM_BASE_URL`` required,
    ``LLM_API_KEY`` optional for keyless local endpoints), never from
    committed config (PRD §5).
    """
    state: AppState = ctx.obj
    layout = DataLayout(state.data_root)
    run_id = new_run_id(state.clock)
    started_at = to_iso(state.clock.now())

    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url:
        typer.echo("backfill: LLM_BASE_URL is not set; nothing to do", err=True)
        raise typer.Exit(code=1)

    config = FilesystemConfigProvider(state.config_root)
    repository = JsonlSqliteRepository(state.data_root)
    try:
        settings = config.settings()
        profile = settings.llm.profiles[settings.llm.active_profile]
        capped_profile = profile.model_copy(update={"max_calls_per_run": max_calls})
        client = build_client(
            capped_profile,
            api_key=os.environ.get("LLM_API_KEY") or None,
            base_url=base_url,
            log_sink=repository.log_llm,
            clock=state.clock,
        )
        items = list(repository.iter_items())
        keywords = list(repository.iter_item_keywords())
        result = run_fallback_backfill(
            items,
            keywords,
            client,
            run_id=run_id,
            clock=state.clock,
            fallback=YakeFallbackExtractor(),
            max_items_per_call=settings.llm.max_items_per_call,
        )
        written = repository.add_item_keywords(result.keywords)
    finally:
        repository.close()

    manifest = RunManifest(
        run_id=run_id,
        command="backfill",
        started_at=started_at,
        finished_at=to_iso(state.clock.now()),
        ok=True,
        counts={
            "batches_total": result.batches_total,
            "batches_llm": result.batches_llm,
            "batches_fallback": result.batches_fallback,
            "keywords_written": written,
        },
        notes=(
            ["llm budget exhausted before all candidates were re-extracted"]
            if result.budget_exhausted
            else []
        ),
    )
    write_manifest(layout, manifest)
    typer.echo(
        f"backfill: {result.batches_llm} llm batches, {result.batches_fallback} still-fallback "
        f"batches, {written} new keyword rows; run {run_id}"
    )


# --- other wired commands -----------------------------------------------------


@app.command()
def validate(ctx: typer.Context) -> None:
    """Schema-validate config; exit non-zero if invalid (CI gate on every MR)."""
    state: AppState = ctx.obj
    provider = FilesystemConfigProvider(state.config_root)
    report = provider.validate()
    typer.echo(report.summary())
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}")
    for error in report.errors:
        typer.echo(f"  error: {error}")
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def health(ctx: typer.Context) -> None:
    """Print the latest run manifest (PRD §8 F-OPS-04)."""
    state: AppState = ctx.obj
    manifest = latest_manifest(DataLayout(state.data_root))
    if manifest is None:
        typer.echo("no runs recorded yet")
        return
    typer.echo(manifest.model_dump_json(indent=2))


# --- helpers ----------------------------------------------------------------


def _record_stub(ctx: typer.Context, command: str, epic: str) -> None:
    state: AppState = ctx.obj
    now = to_iso(state.clock.now())
    manifest = RunManifest(
        run_id=new_run_id(state.clock),
        command=command,
        started_at=now,
        finished_at=now,
        ok=True,
        notes=[f"stub: {command} not yet implemented ({epic})"],
    )
    write_manifest(DataLayout(state.data_root), manifest)
    typer.echo(f"{command}: stub (implemented in {epic}); recorded run {manifest.run_id}")


if __name__ == "__main__":  # pragma: no cover
    app()
