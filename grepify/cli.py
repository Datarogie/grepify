"""Single-entrypoint CLI (PRD §8 F-OPS-01): ``grepify <subcommand>``.

Subcommands: ``ingest extract trends digest build validate health doctor
backfill datasize``, plus ``maintain renormalize``. Each command's own docstring
is its contract; ``trends`` is still a stub that records a run manifest so
``health`` works end to end.

Failure modes
-------------
- ``validate`` exits non-zero when config is invalid (CI gate on every MR),
  including an *enabled* source whose ``kind`` has no fetcher registered in
  the production registry (GRP-56: ``kind`` passes schema validation - it is
  a real :class:`~grepify.models.SourceKind` - but nothing dispatches it, so
  this is checked explicitly rather than left to surface at ingest time).
- ``ingest`` never fails the process for a single dead source (isolated in the
  orchestrator, PRD §9); it only propagates systemic failures (bad config,
  unreadable truth). An enabled source whose kind has no registered fetcher
  is the same per-source isolation, defense in depth against the case above
  slipping past ``validate`` (config edited after the last validate run): it
  is logged as an ``error`` fetch_log row, not a systemic failure. A source
  whose kind is cadence-reduced (T6, GRP-31 - Reddit by default) is simply not
  dispatched on runs it is not due, logged ``skipped`` instead - never an
  error, never something that can fail the run. The health snapshot it writes
  never flags a ``settings.ingest.quiet_kinds`` source (Reddit by default) on
  consecutive failures either, though the failure count is still computed and
  shown (:mod:`grepify.health`).
- ``extract`` exits non-zero if ``LLM_BASE_URL`` is unset (nothing to call) -
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
- ``digest`` no-ops (writes a manifest note, exits 0, no LLM calls, no files)
  when ``settings.digest.enabled`` is false - the pause switch for data
  remediation. Otherwise it exits non-zero if ``LLM_BASE_URL`` is unset (same
  convention as ``extract``); once running, an LLM that is down or over budget
  degrades each affected category to a deterministic template digest (PRD §9/§13)
  rather than failing the run, and a category below the item threshold is skipped
  (F-DIG-03).
- ``maintain renormalize`` exits non-zero if ``LLM_BASE_URL`` is unset (same
  convention as ``extract``); the summary rewrite + keyword-row drop happen only
  after that check, and the forced re-extract degrades per-batch to the fallback
  extractor rather than failing the run (PRD §9). A clean corpus is a no-op.
- ``digest-gate`` (GRP-63) reads config + the digests already in truth to check
  existence; a bad config propagates :class:`~grepify.errors.ConfigError` same as
  every other config-reading command, but a missing/empty data root is not an
  error - no digests found just means every category's existence check is false.
- The ``trends`` stub never fails the process; it writes a manifest noting the
  not-yet-implemented epic and returns 0.
- ``health`` with no recorded runs prints a friendly notice, exit 0.
- ``doctor`` (T5, GRP-30) is a read-only per-source status + error-class
  triage report (:mod:`grepify.doctor`); it recomputes fresh from ``fetch_log``
  on every call (not from a possibly-stale ``health.json``), writes nothing,
  and never fails the run itself - a malformed config still raises
  :class:`~grepify.errors.ConfigError`, same as every other command that reads
  config. Like ``ingest``'s health snapshot, it never flags a
  ``settings.ingest.quiet_kinds`` source (T6, GRP-31).
- ``datasize`` (GRP-63) is a read-only, config-free, network-free directory-size
  sum over ``items/`` + ``keywords/`` + ``transcripts/`` under the data root
  (:mod:`grepify.datasize`); it never raises for a missing directory (treated as
  zero bytes) and writes no manifest. It exits 0 under the warn threshold, 0
  (with a ``WARN`` line) in the warn band, and non-zero at/over the fail
  threshold - the only command in this module whose exit code is a deliberate
  CI gate on data volume rather than a config/LLM/systemic failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from grepify.clock import Clock, SystemClock, to_iso
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.config.schemas import SettingsConfig
from grepify.datasize import (
    DEFAULT_FAIL_BYTES,
    DEFAULT_WARN_BYTES,
    SizeLevel,
    compute_data_size,
    format_report,
)
from grepify.digest import (
    TEMPLATE_MODEL,
    digest_gate,
    digest_id_for,
    format_gate,
    run_digest_pipeline,
)
from grepify.digest.periods import previous_day, previous_iso_week
from grepify.doctor import build_doctor_report, format_doctor_report
from grepify.extract import (
    ExtractPipelineResult,
    YakeFallbackExtractor,
    run_extract_pipeline,
    run_fallback_backfill,
)
from grepify.health import compute_health, write_health_snapshot
from grepify.ingest.orchestrator import IngestServices, build_registry, run_ingest
from grepify.ingest.transcript import TranscriptStore, YouTubeTranscriptApiClient
from grepify.keywords import KeywordRules
from grepify.llm import build_client
from grepify.maintenance import renormalize_summaries
from grepify.models import DigestKind, RunManifest, Source
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.run import latest_manifest, new_run_id, write_manifest
from grepify.site.build import build_site
from grepify.site.trends import TrendQueries, open_cache

app = typer.Typer(add_completion=False, help="grep the firehose - grepify CLI.")
maintain_app = typer.Typer(
    add_completion=False, help="One-time data-remediation commands (not in the cron)."
)
app.add_typer(maintain_app, name="maintain")


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
        settings = config.settings()
        transcript_store = _transcript_store(layout, settings)
        summary = run_ingest(
            IngestServices(
                config=config,
                repository=repository,
                registry=build_registry(transcript_store=transcript_store),
                clock=state.clock,
            ),
            run_id=run_id,
        )
        write_health_snapshot(
            repository.iter_fetch_log(),
            layout,
            run_id=run_id,
            generated_at=to_iso(state.clock.now()),
            quiet_source_ids=_quiet_source_ids(config.sources(), settings),
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
            "sources_skipped": summary.sources_skipped,
            "items_new": summary.items_new,
        },
        durations_ms={"total_ms": summary.duration_ms},
        notes=[f"{r.source_id}: {r.error}" for r in summary.results if r.error],
    )
    write_manifest(layout, manifest)
    typer.echo(
        f"ingest: {summary.sources_ok} ok, {summary.sources_empty} empty, "
        f"{summary.sources_error} error, {summary.sources_skipped} skipped (cadence), "
        f"{summary.items_new} new items; run {run_id}"
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
    endpoints), never from committed config (PRD §5) - same convention as
    ``backfill``. ``--force`` bypasses untagged-item selection entirely and
    re-extracts every item in truth (F-EXT-04) - a deliberate manual escape
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
        # Read-only here (ingest fetches + stores): supplies each youtube item's
        # transcript excerpt to its extraction prompt, or none if absent.
        transcript_store = _transcript_store(layout, settings)
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
            transcript_reader=transcript_store.read,
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


# --- pipeline stubs -----------------------------------------------------------


@app.command()
def trends(ctx: typer.Context) -> None:
    """Compute trend datasets (E3/E4)."""
    _record_stub(ctx, "trends", "E3")


KindOpt = Annotated[
    DigestKind,
    typer.Option(help="Which digest to generate (daily=yesterday, weekly=last ISO week)."),
]


def _real_digest_ids(repository: JsonlSqliteRepository) -> set[str]:
    """Digest ids already in truth, excluding template (degraded) ones.

    Shared by the ``digest`` pipeline's own idempotent skip and ``digest-gate``'s
    existence check (GRP-63): a template digest must not count as present, so a
    later run with a working LLM upgrades it rather than treating the period as
    already done.
    """
    return {d.digest_id for d in repository.iter_digests() if d.model != TEMPLATE_MODEL}


@app.command()
def digest(ctx: typer.Context, kind: KindOpt = DigestKind.DAILY) -> None:
    """Generate per-category digests for the just-completed period (GRP-41/42).

    When ``settings.digest.enabled`` is false this no-ops: it records a manifest
    note and exits 0 without any LLM call or file write (the pause switch used
    while data remediation is in flight). Otherwise it
    rebuilds the cache from truth, then for every enabled category assembles its
    top/rising keywords (GRP-40) and generates one digest via the active LLM
    profile (``purpose='digest'``), degrading to a deterministic template digest
    when the LLM is down or over budget (PRD §9/§13). A category below
    ``settings.digest.min_items`` is skipped (F-DIG-03). Digests key on
    **category**, never user (PRD §7). Reads the LLM endpoint from the active
    profile but resolves the deployment secrets from the environment
    (``LLM_BASE_URL`` required, ``LLM_API_KEY`` optional), never from committed
    config (PRD §5) - same convention as ``extract``. The period boundary is
    America/Edmonton (PRD §5); the injected clock decides which period.

    Daily runs are self-healing: they walk a catch-up window of the last
    ``settings.digest.daily_lookback_days`` completed days and generate any that
    have no digest yet, so a morning run that lands outside the GRP-45 gate window
    (cron jitter) does not permanently drop a day. A period whose digest already
    exists is skipped with no LLM call, so the command is idempotent.
    """
    state: AppState = ctx.obj
    layout = DataLayout(state.data_root)
    run_id = new_run_id(state.clock)
    started_at = to_iso(state.clock.now())

    config = FilesystemConfigProvider(state.config_root)
    settings = config.settings()

    if not settings.digest.enabled:
        manifest = RunManifest(
            run_id=run_id,
            command="digest",
            started_at=started_at,
            finished_at=to_iso(state.clock.now()),
            ok=True,
            counts={
                "categories": 0,
                "digests_generated": 0,
                "categories_skipped": 0,
                "categories_template": 0,
            },
            notes=["paused: settings.digest.enabled is false; no digests generated"],
        )
        write_manifest(layout, manifest)
        typer.echo(
            f"digest ({kind.value}): paused (settings.digest.enabled=false); "
            f"nothing generated; run {run_id}"
        )
        return

    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url:
        typer.echo("digest: LLM_BASE_URL is not set; nothing to do", err=True)
        raise typer.Exit(code=1)

    repository = JsonlSqliteRepository(state.data_root)
    try:
        profile = settings.llm.profiles[settings.llm.active_profile]
        client = build_client(
            profile,
            api_key=os.environ.get("LLM_API_KEY") or None,
            base_url=base_url,
            log_sink=repository.log_llm,
            clock=state.clock,
        )
        rules = KeywordRules.from_config(config.keywords())
        groups = config.groups()
        categories = [g.category for g in groups if g.enabled]

        repository.load_config(groups, config.sources())
        existing_digest_ids = _real_digest_ids(repository)
        repository.rebuild_cache()
        conn = open_cache(layout)
        try:
            queries = TrendQueries(conn, rules)
            summary, digests = run_digest_pipeline(
                queries,
                client,
                categories=categories,
                kind=kind,
                clock=state.clock,
                run_id=run_id,
                settings=settings,
                existing_digest_ids=existing_digest_ids,
            )
        finally:
            conn.close()
        for generated in digests:
            repository.add_digest(generated)
    finally:
        repository.close()

    notes = []
    if summary.skipped_categories:
        notes.append(
            f"skipped (< {settings.digest.min_items} items): "
            + ", ".join(summary.skipped_categories)
        )
    if summary.template_categories:
        notes.append(
            "template fallback (llm down/over budget): " + ", ".join(summary.template_categories)
        )

    manifest = RunManifest(
        run_id=run_id,
        command="digest",
        started_at=started_at,
        finished_at=to_iso(state.clock.now()),
        ok=True,
        counts={
            "categories": len(set(categories)),
            "category_periods_considered": summary.categories_total,
            "digests_generated": summary.digests_generated,
            "digests_already_present": summary.already_present,
            "categories_skipped": len(summary.skipped_categories),
            "categories_template": len(summary.template_categories),
        },
        notes=notes,
    )
    write_manifest(layout, manifest)
    typer.echo(
        f"digest ({kind.value}, {summary.period_key}): {summary.digests_generated} generated, "
        f"{summary.already_present} already present, "
        f"{len(summary.skipped_categories)} skipped; run {run_id}"
    )


@app.command(name="digest-gate")
def digest_gate_command(ctx: typer.Context) -> None:
    """Print ``daily=/weekly=`` flags: are digest steps due now? (GRP-45/GRP-63).

    America/Edmonton-pinned, DST-aware pure gate (:func:`grepify.digest.digest_gate`)
    over the injected clock and today's digest existence: a step is due once local
    time is at or past the morning opening and its own period's digest is not yet
    in truth (GRP-63), so a later run naturally retries a missed morning window.
    Output is valid to append to ``$GITHUB_OUTPUT`` or to ``eval`` into shell vars -
    it replaces the coarse ``scripts/digest-gate.sh`` placeholder the GRP-06
    workflows shipped.
    """
    state: AppState = ctx.obj
    now = state.clock.now()
    config = FilesystemConfigProvider(state.config_root)
    categories = {g.category for g in config.groups() if g.enabled}

    repository = JsonlSqliteRepository(state.data_root)
    try:
        real_ids = _real_digest_ids(repository)
    finally:
        repository.close()

    daily_key = previous_day(now).key
    weekly_key = previous_iso_week(now).key
    daily_exists = all(
        digest_id_for(DigestKind.DAILY, c, daily_key) in real_ids for c in categories
    )
    weekly_exists = all(
        digest_id_for(DigestKind.WEEKLY, c, weekly_key) in real_ids for c in categories
    )

    gate = digest_gate(now, daily_exists=daily_exists, weekly_exists=weekly_exists)
    typer.echo(format_gate(gate))


OutputDirOpt = Annotated[Path, typer.Option(help="Output directory for the rendered site.")]
BasePathOpt = Annotated[
    str,
    typer.Option(
        envvar="GREPIFY_BASE_PATH",
        help="Root path the site is served under (e.g. '/grepify/' for project Pages).",
    ),
]


@app.command()
def build(
    ctx: typer.Context,
    output_dir: OutputDirOpt = Path("public"),
    base_path: BasePathOpt = "/",
) -> None:
    """Render the static site into ``public/`` from the cache + config (GRP-35).

    Rebuilds the SQLite cache from JSONL truth, then renders the home, items
    browser (trailing-90d, near-dup collapsed, client-filterable), sources, and
    health pages plus the tokenised stylesheet. Pure function of cache + config
    + the injected clock (F-SIT-08) - no network, no LLM. ``--base-path``
    prefixes every internal link so the same build works at a project-Pages
    sub-path or a root deploy (read from ``GREPIFY_BASE_PATH`` in CI).
    """
    state: AppState = ctx.obj
    layout = DataLayout(state.data_root)
    run_id = new_run_id(state.clock)
    started_at = to_iso(state.clock.now())

    config = FilesystemConfigProvider(state.config_root)
    repository = JsonlSqliteRepository(state.data_root)
    try:
        result = build_site(
            config=config,
            repository=repository,
            layout=layout,
            clock=state.clock,
            run_id=run_id,
            output_dir=output_dir,
            base_path=base_path,
        )
    finally:
        repository.close()

    manifest = RunManifest(
        run_id=run_id,
        command="build",
        started_at=started_at,
        finished_at=to_iso(state.clock.now()),
        ok=True,
        counts={
            "pages_written": result.pages_written,
            "items_emitted": result.items_emitted,
            "items_total": result.items_total,
        },
    )
    write_manifest(layout, manifest)
    typer.echo(
        f"build: {result.pages_written} pages, {result.items_emitted} items emitted "
        f"(of {result.items_total} total) → {result.output_dir}; run {run_id}"
    )


BackfillMaxCallsOpt = Annotated[
    int, typer.Option(help="Cap on real LLM calls this run (playbook S7 recommends 200).")
]


@app.command()
def backfill(ctx: typer.Context, max_calls: BackfillMaxCallsOpt = 200) -> None:
    """Re-extract items whose keywords are entirely ``method='fallback'`` (GRP-22).

    Manual/one-time command - not wired into the pipeline cron (GRP-25). Reads
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


