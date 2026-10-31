from pathlib import Path

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
    permissions = data.get("permissions", {})
    assert permissions.get("contents") == "write"
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
    scan_steps = [s for s in steps if "trivy" in str(s.get("run", "")).lower()]
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
