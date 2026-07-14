"""``doctor`` report: per-source status + error-class triage, plus transition
proposals (T5 GRP-30; ADR 0002 §3, GRP-66).

Joins the current config's source list (id, kind, group, ``enabled``, lifecycle
``status``) with the fetch-log-derived
:class:`~grepify.health.HealthSnapshot` (last status, error class, consecutive
failures, served rung) into one flat, deterministic table, so a feed triage
pass ("is this source dead? recovered? already classified?") never has to be
redone by hand by re-reading raw fetch_log rows. It is read-only and makes no
disable/enable/classification decisions itself - see the module docstring on
:mod:`grepify.health` for the ``ErrorClass`` bucketing this report displays.

Transition proposals (ADR 0002 §3)
-----------------------------------
:func:`propose_transition` is a pure function of ``(current SourceStatus,
SourceHealth row, quiet)`` that applies the ADR transition rules and returns the
lifecycle class the evidence justifies, or ``None`` when nothing crosses a
threshold. ``--propose`` (:func:`format_propose_patch`) renders the crossings as
a minimal, reviewable YAML patch grouped by group file; the maintainer reads the
diff, edits config, and commits. Nothing is auto-applied (PRD §2 v1 has no
auto-disable): the ToS/paywall judgment in particular is deliberately left to a
human, so ``paywalled`` is only ever *hinted*, never asserted. Reddit and any
other ``quiet_kinds`` source is exempt from down transitions (its 429/403 from
CI IPs is expected, not evidence the subreddit is gone).

Repeatability
-------------
:func:`build_doctor_report` is a pure join over its two inputs, sorted by
``source_id``, so running it twice against the same truth produces an identical
report - the ``doctor`` CLI recomputes it fresh from ``fetch_log`` on every
invocation rather than depending on a previously-written ``health.json``.

Failure modes
-------------
Pure functions of their inputs - none raise. A source with no fetch_log history
at all (never yet attempted) still gets a row, with ``last_status=None`` and no
proposal.
"""

from __future__ import annotations

from collections.abc import Iterable

import yaml
from pydantic import BaseModel, ConfigDict

from grepify.health import ErrorClass, HealthSnapshot, SourceHealth
from grepify.models import FetchStatus, Rung, Source, SourceStatus

DEAD_THRESHOLD = 16  # ADR 0002 §2: consecutive full-ladder failures before proposing `dead`