@maintain_app.command(name="renormalize")
def maintain_renormalize(ctx: typer.Context) -> None:
    """Re-clean stored summaries and re-extract the items that changed (GRP-60).

    One-time data remediation, not wired into the pipeline cron. Re-applies the
    current summary cleaner (:func:`grepify.ingest.normalize.clean_summary`) to
    every stored item, rewrites the ones whose summary changed to truth, drops
    those items' stale keyword rows, then force re-extracts *just* those items so
    their keywords regenerate from the corrected text. Reads the LLM endpoint from
    ``settings.yml``'s active profile but resolves deployment secrets from the
    environment (``LLM_BASE_URL`` required, ``LLM_API_KEY`` optional), never from
    committed config (PRD §5) - same convention as ``extract``. Idempotent: on an
    already-clean corpus it rewrites nothing and re-extracts nothing. The summary
    rewrite + keyword-row drop happen before the re-extract; if the re-extract is
    interrupted, the changed items are left cleaned but untagged and a plain
    ``grepify extract`` (untagged selection) finishes the job - exactly what the
    O1 remediation procedure runs next.
    """
    state: AppState = ctx.obj
    layout = DataLayout(state.data_root)
    run_id = new_run_id(state.clock)
    started_at = to_iso(state.clock.now())

    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url:
        typer.echo("maintain renormalize: LLM_BASE_URL is not set; nothing to do", err=True)
        raise typer.Exit(code=1)

    config = FilesystemConfigProvider(state.config_root)
    repository = JsonlSqliteRepository(state.data_root)
    written = 0
    reextract: ExtractPipelineResult | None = None
    try:
        settings = config.settings()
        result = renormalize_summaries(repository)
        if result.changed_item_ids:
            profile = settings.llm.profiles[settings.llm.active_profile]
            client = build_client(
                profile,
                api_key=os.environ.get("LLM_API_KEY") or None,
                base_url=base_url,
                log_sink=repository.log_llm,
                clock=state.clock,
            )
            rules = KeywordRules.from_config(config.keywords())
            changed_ids = set(result.changed_item_ids)
            changed_items = [i for i in repository.iter_items() if i.item_id in changed_ids]
            transcript_store = _transcript_store(layout, settings)
            # force=True + existing=[]: the changed items are already untagged
            # (their rows were just deleted); re-extract exactly this set.
            reextract, new_keywords = run_extract_pipeline(
                changed_items,
                [],
                client,
                run_id=run_id,
                clock=state.clock,
                fallback=YakeFallbackExtractor(),
                rules=rules,
                force=True,
                max_items_per_call=settings.llm.max_items_per_call,
                transcript_reader=transcript_store.read,
            )
            written = repository.add_item_keywords(new_keywords)
    finally:
        repository.close()

    notes = []
    if reextract and reextract.no_keywords_item_ids:
        notes.append(
            "items with no keywords after re-extraction: "
            + ", ".join(reextract.no_keywords_item_ids)
        )
    if reextract and reextract.budget_exhausted:
        notes.append("llm budget exhausted before all changed items were re-extracted")

    manifest = RunManifest(
        run_id=run_id,
        command="maintain-renormalize",
        started_at=started_at,
        finished_at=to_iso(state.clock.now()),
        ok=True,
        counts={
            "items_scanned": result.items_scanned,
            "items_rewritten": result.items_rewritten,
            "keyword_rows_deleted": result.keyword_rows_deleted,
            "items_reextracted": reextract.items_selected if reextract else 0,
            "keywords_written": written,
        },
        notes=notes,
    )
    write_manifest(layout, manifest)
    typer.echo(
        f"maintain renormalize: {result.items_rewritten} summaries rewritten, "
        f"{result.keyword_rows_deleted} keyword rows dropped, {written} re-extracted "
        f"keyword rows written; run {run_id}"
    )


