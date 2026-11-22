"""Auto-revert and nightly flake tracking for the fork's main (scripts/ci/main_health.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("main_health", ROOT / "scripts/ci/main_health.py")
mh = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mh)


def _run(conclusion, created="2026-11-22T01:00:00Z", status="completed", run_id=1, sha="s" * 40):
    return {"event": "push", "head_branch": "main", "path": mh.WORKFLOW_PATH, "status": status,
            "conclusion": conclusion, "created_at": created, "id": run_id, "head_sha": sha}


TEST_JOB = {"name": "Python tests / Run tests (5, 16)", "conclusion": "failure",
            "steps": [{"name": "Checkout code", "conclusion": "success"},
                      {"name": "Run tests", "conclusion": "failure"}]}
GATE_JOB = {"name": "All required checks pass", "conclusion": "failure",
            "steps": [{"name": "Evaluate job results", "conclusion": "failure"}]}
STREAK = [{"sha": "c" * 40, "message": "fix: thing\n", "reverted": False}]


def _decide(**kw):
    args = {"run": _run("failure"), "newer": [], "jobs": [TEST_JOB, GATE_JOB],
            "streak": STREAK, "revert_branch_open": False, **kw}
    return mh.decide(**args)[0]


def test_red_culprit_failing_in_tests_is_reverted() -> None:
    assert _decide() == "revert"


def test_only_failure_is_red_cancelled_is_unknown() -> None:
    assert _decide(run=_run("cancelled")) == "none"
    assert _decide(run=_run(None, status="in_progress")) == "none"
    assert mh.run_state(_run("cancelled")) == "unknown"
    assert mh.run_state(_run("skipped")) == "unknown"
    assert mh.run_state(_run("failure")) == "red"


def test_fix_forward_newer_runs_decide() -> None:
    # (a) a newer main push run pending or green: never revert under a fix.
    assert _decide(newer=[_run(None, status="queued", created="2026-11-22T02:00:00Z")]) == "none"
    assert _decide(newer=[_run("success", created="2026-11-22T02:00:00Z")]) == "none"
    # A newer cancelled run proves nothing either way; the red verdict stands.
    assert _decide(newer=[_run("cancelled", created="2026-11-22T02:00:00Z")]) == "revert"


def test_infrastructure_failures_only_report() -> None:
    # (c) setup/checkout/install failures, no failed step, or only the gates failing.
    infra = {**TEST_JOB, "steps": [{"name": "Set up locked Python and test dependencies", "conclusion": "failure"}]}
    assert _decide(jobs=[infra, GATE_JOB]) == "report"
    assert _decide(jobs=[{**TEST_JOB, "steps": []}]) == "report"
    assert _decide(jobs=[GATE_JOB]) == "report"
    action = {**TEST_JOB, "steps": [{"name": "Run actions/checkout@abc", "conclusion": "failure"}]}
    assert _decide(jobs=[action]) == "report"
    probe = {**TEST_JOB, "steps": [{"name": "Check SQLite runs WAL", "conclusion": "failure"}]}
    assert _decide(jobs=[probe]) == "report"
    lint = {"name": "Python lints / Python lints", "conclusion": "failure",
            "steps": [{"name": "ruff check  .", "conclusion": "failure"}]}
    assert _decide(jobs=[lint, GATE_JOB]) == "revert"


def test_reverts_are_detected_by_trailer() -> None:
    # (d) never revert a revert; never revert twice.
    trailer = [{**STREAK[0], "message": "Undo thing (main CI red)\n\nThis reverts commit " + "d" * 40 + ".\n"}]
    assert _decide(streak=trailer) == "report"
    assert _decide(streak=[{**STREAK[0], "message": 'Revert "fix: thing"\n'}]) == "report"
    assert _decide(streak=[{**STREAK[0], "reverted": True}]) == "none"
    assert _decide(revert_branch_open=True) == "none"
    assert _decide(streak=None) == "report"


def _git_fake(first_parents, messages=None, main_log=""):
    def git(*args, check=True):
        if args[0] == "rev-list":
            return "\n".join(first_parents)
        if args[:2] == ("log", "--format=%B"):
            return main_log
        if args[:3] == ("log", "-1", "--format=%B"):
            return (messages or {}).get(args[3], "fix\n")
        raise AssertionError(args)
    return git


def test_red_streak_walks_to_the_culprit_with_its_own_red_run() -> None:
    states = {"r": "red", "c": "red", "g": "green"}
    streak = mh.red_streak("r", states.__getitem__, _git_fake(["r", "c", "g"]))
    assert [x["sha"] for x in streak] == ["c", "r"]
    # A skipped-over commit with no run of its own (absent) is crossed, but the culprit needs one.
    states = {"r": "red", "a": "absent", "g": "green"}
    assert mh.red_streak("r", states.__getitem__, _git_fake(["r", "a", "g"])) is None
    # Cancelled or pending on the way: unknown culprit.
    for bad in ("unknown", "pending"):
        states = {"r": "red", "x": bad, "g": "green"}
        assert mh.red_streak("r", states.__getitem__, _git_fake(["r", "x", "g"])) is None
    # Already reverted on main (trailer).
    states = {"r": "red", "g": "green"}
    streak = mh.red_streak("r", states.__getitem__, _git_fake(["r", "g"], main_log="This reverts commit r."))
    assert streak[0]["reverted"] is True


def test_cmd_decide_checks_the_culprits_own_run(tmp_path, monkeypatch) -> None:
    red, culprit, green = "e" * 40, "c" * 40, "a" * 40
    runs = {red: _run("failure", run_id=2, sha=red, created="2026-11-22T02:00:00Z"),
            culprit: _run("failure", run_id=1, sha=culprit), green: _run("success", run_id=0, sha=green)}
    infra = {**TEST_JOB, "steps": [{"name": "Install dependencies", "conclusion": "failure"}]}
    jobs = {2: [TEST_JOB], 1: [infra]}

    def api(*args, **_kw):
        path = args[1]
        if path.endswith("/actions/runs/2"):
            return json.dumps(runs[red])
        if "/actions/workflows/ci.yaml/runs?" in path:
            sha = path.split("head_sha=")[1].split("&")[0] if "head_sha=" in path else None
            return json.dumps({"workflow_runs": [runs[sha]] if sha else list(runs.values())})
        if "/jobs?" in path:
            return json.dumps({"jobs": jobs[int(path.split("/runs/")[1].split("/")[0])]})
        if "/branches/" in path:
            raise RuntimeError("gh: Branch not found (HTTP 404)")
        raise AssertionError(path)

    def git(*args, check=True):
        if args[:2] == ("rev-list", "--first-parent"):
            return f"{red}\n{culprit}\n{green}"
        if args[:2] == ("rev-list", "--parents"):
            return f"{culprit} {green}"
        return "fix: thing\n" if args[:3] == ("log", "-1", "--format=%B") else ""

    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    mh.cmd_decide({"REPO": "switchroom/hermes-agent", "RUN_ID": "2"}, api, git)
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    # The triggering run failed in tests, but the culprit's own run failed in setup: report.
    assert lines["action"] == "report" and lines["sha"] == culprit
    jobs[1] = [TEST_JOB]
    out.unlink()
    mh.cmd_decide({"REPO": "switchroom/hermes-agent", "RUN_ID": "2"}, api, git)
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert lines["action"] == "revert" and lines["branch"] == f"revert/auto-{culprit[:12]}"
    assert lines["mainline"] == ""


def test_slice_log_parsing_separates_flaky_from_failed() -> None:
    log = "\n".join([
        "2026-11-22T01:00:00.1Z === ⚠ 1 FLAKY file (failed once, passed on retry — fix these) ===",
        "2026-11-22T01:00:00.2Z   tests/gateway/test_a.py",
        "2026-11-22T01:00:00.3Z ⚠ FLAKY: failed on attempt 1, passed on retry (attempt 2).",
        "2026-11-22T01:00:00.4Z FAILED tests/tools/test_b.py::test_x - AssertionError",
        "2026-11-22T01:00:00.5Z   tests/tools/test_not_flaky.py",
    ])
    flaky, failed = mh.parse_slice_log(log)
    assert flaky == {"tests/gateway/test_a.py"}
    assert failed == {"tests/tools/test_b.py"}


def test_ledger_accumulates_and_round_trips_through_the_issue_body() -> None:
    ledger = mh.fold_ledger({}, "run/1", {"tests/a.py"}, set())
    ledger = mh.fold_ledger(ledger, "run/2", {"tests/a.py"}, {"tests/b.py"})
    assert ledger["files"]["tests/a.py"]["flaky"] == 2
    assert ledger["files"]["tests/b.py"] == {"flaky": 0, "failed": 1, "last": "run/2"}
    body = mh.render_tracker(ledger, "run/2", "failure")
    assert json.loads(mh.LEDGER_RE.search(body).group(1)) == ledger
    assert body.index("tests/a.py") < body.index("tests/b.py")


def test_workflow_jobs_are_split_least_privilege_and_report_always() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/main-health.yml").read_text(encoding="utf-8"))
    assert wf[True] == {"workflow_run": {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}}
    jobs = wf["jobs"]
    for name in ("retry", "decide"):
        cond = " ".join(jobs[name]["if"].split())
        for part in ("workflow_run.event == 'push'", "head_branch == 'main'",
                     "head_repository.full_name == github.repository", "conclusion == 'failure'"):
            assert part in cond, name
    assert jobs["retry"]["permissions"] == {"actions": "write"}
    assert "run_attempt == 1" in jobs["retry"]["if"]
    assert jobs["decide"]["permissions"] == {"actions": "read", "contents": "read"}
    assert jobs["revert"]["permissions"] == {"contents": "read"}
    assert jobs["report"]["permissions"] == {"issues": "write"}
    assert jobs["report"]["if"].split()[0] == "always()"
    # (b) the rerun is waited for in-job (GITHUB_TOKEN reruns may not fire workflow_run).
    rerun = jobs["retry"]["steps"][0]["run"]
    assert "gh run rerun" in rerun and "run_attempt" in rerun
    # The revert PR is pushed with the App so its CI runs; plain squash when --auto refuses.
    steps = {s.get("id"): s for s in jobs["revert"]["steps"]}
    assert "create-github-app-token" in steps["app-token"]["uses"]
    script = steps["revert"]["run"]
    assert "--auto --squash" in script and "This reverts commit $SHA." in script
    assert script.count("gh pr merge") == 2
    for job in jobs.values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["ref"] == "main"
                assert step["with"]["persist-credentials"] is False
    assert "workflow_run.event == 'schedule'" in jobs["nightly-flakes"]["if"]
