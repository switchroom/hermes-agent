"""Small fork-only test plans; unknown impact always keeps full discovery."""

from __future__ import annotations

import os
import re
import subprocess
from itertools import pairwise
from pathlib import Path

from scripts.test_discovery import UNIT_TEST_EXCLUDED_DIRS


def _standalone_tests(files: list[str], root: Path) -> bool:
    """Only existing test modules with no other Python reference can narrow."""
    for name in files:
        path = Path(name)
        if (not name.startswith("tests/") or not path.name.startswith("test_")
                or path.suffix != ".py" or not (root / path).is_file()
                or any(word in path.stem for word in ("helper", "fixture", "shared", "conftest"))
                or any(part.startswith("_") or part in {"e2e", "fixtures", "fakes", "helpers"}
                       for part in path.parts[1:-1])):
            return False
    # Text rather than import-only matching also catches file-based dynamic
    # imports and fixture plugins. False positives deliberately run the suite.
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "venv", "__pycache__"}]
        for name in names:
            if not name.endswith(".py"):
                continue
            source = Path(directory) / name
            try:
                text = source.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                return False
            for changed in files:
                if source != root / changed and Path(changed).stem in text:
                    return False
    return True


# Defer-CI fast lane (CI speed review 2026-11-22, section 3): a PR that changes
# only guidance prose runs the tests that read it; the push to main always runs
# the full suite (classify_changes plans pushes in full), and the auto-revert
# bot (main-health.yml) repairs main if that full run goes red. Only website
# docs with proven plain Markdown in both Git trees qualify. Skills Markdown
# ships in the agent image and feeds runtime behaviour, so it keeps the full
# PR suite. The lane is inert unless the auto-revert workflow exists, since
# deferring to main is only safe with it.
_AUTO_REVERT_WORKFLOW = ".github/workflows/main-health.yml"
_INSTRUCTION_FILES = frozenset({
    "agents.md", "agents.override.md", "claude.md", "claude.local.md", ".cursorrules",
    ".hermes.md", "hermes.md", "memory.md", "skill.md", "soul.md", "user.md",
})
_INSTRUCTION_DIRS = {(".claude", "rules"), (".cursor", "rules")}
_GIT_SHA = re.compile(r"[0-9a-fA-F]{40}")
_REGULAR_BLOB_EDIT = re.compile(r":100644 100644 ([0-9a-f]{40}) ([0-9a-f]{40}) M")
_MDX_SYNTAX = re.compile(r"\b(?:import|export)\b|[<{}\x00]")
_PROSE_MAX_TESTS = 400
# Repository-policy suites that read docs through scripts/ (docs drift, link
# and size checks) without naming the path themselves: always included.
_PROSE_ALWAYS_DIRS = ("tests/ci/", "tests/website/")
# This test reads the page through scripts/gen_serve_handler_ledger.py.
_INDIRECT_PROSE_CONSUMERS = {
    "website/docs/developer-guide/programmatic-integration.md": "tests/hermes_cli/test_serve_handler_ledger.py",
}


def is_agent_instruction(path: Path) -> bool:
    """Recognize instruction and identity files independently of their location."""
    if path.name.casefold() in _INSTRUCTION_FILES:
        return True
    parents = tuple(part.casefold() for part in path.parts[:-1])
    return any(pair in _INSTRUCTION_DIRS for pair in pairwise(parents))


