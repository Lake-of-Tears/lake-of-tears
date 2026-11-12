import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_valid_yaml():
    assert CI_WORKFLOW_PATH.exists(), f"Missing CI workflow file at {CI_WORKFLOW_PATH}"
    with open(CI_WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), "ci.yml must parse into a dictionary"
    assert data.get("name") == "CI"


def test_ci_workflow_permissions_and_concurrency():
    with open(CI_WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # Least privilege (#481): read-only by default, write only where the release tag is pushed
    permissions = data.get("permissions", {})
    assert permissions.get("contents") == "read"
    assert data["jobs"]["summary"]["permissions"]["contents"] == "write"
    for job_id, job in data["jobs"].items():
        if job_id != "summary":
            assert "permissions" not in job, f"Job '{job_id}' should inherit read-only permissions"
    concurrency = data.get("concurrency", {})
    assert concurrency.get("cancel-in-progress") is True


def test_ci_workflow_decomposed_jobs_present():
    with open(CI_WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    jobs = data.get("jobs", {})

    expected_jobs = [
        "lint",
        "security-sast",
        "security-deps",
        "security-docker",
        "docker-build-backend",
        "docker-build-ui",
        "docker-build-airflow",
        "helm-lint",
        "test",
        "summary",
    ]
    for job_id in expected_jobs:
        assert job_id in jobs, f"Job '{job_id}' is missing from ci.yml"


def test_ci_workflow_security_docker_scans_airflow():
    with open(CI_WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sec_docker = data["jobs"]["security-docker"]
    steps = sec_docker.get("steps", [])
    scan_steps = [s for s in steps if "trivy config" in str(s.get("run", "")).lower()]
    assert len(scan_steps) >= 1
    run_cmd = scan_steps[0]["run"]
    assert "backend/Dockerfile" in run_cmd
    assert "ui/Dockerfile" in run_cmd
    assert "deploy/airflow/Dockerfile" in run_cmd


def test_ci_workflow_docker_build_airflow():
    with open(CI_WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    airflow_build = data["jobs"]["docker-build-airflow"]
    steps = airflow_build.get("steps", [])
    build_steps = [s for s in steps if s.get("uses", "").startswith("docker/build-push-action")]
    assert len(build_steps) == 1
    with_args = build_steps[0].get("with", {})
    assert with_args.get("file") == "deploy/airflow/Dockerfile"
    assert "lake-of-tears-airflow:ci" in with_args.get("tags")


def test_ci_workflow_summary_job_structure():
    with open(CI_WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    summary_job = data["jobs"]["summary"]
    assert summary_job.get("if") == "always()"

    needs = summary_job.get("needs", [])
    expected_dependencies = [
        "lint",
        "security-sast",
        "security-deps",
        "security-docker",
        "docker-build-backend",
        "docker-build-ui",
        "docker-build-airflow",
        "helm-lint",
        "test",
    ]
    for dep in expected_dependencies:
        assert dep in needs, f"summary job must depend on '{dep}'"

    steps = summary_job.get("steps", [])
    summary_steps = [s for s in steps if "GITHUB_STEP_SUMMARY" in str(s.get("run", ""))]
    assert len(summary_steps) >= 1
    summary_script = summary_steps[0]["run"]
    assert "BUILD_TAG" in summary_script
    assert "git tag" in summary_script
    assert "helm upgrade" in summary_script


# ── PR preview vs main release build tag (#481) ───────────────────────────────

RESULT_ENV_VARS = [
    "LINT_RESULT",
    "HELM_RESULT",
    "SAST_RESULT",
    "DEPS_RESULT",
    "DOCKER_SEC_RESULT",
    "TEST_RESULT",
    "E2E_ANGLER_RESULT",
    "BACKEND_RESULT",
    "UI_RESULT",
    "AIRFLOW_RESULT",
]


def _run_summary_step(tmp_path, github: dict[str, str], all_success: bool = True):
    """Run the summary step's shell script with GitHub expressions substituted and git stubbed."""
    with open(CI_WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    steps = data["jobs"]["summary"]["steps"]
    script = next(s["run"] for s in steps if "GITHUB_STEP_SUMMARY" in str(s.get("run", "")))
    values = {
        "github.run_number": "748",
        "github.server_url": "https://github.com",
        "github.repository": "Lake-of-Tears/lake-of-tears",
        "github.ref_name": "ref",
        "github.run_id": "1",
        "github.actor": "ci-bot",
        **github,
    }
    body = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: values[m.group(1)], script)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_stub = bin_dir / "git"
    git_stub.write_text('#!/bin/sh\necho "git $*" >> "$GIT_LOG"\n')
    git_stub.chmod(git_stub.stat().st_mode | stat.S_IEXEC)

    summary = tmp_path / "summary.md"
    git_log = tmp_path / "git.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(summary),
        "GIT_LOG": str(git_log),
        **{k: "success" for k in RESULT_ENV_VARS},
    }
    if not all_success:
        env["TEST_RESULT"] = "failure"
    result = subprocess.run(
        ["bash", "-e", "-c", body], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    git_calls = git_log.read_text() if git_log.exists() else ""
    return result, summary.read_text(), git_calls


MAIN_PUSH = {
    "github.ref": "refs/heads/main",
    "github.event_name": "push",
    "github.sha": "435b4842505e0765dfe3543a2c6f979ed242dd0a",
    "github.event.pull_request.head.sha || github.sha": "435b4842505e0765dfe3543a2c6f979ed242dd0a",
    "github.event.pull_request.number": "",
}


@pytest.mark.skipif(os.name == "nt", reason="requires bash")
def test_ci_summary_pr_run_is_labelled_preview_and_not_deployable(tmp_path):
    result, summary, git_calls = _run_summary_step(
        tmp_path,
        {
            "github.ref": "refs/pull/480/merge",
            "github.event_name": "pull_request",
            # Synthetic PR merge commit: must not appear in the label
            "github.sha": "5ee44b9000000000000000000000000000000000",
            "github.event.pull_request.head.sha || github.sha": "a71d13c000000000000000000000000000000000",
            "github.event.pull_request.number": "480",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "`pr-480-a71d13c`" in summary
    assert "v1.0.0-build." not in summary.split("### ⛔ Not Deployable")[0]
    assert "5ee44b9" not in summary
    assert "helm upgrade" not in summary
    assert "Not Deployable" in summary
    assert "git tag" not in git_calls and "git push" not in git_calls


@pytest.mark.skipif(os.name == "nt", reason="requires bash")
def test_ci_summary_main_push_tags_release_and_shows_deploy(tmp_path):
    result, summary, git_calls = _run_summary_step(tmp_path, MAIN_PUSH)
    assert result.returncode == 0, result.stderr
    assert "`v1.0.0-build.748-435b484`" in summary
    assert "helm upgrade" in summary
    assert "git tag -a v1.0.0-build.748-435b484" in git_calls
    assert "git push origin v1.0.0-build.748-435b484" in git_calls


@pytest.mark.skipif(os.name == "nt", reason="requires bash")
def test_ci_summary_failed_main_push_creates_no_tag(tmp_path):
    result, summary, git_calls = _run_summary_step(tmp_path, MAIN_PUSH, all_success=False)
    assert result.returncode != 0
    assert "helm upgrade" not in summary
    assert "no git tag was created" in summary
    assert "git push" not in git_calls
