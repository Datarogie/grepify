from __future__ import annotations

import re
from pathlib import Path

import yaml


class ActionsYamlLoader(yaml.SafeLoader):
    pass


for first_letter, mappings in list(ActionsYamlLoader.yaml_implicit_resolvers.items()):
    ActionsYamlLoader.yaml_implicit_resolvers[first_letter] = [
        (tag, regexp) for tag, regexp in mappings if tag != "tag:yaml.org,2002:bool"
    ]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_COMMENT_RE = re.compile(r"#\s*v\d+(?:\.\d+){0,2}\b")
LLM_SECRETS = {"secrets.LLM_BASE_URL", "secrets.LLM_API_KEY"}


def load_workflow(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=ActionsYamlLoader)  # noqa: S506


def workflow_paths() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def iter_steps(workflow: dict):
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []) or []:
            yield job_name, step


def test_workflow_yaml_parses_and_local_actions_are_scanned() -> None:
    assert workflow_paths()
    for path in workflow_paths():
        assert isinstance(load_workflow(path), dict), path
    local_actions = list(ROOT.glob("**/action.yml")) + list(ROOT.glob("**/action.yaml"))
    assert local_actions == []


def test_all_external_actions_are_full_sha_pinned_with_adjacent_version_comments() -> None:
    for path in workflow_paths():
        for line in path.read_text().splitlines():
            if "uses:" not in line or "./" in line:
                continue
            ref = line.split("uses:", 1)[1].strip().split()[0]
            if "@" not in ref:
                continue
            _, version = ref.rsplit("@", 1)
            assert SHA_RE.match(version), f"{path}: mutable action ref {ref}"
            assert VERSION_COMMENT_RE.search(line), f"{path}: missing version comment near {ref}"


def test_pull_request_validation_is_read_only_and_secret_free() -> None:
    workflow = load_workflow(WORKFLOW_DIR / "validate.yml")
    assert "pull_request" in workflow["on"]
    assert workflow.get("permissions") == {"contents": "read"}
    for job_name, job in workflow["jobs"].items():
        assert job.get("permissions") == {"contents": "read"}, job_name
    assert "secrets." not in (WORKFLOW_DIR / "validate.yml").read_text()


def test_write_permissions_are_isolated_by_capability() -> None:
    workflow = load_workflow(WORKFLOW_DIR / "pipeline.yml")
    assert workflow.get("permissions") == {}
    expected = {
        "guard-production-ref": {},
        "validate-source": {"contents": "read"},
        "prepare-data": {"contents": "read"},
        "run-pipeline": {"contents": "read"},
        "data-update": {"contents": "write"},
        "build-site": {"contents": "read"},
        "deploy-pages": {"pages": "write", "id-token": "write"},
        "final-status": {},
        "notify-failure": {"issues": "write"},
    }
    actual = {name: job.get("permissions", {}) for name, job in workflow["jobs"].items()}
    assert actual == expected
    assert workflow["jobs"]["deploy-pages"].get("environment", {}).get("name") == "github-pages"
    for name, job in workflow["jobs"].items():
        if name != "deploy-pages":
            assert "environment" not in job


def test_llm_secrets_only_on_consuming_steps() -> None:
    allowed_steps = {
        "Remediate HTML-contaminated keywords (O1, one-off)",
        "Extract",
        "Daily digest",
        "Weekly digest",
    }
    workflow = load_workflow(WORKFLOW_DIR / "pipeline.yml")
    for job_name, job in workflow["jobs"].items():
        assert not (set(map(str, (job.get("env") or {}).values())) & LLM_SECRETS), job_name
        for step in job.get("steps", []) or []:
            env_values = set(map(str, (step.get("env") or {}).values()))
            if env_values & LLM_SECRETS:
                assert step.get("name") in allowed_steps
                assert job_name == "run-pipeline"


def test_manual_production_is_default_branch_guarded_before_secret_or_write_jobs() -> None:
    workflow = load_workflow(WORKFLOW_DIR / "pipeline.yml")
    guard = workflow["jobs"]["guard-production-ref"]
    guard_text = str(guard)
    assert "workflow_dispatch" in guard_text
    assert "github.ref_name" in guard_text
    assert "github.event.repository.default_branch" in guard_text

    def depends_on_guard(job_name: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if job_name in seen:
            return False
        seen.add(job_name)
        needs = workflow["jobs"][job_name].get("needs", [])
        needs_list = needs if isinstance(needs, list) else [needs]
        return "guard-production-ref" in needs_list or any(
            need and depends_on_guard(need, seen) for need in needs_list
        )

    production_jobs = {
        "validate-source",
        "prepare-data",
        "run-pipeline",
        "data-update",
        "build-site",
        "deploy-pages",
        "final-status",
    }
    notify = workflow["jobs"]["notify-failure"]
    assert "github.ref_name == github.event.repository.default_branch" in notify["if"]
    for name in production_jobs:
        assert depends_on_guard(name), name


def test_every_job_has_timeout_and_dependabot_keeps_github_actions() -> None:
    for path in workflow_paths():
        workflow = load_workflow(path)
        for name, job in workflow["jobs"].items():
            assert "timeout-minutes" in job, f"{path}:{name} lacks timeout"
    dependabot = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {entry["package-ecosystem"] for entry in dependabot["updates"]}
    assert "github-actions" in ecosystems
