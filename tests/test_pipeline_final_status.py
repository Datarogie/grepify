from __future__ import annotations

from scripts.pipeline_final_status import decide_final_status

BASE = {
    "truth_ok": "true",
    "guard": "success",
    "validate": "success",
    "prepare": "success",
    "run_pipeline": "success",
    "data_update": "success",
    "build": "success",
    "deploy": "success",
}


def decision(**overrides: str | None) -> bool:
    args = BASE | overrides
    ok, _ = decide_final_status(**args)  # type: ignore[arg-type]
    return ok


def test_truth_success_and_all_downstream_success_is_success() -> None:
    assert decision()


def test_truth_success_requires_data_update_success() -> None:
    assert not decision(data_update="failure")
    assert not decision(data_update="skipped")


def test_truth_failure_deploys_last_known_good_then_fails_final_status() -> None:
    assert not decision(truth_ok="false", data_update="skipped", build="success", deploy="success")


def test_build_and_deploy_failures_fail_final_status() -> None:
    assert not decision(build="failure")
    assert not decision(deploy="failure")


def test_guard_failure_fails_final_status() -> None:
    assert not decision(guard="failure")


def test_cancellation_never_reports_success() -> None:
    assert not decision(deploy="cancelled")