class DoctorRow(BaseModel):
    """One source's triage row (T5, GRP-30; lifecycle proposal, GRP-66)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    kind: str
    group_id: str
    enabled: bool
    status: SourceStatus
    last_status: FetchStatus | None = None
    last_rung: Rung | None = None
    error_class: ErrorClass | None = None
    consecutive_failures: int = 0
    flagged: bool = False
    last_error: str | None = None
    proposed_status: SourceStatus | None = None
    reason: str | None = None


def _looks_gone(error: str | None) -> bool:
    return error is not None and ("404" in error or "410" in error)


def _looks_paywalled(error: str | None) -> bool:
    return error is not None and "402" in error


def propose_transition(
    current: SourceStatus, health: SourceHealth | None, *, quiet: bool
) -> tuple[SourceStatus, str] | None:
    """The lifecycle transition the evidence justifies, or ``None`` (ADR 0002 §2).

    Recovery/degrade transitions apply to any source; the down transitions
    (``-> dead`` / ``-> gone``) are suppressed for ``quiet`` sources. ``paywalled``
    is only ever hinted (HTTP 402), never asserted, because it is a ToS judgment
    a human must make.
    """
    if health is None or health.last_status is None:
        return None
    last = health.last_status
    rung = health.last_rung
    served = last in (FetchStatus.OK, FetchStatus.EMPTY)

    if current is SourceStatus.DEGRADED and served and (rung is None or rung is Rung.DIRECT):
        return SourceStatus.ACTIVE, "rung 0 (direct) served again - recovered from degraded"
    if current is SourceStatus.ACTIVE and served and rung is not None and rung.is_fallback:
        return SourceStatus.DEGRADED, f"served from fallback rung {rung.value!r} - primary failing"
    if current is SourceStatus.DEAD and served:
        via = rung.value if rung is not None else "direct"
        target = (
            SourceStatus.DEGRADED if rung is not None and rung.is_fallback else SourceStatus.ACTIVE
        )
        return target, f"dead re-check succeeded via rung {via!r}"

    down_eligible = (
        not quiet
        and last is FetchStatus.ERROR
        and current in (SourceStatus.ACTIVE, SourceStatus.DEGRADED)
    )
    if not down_eligible:
        return None
    if _looks_paywalled(health.last_error):
        return SourceStatus.PAYWALLED, "HTTP 402 - hint only; a human must confirm the ToS status"
    if health.consecutive_failures >= DEAD_THRESHOLD:
        if _looks_gone(health.last_error):
            return (
                SourceStatus.GONE,
                f"{health.consecutive_failures} consecutive failures, target returns 404/410 "
                "(no longer exists) - remove from the group file",
            )
        return (
            SourceStatus.DEAD,
            f"{health.consecutive_failures} consecutive failures across the full ladder",
        )
    return None


def build_doctor_report(
    sources: list[Source],
    snapshot: HealthSnapshot,
    *,
    quiet_source_ids: Iterable[str] = (),
) -> list[DoctorRow]:
    """Join ``sources`` (config truth) with ``snapshot`` (fetch-log truth).

    Sorted by ``source_id`` for a deterministic, diffable report. Each row
    carries the transition :func:`propose_transition` would apply, if any.
    """
    by_id = {health.source_id: health for health in snapshot.sources}
    quiet = frozenset(quiet_source_ids)
    rows = []
    for source in sorted(sources, key=lambda s: s.source_id):
        health = by_id.get(source.source_id)
        proposal = propose_transition(source.status, health, quiet=source.source_id in quiet)
        rows.append(
            DoctorRow(
                source_id=source.source_id,
                kind=source.kind.value,
                group_id=source.group_id,
                enabled=source.enabled,
                status=source.status,
                last_status=health.last_status if health else None,
                last_rung=health.last_rung if health else None,
                error_class=health.error_class if health else None,
                consecutive_failures=health.consecutive_failures if health else 0,
                flagged=health.flagged if health else False,
                last_error=health.last_error if health else None,
                proposed_status=proposal[0] if proposal else None,
                reason=proposal[1] if proposal else None,
            )
        )
    return rows


def format_doctor_report(rows: list[DoctorRow]) -> str:
    """Render ``rows`` as a fixed, deterministic pipe-delimited text table -
    readable in a phone-sized terminal, no table library required. Leads with a
    one-line summary count so the flagged/error/proposal tally is visible
    without scrolling."""
    if not rows:
        return "no sources configured"

    errored = sum(1 for row in rows if row.last_status is FetchStatus.ERROR)
    flagged = sum(1 for row in rows if row.flagged)
    proposed = sum(1 for row in rows if row.proposed_status is not None)
    summary = (
        f"{len(rows)} sources, {errored} last-run error, {flagged} flagged "
        f"(>=5 consecutive), {proposed} transitions proposed"
    )

    header = (
        "source_id | kind | group | status | last | rung | error_class | streak | "
        "proposed | last_error"
    )
    lines = [summary, header]
    for row in rows:
        lines.append(
            " | ".join(
                [
                    row.source_id,
                    row.kind,
                    row.group_id,
                    row.status.value,
                    row.last_status.value if row.last_status else "never-fetched",
                    row.last_rung.value if row.last_rung else "-",
                    row.error_class.value if row.error_class else "-",
                    str(row.consecutive_failures),
                    row.proposed_status.value if row.proposed_status else "-",
                    row.last_error or "-",
                ]
            )
        )
    return "\n".join(lines)


def format_propose_patch(rows: list[DoctorRow]) -> str:
    """Render the proposed transitions as a reviewable YAML patch (ADR 0002 §3).

    Grouped by group file, one entry per crossing, with the current and proposed
    class and the evidence. A ``-> gone`` proposal is a removal (``action:
    remove``); everything else sets ``status``/``evidence``. This is a
    suggestion artifact - the maintainer edits config and commits, doctor never
    writes. Deterministic (sorted) so it diffs cleanly run to run.
    """
    proposed = [row for row in rows if row.proposed_status is not None]
    if not proposed:
        return "no transitions proposed"

    patch: dict[str, list[dict[str, str]]] = {}
    for row in sorted(proposed, key=lambda r: (r.group_id, r.source_id)):
        assert row.proposed_status is not None  # noqa: S101 - filtered above; narrows for mypy
        patch.setdefault(row.group_id, []).append(
            {
                "id": row.source_id,
                "action": "remove"
                if row.proposed_status is SourceStatus.GONE
                else "set-status",
                "current": row.status.value,
                "proposed": row.proposed_status.value,
                "evidence": row.reason or "",
            }
        )

    header = (
        "# grepify doctor --propose: review, edit sources/groups/*.yml, commit. "
        "Not auto-applied.\n"
    )
    return header + yaml.safe_dump(patch, sort_keys=True, default_flow_style=False)
