#!/usr/bin/env python3
"""Keep the fork's main green without a human: auto-revert and nightly flake tracking.

Used by ``.github/workflows/main-health.yml`` after every completed ``CI`` run
on main. ``gh`` (``GH_TOKEN``) is the only network path; nothing here runs
pull-request code.

``decide`` (a failed push run, after main-health.yml's ``retry`` job reran its
failed jobs once and waited in-job): name the culprit and choose an action.
Following switchroom #398, it reverts only when ALL of these hold, else it
reports (or does nothing):

* the run's latest attempt concluded ``failure``. Only ``failure`` is red: a
  cancelled or skipped run (fork main pushes cancel each other) is unknown;
* every newer main push run has completed and none is green (a fix may be
  landing; the newest completion decides);
* the culprit is the oldest commit of the red streak: walking first parents
  back from the red commit crosses only ``failure`` or absent runs to a green
  parent, and the culprit has its own ``failure`` run. Any cancelled, skipped
  or pending run on the way makes the culprit unknown;
* the culprit's own run failed in test or check steps only, not setup,
  checkout, install or download (infrastructure), and not only in the
  aggregate gates;
* the culprit is not itself a revert (``This reverts commit`` trailer or a
  ``Revert "`` subject) and has not been reverted already (trailer on main,
  or an open ``revert/auto-<sha>`` branch).

``flakes`` (the nightly scheduled full run): read every Python test slice's
log, count FLAKY files (failed once, passed on retry) and FAILED files, and
fold them into the tracker issue's JSON ledger so recurring flakes are visible
and ranked instead of rerun by hand.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

REVERT_BRANCH_PREFIX = "revert/auto-"
TRACKER_TITLE = "Nightly full CI: flake and failure tracker"
TRACKER_LABEL = "ci-flake-tracker"
LEDGER_RE = re.compile(r"<!-- ledger\n(.*?)\n-->", re.DOTALL)
TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
FLAKY_FILE_RE = re.compile(r"^  (tests/\S+\.py)$")
FAILED_RE = re.compile(r"^FAILED (tests/[^\s:]+\.py)")

Api = Callable[..., str]


def gh(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=input_text, text=True, encoding="utf-8",
                            capture_output=True, check=False, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh {' '.join(args[:2])} failed")
    return result.stdout


def _write_outputs(values: Mapping[str, str]) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    print(lines, end="")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as output:
            output.write(lines)


# ── decide ────────────────────────────────────────────────────────────────


WORKFLOW_PATH = ".github/workflows/ci.yaml"
SHA_RE = re.compile(r"[0-9a-f]{40}")
STREAK_LIMIT = 20
GATE_JOBS = frozenset({"All required checks pass", "Image required checks pass"})
# A failed step with one of these names is infrastructure, not a verdict on the commit.
INFRA_STEP_RE = re.compile(
    r"^(Set up job|Complete job|Post .*|Checkout.*|Set up .*|Setup .*|Install .*|Restore .*|Download .*"
    r"|Cache .*|Save .*|Upload .*|Publish .*|Prepare .*|Snapshot .*|Mint .*|Get GitHub App token.*"
    r"|Determine base ref|Check SQLite .*|Require a WAL-capable SQLite|Run [\w.-]+/[\w.-]+@\S+)$"
)


def push_runs(runs: Iterable[Mapping[str, Any]], sha: str | None = None) -> list[Mapping[str, Any]]:
    keep = [r for r in runs if r.get("event") == "push" and r.get("head_branch") == "main"
            and r.get("path") == WORKFLOW_PATH and (sha is None or r.get("head_sha") == sha)]
    return sorted(keep, key=lambda r: (str(r.get("created_at", "")), int(r.get("id", 0))))


def run_state(run: Mapping[str, Any] | None) -> str:
    """'green', 'red', 'pending', 'unknown' or 'absent'. Only ``failure`` is red."""
    if not run:
        return "absent"
    if run.get("status") != "completed":
        return "pending"
    return {"success": "green", "failure": "red"}.get(str(run.get("conclusion")), "unknown")


def newest_state(runs: Iterable[Mapping[str, Any]]) -> str:
    ordered = push_runs(runs)
    return run_state(ordered[-1] if ordered else None)


def is_revert(message: str) -> bool:
    return message.startswith('Revert "') or "This reverts commit" in message


def failed_in_tests(jobs: Iterable[Mapping[str, Any]]) -> bool:
    """Every failed job failed in test/check steps only; the aggregate gates don't count."""
    failed = [j for j in jobs if j.get("conclusion") == "failure" and j.get("name") not in GATE_JOBS]
    if not failed:
        return False
    for job in failed:
        steps = [str(s.get("name", "")) for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
        if not steps or any(INFRA_STEP_RE.match(name) for name in steps):
            return False
    return True


def decide(*, run: Mapping[str, Any], newer: Iterable[Mapping[str, Any]], jobs: Iterable[Mapping[str, Any]],
           streak: list[Mapping[str, Any]] | None, revert_branch_open: bool) -> tuple[str, str]:
    """Return (action, reason); action is 'revert', 'report' or 'none'.

    ``streak`` lists the red streak oldest first, each {'sha', 'message', 'reverted'};
    None when it cannot be proved back to a green parent within STREAK_LIMIT.
    """
    if run_state(run) != "red":
        return "none", f"run is {run.get('status')}/{run.get('conclusion')}: not red"
    newer = push_runs(newer)
    if any(r.get("status") != "completed" for r in newer):
        return "none", "a newer main run is still in flight; its completion decides"
    if any(r.get("conclusion") == "success" for r in newer):
        return "none", "main has gone green again since this run"
    if not streak:
        return "report", "no culprit: the red streak does not reach a green parent through failed runs only"
    culprit = streak[0]
    short = str(culprit["sha"])[:12]
    if is_revert(str(culprit["message"])):
        return "report", f"culprit {short} is itself a revert; never reverting a revert"
    if culprit["reverted"] or revert_branch_open:
        return "none", f"culprit {short} is already reverted or has an open revert"
    if not failed_in_tests(jobs):
        return "report", f"culprit {short}'s run failed outside its test steps (infrastructure); not reverting"
    return "revert", f"{short} turned main red (its parent was green; its own run failed in tests)"


def _git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], text=True, encoding="utf-8", capture_output=True,
                          check=check, timeout=60).stdout


