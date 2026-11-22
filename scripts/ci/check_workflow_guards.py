#!/usr/bin/env python3
"""Automated review of a pull request's workflow and action changes (fork, HA-5).

Replaces the ``ci-reviewed`` label for same-repository pull requests that touch
``.github/workflows`` or ``.github/actions``. Two kinds of result:

* **Hard failures** (exit 1): an external ``uses:`` (found by walking the
  parsed YAML, so flow-style steps count) that is not pinned to a 40-hex
  commit SHA (or a ``docker://`` image pinned by ``@sha256:``), or a changed
  workflow/action file that is not valid YAML.
* **Escalations** (``escalation=true`` on ``$GITHUB_OUTPUT``, exit 0): the
  change grants more authority than the base revision had. repo-guards feeds
  this into ``critical_findings``, which routes the PR to the existing review
  label gate. An escalation is any of:

  - a new write permission scope (or ``write-all``) anywhere in a file, or a
    job/workflow ``permissions:`` block removed (falls back to the default
    token permissions);
  - a new ``secrets`` reference in any expression (``secrets.NAME``,
    ``secrets['NAME']``, or a whole-context use such as ``toJSON(secrets)``;
    ``GITHUB_TOKEN`` excepted) or a new ``secrets: inherit``;
  - a pinned external ``uses:`` (owner/repo and SHA) that no base workflow or
    action already used: a swapped owner or an unknown (possibly impostor)
    commit needs a human;
  - a new ``environment:`` (protected-environment credentials);
  - a new privileged trigger (``pull_request_target``, ``workflow_run``, ...);
  - a changed ``.pth`` file (code that runs at interpreter start-up);
  - a change to the guard machinery itself (``GUARD_FILES``, and in
    ``ci.yaml`` the detect job, repo-guards, review-labels and the two
    aggregate gate jobs). repo-guards
    also runs this script from the base revision when the base has it, so a
    PR cannot weaken the check that judges it.

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
PINNED_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
DOCKER_PINNED_RE = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
SECRET_REF_RE = re.compile(
    r"\bsecrets\b(?:\s*\.\s*([A-Za-z_][A-Za-z0-9_-]*)|\s*\[\s*['\"]([^'\"]+)['\"]\s*\])?",
    re.IGNORECASE,  # expression contexts are case-insensitive
)
WHOLE_SECRETS = "secrets[*]"
GUARD_FILES = frozenset({
    "scripts/ci/check_workflow_guards.py", "scripts/ci/ci_policy.py",
    "scripts/ci/fork_review_files.py", "scripts/ci/classify_changes.py",
    ".github/workflows/repo-guards.yml", ".github/workflows/review-labels.yml",
    ".github/workflows/label-rerun.yml", "scripts/ci/required_results.py",
    "scripts/ci/emit_review_status.py", ".github/actions/detect-changes/action.yml",
})
# ci.yaml is edited routinely, so only the parts that route an escalation to the label gate
# are guard machinery: the whole detect job (lanes, event_name, selection), repo-guards,
# review-labels and the two gates.
CI_WORKFLOW = ".github/workflows/ci.yaml"
CI_GATE_JOBS = ("detect", "repo-guards", "review-labels", "all-checks-pass", "image-checks-pass")
PRIVILEGED_TRIGGERS = frozenset({
    "pull_request_target", "workflow_run", "issue_comment", "issues",
    "pull_request_review", "pull_request_review_comment", "discussion",
    "discussion_comment", "repository_dispatch", "check_run", "check_suite",
    "deployment", "deployment_status", "status",
})


def guarded_path(path: str) -> bool:
    return bool(WORKFLOW_RE.match(path) or ACTION_RE.match(path))


def _walk(node: Any, key: str = ""):
    """Yield (key, scalar) for every string scalar in a parsed YAML tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, str(k))
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, key)
    elif isinstance(node, str):
        yield key, node


def uses_refs(doc: Any) -> set[str]:
    """Every ``uses:`` value anywhere in the parsed file (steps, reusable jobs, flow style)."""
    return {value.strip() for key, value in _walk(doc) if key == "uses"}


def _external(ref: str) -> bool:
    return not ref.startswith("./")


def unpinned_uses(doc: Any) -> list[str]:
    return sorted(
        ref for ref in uses_refs(doc)
        if _external(ref) and not (PINNED_RE.match(ref) or DOCKER_PINNED_RE.match(ref))
    )


