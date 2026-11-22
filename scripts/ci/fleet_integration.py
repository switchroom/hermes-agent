#!/usr/bin/env python3
"""Scope and result record for .github/workflows/fleet-integration.yml.

`scope` decides whether a same-repository PR runs switchroom's real-Agent legs. Push to main
always runs: a switchroom pin PR reuses only a push run of the exact pinned commit. A PR skips
only when every changed path is one the fleet runtime never loads (docs, the website, desktop
and web front ends, editor and JS tooling, other workflows). Any doubt runs.

`result` checks every expected leg job of this run, writes the result artifact, and emits the
same record as a `fleet-integration-result` notice annotation, which switchroom's
scripts/source-ci.py (find_pin_reuse) reads and validates field by field.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "fleet-integration/v1"
WORKFLOW_PATH = ".github/workflows/fleet-integration.yml"
SWITCHROOM = "switchroom/switchroom"
RECORD_TITLE = "fleet-integration-result"
# Changing either of these changes what the workflow proves, so it always runs.
SELF = {WORKFLOW_PATH, "scripts/ci/fleet_integration.py", "tests/ci/test_fleet_integration_workflow.py"}
NON_RUNTIME_PREFIXES = (
    "website/", "docs/", "apps/", "web/", "ui-tui/", "tests-js/", "assets/", "contributors/",
    "nix/", ".github/",
)
NON_RUNTIME_FILES = {
    ".coderabbit.yaml", ".prettierrc", ".prettierignore", ".mailmap", ".nvmrc", ".npmrc",
    ".hadolint.yaml", "eslint.config.shared.mjs", "flake.nix", "flake.lock", "package.json",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "lefthook.yml", "LICENSE",
}
SHA = re.compile(r"[0-9a-f]{40}")


def affects_runtime(path: str) -> bool:
    if path in SELF:
        return True
    if path in NON_RUNTIME_FILES or path.startswith(NON_RUNTIME_PREFIXES):
        return False
    # Top-level prose only: Markdown inside packages (skills, plugins) can be runtime input.
    return not ("/" not in path and path.endswith(".md"))


def should_run(event: str, paths: list[str] | None) -> tuple[bool, str]:
    if event != "pull_request":
        return True, f"{event}: always runs"
    if not paths:
        return True, "changed paths unknown: runs"
    hits = [p for p in paths if affects_runtime(p)]
    if hits:
        return True, f"runtime-affecting paths, e.g. {hits[:5]}"
    return False, "only paths the fleet runtime never loads"


def changed_paths(repo: Path, base: str, head: str) -> list[str] | None:
    if not (SHA.fullmatch(base or "") and SHA.fullmatch(head or "")):
        return None
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "diff", "--name-only", "-z", "--no-renames", f"{base}...{head}", "--"],
            stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in raw.decode("utf-8", "replace").split("\0") if p]


def collect(jobs: list[dict], expected: list[str]) -> dict[str, str]:
    legs = {}
    for name in expected:
        found = [j for j in jobs if j.get("name") == f"fleet leg ({name})"]
        legs[name] = str(found[0].get("conclusion")) if len(found) == 1 else "missing"
    return legs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scope = sub.add_parser("scope")
    scope.add_argument("--event", required=True)
    scope.add_argument("--base", default="")
    scope.add_argument("--head", default="")
    scope.add_argument("--repo", type=Path, default=Path("."))
    scope.add_argument("--files", nargs="*", default=None, help="explicit paths (tests)")
    result = sub.add_parser("result")
    result.add_argument("--jobs", type=Path, required=True, help="this run's jobs API response (JSON)")
    result.add_argument("--context", type=Path, required=True, help="JSON of run facts from the workflow")
    result.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "scope":
        try:
            paths = args.files if args.files is not None else changed_paths(args.repo, args.base, args.head)
            run, why = should_run(args.event, paths)
        except Exception as exc:  # Fail open to running.
            run, why = True, f"scope error ({type(exc).__name__}): runs"
        print(f"fleet integration: run={str(run).lower()} ({why})", file=sys.stderr)
        print(f"run={str(run).lower()}")
        return 0

    ctx = json.loads(args.context.read_text(encoding="utf-8"))
    expected = [leg["name"] for leg in json.loads(ctx["legs"] or "[]")]
    ran = ctx["run"] == "true"
    record = {
        "schema": SCHEMA,
        "event": ctx["event"], "ref": ctx["ref"], "workflow": WORKFLOW_PATH,
        "run_id": int(ctx["run_id"]), "run_attempt": int(ctx["run_attempt"]),
        "agent_repository": ctx["repository"], "agent_commit": ctx["agent_commit"],
        "agent_tree": ctx["agent_tree"], "agent_pr_head": ctx.get("pr_head") or None,
        "switchroom_repository": SWITCHROOM, "switchroom_commit": ctx["switchroom_commit"],
        "switchroom_tree": ctx["switchroom_tree"], "ran_legs": ran, "scope": ctx["scope_result"],
        "legs": {},
    }
    scope_ok = ctx["scope_result"] == "success"
    if ran:
        jobs = json.loads(args.jobs.read_text(encoding="utf-8")).get("jobs", [])
        record["legs"] = collect(jobs, expected)
        ok = (scope_ok and bool(expected) and ctx["leg_result"] == "success"
              and all(v == "success" for v in record["legs"].values()))
    else:
        # Only a PR may skip, and only when scope said so.
        ok = scope_ok and ctx["run"] == "false" and ctx["leg_result"] == "skipped" and ctx["event"] == "pull_request"
    if ok and not all(SHA.fullmatch(str(record[k] or "")) for k in
                      ("agent_commit", "agent_tree", "switchroom_commit", "switchroom_tree")):
        ok = False
    record["result"] = "success" if ok else "failure"
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, sort_keys=True))
    # The same record as one notice annotation on this job's check run: switchroom reads it
    # there, since its workflow token cannot download this repository's artifacts.
    compact = json.dumps(record, sort_keys=True, separators=(",", ":"))
    print("::notice title=" + RECORD_TITLE + "::" + compact.replace("%", "%25"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