def _git_output(root: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", *args], cwd=root, capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _is_plain_markdown_blob(root: Path, oid: str) -> bool:
    blob = _git_output(root, "cat-file", "blob", oid)
    if blob is None:
        return False
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeError:
        return False
    # Scan raw source. Treat ambiguity even inside a code span or fence as
    # possible MDX, since delimiters can have other meanings in Markdown.
    return _MDX_SYNTAX.search(text) is None


def _plain_markdown_changes(files: list[str], root: Path, base_sha: str) -> bool:
    """Require a complete exact-base-to-tested-tree diff of regular prose edits."""
    if not _GIT_SHA.fullmatch(base_sha):
        return False
    raw = _git_output(
        root, "diff", "--raw", "--no-abbrev", "--no-renames", "--no-relative",
        "--no-ext-diff", "--ignore-submodules=none", "-z", f"{base_sha}^{{commit}}", "HEAD", "--",
    )
    if not raw:
        return False
    try:
        fields = raw.decode("utf-8").split("\0")
    except UnicodeError:
        return False
    paths = fields[1::2]
    if fields[-1] or len(paths) != len(files) or set(paths) != set(files):
        return False
    for metadata in fields[:-1:2]:
        edit = _REGULAR_BLOB_EDIT.fullmatch(metadata)
        if edit is None or not all(_is_plain_markdown_blob(root, oid) for oid in edit.groups()):
            return False
    return True


def _prose_tokens(path: str) -> set[str]:
    """Strings whose presence in a test means it may read ``path``."""
    return {path, path.rsplit("/", 1)[0] + "/", "website/docs", '"website"', "'website'"}


def _prose_consumers(files: list[str], root: Path, base_sha: str) -> list[str]:
    """Test modules that may read the changed prose; [] (full suite) when unsure.

    Any non-test module under tests/ (conftest, helpers, fixtures) that matches
    could feed every test, so it fails open, as does an unreadable file or a
    selection too large to be worth narrowing.
    """
    if not (root / _AUTO_REVERT_WORKFLOW).is_file():
        return []  # no auto-revert bot on main: never defer work to it
    if not files:
        return []
    for name in files:
        path = Path(name)
        if (not name.startswith("website/docs/") or path.suffix != ".md"
                or is_agent_instruction(path) or any(part.startswith(".") for part in path.parts)):
            return []
    if not _plain_markdown_changes(files, root, base_sha):
        return []
    tokens = set().union(*map(_prose_tokens, files))
    indirect = {_INDIRECT_PROSE_CONSUMERS[name] for name in files if name in _INDIRECT_PROSE_CONSUMERS}
    if any(not (root / name).is_file() for name in indirect):
        return []
    selected = set(indirect)
    for directory, dirs, names in os.walk(root / "tests"):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in {"node_modules", "__pycache__"} and d not in UNIT_TEST_EXCLUDED_DIRS]
        for name in names:
            if not name.endswith(".py"):
                continue
            source = Path(directory) / name
            try:
                text = source.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                return []
            relative = source.relative_to(root).as_posix()
            if relative.startswith(_PROSE_ALWAYS_DIRS) and name.startswith("test_"):
                selected.add(relative)
                continue
            if not any(token in text for token in tokens):
                continue
            if not name.startswith("test_"):
                return []
            selected.add(relative)
    return sorted(selected) if len(selected) <= _PROSE_MAX_TESTS else []


def js_plan(files: list[str], *, fork: bool) -> dict:
    """Only isolated source trees narrow; manifests and shared inputs run all."""
    full = ["checks", "ui-1/3", "ui-2/3", "ui-3/3"] if fork else ["all"]
    mapping = {"web/src/": "web", "ui-tui/src/": "tui", "scripts/whatsapp-bridge/": "whatsapp"}
    units: set[str] = set()
    if fork and files:
        for path in files:
            unit = next((unit for prefix, unit in mapping.items() if path.startswith(prefix)), None)
            # tests-js/product-builders.test.mjs copies this real helper.
            if unit is None or path == "ui-tui/src/lib/pythonHelpers.ts":
                return {"js_matrix": {"unit": full}, "js_reason": "full: shared or unproven impact"}
            units.add(unit)
    return {
        "js_matrix": {"unit": sorted(units) if units else full},
        "js_reason": "selected: isolated source workspaces" if units else "full: unavailable diff or upstream",
    }


def python_plan(files: list[str], *, root: Path, fork: bool, enabled: bool, base_sha: str = "") -> dict:
    """Return explicit files (empty means full discovery), matrix and reason."""
    selected: list[str] = []
    reason = "full: unproven impact or unavailable diff"
    count = 16 if fork else 1
    if not enabled:
        reason = "not applicable"
        count = 1
    elif fork and files == ["website/static/oauth/client-metadata.json"] and all(
        (root / path).is_file() for path in (*files, "tests/tools/test_mcp_cimd.py")
    ):
        # test_mcp_cimd reads the published document and validates it against
        # tools.mcp_oauth.
        selected = ["tests/tools/test_mcp_cimd.py"]
        count = 1
        reason = "selected: OAuth metadata document consumer"
    elif fork and (prose := _prose_consumers(files, root, base_sha)):
        selected = prose
        count = min(16, (len(selected) + 19) // 20)
        reason = "selected: tests that read the changed prose (full suite on main)"
    elif fork and files and _standalone_tests(files, root):
        selected = sorted(set(files))
        count = min(16, (len(selected) + 19) // 20)
        reason = "selected: standalone test modules"
    return {
        "python_files": selected,
        "python_matrix": {"slice": list(range(1, count + 1)), "slices": [count]},
        "python_reason": reason,
    }
