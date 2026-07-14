"""Decide the production pipeline's final status from GitHub Actions need results.

The workflow intentionally deploys the last-known-good site after an upstream
pipeline failure, then fails at the end so notification still happens. Keeping
that matrix here makes the behavior testable without depending on brittle shell
string assertions.
"""

from __future__ import annotations

import os
import sys

SUCCESS = "success"
FAILURE = "failure"
SKIPPED = "skipped"
CANCELLED = "cancelled"


def decide_final_status(  # noqa: PLR0911, PLR0913
    *,
    truth_ok: str | None,
    guard: str,
    validate: str,
    prepare: str,
    run_pipeline: str,
    data_update: str,
    build: str,
    deploy: str,
) -> tuple[bool, str]:
    results = {
        "guard-production-ref": guard,
        "validate-source": validate,
        "prepare-data": prepare,
        "run-pipeline": run_pipeline,
        "data-update": data_update,
        "build-site": build,
        "deploy-pages": deploy,
    }
    cancelled = [name for name, result in results.items() if result == CANCELLED]
    if cancelled:
        return False, f"cancelled jobs: {', '.join(cancelled)}"

    required_before_pipeline = {
        "guard-production-ref": guard,
        "validate-source": validate,
        "prepare-data": prepare,
        "run-pipeline": run_pipeline,
    }
    failed_before_pipeline = [
        name for name, result in required_before_pipeline.items() if result != SUCCESS
    ]
    if failed_before_pipeline:
        return False, f"required jobs did not succeed: {', '.join(failed_before_pipeline)}"

    if truth_ok == "true":
        expected_success = {
            "data-update": data_update,
            "build-site": build,
            "deploy-pages": deploy,
        }
        unexpected = [name for name, result in expected_success.items() if result != SUCCESS]
        if unexpected:
            return False, f"truth succeeded but downstream jobs did not: {', '.join(unexpected)}"
        return True, "pipeline truth, data update, build, and deploy succeeded"

    if truth_ok == "false":
        if data_update != SKIPPED:
            return False, f"truth failed but data-update was {data_update}, expected skipped"
        last_known_good = {
            "build-site": build,
            "deploy-pages": deploy,
        }
        unexpected = [name for name, result in last_known_good.items() if result != SUCCESS]
        if unexpected:
            return False, "last-known-good deployment did not complete: " + ", ".join(unexpected)
        return False, "upstream pipeline failed; last-known-good site was deployed"

    return False, f"run-pipeline did not publish a valid truth_ok output: {truth_ok!r}"


def main() -> int:
    ok, message = decide_final_status(
        truth_ok=os.environ.get("TRUTH_OK"),
        guard=os.environ["GUARD_RESULT"],
        validate=os.environ["VALIDATE_RESULT"],
        prepare=os.environ["PREPARE_RESULT"],
        run_pipeline=os.environ["RUN_PIPELINE_RESULT"],
        data_update=os.environ["DATA_UPDATE_RESULT"],
        build=os.environ["BUILD_RESULT"],
        deploy=os.environ["DEPLOY_RESULT"],
    )
    print(message, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
