#!/usr/bin/env python3
"""Tree-dedupe for the fork's main push CI (HA-1).

Every recent fork merge commit has exactly the tree its PR run already tested
(the ruleset is strict, so the PR's merge ref *is* the squash/merge result).
Re-running 14-17 minutes of identical work on push only delays the image gate.

``record`` (fork pull_request runs): write ``plan.json`` for the tested tree
(the checked-out PR merge commit) so the caller uploads it as the artifact
``ci-plan-<tree>``.

``lookup`` (fork pushes to main): find a ``ci-plan-<tree>`` artifact for this
commit's tree and reuse that PR run only when ALL of these hold, else run
everything (fail open on any doubt or API error):

* the run is a ``pull_request`` run of ``.github/workflows/ci.yaml`` from a
  branch of this repository (never a cross-repo fork), completed ``success``;
* the run's PR head commit has this exact tree (checked through the API, not
  only the artifact's own claim), and so does the artifact;
* every job this push would select was selected by the PR run, and the PR
  ran the same Python file selection and matrices (no narrowing the push
  would not also have had).

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
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping
from typing import Any

FORK = "switchroom/hermes-agent"
WORKFLOW_PATH = ".github/workflows/ci.yaml"
PLAN_KEYS = ("python_files", "python_matrix", "js_matrix")
MAX_CANDIDATES = 5

Api = Callable[[str], bytes]


def _gh_api(path: str) -> bytes:
    result = subprocess.run(["gh", "api", path], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"gh api {path} failed")
    return result.stdout


def _json(api: Api, path: str) -> Any:
    return json.loads(api(path))


def _tree(ref: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", f"{ref}^{{tree}}"], text=True,
                          capture_output=True, check=True).stdout.strip()


def build_plan(tree: str, env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "tree": tree,
        "selected_jobs": json.loads(env.get("SELECTED_JOBS") or "{}"),
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
    return all(pr_plan.get(key) == push_plan.get(key) for key in PLAN_KEYS)


def _artifact_plan(api: Api, repository: str, artifact_id: int) -> dict[str, Any] | None:
    data = api(f"repos/{repository}/actions/artifacts/{artifact_id}/zip")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if "plan.json" not in archive.namelist():
            return None
        plan = json.loads(archive.read("plan.json"))
    return plan if isinstance(plan, dict) else None


def find_reusable_run(api: Api, repository: str, tree: str, push_plan: Mapping[str, Any],
                      log: Callable[[str], Any] = print) -> str | None:
    """Return the html_url of a reusable green PR run for ``tree``, else None."""
    listing = _json(api, f"repos/{repository}/actions/artifacts?name=ci-plan-{tree}&per_page=20")
    artifacts = [a for a in listing.get("artifacts", []) if isinstance(a, dict) and not a.get("expired")]
    artifacts.sort(key=lambda a: str(a.get("created_at", "")), reverse=True)
    for artifact in artifacts[:MAX_CANDIDATES]:
        run_id = (artifact.get("workflow_run") or {}).get("id")
        if type(run_id) is not int:
            continue
        run = _json(api, f"repos/{repository}/actions/runs/{run_id}")
        why = None
        if run.get("event") != "pull_request" or run.get("path") != WORKFLOW_PATH:
            why = "not a ci.yaml pull_request run"
        elif (run.get("head_repository") or {}).get("full_name") != repository:
            why = "cross-repository PR"
        elif run.get("status") != "completed" or run.get("conclusion") != "success":
            why = f"run is {run.get('status')}/{run.get('conclusion')}"
        else:
            head_tree = _json(api, f"repos/{repository}/git/commits/{run.get('head_sha')}").get("tree", {}).get("sha")
            if head_tree != tree:
                why = f"PR head tree {head_tree} differs"
            else:
                plan = _artifact_plan(api, repository, int(artifact["id"]))
                if plan is None or plan.get("tree") != tree:
                    why = "plan artifact missing or for another tree"
                elif not plan_covers(plan, push_plan):
                    why = "PR plan narrower than this push's plan"
        if why is None:
            return str(run.get("html_url") or run_id)
        log(f"candidate run {run_id}: {why}")
    return None


def _write_outputs(values: Mapping[str, str]) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    print(lines, end="")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as output:
            output.write(lines)


def record(out_dir: str) -> int:
    tree = _tree()
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8") as handle:
        json.dump(build_plan(tree, os.environ), handle, sort_keys=True)
    _write_outputs({"tree": tree})
    return 0


def lookup() -> int:
    repository = os.environ.get("REPO", "")
    try:
        tree = _tree()
        push_plan = build_plan(tree, os.environ)
        url = find_reusable_run(_gh_api, repository, tree, push_plan) if repository == FORK else None
    except Exception as exc:  # any lookup failure runs the full push CI
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
