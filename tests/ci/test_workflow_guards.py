"""Behaviour of the automated workflow/action guard that replaces the same-repo label (HA-5)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/check_workflow_guards.py"
_spec = importlib.util.spec_from_file_location("check_workflow_guards", SCRIPT)
guards = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(guards)

PIN = "actions/checkout@" + "a" * 40
BASE = f"""name: X
on: [pull_request]
permissions:
  contents: read
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: {PIN}
      - run: echo ${{{{ secrets.GITHUB_TOKEN }}}}
"""
WF = ".github/workflows/x.yml"


def _review(base, head, path=WF):
    return guards.review([(path, base, head)], yaml.safe_load)


def test_unchanged_authority_is_clean() -> None:
    assert _review(BASE, BASE.replace("echo", "printf")) == ([], [])


def test_unpinned_external_action_fails() -> None:
    failures, _ = _review(BASE, BASE.replace(PIN, "actions/checkout@v4"))
    assert failures and "actions/checkout@v4" in failures[0]
    docker = BASE.replace(PIN, "docker://alpine:3")
    assert _review(BASE, docker)[0]
    assert _review(BASE, BASE.replace(PIN, "./.github/actions/local")) == ([], [])


def test_new_authority_escalates_but_does_not_fail() -> None:
    cases = {
        "write": BASE.replace("contents: read", "contents: write"),
        "write-all": BASE.replace("permissions:\n  contents: read", "permissions: write-all"),
        "secret": BASE.replace("secrets.GITHUB_TOKEN", "secrets.RELEASE_BOT_PRIVATE_KEY"),
        "trigger": BASE.replace("on: [pull_request]", "on: [pull_request, pull_request_target]"),
        "environment": BASE.replace("    runs-on: ubuntu-latest", "    runs-on: ubuntu-latest\n    environment: agent-image-publish"),
        "removed permissions": BASE.replace("permissions:\n  contents: read\n", ""),
        "inherit": BASE + "  b:\n    uses: ./.github/workflows/y.yml\n    secrets: inherit\n",
    }
    for name, head in cases.items():
        failures, escalated = _review(BASE, head)
        assert failures == [], name
        assert escalated, name


def test_new_workflow_without_permissions_escalates() -> None:
    head = BASE.replace("permissions:\n  contents: read\n", "")
    assert _review(None, head)[1]
    assert _review(None, BASE) == ([], [])


def test_pth_change_escalates_and_deletion_is_clean() -> None:
    assert _review(None, "import os\n", "lib/x.pth")[1]
    assert _review(BASE, None) == ([], [])


def test_invalid_yaml_fails() -> None:
    assert _review(BASE, "jobs: [unclosed\n")[0]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def test_cli_reads_the_pr_diff_and_emits_escalation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "t")):
        _git(repo, "config", key, value)
    (repo / WF).write_text(BASE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / WF).write_text(BASE.replace("contents: read", "contents: write"))
    _git(repo, "commit", "-qam", "head")
    head = _git(repo, "rev-parse", "HEAD")
    out = tmp_path / "out"
    env = {**os.environ, "_HERMES_PR_BASE_SHA": base, "_HERMES_PR_HEAD_SHA": head, "GITHUB_OUTPUT": str(out)}
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.read_text() == "escalation=true\n"
    # Missing SHAs fail closed into the label gate, not a silent pass.
    out.unlink()
    env["_HERMES_PR_BASE_SHA"] = ""
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert out.read_text() == "escalation=true\n"


def test_repo_guards_routes_escalation_into_critical_findings() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/repo-guards.yml").read_text())
    job = wf["jobs"]["guards"]
    assert "steps.workflow-guard.outputs.escalation == 'true'" in job["outputs"]["critical_findings"]
    step = next(s for s in job["steps"] if s.get("id") == "workflow-guard")
    assert step["run"] == "python3 scripts/ci/check_workflow_guards.py"
    assert "switchroom/hermes-agent" in step["if"]


def test_every_current_workflow_and_action_is_pinned() -> None:
    files = [*ROOT.glob(".github/workflows/*.y*ml"), *ROOT.glob(".github/actions/**/action.y*ml")]
    assert files
    for path in files:
        assert guards.unpinned_uses(path.read_text(encoding="utf-8")) == [], path


def test_label_rerun_is_event_driven() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/label-rerun.yml").read_text())
    assert wf[True]["workflow_run"] == {"workflows": ["CI"], "types": ["completed"]}
    job = wf["jobs"]["rerun-review-labels"]
    assert job["timeout-minutes"] <= 5
    script = job["steps"][0]["run"]
    assert "gh run watch" not in script and "sleep" not in script
    assert "Fail on missing label" in script
    assert "actions/checkout" not in str(job)
