#!/usr/bin/env python3
"""Keep the fork's main green without a human: auto-revert and nightly flake tracking.

Used by ``.github/workflows/main-health.yml`` after every completed ``CI`` run
on main. ``gh`` (``GH_TOKEN``) is the only network path; nothing here runs
pull-request code.

``decide`` (a failed push run): choose what to do about it.

* ``rerun``  - first attempt: rerun the failed jobs once. A file that fails
  and then passes inside one job is already retried by run_tests_parallel, so
  this only absorbs runner/infra noise; a second red attempt is real.
* ``revert`` - the run's commit broke main: its parent's latest push CI run
  was green (or reused a green PR run), the commit is not itself an
  auto-revert, and no open auto-revert PR already exists for it.
* ``issue``  - red, but no single culprit can be named safely (parent not
  green, already reverted, or a revert of a revert): report it only.

``flakes`` (the nightly scheduled full run): read every Python test slice's
log, count FLAKY files (failed once, passed on retry) and FAILED files, and
fold them into the tracker issue's JSON ledger so recurring flakes are visible
and ranked instead of rerun by hand.
"""

from __future__ import annotations

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


def parent_is_green(runs: Iterable[Mapping[str, Any]]) -> bool:
    """The parent's newest completed CI push run on main succeeded."""
    done = [r for r in runs if r.get("event") == "push" and r.get("head_branch") == "main"
            and r.get("status") == "completed"]
    if not done:
        return False
    latest = max(done, key=lambda r: (str(r.get("created_at", "")), int(r.get("id", 0))))
    return latest.get("conclusion") == "success"


def decide(*, attempt: int, subject: str, parents: list[str], parent_runs: list[Mapping[str, Any]],
           open_revert_prs: int) -> tuple[str, str]:
    """Return (action, reason) for a failed main push CI run."""
    if attempt <= 1:
        return "rerun", "first red attempt; rerunning failed jobs once"
    if subject.startswith("Revert ") or subject.startswith("revert:"):
        return "issue", "the red commit is itself a revert; not reverting a revert"
    if open_revert_prs:
        return "issue", "an auto-revert PR for this commit is already open"
    if len(parents) not in (1, 2):
        return "issue", "commit has no usable parent"
    if not parent_is_green(parent_runs):
        return "issue", "main was not green before this commit; no single culprit"
    return "revert", "parent green, this commit red on two attempts"


def cmd_decide(env: Mapping[str, str], api: Api = gh) -> int:
    repo, sha = env["REPO"], env["HEAD_SHA"]
    attempt = int(env.get("RUN_ATTEMPT") or "1")
    commit = json.loads(api("api", f"repos/{repo}/commits/{sha}"))
    parents = [p["sha"] for p in commit.get("parents", [])]
    subject = commit.get("commit", {}).get("message", "").splitlines()[0] if commit.get("commit") else ""
    parent_runs: list[Mapping[str, Any]] = []
    if parents:
        payload = json.loads(api("api", f"repos/{repo}/actions/workflows/ci.yaml/runs?event=push&head_sha={parents[0]}&per_page=20"))
        parent_runs = payload.get("workflow_runs", [])
    branch = f"{REVERT_BRANCH_PREFIX}{sha[:12]}"
    open_prs = json.loads(api("pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number"))
    action, reason = decide(attempt=attempt, subject=subject, parents=parents,
                            parent_runs=parent_runs, open_revert_prs=len(open_prs))
    print(f"::notice::{action}: {reason}")
    _write_outputs({"action": action, "reason": reason, "branch": branch,
                    "mainline": "1" if len(parents) == 2 else "", "subject": subject.replace("\n", " ")})
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
