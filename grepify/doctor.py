"""``doctor`` report: per-source status + error-class triage view (T5, GRP-30).

Joins the current config's source list (id, kind, group, ``enabled``) with the
fetch-log-derived :class:`~grepify.health.HealthSnapshot` (last status, error
class, consecutive failures) into one flat, deterministic table, so a feed
triage pass ("is this source dead? fixed? already disabled?") never has to be
redone by hand by re-reading raw fetch_log rows. It is read-only and makes no
disable/enable decisions itself - see the module docstring on
:mod:`grepify.health` for the ``ErrorClass`` bucketing this report displays;
disabling a source is still a deliberate config-file edit (PRD §2 Non-Goals:
v1 has no auto-disable).

Repeatability
-------------
:func:`build_doctor_report` is a pure join over its two inputs, sorted by
``source_id``, so running it twice against the same truth produces an
identical report - the ``doctor`` CLI command (:mod:`grepify.cli`) recomputes
it fresh from ``fetch_log`` on every invocation rather than depending on a
previously-written ``health.json``, so it stays accurate even if ``ingest``
has not run in the current environment.

Failure modes
-------------
Pure functions of their inputs - neither raises. A source with no fetch_log
history at all (never yet attempted, e.g. freshly added) still gets a row,
with ``last_status=None`` / ``error_class=None`` rather than being omitted.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from grepify.health import ErrorClass, HealthSnapshot
from grepify.models import FetchStatus, Source


class DoctorRow(BaseModel):
    """One source's triage row (T5, GRP-30)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    kind: str
    group_id: str
    enabled: bool
    last_status: FetchStatus | None = None
    error_class: ErrorClass | None = None
    consecutive_failures: int = 0
    flagged: bool = False
    last_error: str | None = None


def build_doctor_report(sources: list[Source], snapshot: HealthSnapshot) -> list[DoctorRow]:
    """Join ``sources`` (config truth) with ``snapshot`` (fetch-log truth).

    Sorted by ``source_id`` for a deterministic, diffable report.
    """
    by_id = {health.source_id: health for health in snapshot.sources}
    rows = []
    for source in sorted(sources, key=lambda s: s.source_id):
        health = by_id.get(source.source_id)
        rows.append(
            DoctorRow(
                source_id=source.source_id,
                kind=source.kind.value,
                group_id=source.group_id,
                enabled=source.enabled,
                last_status=health.last_status if health else None,
                error_class=health.error_class if health else None,
                consecutive_failures=health.consecutive_failures if health else 0,
                flagged=health.flagged if health else False,
                last_error=health.last_error if health else None,
            )
        )
    return rows


def format_doctor_report(rows: list[DoctorRow]) -> str:
    """Render ``rows`` as a fixed, deterministic pipe-delimited text table -
    readable in a phone-sized terminal, no table library required. Leads with
    a one-line summary count so the flagged/error tally is visible without
    scrolling."""
    if not rows:
        return "no sources configured"

    errored = sum(1 for row in rows if row.last_status is FetchStatus.ERROR)
    flagged = sum(1 for row in rows if row.flagged)
    summary = f"{len(rows)} sources, {errored} last-run error, {flagged} flagged (>=5 consecutive)"

    header = "source_id | kind | group | enabled | status | error_class | streak | last_error"
    lines = [summary, header]
    for row in rows:
        lines.append(
            " | ".join(
                [
                    row.source_id,
                    row.kind,
                    row.group_id,
                    "yes" if row.enabled else "no",
                    row.last_status.value if row.last_status else "never-fetched",
                    row.error_class.value if row.error_class else "-",
                    str(row.consecutive_failures),
                    row.last_error or "-",
                ]
            )
        )
    return "\n".join(lines)
