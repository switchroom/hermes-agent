"""Behavior contracts for conservative fork test selection."""

import io
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci import classify_changes
from scripts.ci.affected_test_plan import python_plan


def test_standalone_test_selects_one_file_and_one_shard(tmp_path, monkeypatch, capsys):
    target = tmp_path / "tests" / "test_example.py"
    target.parent.mkdir()
    target.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "switchroom/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO("tests/test_example.py\n"))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert json.loads(outputs["python_files"]) == ["tests/test_example.py"]
    assert json.loads(outputs["python_matrix"]) == {"slice": [1], "slices": [1]}

@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("unit", "workspace"), [("web", "web"), ("tui", "ui-tui")])
def test_js_workflow_executes_only_selected_workspace(unit, workspace, tmp_path):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/js-tests.yml").read_text(encoding="utf-8-sig"))
    step = next(s for s in workflow["jobs"]["check"]["steps"] if s.get("name") == "Run all workspace checks")
    npm = tmp_path / "npm"
    npm.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*"\nexit 7\n', encoding="utf-8")
    npm.chmod(0o755)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, check=False,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "CI_FORK": "true", "CHECK_UNIT": unit}, capture_output=True, text=True)
    assert result.returncode == 7  # selected-check failure must propagate
    assert result.stdout.strip() == f"run --prefix {workspace} check"


def test_js_workflow_routes_matrix_and_runs_whatsapp_once():
    root = Path(__file__).resolve().parents[2]
    ci = yaml.safe_load((root / ".github/workflows/ci.yaml").read_text(encoding="utf-8-sig"))
    workflow = yaml.safe_load((root / ".github/workflows/js-tests.yml").read_text(encoding="utf-8-sig"))
    assert ci["jobs"]["js-tests"]["with"]["js_matrix"] == "${{ needs.detect.outputs.js_matrix }}"
    assert "inputs.js_matrix" in workflow["jobs"]["check"]["strategy"]["matrix"]
    for name in ("Install WhatsApp bridge dependencies", "Run WhatsApp bridge node tests"):
        step = next(s for s in workflow["jobs"]["check"]["steps"] if s.get("name") == name)
        assert step["if"] == "matrix.unit == 'all' || matrix.unit == 'checks' || matrix.unit == 'whatsapp'"



def test_workflow_threads_selected_files_and_dynamic_matrix():
    root = Path(__file__).resolve().parents[2]
    ci = yaml.safe_load((root / ".github/workflows/ci.yaml").read_text(encoding="utf-8-sig"))
    tests = yaml.safe_load((root / ".github/workflows/tests.yml").read_text(encoding="utf-8-sig"))
    action = yaml.safe_load((root / ".github/actions/detect-changes/action.yml").read_text(encoding="utf-8-sig"))
    for key in ("python_files", "python_matrix"):
        assert key in action["outputs"]
        assert key in ci["jobs"]["detect"]["outputs"]
        assert ci["jobs"]["tests"]["with"][key] == "${{ needs.detect.outputs." + key + " }}"
    assert "inputs.python_matrix" in tests["jobs"]["test"]["strategy"]["matrix"]
    step = next(s for s in tests["jobs"]["test"]["steps"] if s.get("name") == "Run tests")
    assert "--files-from" in step["run"]
    assert step["env"]["PYTHON_FILES"] == "${{ inputs.python_files }}"


@pytest.mark.parametrize("changed", [[], ["tests/conftest.py"], ["tests/helpers/test_helper.py"],
    ["tests/test_deleted.py"], ["agent/run.py"], [".github/workflows/tests.yml"],
    ["skills/sample/SKILL.md"], ["website/docs/guide.md"]])
def test_unproven_inputs_retain_full_suite(changed, tmp_path):
    plan = python_plan(changed, root=tmp_path, fork=True, enabled=True)
    assert plan["python_files"] == []
    assert plan["python_matrix"]["slices"] == [16]


@pytest.mark.parametrize("relative", ["tests/test_helper.py", "tests/test_shared.py", "tests/test_fixtures.py"])
def test_helper_named_modules_do_not_narrow(relative, tmp_path):
    path = tmp_path / relative
    path.parent.mkdir()
    path.write_text("def test_example(): pass\n", encoding="utf-8")
    assert python_plan([relative], root=tmp_path, fork=True, enabled=True)["python_files"] == []


