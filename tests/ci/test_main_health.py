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


def _run(conclusion, created="2026-11-22T01:00:00Z", status="completed", run_id=1):
    return {"event": "push", "head_branch": "main", "status": status, "conclusion": conclusion,
            "created_at": created, "id": run_id}


def _decide(**kw):
    args = {"attempt": 2, "subject": "fix: thing", "parents": ["p"],
            "parent_runs": [_run("success")], "open_revert_prs": 0, **kw}
    return mh.decide(**args)[0]


def test_first_red_attempt_reruns_failed_jobs_once() -> None:
    assert _decide(attempt=1) == "rerun"


def test_second_red_attempt_after_green_parent_reverts() -> None:
    assert _decide() == "revert"
    assert _decide(parents=["p", "upstream"]) == "revert"  # sync merge: revert -m 1


def test_no_safe_culprit_only_reports() -> None:
    assert _decide(parent_runs=[_run("failure")]) == "issue"
    assert _decide(parent_runs=[]) == "issue"
    # The parent's newest completed run is the verdict, not an older green one.
    assert _decide(parent_runs=[_run("success", run_id=1),
                                _run("failure", created="2026-11-22T02:00:00Z", run_id=2)]) == "issue"
    assert _decide(subject='Revert "fix: thing"') == "issue"
    assert _decide(open_revert_prs=1) == "issue"
    assert _decide(parents=[]) == "issue"


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


def test_workflow_runs_no_pr_code_and_reverts_only_main_push_failures() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/main-health.yml").read_text(encoding="utf-8"))
    assert wf[True] == {"workflow_run": {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}}
    revert = wf["jobs"]["auto-revert"]
    cond = " ".join(revert["if"].split())
    for part in ("workflow_run.event == 'push'", "head_branch == 'main'",
                 "head_repository.full_name == github.repository", "conclusion == 'failure'"):
        assert part in cond
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["ref"] == "main"
                assert step["with"]["persist-credentials"] is False
    assert "workflow_run.event == 'schedule'" in wf["jobs"]["nightly-flakes"]["if"]