# --- other wired commands -----------------------------------------------------


@app.command()
def validate(ctx: typer.Context) -> None:
    """Schema-validate config; exit non-zero if invalid (CI gate on every MR).

    Also rejects any enabled source whose ``kind`` has no fetcher in the
    production registry (GRP-56) - the same registry ``ingest`` builds, so
    this reports exactly what ``ingest`` would otherwise only discover
    per-source at run time.
    """
    state: AppState = ctx.obj
    provider = FilesystemConfigProvider(state.config_root)
    report = provider.validate(registered_kinds=build_registry().registered_kinds())
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


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Per-source fetch status + error-class triage report (T5, GRP-30).

    Joins the configured sources with a fresh recompute over ``fetch_log``
    (:mod:`grepify.doctor`) - repeatable and read-only, so re-running it as
    triage progresses always reflects current config + current truth without
    needing a prior ``ingest`` to have written ``health.json``.
    """
    state: AppState = ctx.obj
    config = FilesystemConfigProvider(state.config_root)
    repository = JsonlSqliteRepository(state.data_root)
    try:
        sources = config.sources()
        settings = config.settings()
        entries = list(repository.iter_fetch_log())
    finally:
        repository.close()

    snapshot = compute_health(
        entries,
        run_id="doctor",
        generated_at=to_iso(state.clock.now()),
        quiet_source_ids=_quiet_source_ids(sources, settings),
    )
    rows = build_doctor_report(sources, snapshot)
    typer.echo(format_doctor_report(rows))


WarnBytesOpt = Annotated[
    int, typer.Option(help="Warn threshold in bytes (GRP-63; default 100 MB).")
]
FailBytesOpt = Annotated[
    int, typer.Option(help="Fail threshold in bytes (GRP-63; default 200 MB).")
]


@app.command()
def datasize(
    ctx: typer.Context,
    warn_bytes: WarnBytesOpt = DEFAULT_WARN_BYTES,
    fail_bytes: FailBytesOpt = DEFAULT_FAIL_BYTES,
) -> None:
    """Data-branch size guardrail: sum items/keywords JSONL + transcripts (GRP-63).

    Read-only directory-size arithmetic over the data root
    (:mod:`grepify.datasize`) - no config, no LLM, no manifest write - so it is
    cheap to run every pipeline invocation ahead of the network/LLM steps.
    Prints one summary line and exits 0 under ``--warn-bytes``, 0 with a
    ``WARN`` line in the warn band, and non-zero at/over ``--fail-bytes``. See
    :mod:`grepify.datasize` for the full threshold contract and the documented
    parquet-compaction escape hatch for when the fail threshold is actually hit.
    """
    state: AppState = ctx.obj
    layout = DataLayout(state.data_root)
    report = compute_data_size(layout, warn_bytes=warn_bytes, fail_bytes=fail_bytes)
    typer.echo(format_report(report))
    if report.level is SizeLevel.FAIL:
        raise typer.Exit(code=1)


# --- helpers ----------------------------------------------------------------


def _quiet_source_ids(sources: list[Source], settings: SettingsConfig) -> set[str]:
    """Source ids whose kind is in ``settings.ingest.quiet_kinds`` (T6, GRP-31) -
    the health snapshot never flags these on consecutive failures."""
    quiet_kinds = set(settings.ingest.quiet_kinds)
    return {s.source_id for s in sources if s.kind in quiet_kinds}


def _transcript_store(layout: DataLayout, settings: SettingsConfig) -> TranscriptStore:
    """A transcript store wired to the real youtube-transcript-api client and the
    PRD §7 transcript caps/languages. The client imports its library lazily and
    degrades to null refs when the optional ``transcripts`` extra is absent, so
    this is safe to build unconditionally (E5). Used for fetch+store in
    ``ingest`` and read-only excerpting in ``extract``."""
    return TranscriptStore(
        layout,
        YouTubeTranscriptApiClient(),
        max_chars=settings.limits.transcript_max_chars,
        languages=settings.limits.transcript_langs,
    )


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