@pytest.mark.parametrize("count, shards", [(1, 1), (20, 1), (21, 2), (41, 3)])
def test_selected_shards_are_small_complete_and_cache_independent(count, shards, tmp_path):
    from scripts.run_tests_parallel import _slice_files
    tests = tmp_path / "tests"
    tests.mkdir()
    files = [f"tests/test_case_{index:04d}.py" for index in range(count)]
    for name in files:
        (tmp_path / name).write_text("def test_ok(): pass\n", encoding="utf-8")
    plan = python_plan(files, root=tmp_path, fork=True, enabled=True)
    assert plan["python_files"] == files
    assert plan["python_matrix"]["slices"] == [shards]
    paths = [tmp_path / name for name in files]
    selected = [p for index in range(1, shards + 1)
        for p in _slice_files(paths, index, shards, {files[0]: index * 9000}, tmp_path)]
    assert sorted(selected) == sorted(paths)
    assert len(selected) == len(set(selected))


def test_exact_oauth_document_selects_verified_consumer(tmp_path):
    document = "website/static/oauth/client-metadata.json"
    consumer = "tests/tools/test_mcp_cimd.py"
    for name in (document, consumer):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    plan = python_plan([document], root=tmp_path, fork=True, enabled=True)
    assert plan["python_files"] == [consumer]
    assert plan["python_matrix"]["slices"] == [1]
    # Missing/deleted data or consumer must broaden, never become an empty lane.
    (tmp_path / consumer).unlink()
    assert python_plan([document], root=tmp_path, fork=True, enabled=True)["python_files"] == []


def test_imported_test_retains_full_suite(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tests / "test_b.py").write_text("from tests.test_a import test_a\n", encoding="utf-8")
    assert python_plan(["tests/test_a.py"], root=tmp_path, fork=True, enabled=True)["python_files"] == []



@pytest.mark.parametrize(("paths", "units"), [
    (["web/src/App.tsx"], ["web"]),
    (["ui-tui/src/entry.tsx"], ["tui"]),
    (["scripts/whatsapp-bridge/index.mjs"], ["whatsapp"]),
    (["web/src/App.tsx", "scripts/whatsapp-bridge/index.mjs"], ["web", "whatsapp"]),
    (["ui-tui/src/lib/pythonHelpers.ts"], ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]),
    (["apps/shared/src/client.ts"], ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]),
    (["package-lock.json"], ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]),
])
def test_js_selection_isolated_sources_and_shared_fallback(paths, units, monkeypatch, capsys):
    monkeypatch.setenv("REPO", "switchroom/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO("\n".join(paths)))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert json.loads(outputs["js_matrix"]) == {"unit": units}


def test_fork_empty_exact_diff_cannot_be_narrowed_by_live_pr_files(monkeypatch, capsys):
    monkeypatch.setenv("REPO", "switchroom/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(classify_changes, "pull_request_changed_files", lambda: ["README.md"])
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    assert "python=true" in capsys.readouterr().out


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
@pytest.mark.parametrize("changed", ["tests/test_example.py", "web/src/App.tsx"])
def test_non_pull_request_events_always_plan_the_full_suite(event, changed, tmp_path, monkeypatch, capsys):
    target = tmp_path / "tests" / "test_example.py"
    target.parent.mkdir()
    target.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "switchroom/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", event)
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO(changed + "\n"))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert json.loads(outputs["python_files"]) == []
    if outputs["python"] == "true":
        assert json.loads(outputs["python_matrix"]) == {"slice": list(range(1, 17)), "slices": [16]}
    assert json.loads(outputs["js_matrix"]) == {"unit": ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]}


def _tree(root: Path, files: dict[str, str]) -> None:
    files = {".github/workflows/main-health.yml": "name: Main health\n", **files}
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def _markdown_update(root: Path, path: str, before: str | None, after: str) -> str:
    _git(root, "init", "-q")
    if before is not None:
        _tree(root, {path: before})
    _git(root, "add", ".")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "commit", "--no-gpg-sign", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    _tree(root, {path: after})
    _git(root, "add", ".")
    _git(root, "commit", "--no-gpg-sign", "-qm", "update")
    return base


