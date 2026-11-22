"""Small fork-only test plans; unknown impact always keeps full discovery."""

from __future__ import annotations

import os
from pathlib import Path


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
# bot (main-health.yml) repairs main if that full run goes red. Skills Markdown
# is an image input, so Fork Agent Image still waits for that exact-SHA run.
_PROSE_PREFIXES = ("website/docs/", "skills/", "optional-skills/")
_PROSE_SUFFIXES = (".md", ".mdx")
_PROSE_MAX_TESTS = 400
# Repository-policy suites that read docs through scripts/ (docs drift, link
# and size checks) without naming the path themselves: always included.
_PROSE_ALWAYS_DIRS = ("tests/ci/", "tests/website/")


def _prose_tokens(path: str) -> set[str]:
    """Strings whose presence in a test means it may read ``path``."""
    tokens = {path, path.rsplit("/", 1)[0] + "/"}
    if path.startswith(("skills/", "optional-skills/")):
        tokens |= {"SKILL.md", "skills/", "optional-skills/", '"skills"', "'skills'",
                   '"optional-skills"', "'optional-skills'"}
    if path.startswith("website/"):
        tokens |= {"website/docs", '"website"', "'website'"}
    return tokens


def _prose_consumers(files: list[str], root: Path) -> list[str]:
    """Test modules that may read the changed prose; [] (full suite) when unsure.

    Any non-test module under tests/ (conftest, helpers, fixtures) that matches
    could feed every test, so it fails open, as does an unreadable file or a
    selection too large to be worth narrowing.
    """
    if not files or not all(p.startswith(_PROSE_PREFIXES) and p.endswith(_PROSE_SUFFIXES)
                            and (root / p).is_file() for p in files):
        return []
    tokens = set().union(*map(_prose_tokens, files))
    selected: list[str] = []
    for directory, dirs, names in os.walk(root / "tests"):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__"}]
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
                selected.append(relative)
                continue
            if not any(token in text for token in tokens):
                continue
            if not name.startswith("test_"):
                return []
            selected.append(relative)
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
        # tools.mcp_oauth. Other website/docs and skills inputs remain broad.
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
