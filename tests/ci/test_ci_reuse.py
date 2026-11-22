"""Tree-dedupe of the fork's main push CI (scripts/ci/ci_reuse.py, HA-1)."""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ci_reuse", ROOT / "scripts/ci/ci_reuse.py")
reuse = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(reuse)

REPO = reuse.FORK
TREE = "t" * 40
HEAD = "h" * 40
JOBS = {"detect": True, "tests": True, "lint": True, "js-tests": False, "repo-guards": True}
PLAN = {"tree": TREE, "selected_jobs": JOBS, "python_files": "[]", "python_matrix": "m", "js_matrix": ""}


def _zip(plan) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("plan.json", json.dumps(plan))
    return buf.getvalue()


def _api(*, run=None, head_tree=TREE, plan=PLAN, artifacts=None):
    run = {"id": 7, "event": "pull_request", "path": reuse.WORKFLOW_PATH, "status": "completed",
           "conclusion": "success", "head_sha": HEAD, "head_repository": {"full_name": REPO},
           "html_url": "https://example.invalid/run/7", **(run or {})}
    artifacts = artifacts if artifacts is not None else [
        {"id": 3, "expired": False, "created_at": "2026-11-22T00:00:00Z", "workflow_run": {"id": 7}}]

    def api(path: str) -> bytes:
        if path.startswith(f"repos/{REPO}/actions/artifacts?name=ci-plan-{TREE}"):
            return json.dumps({"artifacts": artifacts}).encode()
        if path == f"repos/{REPO}/actions/runs/7":
            return json.dumps(run).encode()
        if path == f"repos/{REPO}/git/commits/{HEAD}":
            return json.dumps({"tree": {"sha": head_tree}}).encode()
        if path == f"repos/{REPO}/actions/artifacts/3/zip":
            return _zip(plan)
        raise AssertionError(path)

    return api


def _find(api, push_plan=PLAN):
    return reuse.find_reusable_run(api, REPO, TREE, push_plan, log=lambda _m: None)


def test_green_same_repo_run_of_the_same_tree_is_reused() -> None:
    assert _find(_api()) == "https://example.invalid/run/7"


@pytest.mark.parametrize("override", [
    {"conclusion": "failure"}, {"status": "in_progress", "conclusion": None},
    {"event": "push"}, {"path": ".github/workflows/other.yml"},
    {"head_repository": {"full_name": "someone/hermes-agent"}},
])
def test_any_doubt_about_the_run_runs_everything(override) -> None:
    assert _find(_api(run=override)) is None


def test_tree_must_match_both_the_pr_head_and_the_artifact() -> None:
    assert _find(_api(head_tree="x" * 40)) is None
    assert _find(_api(plan={**PLAN, "tree": "x" * 40})) is None
    assert _find(_api(artifacts=[])) is None


def test_a_narrower_pr_plan_is_not_reused() -> None:
    narrower = {**PLAN, "selected_jobs": {**JOBS, "tests": False}}
    assert _find(_api(plan=narrower)) is None
    assert _find(_api(plan={**PLAN, "python_files": '["tests/a.py"]'})) is None
    # A PR that ran more than the push needs (e.g. its label gate) still covers it.
    wider = {**PLAN, "selected_jobs": {**JOBS, "js-tests": True, "review-labels": True}}
    assert _find(_api(plan=wider)) == "https://example.invalid/run/7"


def test_lookup_fails_open_on_api_errors(monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("SELECTED_JOBS", json.dumps(JOBS))
    for key in reuse.PLAN_KEYS:
        monkeypatch.setenv(key.upper(), PLAN[key])
    monkeypatch.setattr(reuse, "_tree", lambda ref="HEAD": TREE)

    def boom(_path):
        raise RuntimeError("api down")

    monkeypatch.setattr(reuse, "_gh_api", boom)
    assert reuse.lookup() == 0
    assert out.read_text() == "reuse=false\n"
    out.unlink()
    monkeypatch.setattr(reuse, "_gh_api", _api())
    assert reuse.lookup() == 0
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert lines["reuse"] == "true"
    assert json.loads(lines["selected_jobs"]) == {name: name == "detect" for name in JOBS}


def _ci():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))


def test_reuse_keeps_both_gates_and_every_job_obeys_selected_jobs() -> None:
    wf = _ci()
    jobs = wf["jobs"]
    detect = jobs["detect"]
    assert "steps.reuse.outputs.reuse == 'true'" in detect["outputs"]["selected_jobs"]
    assert jobs["all-checks-pass"]["name"] == "All required checks pass"
    assert jobs["image-checks-pass"]["name"] == "Image required checks pass"
    for name, job in jobs.items():
        if name in {"detect", "all-checks-pass", "image-checks-pass", "review-labels"} or job.get("if") is False:
            continue
        assert "needs.detect.outputs.selected_jobs" in str(job.get("if")), name
    step = next(s for s in detect["steps"] if s.get("id") == "reuse")
    assert "github.event_name == 'push'" in step["if"] and "inputs.release != true" in step["if"]
    assert step["run"] == "python3 scripts/ci/ci_reuse.py lookup"


def test_nightly_schedule_runs_full_and_is_never_cancelled_by_pushes() -> None:
    wf = _ci()
    assert wf[True]["schedule"]
    assert "github.event_name == 'schedule' && 'nightly'" in wf["concurrency"]["group"]
    assert "schedule" not in wf["concurrency"]["cancel-in-progress"]
