#!/usr/bin/env python3
"""Tree-dedupe for the fork's main push CI (HA-1).

Every recent fork merge commit has exactly the tree its PR run already tested
(the ruleset is strict, so the PR's merge ref *is* the squash/merge result).
Re-running 14-17 minutes of identical work on push only delays the image gate.

``record`` (fork pull_request runs): write ``plan.json`` for the tested tree
(the checked-out PR merge commit) so the caller uploads it as the artifact
``ci-plan-<tree>``.

``lookup`` (fork pushes to main): reuse a PR run only when ALL of these hold,
else run everything (fail open on any doubt or API error). The binding follows
switchroom #397, because GitHub clears ``run.pull_requests`` once a PR merges:

* the push commit is the merge commit of exactly one merged PR into ``main``
  (direct pushes have none and always run full);
* the push's ``before`` (main's previous tip) is an ancestor of, or equal to,
  the PR head, so the PR was current with main and its merge result is its
  head tree; that tree equals this push's tree;
* the PR head commit is unique to this PR (no other PR shares it);
* the newest ``pull_request`` run of ``.github/workflows/ci.yaml`` for that
  head sha, from this repository and the PR's head branch, completed
  ``success``;
* the PR's base ref was not changed at or after that run started (it tested
  against ``main``, not a stacked parent it was later retargeted from);
* that run's own ``ci-plan-<tree>`` artifact covers this push's plan: every
  selected job, every true gate lane, and equal file lists and matrices.

On reuse it prints ``reuse=true``, ``reused_from=<run url>`` and a
``selected_jobs`` map with every job but ``detect`` off. The aggregate gates
``All required checks pass`` / ``Image required checks pass`` still run under
their exact names, so ``verify-exact-main-ci.py`` is unchanged. A nightly
scheduled full run (ci.yaml ``schedule``) is the post-merge backstop and the
duration feed.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping
from typing import Any

FORK = "switchroom/hermes-agent"
WORKFLOW_PATH = ".github/workflows/ci.yaml"
PLAN_KEYS = ("python_files", "python_matrix", "js_matrix")
SHA_RE = re.compile(r"[0-9a-f]{40}")

Api = Callable[[str], bytes]


def _gh_api(path: str) -> bytes:
    result = subprocess.run(["gh", "api", path], capture_output=True, check=False, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"gh api {path} failed")
    return result.stdout


def _json(api: Api, path: str) -> Any:
    return json.loads(api(path))


def _tree(ref: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", f"{ref}^{{tree}}"], text=True, encoding="utf-8",
                          capture_output=True, check=True, timeout=30).stdout.strip()


def build_plan(tree: str, env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "tree": tree,
        "selected_jobs": json.loads(env.get("SELECTED_JOBS") or "{}"),
        "gate_lanes": json.loads(env.get("GATE_LANES") or "{}"),
        **{key: env.get(key.upper(), "") for key in PLAN_KEYS},
    }


def plan_covers(pr_plan: Mapping[str, Any], push_plan: Mapping[str, Any]) -> bool:
    """The PR run tested at least everything this push would run."""
    pr_jobs = pr_plan.get("selected_jobs")
    push_jobs = push_plan.get("selected_jobs")
    if not isinstance(pr_jobs, dict) or not isinstance(push_jobs, dict) or not push_jobs:
        return False
    if any(required is True and pr_jobs.get(name) is not True for name, required in push_jobs.items()):
        return False
    pr_lanes, push_lanes = pr_plan.get("gate_lanes"), push_plan.get("gate_lanes")
    if not isinstance(pr_lanes, dict) or not isinstance(push_lanes, dict) or not push_lanes:
        return False
    for lane, value in push_lanes.items():
        if value == "false":
            continue  # the push would not run it; the PR may have
        if pr_lanes.get(lane) != value:
            return False  # a true lane the PR did not run, or a different file list / matrix
    return all(pr_plan.get(key) == push_plan.get(key) for key in PLAN_KEYS)


def _artifact_plan(api: Api, repository: str, artifact_id: int) -> dict[str, Any] | None:
    data = api(f"repos/{repository}/actions/artifacts/{artifact_id}/zip")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if "plan.json" not in archive.namelist():
            return None
        plan = json.loads(archive.read("plan.json"))
    return plan if isinstance(plan, dict) else None


def find_reusable_run(api: Api, repository: str, sha: str, before: str, tree: str,
                      push_plan: Mapping[str, Any], log: Callable[[str], Any] = print) -> str | None:
    """Return the html_url of the green PR run that already tested this push, else None."""
    if not all(SHA_RE.fullmatch(v or "") and v != "0" * 40 for v in (sha, before)):
        log("no exact before/after range (new branch or forced push)")
        return None
    merged = [pr for pr in _json(api, f"repos/{repository}/commits/{sha}/pulls")
              if isinstance(pr, dict) and pr.get("merged_at") and pr.get("merge_commit_sha") == sha
              and (pr.get("base") or {}).get("ref") == "main"]
    if len(merged) != 1:
        log(f"{len(merged)} merged PRs into main for {sha} (direct push: full run)")
        return None
    pr = merged[0]
    number = int(pr["number"])
    head = str((pr.get("head") or {}).get("sha") or "")
    branch = (pr.get("head") or {}).get("ref")
    if not SHA_RE.fullmatch(head) or not branch:
        return None
    head_tree = _json(api, f"repos/{repository}/git/commits/{head}").get("tree", {}).get("sha")
    if head_tree != tree:
        log(f"PR #{number} head tree {head_tree} differs from {tree}")
        return None
    status = _json(api, f"repos/{repository}/compare/{before}...{head}").get("status")
    if status not in ("ahead", "identical"):
        log(f"{before} is not an ancestor of PR #{number} head {head} ({status})")
        return None
    sharing = [p.get("number") for p in _json(api, f"repos/{repository}/commits/{head}/pulls")
               if isinstance(p, dict) and (p.get("head") or {}).get("sha") == head]
    if sharing != [number]:
        log(f"head {head} is not unique to PR #{number} ({sharing})")
        return None
    listing = _json(api, f"repos/{repository}/actions/runs?event=pull_request&head_sha={head}&per_page=100")
    runs = [r for r in listing.get("workflow_runs", [])
            if isinstance(r, dict) and r.get("head_sha") == head and r.get("path") == WORKFLOW_PATH
            and r.get("event") == "pull_request" and r.get("head_branch") == branch
            and (r.get("head_repository") or {}).get("full_name") == repository]
    if not runs:
        log(f"no same-repo ci.yaml PR run for {head} on {branch}")
        return None
    run = max(runs, key=lambda r: (str(r.get("created_at", "")), int(r.get("id", 0))))
    url = str(run.get("html_url") or run.get("id"))
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        log(f"newest PR run {url} is {run.get('status')}/{run.get('conclusion')}")
        return None
    events = _json(api, f"repos/{repository}/issues/{number}/events?per_page=100")
    if not isinstance(events, list) or len(events) >= 100:
        log(f"cannot read every event of PR #{number}")
        return None
    started = str(run.get("created_at", ""))
    if not started or any(str(e.get("created_at", "")) >= started
                          for e in events if isinstance(e, dict) and e.get("event") == "base_ref_changed"):
        log(f"PR #{number} base ref changed at or after {url} started")
        return None
    artifacts = _json(api, f"repos/{repository}/actions/runs/{int(run['id'])}/artifacts?name=ci-plan-{tree}")
    found = [a for a in artifacts.get("artifacts", []) if isinstance(a, dict) and not a.get("expired")
             and a.get("name") == f"ci-plan-{tree}"]
    if len(found) != 1:
        log(f"{url} has {len(found)} ci-plan-{tree} artifacts")
        return None
    plan = _artifact_plan(api, repository, int(found[0]["id"]))
    if plan is None or plan.get("tree") != tree:
        log("plan artifact missing or for another tree")
        return None
    if not plan_covers(plan, push_plan):
        log(f"PR plan of {url} narrower than this push's plan")
        return None
    return url


def _write_outputs(values: Mapping[str, str]) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    print(lines, end="")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as output:
            output.write(lines)


def record(out_dir: str) -> int:
    tree = _tree()
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8") as handle:  # windows-footgun: ok (write)
        json.dump(build_plan(tree, os.environ), handle, sort_keys=True)
    _write_outputs({"tree": tree})
    return 0


def lookup() -> int:
    repository = os.environ.get("REPO", "")
    try:
        tree = _tree()
        push_plan = build_plan(tree, os.environ)
        sha, before = os.environ.get("PUSH_SHA", ""), os.environ.get("PUSH_BEFORE", "")
        url = find_reusable_run(_gh_api, repository, sha, before, tree, push_plan) if repository == FORK else None
    except Exception as exc:  # health: allow BLE001 -- fail-open boundary: any lookup error runs the full push CI
        print(f"::notice::Tree-dedupe lookup failed ({exc}); running the full push CI.")
        url = None
    if url is None:
        _write_outputs({"reuse": "false"})
        return 0
    selected = {name: name == "detect" for name in push_plan["selected_jobs"]}
    print(f"::notice::Tree {tree} was already tested green by {url}; reusing it.")
    _write_outputs({
        "reuse": "true",
        "reused_from": url,
        "selected_jobs": json.dumps(selected, separators=(",", ":")),
    })
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["record"] and len(argv) == 2:
        return record(argv[1])
    if argv == ["lookup"]:
        return lookup()
    print("usage: ci_reuse.py record <dir> | lookup", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
