"""GRP-24: eval harness — jaccard scorer, fixture loading, report formatting.

Canned labeled data only (PRD §10.5's own instruction: the scoring logic is
unit-tested without waiting on real human labels). The one exception is
`test_committed_fixture_is_30_valid_real_items`, which loads the actual
committed `tests/fixtures/eval/keyword_eval_candidates.jsonl` to guard the
fixture's shape (30 rows, unique item_ids, non-empty title/summary) —
independent of whether it has been labeled yet.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grepify.clock import FixedClock
from grepify.extract.eval import (
    EvalCase,
    eval_cases_to_items,
    format_report,
    group_keywords_by_item,
    jaccard_similarity,
    load_eval_cases,
    score_predictions,
)
from grepify.models import SourceKind
from tests.conftest import FIXTURES_DIR, make_keyword

_CLOCK = FixedClock(datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC))
_FIXTURE = FIXTURES_DIR / "eval" / "keyword_eval_candidates.jsonl"


def _case(item_id: str, expected: list[str] | None = None) -> EvalCase:
    return EvalCase(
        item_id=item_id,
        title=f"title {item_id}",
        summary="a summary",
        expected_keywords=expected or [],
    )


# --- jaccard_similarity --------------------------------------------------------


def test_identical_sets_score_one() -> None:
    assert jaccard_similarity(["genai", "openai"], ["genai", "openai"]) == 1.0


def test_disjoint_sets_score_zero() -> None:
    assert jaccard_similarity(["genai"], ["dbt"]) == 0.0


def test_partial_overlap_is_intersection_over_union() -> None:
    # {genai, openai} vs {genai, anthropic} -> intersection 1, union 3
    assert jaccard_similarity(["genai", "openai"], ["genai", "anthropic"]) == pytest.approx(1 / 3)


def test_both_empty_is_a_perfect_match() -> None:
    assert jaccard_similarity([], []) == 1.0


def test_one_empty_one_nonempty_scores_zero() -> None:
    assert jaccard_similarity([], ["genai"]) == 0.0
    assert jaccard_similarity(["genai"], []) == 0.0


def test_scoring_normalizes_casing_and_whitespace_before_comparing() -> None:
    assert jaccard_similarity(["  Gen   AI!! "], ["gen ai"]) == 1.0


def test_duplicate_keywords_do_not_inflate_the_union() -> None:
    assert jaccard_similarity(["genai", "genai", "openai"], ["genai", "openai"]) == 1.0


# --- load_eval_cases ------------------------------------------------------------


def test_loads_one_case_per_nonblank_line(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"item_id": "a", "title": "T1", "summary": "S1", "expected_keywords": ["x"]}\n'
        "\n"  # blank line tolerated
        '{"item_id": "b", "title": "T2", "summary": null, "expected_keywords": []}\n',
        encoding="utf-8",
    )
    cases = load_eval_cases(path)
    assert [c.item_id for c in cases] == ["a", "b"]
    assert cases[0].expected_keywords == ["x"]
    assert cases[1].summary is None


def test_malformed_line_raises_value_error_naming_the_line(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"item_id": "a", "title": "T1", "summary": "S1", "expected_keywords": []}\n'
        "not json at all\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"cases\.jsonl:2"):
        load_eval_cases(path)


def test_unknown_field_is_rejected_extra_forbid(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "cases.jsonl"
    path.write_text('{"item_id": "a", "title": "T1", "typo_field": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"cases\.jsonl:1"):
        load_eval_cases(path)


def test_committed_fixture_is_30_valid_real_items() -> None:
    cases = load_eval_cases(_FIXTURE)
    assert len(cases) == 30
    assert len({c.item_id for c in cases}) == 30
    for case in cases:
        assert case.title.strip()
        assert case.summary is None or case.summary.strip()


# --- eval_cases_to_items --------------------------------------------------------


def test_cases_become_items_carrying_title_and_summary() -> None:
    cases = [_case("a"), _case("b")]
    items = eval_cases_to_items(cases, clock=_CLOCK)
    assert [item.item_id for item in items] == ["a", "b"]
    assert items[0].title == "title a"
    assert items[0].summary == "a summary"
    assert items[0].kind is SourceKind.RSS


# --- group_keywords_by_item -----------------------------------------------------


def test_groups_and_orders_by_rank() -> None:
    keywords = [
        make_keyword("a", "second", rank=2),
        make_keyword("a", "first", rank=1),
        make_keyword("b", "only", rank=1),
    ]
    grouped = group_keywords_by_item(keywords)
    assert grouped == {"a": ["first", "second"], "b": ["only"]}


def test_item_with_no_rows_is_absent_from_the_mapping() -> None:
    assert group_keywords_by_item([]) == {}


# --- score_predictions -----------------------------------------------------------


def test_unlabeled_cases_get_score_none_and_are_excluded_from_mean() -> None:
    cases = [_case("a", ["genai"]), _case("b")]  # b unlabeled
    report = score_predictions(cases, {"a": ["genai"], "b": ["whatever"]})
    scores = {c.item_id: c.score for c in report.cases}
    assert scores["a"] == 1.0
    assert scores["b"] is None
    assert report.labeled_count == 1
    assert report.unlabeled_count == 1
    assert report.mean_score == 1.0


def test_mean_score_averages_only_labeled_cases() -> None:
    cases = [_case("a", ["genai"]), _case("b", ["genai", "openai"])]
    report = score_predictions(cases, {"a": ["genai"], "b": ["genai"]})
    # a: 1.0 (perfect), b: 1/2 (intersection 1, union 2)
    assert report.mean_score == pytest.approx((1.0 + 0.5) / 2)


def test_no_labeled_cases_gives_none_mean() -> None:
    cases = [_case("a"), _case("b")]
    report = score_predictions(cases, {})
    assert report.mean_score is None
    assert report.labeled_count == 0
    assert report.unlabeled_count == 2


def test_missing_prediction_is_treated_as_empty_list() -> None:
    cases = [_case("a", ["genai"])]
    report = score_predictions(cases, {})
    assert report.cases[0].predicted == []
    assert report.cases[0].score == 0.0


# --- format_report ---------------------------------------------------------------


def test_report_shows_mean_and_per_case_rows() -> None:
    cases = [_case("a", ["genai"]), _case("b")]
    report = score_predictions(cases, {"a": ["genai"], "b": ["dbt"]})
    text = format_report(report, heading="test heading")
    assert "## test heading" in text
    assert "mean jaccard: 1.000" in text
    assert "1 labeled / 1 unlabeled" in text
    assert "TODO" in text  # unlabeled row


def test_report_with_no_labels_says_so() -> None:
    cases = [_case("a"), _case("b")]
    report = score_predictions(cases, {})
    text = format_report(report)
    assert "n/a (0/2 labeled" in text


def test_report_rows_show_the_title_not_just_the_item_id() -> None:
    # A bare item_id fragment forces cross-referencing the fixture for every
    # row, defeating the point of a report meant to be pasted into an MR for
    # human review (PRD §10.5).
    cases = [_case("a", ["genai"])]
    report = score_predictions(cases, {"a": ["genai"]})
    text = format_report(report)
    assert "title a" in text


def test_report_truncates_long_titles() -> None:
    long_title = "x" * 100
    case = EvalCase(item_id="a", title=long_title, expected_keywords=["genai"])
    report = score_predictions([case], {"a": ["genai"]})
    text = format_report(report)
    assert long_title not in text
    assert "…" in text
