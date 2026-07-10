"""Single-entrypoint CLI (PRD §8 F-OPS-01): ``grepify <subcommand>``.

Subcommands: ``ingest extract trends digest build validate health backfill``,
plus ``maintain renormalize`` (GRP-60 data remediation).
``ingest`` is wired to the E1 orchestrator (GRP-15/16); ``extract`` is wired to
the E2 pipeline (GRP-25: untagged-item selection, real LLM client, keyword
normalization, PRD §10.7 data-quality gate); ``validate`` is fully wired to
the config layer; ``backfill`` re-extracts ``method='fallback'`` rows through
a real LLM client (GRP-22). ``build`` is wired to the E3 site renderer
(GRP-35: cache rebuild → Jinja SSG → ``public/``). ``digest`` is wired to the
E4 digest pipeline (GRP-41/42: rebuild cache → assemble per category → one LLM
call each → store), with ``digest-gate`` printing the America/Edmonton
time-of-day gate (GRP-45). ``trends`` remains a stub that records a run
manifest so the operator tooling (``health``) works end to end.
(``backfill``'s scope here is GRP-22's fallback-only re-extraction.)
``maintain renormalize`` (GRP-60) is a one-time data-remediation command:
re-clean stored summaries, rewrite the changed items to truth, drop their stale
keyword rows, and force re-extract just those items. Broader maintenance modes
(reindex, vacuum, prune) are later work behind the same ``maintain`` group.

Failure modes
-------------
- ``validate`` exits non-zero when config is invalid (CI gate on every MR).
- ``ingest`` never fails the process for a single dead source (isolated in the
  orchestrator, PRD §9); it only propagates systemic failures (bad config,
  unreadable truth).
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
- ``digest-gate`` is a pure clock read; it never fails.
- The ``trends`` stub never fails the process; it writes a manifest noting the
  not-yet-implemented epic and returns 0.
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
from grepify.config.schemas import SettingsConfig
from grepify.digest import digest_gate, format_gate, run_digest_pipeline
from grepify.extract import (
    ExtractPipelineResult,
    YakeFallbackExtractor,
    run_extract_pipeline,
    run_fallback_backfill,
)
from grepify.health import write_health_snapshot
from grepify.ingest.orchestrator import IngestServices, build_registry, run_ingest
from grepify.ingest.transcript import TranscriptStore, YouTubeTranscriptApiClient
from grepify.keywords import KeywordRules
from grepify.llm import build_client
from grepify.maintenance import renormalize_summaries
from grepify.models import DigestKind, RunManifest
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
        # E5 transcripts (GRP-52), best-effort and absence-tolerant (PRD §13):
        # the store degrades to null refs when youtube-transcript-api is absent
        # or a transcript can't be fetched, so it is safe to wire unconditionally.
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
        # GRP-53: feed each youtube item's stored transcript excerpt (<=1500
        # chars) into its extraction prompt. Read-only here (ingest fetches +
        # stores); a missing/unreadable blob just yields no excerpt (PRD §13).
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

    # T1 pause switch (settings.digest.enabled): freeze generation during data
    # remediation. When off, no LLM calls and no digest files - just a manifest
    # note so the paused run is on the record, and exit 0 (not an error).
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
        # Digests already in truth are skipped (no LLM call) so the catch-up run
        # is idempotent - it only generates the genuinely-missing periods (T3).
        existing_digest_ids = {d.digest_id for d in repository.iter_digests()}
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
            "categories": summary.categories_total,
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
    """Print ``daily=/weekly=`` flags: are digest steps due now? (GRP-45).

    America/Edmonton-pinned, DST-aware pure gate (:func:`grepify.digest.digest_gate`)
    over the injected clock. Output is valid to append to ``$GITHUB_OUTPUT`` or to
    ``eval`` into shell vars - it replaces the coarse ``scripts/digest-gate.sh``
    placeholder the GRP-06 workflows shipped.
    """
    state: AppState = ctx.obj
    typer.echo(format_gate(digest_gate(state.clock.now())))


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