@pytest.mark.parametrize(("before", "after", "narrowed"), [
    ("# Old prose\n", "# New prose\n", True),
    ("# Old prose\n\n<Value />\n", "# New prose\n", False),
    (
        "# Old prose\n",
        "# New prose\n\n[Link](https://example.com/`) {expression} and ` then text.\n",
        False,
    ),
    (None, "# New prose\n", False),
])
def test_pr_requires_prose_in_both_git_trees(before, after, narrowed, tmp_path, monkeypatch, capsys):
    changed = "website/docs/a.md"
    consumer = "tests/website/test_docs.py"
    _tree(tmp_path, {consumer: "def test_docs(): pass\n"})
    base = _markdown_update(tmp_path, changed, before, after)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "switchroom/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO(changed + "\n"))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert outputs["python"] == "true"
    assert json.loads(outputs["python_files"]) == ([consumer] if narrowed else [])
    assert json.loads(outputs["python_matrix"])["slices"] == ([1] if narrowed else [16])


def test_docs_prose_runs_only_its_readers_and_the_policy_suites(tmp_path):
    document = "website/docs/guide/a.md"
    _tree(tmp_path, {
        "tests/website/test_links.py": "def test_links(): pass\n",
        "tests/ci/test_docs_drift.py": "def test_drift(): pass\n",
        "tests/tools/test_reads_docs.py": "ROOT = 'website/docs'\n",
        "tests/tools/test_unrelated.py": "def test_x(): pass\n",
        "tests/e2e/test_reads_docs.py": "ROOT = 'website/docs'\n",
        "tests/integration/test_reads_docs.py": "ROOT = 'website/docs'\n",
        "tests/docker/test_reads_docs.py": "ROOT = 'website/docs'\n",
    })
    base = _markdown_update(tmp_path, document, "# Before\n", "# A\n\nUse `hermes status` here.\n")
    plan = python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=base)
    assert plan["python_files"] == [
        "tests/ci/test_docs_drift.py", "tests/tools/test_reads_docs.py", "tests/website/test_links.py"]
    assert plan["python_matrix"]["slices"] == [1]
    assert plan["python_reason"].startswith("selected: tests that read the changed prose")


@pytest.mark.parametrize(("name", "text"), [
    ("page.mdx", "# A\n"),
    ("page.md", "import Demo from './example';\n"),
    ("page.md", "Text with <Demo /> and {value}.\n"),
])
def test_mdx_keeps_the_full_python_suite(tmp_path, name, text):
    changed = f"website/docs/{name}"
    _tree(tmp_path, {"tests/website/test_docs.py": "def test_docs(): pass\n"})
    base = _markdown_update(tmp_path, changed, "# Before\n", text)
    plan = python_plan([changed], root=tmp_path, fork=True, enabled=True, base_sha=base)
    assert plan["python_files"] == []
    assert plan["python_matrix"]["slices"] == [16]


def test_the_programmatic_integration_doc_runs_its_indirect_consumer(tmp_path):
    document = "website/docs/developer-guide/programmatic-integration.md"
    consumer = "tests/hermes_cli/test_serve_handler_ledger.py"
    _tree(tmp_path, {
        consumer: "def test_ledger(): assert read_generated_ledger()\n",
        "tests/website/test_docs.py": "def test_docs(): pass\n",
    })
    base = _markdown_update(tmp_path, document, "# Before\n", "# Programmatic integration\n")
    plan = python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=base)
    assert plan["python_files"] == [consumer, "tests/website/test_docs.py"]
    (tmp_path / consumer).unlink()
    assert python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=base)["python_files"] == []


@pytest.mark.parametrize(("changed", "helper", "text"), [
    # A conftest or helper that reads the prose can feed every test: run them all.
    ("website/docs/a.md", "tests/conftest.py", "DOCS = 'website/docs'\n"),
    ("website/docs/a.md", "tests/helpers/docs.py", "ROOT = 'website/docs'\n"),
])
def test_prose_read_by_a_shared_test_helper_runs_the_full_suite(tmp_path, changed, helper, text):
    _tree(tmp_path, {helper: text, "tests/test_a.py": "P = 'website/docs'; S = 'SKILL.md'\n"})
    base = _markdown_update(tmp_path, changed, "# Before\n", "x\n")
    assert python_plan([changed], root=tmp_path, fork=True, enabled=True, base_sha=base)["python_files"] == []