def _api_json(api: Api, path: str) -> Any:
    return json.loads(api("api", path))


def red_streak(sha: str, state: Callable[[str], str], git: Callable[..., str] = _git) -> list[dict[str, Any]] | None:
    """Commits of the red streak ending at ``sha``, oldest first; None if it cannot be proved."""
    first_parent = git("rev-list", "--first-parent", f"--max-count={STREAK_LIMIT + 1}", sha).split()
    streak: list[str] = []
    for commit, parent in itertools.pairwise(first_parent):
        streak.insert(0, commit)
        parent_state = state(parent)
        if parent_state == "green":
            break
        if parent_state not in ("red", "absent"):
            return None
    else:
        return None
    if state(streak[0]) != "red":
        return None
    main_log = git("log", "--format=%B", "origin/main")
    return [{"sha": c, "message": git("log", "-1", "--format=%B", c),
             "reverted": f"This reverts commit {c}" in main_log} for c in streak]


def cmd_decide(env: Mapping[str, str], api: Api = gh, git: Callable[..., str] = _git) -> int:
    repo, run_id = env["REPO"], env["RUN_ID"]
    run = _api_json(api, f"repos/{repo}/actions/runs/{run_id}")
    sha = str(run.get("head_sha") or "")
    if not SHA_RE.fullmatch(sha):
        raise SystemExit(f"run {run_id} has no commit SHA")

    def main_runs(**query: str) -> list[Mapping[str, Any]]:
        params = "&".join(f"{k}={v}" for k, v in {"event": "push", "branch": "main", "per_page": "100", **query}.items())
        return _api_json(api, f"repos/{repo}/actions/workflows/ci.yaml/runs?{params}").get("workflow_runs", [])

    newer = [r for r in push_runs(main_runs()) if str(r.get("created_at", "")) > str(run.get("created_at", ""))]
    states: dict[str, str] = {sha: run_state(run)}

    def state(commit: str) -> str:
        if commit not in states:
            states[commit] = newest_state(main_runs(head_sha=commit))
        return states[commit]

    streak = red_streak(sha, state, git) if states[sha] == "red" else None
    culprit, jobs, parents = sha, [], []
    if streak:
        culprit = streak[0]["sha"]
        # The culprit's own failed run decides tests-vs-infrastructure, not the triggering run.
        own = run if culprit == sha else push_runs(main_runs(head_sha=culprit))[-1]
        jobs = _api_json(api, f"repos/{repo}/actions/runs/{int(own['id'])}/jobs?filter=latest&per_page=100").get("jobs", [])
        parents = git("rev-list", "--parents", "-n", "1", culprit).split()[1:]
    branch = f"{REVERT_BRANCH_PREFIX}{culprit[:12]}"
    try:
        api("api", f"repos/{repo}/branches/{branch}")
        branch_open = True
    except RuntimeError as exc:
        if "404" not in str(exc) and "Not Found" not in str(exc):
            raise  # an API error is not "no branch": fail, and the report job says so
        branch_open = False
    action, reason = decide(run=run, newer=newer, jobs=jobs, streak=streak, revert_branch_open=branch_open)
    subject = (streak[0]["message"].splitlines() or [""])[0] if streak else ""
    print(f"::notice::{action}: {reason}")
    _write_outputs({"action": action, "reason": reason, "sha": culprit, "branch": branch,
                    "mainline": "1" if len(parents) == 2 else "", "subject": subject.replace('"', "'")})
    return 0


