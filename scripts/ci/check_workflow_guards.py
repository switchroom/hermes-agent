#!/usr/bin/env python3
"""Automated review of a pull request's workflow and action changes (fork, HA-5).

Replaces the ``ci-reviewed`` label for same-repository pull requests that touch
``.github/workflows`` or ``.github/actions``. Two kinds of result:

* **Hard failures** (exit 1): an external ``uses:`` that is not pinned to a
  40-hex commit SHA (or a ``docker://`` image pinned by ``@sha256:``), or a
  changed workflow/action file that is not valid YAML.
* **Escalations** (``escalation=true`` on ``$GITHUB_OUTPUT``, exit 0): the
  change grants more authority than the base revision had. repo-guards feeds
  this into ``critical_findings``, which routes the PR to the existing review
  label gate. An escalation is any of:

  - a new write permission scope (or ``write-all``) anywhere in a file, or a
    job/workflow ``permissions:`` block removed (falls back to the default
    token permissions);
  - a new ``secrets.<NAME>`` reference (``GITHUB_TOKEN`` excepted) or a new
    ``secrets: inherit``;
  - a new ``environment:`` (protected-environment credentials);
  - a new privileged trigger (``pull_request_target``, ``workflow_run``, ...);
  - a changed ``.pth`` file (code that runs at interpreter start-up).

Anything the guard cannot judge (missing base/head, unreadable diff, no YAML
parser) fails closed as an escalation, never as a silent pass.

Environment: ``_HERMES_PR_BASE_SHA`` and ``_HERMES_PR_HEAD_SHA``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable
from typing import Any

WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
ACTION_RE = re.compile(r"^\.github/actions/.+/action\.ya?ml$")
USES_RE = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)")
PINNED_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
DOCKER_PINNED_RE = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
SECRET_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
INHERIT_RE = re.compile(r"(?m)^\s*secrets:\s*inherit\s*$")
PRIVILEGED_TRIGGERS = frozenset({
    "pull_request_target", "workflow_run", "issue_comment", "issues",
    "pull_request_review", "pull_request_review_comment", "discussion",
    "discussion_comment", "repository_dispatch", "check_run", "check_suite",
    "deployment", "deployment_status", "status",
})


def guarded_path(path: str) -> bool:
    return bool(WORKFLOW_RE.match(path) or ACTION_RE.match(path))


def unpinned_uses(text: str) -> list[str]:
    bad = []
    for ref in USES_RE.findall(text):
        if ref.startswith("./") or PINNED_RE.match(ref) or DOCKER_PINNED_RE.match(ref):
            continue
        bad.append(ref)
    return bad


def _triggers(doc: dict[Any, Any]) -> set[str]:
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` key as True.
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    if isinstance(on, dict):
        return {str(key) for key in on}
    return set()


def _write_scopes(block: Any) -> set[str]:
    if block == "write-all":
        return {"*write-all*"}
    if isinstance(block, dict):
        return {str(scope) for scope, level in block.items() if level == "write"}
    return set()


def _permission_blocks(doc: dict[Any, Any]) -> dict[str, Any]:
    blocks: dict[str, Any] = {}
    if "permissions" in doc:
        blocks["<workflow>"] = doc["permissions"]
    for name, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict) and "permissions" in job:
            blocks[str(name)] = job["permissions"]
    return blocks


def _environments(doc: dict[Any, Any]) -> set[str]:
    names = set()
    for job in (doc.get("jobs") or {}).values():
        env = job.get("environment") if isinstance(job, dict) else None
        if isinstance(env, dict):
            env = env.get("name")
        if env:
            names.add(str(env))
    return names


def escalations(path: str, base: str | None, head: str, load: Any) -> list[str]:
    """Authority the head revision of one file has that its base did not."""
    found: list[str] = []
    base_text = base or ""
    base_secrets = set(SECRET_RE.findall(base_text)) | {"GITHUB_TOKEN"}
    for name in sorted(set(SECRET_RE.findall(head)) - base_secrets):
        found.append(f"{path}: new secret reference secrets.{name}")
    if INHERIT_RE.search(head) and not INHERIT_RE.search(base_text):
        found.append(f"{path}: new `secrets: inherit`")
    if not WORKFLOW_RE.match(path):
        return found
    head_doc = load(head) or {}
    base_doc = (load(base_text) or {}) if base_text else {}
    if not isinstance(head_doc, dict) or not isinstance(base_doc, dict):
        return [*found, f"{path}: not a YAML mapping"]
    for trigger in sorted((_triggers(head_doc) - _triggers(base_doc)) & PRIVILEGED_TRIGGERS):
        found.append(f"{path}: new privileged trigger `{trigger}`")
    head_blocks, base_blocks = _permission_blocks(head_doc), _permission_blocks(base_doc)
    base_writes = set().union(*map(_write_scopes, base_blocks.values())) if base_blocks else set()
    head_writes = set().union(*map(_write_scopes, head_blocks.values())) if head_blocks else set()
    for scope in sorted(head_writes - base_writes):
        found.append(f"{path}: new write permission `{scope}`")
    for owner in sorted(set(base_blocks) - set(head_blocks)):
        if owner == "<workflow>" or "<workflow>" not in head_blocks:
            found.append(f"{path}: `permissions:` removed from {owner} (default token scope)")
    if not base_text and "<workflow>" not in head_blocks and any(
        isinstance(job, dict) and "permissions" not in job for job in (head_doc.get("jobs") or {}).values()
    ):
        found.append(f"{path}: new workflow has a job on the default token scope (no `permissions:`)")
    for env in sorted(_environments(head_doc) - _environments(base_doc)):
        found.append(f"{path}: new environment `{env}`")
    return found


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], text=True, encoding="utf-8", capture_output=True, check=True, timeout=60).stdout


def _show(sha: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{sha}:{path}"], text=True, encoding="utf-8", capture_output=True, check=False, timeout=60)
    return result.stdout if result.returncode == 0 else None


def review(changes: Iterable[tuple[str, str | None, str | None]], load: Any) -> tuple[list[str], list[str]]:
    """Return (hard failures, escalations) for (path, base_text, head_text) triples."""
    failures: list[str] = []
    escalated: list[str] = []
    for path, base, head in changes:
        if path.endswith(".pth") and head is not None:
            escalated.append(f"{path}: .pth start-up hook changed")
            continue
        if head is None or not guarded_path(path):
            continue  # deletions remove authority; other paths are not guarded here
        for ref in unpinned_uses(head):
            failures.append(f"{path}: `uses: {ref}` is not pinned to a full commit SHA")
        try:
            escalated.extend(escalations(path, base, head, load))
        except Exception as exc:  # health: allow BLE001 -- any parse failure is reported as a hard failure
            failures.append(f"{path}: cannot parse ({exc.__class__.__name__}: {exc})")
    return failures, escalated


def _emit(escalated: list[str]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write(f"escalation={'true' if escalated else 'false'}\n")


def main() -> int:
    base = os.environ.get("_HERMES_PR_BASE_SHA", "")
    head = os.environ.get("_HERMES_PR_HEAD_SHA", "")
    try:
        import yaml  # PyYAML ships with the hosted runner's system Python.
    except ImportError:
        print("::warning::PyYAML unavailable; routing this PR to the review label gate (fail closed).")
        _emit(["no YAML parser"])
        return 0
    try:
        if not (re.fullmatch(r"[0-9a-f]{40}", base) and re.fullmatch(r"[0-9a-f]{40}", head)):
            raise ValueError("base/head SHA missing")
        merge_base = _git("merge-base", base, head).strip()
        paths = [p for p in _git("diff", "--name-only", "--no-renames", merge_base, head).splitlines() if p]
    except Exception as exc:  # health: allow BLE001 -- fail closed into the label gate
        print(f"::warning::Cannot read the PR diff ({exc}); routing to the review label gate (fail closed).")
        _emit(["unreadable diff"])
        return 0
    changes = [(p, _show(merge_base, p), _show(head, p)) for p in paths if guarded_path(p) or p.endswith(".pth")]
    failures, escalated = review(changes, yaml.safe_load)
    for item in failures:
        print(f"::error::{item}")
    for item in escalated:
        print(f"::warning::Authority escalation (needs the review label gate): {item}")
    if not changes:
        print("No workflow, action or .pth changes.")
    elif not failures and not escalated:
        print(f"{len(changes)} workflow/action change(s): pinned, no new authority.")
    _emit(escalated)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
