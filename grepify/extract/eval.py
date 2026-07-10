"""Offline eval harness (GRP-24, PRD §10.5): jaccard scoring against a labeled set.

Not part of `make check` or any CI gate - run manually (`make eval`) when the
extract prompt or LLM profile changes, and paste the printed report into the
MR description (PRD §10.5: "no silent prompt regressions"). This module holds
the pure, offline-testable half of the harness: the fixture model, the
jaccard scorer, and the report formatter. It has no LLM/config/repository
dependency and performs no network I/O - driving the real extraction
pipeline against the fixture is `scripts/eval.py`'s job (it is not
worth unit-testing without a network, so it stays a thin script, mirroring
`scripts/commit_pipeline_data.py`).

The fixture (`tests/fixtures/eval/keyword_eval_candidates.jsonl`) is 30 real
ingested items (title + summary), hand-curated once and committed as a fixed
test fixture - not pipeline truth. `expected_keywords` starts empty per item
(a manual labeling task, playbook S7k); unlabeled items are skipped from the
mean score but still show their predicted keywords in the report.

Failure modes
--------------
:func:`load_eval_cases` raises ``ValueError`` (wrapping the underlying
``pydantic.ValidationError`` or ``json.JSONDecodeError``, with the offending
line number) for a malformed fixture line - a bad hand-edit from a phone
editor, not a runtime concern, so it is left to propagate rather than
silently skipped or defaulted. Every other function here is a pure
transform over already-valid :class:`EvalCase` / :class:`~grepify.models.ItemKeyword`
data and raises nothing of its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from grepify.clock import Clock, to_iso
from grepify.keywords import normalize_keyword
from grepify.models import Item, ItemKeyword, SourceKind


class EvalCase(BaseModel):
    """One row of the eval fixture. Not a PRD §6 storage record - a test
    fixture only :mod:`grepify.extract.eval` and `scripts/eval.py` read."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    summary: str | None = None
    expected_keywords: list[str] = Field(default_factory=list)


def load_eval_cases(path: Path) -> list[EvalCase]:
    """Parse one JSON object per non-blank line of ``path``.

    See the module docstring's failure-modes note: a malformed line raises
    ``ValueError`` naming the offending line number rather than skipping it.
    """
    cases: list[EvalCase] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            cases.append(EvalCase.model_validate_json(line))
        except ValidationError as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
    return cases


def jaccard_similarity(predicted: Iterable[str], expected: Iterable[str]) -> float:
    """Jaccard overlap between two keyword sets, after normalizing both sides
    (:func:`grepify.keywords.normalize_keyword`) so a labeler's casing/
    whitespace never costs a point against already-normalized predictions.
    Two empty sets are defined as a perfect match (``1.0``); an empty set
    against a non-empty one is ``0.0`` (their union is non-empty, intersection
    empty).
    """
    predicted_set = {normalize_keyword(k) for k in predicted}
    expected_set = {normalize_keyword(k) for k in expected}
    union = predicted_set | expected_set
    if not union:
        return 1.0
    return len(predicted_set & expected_set) / len(union)


def eval_cases_to_items(cases: Sequence[EvalCase], *, clock: Clock) -> list[Item]:
    """Wrap eval cases as :class:`~grepify.models.Item` so they can be driven
    through the real extraction pipeline unmodified. An eval case is never
    written to truth, so the storage fields extraction itself ignores
    (``source_id``, ``kind``, ``canonical_url``, ``content_hash``) only need
    to be well-typed placeholders, not meaningful.
    """
    now = to_iso(clock.now())
    return [
        Item(
            item_id=case.item_id,
            source_id="eval",
            kind=SourceKind.RSS,
            canonical_url=f"eval:{case.item_id}",
            title=case.title,
            summary=case.summary,
            published_at=now,
            fetched_at=now,
            content_hash=f"eval-{case.item_id}",
        )
        for case in cases
    ]


def group_keywords_by_item(keywords: Sequence[ItemKeyword]) -> dict[str, list[str]]:
    """Group keyword rows by ``item_id``, each list ordered by ``rank``
    (most-salient first, per F-EXT-01)."""
    by_item: dict[str, list[ItemKeyword]] = {}
    for row in keywords:
        by_item.setdefault(row.item_id, []).append(row)
    return {
        item_id: [row.keyword for row in sorted(rows, key=lambda r: r.rank)]
        for item_id, rows in by_item.items()
    }


@dataclass(frozen=True)
class EvalCaseScore:
    """One scored (or still-unlabeled) fixture row."""

    item_id: str
    title: str
    predicted: list[str]
    expected: list[str]
    score: float | None  # None iff expected_keywords was empty (unlabeled/TODO)


@dataclass(frozen=True)
class EvalReport:
    """Whole-run rollup: per-case scores plus the aggregate mean."""

    cases: list[EvalCaseScore]
    labeled_count: int
    unlabeled_count: int
    mean_score: float | None  # None iff no case in the fixture is labeled yet


def score_predictions(
    cases: Sequence[EvalCase], predicted_by_id: Mapping[str, Sequence[str]]
) -> EvalReport:
    """Score each labeled case's predicted keywords against its
    ``expected_keywords`` via :func:`jaccard_similarity`; unlabeled cases
    (``expected_keywords == []``) are recorded with ``score=None`` and
    excluded from :attr:`EvalReport.mean_score`, not treated as a score of 0.
    """
    scored: list[EvalCaseScore] = []
    labeled_scores: list[float] = []
    for case in cases:
        predicted = list(predicted_by_id.get(case.item_id, []))
        if not case.expected_keywords:
            scored.append(EvalCaseScore(case.item_id, case.title, predicted, [], None))
            continue
        score = jaccard_similarity(predicted, case.expected_keywords)
        labeled_scores.append(score)
        scored.append(
            EvalCaseScore(case.item_id, case.title, predicted, list(case.expected_keywords), score)
        )
    mean = sum(labeled_scores) / len(labeled_scores) if labeled_scores else None
    return EvalReport(
        cases=scored,
        labeled_count=len(labeled_scores),
        unlabeled_count=len(scored) - len(labeled_scores),
        mean_score=mean,
    )


def format_report(report: EvalReport, *, heading: str = "grepify eval") -> str:
    """Render ``report`` as Markdown, ready to paste into an MR description
    (PRD §10.5 - eval results are pasted manually, never gated in CI)."""
    lines = [f"## {heading}"]
    if report.mean_score is None:
        lines.append(
            f"mean jaccard: n/a (0/{len(report.cases)} labeled - "
            "label tests/fixtures/eval/keyword_eval_candidates.jsonl for a baseline)"
        )
    else:
        lines.append(
            f"mean jaccard: {report.mean_score:.3f} "
            f"({report.labeled_count} labeled / {report.unlabeled_count} unlabeled)"
        )
    lines.append("")
    lines.append("| title | jaccard | predicted | expected |")
    lines.append("|---|---|---|---|")
    for case in report.cases:
        score_text = f"{case.score:.3f}" if case.score is not None else "TODO"
        lines.append(
            f"| {_truncate_title(case.title)} | {score_text} | {', '.join(case.predicted)} | "
            f"{', '.join(case.expected)} |"
        )
    return "\n".join(lines)


_TITLE_COL_MAX = 60


def _truncate_title(title: str) -> str:
    """Keep the report's title column readable; a bare item_id (the previous
    behavior) forces a reader to cross-reference the fixture for every row,
    defeating the point of a human-pasteable MR report (PRD §10.5)."""
    if len(title) <= _TITLE_COL_MAX:
        return title
    return title[: _TITLE_COL_MAX - 1].rstrip() + "…"
