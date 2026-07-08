"""Single-entrypoint CLI (PRD §8 F-OPS-01): ``grepify <subcommand>``.

Subcommands: ``ingest extract trends digest build validate health backfill``.
``ingest`` is wired to the E1 orchestrator (GRP-15/16); ``validate`` is fully
wired to the config layer. ``extract``/``trends``/``digest``/``build``/
``backfill`` remain stubs that record a run manifest so the operator tooling
(``health``) works end to end — later epics replace each stub body without
changing the CLI surface.

Failure modes
-------------
- ``validate`` exits non-zero when config is invalid (CI gate on every MR).
- ``ingest`` never fails the process for a single dead source (isolated in the
  orchestrator, PRD §9); it only propagates systemic failures (bad config,
  unreadable truth).
- Pipeline stubs never fail the process; they write a manifest noting the
  not-yet-implemented epic and return 0.
- ``health`` with no recorded runs prints a friendly notice, exit 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from grepify.clock import Clock, SystemClock, to_iso
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.health import write_health_snapshot
from grepify.ingest.orchestrator import IngestServices, build_registry, run_ingest
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


# --- pipeline stubs (E2+) -----------------------------------------------------


@app.command()
def extract(ctx: typer.Context) -> None:
    """LLM keyword extraction (E2)."""
    _record_stub(ctx, "extract", "E2")


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


@app.command()
def backfill(ctx: typer.Context) -> None:
    """Re-process / re-extract historical data (E6)."""
    _record_stub(ctx, "backfill", "E6")


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