def secret_refs(doc: Any) -> set[str]:
    """Normalised secrets references in expressions (``if:`` values are expressions too)."""
    found: set[str] = set()
    for key, value in _walk(doc):
        exprs = EXPR_RE.findall(value)
        if key == "if" and "${{" not in value:
            exprs.append(value)
        for expr in exprs:
            for dotted, indexed in SECRET_REF_RE.findall(expr):
                name = dotted or indexed
                found.add(f"secrets.{name.upper()}" if name else WHOLE_SECRETS)
    found.discard("secrets.GITHUB_TOKEN")
    return found


def _inherits(doc: Any) -> bool:
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    return any(isinstance(job, dict) and job.get("secrets") == "inherit" for job in (jobs or {}).values())


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


def ci_gate_wiring(doc: Any) -> Any:
    """The parts of ci.yaml that decide whether the label gate runs and is enforced."""
    jobs = (doc.get("jobs") or {}) if isinstance(doc, dict) else {}
    return {name: jobs.get(name) for name in CI_GATE_JOBS}


def escalations(path: str, base: str | None, head: str, load: Any, known_uses: set[str]) -> list[str]:
    """Authority the head revision of one file has that its base did not."""
    found: list[str] = []
    head_doc = load(head) or {}
    base_doc = (load(base) or {}) if base else {}
    if not isinstance(head_doc, dict) or not isinstance(base_doc, dict):
        return [f"{path}: not a YAML mapping"]
    for ref in sorted(secret_refs(head_doc) - secret_refs(base_doc)):
        found.append(f"{path}: new secret reference `{ref}`")
    if _inherits(head_doc) and not _inherits(base_doc):
        found.append(f"{path}: new `secrets: inherit`")
    for ref in sorted(r for r in uses_refs(head_doc) - known_uses if _external(r)):
        found.append(f"{path}: new external action `{ref}` (not used anywhere in the base revision)")
    if not WORKFLOW_RE.match(path):
        return found
    if path == CI_WORKFLOW and ci_gate_wiring(head_doc) != ci_gate_wiring(base_doc):
        found.append(f"{path}: the label-gate wiring changed (detect, repo-guards, review-labels or a gate job)")
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
    if "<workflow>" not in head_blocks:
        base_jobs = base_doc.get("jobs") or {}
        for name, job in (head_doc.get("jobs") or {}).items():
            if isinstance(job, dict) and "permissions" not in job and name not in base_jobs:
                found.append(f"{path}: new job `{name}` runs on the default token scope (no `permissions:`)")
    for env in sorted(_environments(head_doc) - _environments(base_doc)):
        found.append(f"{path}: new environment `{env}`")
    return found


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], text=True, encoding="utf-8", capture_output=True, check=True, timeout=60).stdout


def _show(sha: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{sha}:{path}"], text=True, encoding="utf-8", capture_output=True, check=False, timeout=60)
    return result.stdout if result.returncode == 0 else None


def review(
    changes: Iterable[tuple[str, str | None, str | None]], load: Any, known_uses: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (hard failures, escalations) for (path, base_text, head_text) triples.

    ``known_uses`` is every ``uses:`` value in the base revision's workflows and
    actions; when omitted it is taken from the base texts of ``changes``.
    """
    changes = list(changes)
    failures: list[str] = []
    escalated: list[str] = []
    if known_uses is None:
        known_uses = set()
        for path, base, _ in changes:
            if base and guarded_path(path):
                try:
                    known_uses |= uses_refs(load(base))
                except Exception:  # health: allow BLE001 -- an unparsable base contributes nothing
                    pass
    for path, base, head in changes:
        if path in GUARD_FILES:
            escalated.append(f"{path}: the workflow guard machinery changed")
            if not guarded_path(path):
                continue
        if path.endswith(".pth") and head is not None:
            escalated.append(f"{path}: .pth start-up hook changed")
            continue
        if head is None or not guarded_path(path):
            continue  # deletions remove authority; other paths are not guarded here
        try:
            for ref in unpinned_uses(load(head)):
                failures.append(f"{path}: `uses: {ref}` is not pinned to a full commit SHA")
            escalated.extend(escalations(path, base, head, load, known_uses))
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
    changes = [
        (p, _show(merge_base, p), _show(head, p))
        for p in paths if guarded_path(p) or p.endswith(".pth") or p in GUARD_FILES
    ]
    known_uses: set[str] = set()
    for p in _git("ls-tree", "-r", "--name-only", merge_base, ".github").splitlines():
        text = _show(merge_base, p) if guarded_path(p) else None
        if text:
            try:
                known_uses |= uses_refs(yaml.safe_load(text))
            except Exception:  # health: allow BLE001 -- an unparsable base file contributes nothing
                pass
    failures, escalated = review(changes, yaml.safe_load, known_uses)
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