def test_mixed_or_missing_prose_changes_keep_the_full_suite(tmp_path):
    document = "website/docs/a.md"
    _tree(tmp_path, {"tests/test_a.py": "P = 'website/docs'\n"})
    base = _markdown_update(tmp_path, document, "# Before\n", "x\n")
    for files in ([document, "agent/x.py"], ["website/docs/gone.md"], ["website/docs/a.png"]):
        assert python_plan(files, root=tmp_path, fork=True, enabled=True, base_sha=base)["python_files"] == []
    for invalid_base in ("", "HEAD^", "0" * 40):
        assert python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=invalid_base)[
            "python_files"
        ] == []
    # Upstream (non-fork) never narrows.
    assert python_plan([document], root=tmp_path, fork=False, enabled=True, base_sha=base)["python_files"] == []
    # The tested tree has an extra path that the reported change set omits.
    _tree(tmp_path, {"agent/x.py": "x = 1\n"})
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "--no-gpg-sign", "--amend", "--no-edit", "-q")
    assert python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=base)["python_files"] == []
    # Skills prose ships in the image: never narrowed.
    _tree(tmp_path, {"skills/s/SKILL.md": "x\n", "optional-skills/o/SKILL.md": "x\n"})
    for files in (["skills/s/SKILL.md"], ["optional-skills/o/SKILL.md"]):
        assert python_plan(files, root=tmp_path, fork=True, enabled=True)["python_files"] == [], files


@pytest.mark.platforms("posix")
def test_markdown_symlink_keeps_the_full_suite(tmp_path):
    document = "website/docs/a.md"
    _tree(tmp_path, {
        "website/docs/target.md": "# Plain symlink target\n",
        "tests/website/test_docs.py": "def test_docs(): pass\n",
    })
    base = _markdown_update(tmp_path, document, "# Before\n", "# After\n")
    path = tmp_path / document
    path.unlink()
    path.symlink_to("target.md")
    _git(tmp_path, "add", document)
    _git(tmp_path, "commit", "--no-gpg-sign", "--amend", "--no-edit", "-q")
    assert python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=base)["python_files"] == []


def test_fast_lane_is_inert_without_the_auto_revert_workflow(tmp_path):
    document = "website/docs/a.md"
    _tree(tmp_path, {"tests/tools/test_reads_docs.py": "ROOT = 'website/docs'\n"})
    base = _markdown_update(tmp_path, document, "# Before\n", "x\n")
    assert python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=base)["python_files"]
    (tmp_path / ".github/workflows/main-health.yml").unlink()
    assert python_plan([document], root=tmp_path, fork=True, enabled=True, base_sha=base)["python_files"] == []


@pytest.mark.parametrize("name", [
    "AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", "HERMES.md", "SOUL.md", "SKILL.md",
    "USER.md", "MEMORY.md", ".hermes.md", ".claude/rules/gateway.md", ".cursorrules", ".cursor/rules/gateway.mdc",
])
@pytest.mark.parametrize("prefix", ["", "website/docs/guide/", "website/examples/", "plugin-catalog/"])
def test_agent_instructions_keep_the_full_fork_suite(tmp_path, name, prefix):
    changed = prefix + name
    _tree(tmp_path, {"tests/ci/test_docs.py": "def test_docs(): pass\n"})
    base = ""
    if prefix.startswith("website/docs/"):
        base = _markdown_update(tmp_path, changed, "# Before\n", "# Instructions\n")
    lanes = classify_changes.classify([changed], fork=True, root=tmp_path)
    assert lanes["python"]
    plan = python_plan([changed], root=tmp_path, fork=True, enabled=lanes["python"], base_sha=base)
    assert plan["python_files"] == []
    assert plan["python_matrix"]["slice"] == list(range(1, 17))


def test_pushes_plan_the_full_suite_even_for_prose(tmp_path, monkeypatch, capsys):
    document = "website/docs/a.md"
    _tree(tmp_path, {"tests/test_a.py": "P = 'website/docs'\n"})
    base = _markdown_update(tmp_path, document, "# Before\n", "x\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "switchroom/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO(document + "\n"))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert json.loads(outputs["python_files"]) == []
    assert json.loads(outputs["python_matrix"])["slices"] == [16]
