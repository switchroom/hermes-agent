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
HEAD = "c" * 40
SHA = "a" * 40
BEFORE = "b" * 40
JOBS = {"detect": True, "tests": True, "lint": True, "js-tests": False, "repo-guards": True}
LANES = {"python": "true", "docker_meta": "true", "uv_lock": "false", "python_matrix": "m"}
PLAN = {"tree": TREE, "selected_jobs": JOBS, "gate_lanes": LANES,
        "python_files": "[]", "python_matrix": "m", "js_matrix": ""}
RUN_URL = "https://example.invalid/run/7"


def _zip(plan) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("plan.json", json.dumps(plan))
    return buf.getvalue()


def _responses(*, run=None, plan=PLAN):
    run = {"id": 7, "event": "pull_request", "path": reuse.WORKFLOW_PATH, "status": "completed",
           "conclusion": "success", "head_sha": HEAD, "head_branch": "feature",
           "head_repository": {"full_name": REPO}, "created_at": "2026-11-22T00:00:00Z",
           "html_url": RUN_URL, **(run or {})}
    pr = {"number": 12, "merged_at": "2026-11-22T01:00:00Z", "merge_commit_sha": SHA,
          "base": {"ref": "main"}, "head": {"sha": HEAD, "ref": "feature"}}
    return {
        f"repos/{REPO}/commits/{SHA}/pulls": [pr],
        f"repos/{REPO}/git/commits/{HEAD}": {"tree": {"sha": TREE}},
        f"repos/{REPO}/compare/{BEFORE}...{HEAD}": {"status": "ahead"},
        f"repos/{REPO}/commits/{HEAD}/pulls": [pr],
        f"repos/{REPO}/actions/runs?event=pull_request&head_sha={HEAD}&per_page=100": {"workflow_runs": [run]},
        f"repos/{REPO}/issues/12/events?per_page=100": [
            {"event": "base_ref_changed", "created_at": "2026-11-21T00:00:00Z"}],
        f"repos/{REPO}/actions/runs/7/artifacts?name=ci-plan-{TREE}": {"artifacts": [
            {"id": 3, "name": f"ci-plan-{TREE}", "expired": False}]},
        f"repos/{REPO}/actions/artifacts/3/zip": plan,
    }


def _api(responses=None, **kwargs):
    responses = responses if responses is not None else _responses(**kwargs)

    def api(path: str) -> bytes:
        if path not in responses:
            raise AssertionError(path)
        value = responses[path]
        return _zip(value) if path.endswith("/zip") else json.dumps(value).encode()

    return api


def _find(api, push_plan=PLAN, sha=SHA, before=BEFORE):
    return reuse.find_reusable_run(api, REPO, sha, before, TREE, push_plan, log=lambda _m: None)


def test_green_run_of_the_merged_prs_head_is_reused() -> None:
    assert _find(_api()) == RUN_URL
    identical = _responses()
    identical[f"repos/{REPO}/compare/{BEFORE}...{HEAD}"] = {"status": "identical"}
    assert _find(_api(identical)) == RUN_URL


@pytest.mark.parametrize("override", [
    {"conclusion": "failure"}, {"status": "in_progress", "conclusion": None},
    {"event": "push"}, {"path": ".github/workflows/other.yml"},
    {"head_repository": {"full_name": "someone/hermes-agent"}},
    {"head_branch": "other"}, {"head_sha": "d" * 40},
])
def test_any_doubt_about_the_run_runs_everything(override) -> None:
    assert _find(_api(run=override)) is None


def test_newest_run_wins() -> None:
    responses = _responses()
    key = f"repos/{REPO}/actions/runs?event=pull_request&head_sha={HEAD}&per_page=100"
    green = responses[key]["workflow_runs"][0]
    responses[key] = {"workflow_runs": [green, {**green, "id": 8, "conclusion": "failure",
                                                "created_at": "2026-11-22T00:30:00Z"}]}
    assert _find(_api(responses)) is None


def _mutate(path_suffix, value):
    responses = _responses()
    key = next(k for k in responses if k.endswith(path_suffix))
    responses[key] = value
    return _api(responses)


@pytest.mark.parametrize(("name", "suffix", "value"), [
    ("direct push", f"commits/{SHA}/pulls", []),
    ("unmerged", f"commits/{SHA}/pulls", [{"number": 12, "merge_commit_sha": SHA, "base": {"ref": "main"},
                                           "head": {"sha": HEAD, "ref": "feature"}}]),
    ("other base", f"commits/{SHA}/pulls", [{"number": 12, "merged_at": "x", "merge_commit_sha": SHA,
                                             "base": {"ref": "parent"}, "head": {"sha": HEAD, "ref": "feature"}}]),
    ("tree differs", f"git/commits/{HEAD}", {"tree": {"sha": "x" * 40}}),
    ("behind main", f"compare/{BEFORE}...{HEAD}", {"status": "diverged"}),
    ("shared head", f"commits/{HEAD}/pulls", [{"number": 12, "head": {"sha": HEAD}},
                                              {"number": 13, "head": {"sha": HEAD}}]),
    ("retargeted after run", "events?per_page=100", [{"event": "base_ref_changed",
                                                      "created_at": "2026-11-22T00:00:00Z"}]),
    ("unreadable events", "events?per_page=100", [{"event": "labeled"}] * 100),
    ("no artifact", f"artifacts?name=ci-plan-{TREE}", {"artifacts": []}),
])
def test_binding_to_this_pr_and_main(name, suffix, value) -> None:
    assert _find(_mutate(suffix, value)) is None, name


def test_needs_an_exact_push_range() -> None:
    assert _find(_api(), before="0" * 40) is None
    assert _find(_api(), before="") is None


def test_a_narrower_pr_plan_is_not_reused() -> None:
    for plan in (
        {**PLAN, "selected_jobs": {**JOBS, "tests": False}},
        {**PLAN, "python_files": '["tests/a.py"]'},
        {**PLAN, "tree": "x" * 40},
        {**PLAN, "gate_lanes": {**LANES, "docker_meta": "false"}},
        {**PLAN, "gate_lanes": {**LANES, "python_matrix": "other"}},
        {**PLAN, "gate_lanes": {}},
    ):
        assert _find(_api(plan=plan)) is None, plan
    # A PR that ran more than the push needs (e.g. its label gate) still covers it.
    wider = {**PLAN, "selected_jobs": {**JOBS, "js-tests": True, "review-labels": True},
             "gate_lanes": {**LANES, "uv_lock": "true"}}
    assert _find(_api(plan=wider)) == RUN_URL


def test_lookup_fails_open_on_api_errors(monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("SELECTED_JOBS", json.dumps(JOBS))
    monkeypatch.setenv("GATE_LANES", json.dumps(LANES))
    monkeypatch.setenv("PUSH_SHA", SHA)
    monkeypatch.setenv("PUSH_BEFORE", BEFORE)
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
