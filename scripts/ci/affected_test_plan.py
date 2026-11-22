"""Small fork-only test plans; unknown impact always keeps full discovery."""

from __future__ import annotations

import os
import re
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
# docs: skills Markdown ships in the agent image and feeds runtime behaviour,
# so it keeps the full PR suite. The lane is inert unless the auto-revert
# workflow exists, since deferring to main is only safe with it.
_AUTO_REVERT_WORKFLOW = ".github/workflows/main-health.yml"
_INSTRUCTION_FILES = frozenset({
    "agents.md", "agents.override.md", "claude.md", "claude.local.md", ".cursorrules",
    ".hermes.md", "hermes.md", "skill.md", "soul.md",
})
_INSTRUCTION_DIRS = {(".claude", "rules"), (".cursor", "rules")}
_MARKDOWN_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_MARKDOWN_CODE_SPAN = re.compile(r"(?<![\\`])(`+)(?!`)[^`]*\1(?!`)")
_MDX_SYNTAX = re.compile(r"^[ \t]*(?:import|export)\b|[{}]|<(?:[^\W\d]|[/$>])")
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


def _is_plain_markdown(path: Path) -> bool:
    """Keep MDX and Markdown with possible executable markup fully gated."""
    if path.suffix != ".md" or path.is_symlink():
        return False
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return False
    fence = ""
    for line in text.split("\n"):
        marker = _MARKDOWN_FENCE.match(line)
        if fence:
            if marker and marker[2].startswith(fence) and not marker[3].strip(" \t"):
                fence = ""
        elif marker and not marker[1] and (marker[2][0] == "~" or "`" not in marker[3]):
            # An unindented fence is unambiguous outside a Markdown container.
            fence = marker[2]
        elif _MDX_SYNTAX.search(_MARKDOWN_CODE_SPAN.sub(" ", line)):
            return False
    return not fence


def _prose_tokens(path: str) -> set[str]:
    """Strings whose presence in a test means it may read ``path``."""
    return {path, path.rsplit("/", 1)[0] + "/", "website/docs", '"website"', "'website'"}


def _prose_consumers(files: list[str], root: Path) -> list[str]:
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
        if (not name.startswith("website/docs/") or is_agent_instruction(path)
                or any(part.startswith(".") for part in path.parts)
                or not _is_plain_markdown(root / path)):
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


def python_plan(files: list[str], *, root: Path, fork: bool, enabled: bool) -> dict:
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
    elif fork and (prose := _prose_consumers(files, root)):
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