# ── flakes ────────────────────────────────────────────────────────────────


def parse_slice_log(text: str) -> tuple[set[str], set[str]]:
    """Return (flaky files, failed files) from one test slice's job log."""
    flaky: set[str] = set()
    failed: set[str] = set()
    lines = [TIMESTAMP_RE.sub("", line) for line in text.splitlines()]
    for index, line in enumerate(lines):
        if match := FAILED_RE.match(line):
            failed.add(match.group(1))
        if (match := FLAKY_FILE_RE.match(line)) and index + 1 < len(lines) \
                and lines[index + 1].lstrip().startswith("⚠ FLAKY:"):
            flaky.add(match.group(1))
    return flaky, failed - flaky


def fold_ledger(ledger: Mapping[str, Any], run_url: str, flaky: set[str], failed: set[str]) -> dict[str, Any]:
    """Add one nightly run's results to the ledger {file: {flaky, failed, last}}."""
    files: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (ledger.get("files") or {}).items()}
    for kind, names in (("flaky", flaky), ("failed", failed)):
        for name in names:
            entry = files.setdefault(name, {"flaky": 0, "failed": 0})
            entry[kind] = int(entry.get(kind, 0)) + 1
            entry["last"] = run_url
    runs = [*(ledger.get("runs") or []), run_url][-30:]
    return {"runs": runs, "files": files}


def render_tracker(ledger: Mapping[str, Any], latest: str, conclusion: str) -> str:
    rows = sorted(ledger.get("files", {}).items(),
                  key=lambda item: (-(item[1].get("failed", 0) + item[1].get("flaky", 0)), item[0]))
    table = "\n".join(f"| `{name}` | {v.get('flaky', 0)} | {v.get('failed', 0)} | [last]({v.get('last', '')}) |"
                      for name, v in rows[:60]) or "| (none yet) | | | |"
    return (
        "Maintained by `.github/workflows/main-health.yml` after every nightly full CI run on main.\n"
        "A file listed here is a bug: fix it with the process_time-ratio pattern, never by rerunning.\n\n"
        f"Latest nightly: {latest} ({conclusion}). Runs tracked: {len(ledger.get('runs', []))}.\n\n"
        "| Test file | FLAKY (failed once, passed on retry) | FAILED | |\n|---|---|---|---|\n"
        f"{table}\n\n<!-- ledger\n{json.dumps(ledger, sort_keys=True)}\n-->\n"
    )


def cmd_flakes(env: Mapping[str, str], api: Api = gh) -> int:
    repo, run_id = env["REPO"], env["RUN_ID"]
    run_url, conclusion = env.get("RUN_URL", run_id), env.get("CONCLUSION", "unknown")
    pages = json.loads(api("api", "--paginate", "--slurp", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"))
    jobs = [job for page in pages for job in page.get("jobs", [])]
    flaky: set[str] = set()
    failed: set[str] = set()
    for job in jobs:
        if "Run tests" not in job.get("name", ""):
            continue
        try:
            log = api("api", f"repos/{repo}/actions/jobs/{job['id']}/logs")
        except RuntimeError as exc:
            print(f"::warning::no log for job {job.get('name')}: {exc}")
            continue
        job_flaky, job_failed = parse_slice_log(log)
        flaky |= job_flaky
        failed |= job_failed
    issues = json.loads(api("issue", "list", "--repo", repo, "--label", TRACKER_LABEL, "--state", "open",
                            "--json", "number,body", "--limit", "5"))
    ledger: dict[str, Any] = {}
    if issues and (match := LEDGER_RE.search(issues[0].get("body") or "")):
        try:
            ledger = json.loads(match.group(1))
        except json.JSONDecodeError:
            ledger = {}
    body = render_tracker(fold_ledger(ledger, run_url, flaky, failed), run_url, conclusion)
    if issues:
        api("issue", "edit", str(issues[0]["number"]), "--repo", repo, "--body-file", "-", input_text=body)
    else:
        api("label", "create", TRACKER_LABEL, "--repo", repo, "--force", "--color", "d93f0b",
            "--description", "Nightly CI flake tracker (automated)")
        api("issue", "create", "--repo", repo, "--title", TRACKER_TITLE, "--label", TRACKER_LABEL,
            "--body-file", "-", input_text=body)
    print(f"nightly {conclusion}: {len(flaky)} flaky, {len(failed)} failed file(s)")
    for name in sorted(flaky):
        print(f"::warning::FLAKY {name}")
    for name in sorted(failed):
        print(f"::error::FAILED {name}")
    return 0


def main(argv: list[str]) -> int:
    commands = {"decide": cmd_decide, "flakes": cmd_flakes}
    if len(argv) != 1 or argv[0] not in commands:
        print("usage: main_health.py decide|flakes", file=sys.stderr)
        return 2
    return commands[argv[0]](os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
