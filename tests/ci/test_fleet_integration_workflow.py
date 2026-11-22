"""Contract for .github/workflows/fleet-integration.yml and scripts/ci/fleet_integration.py.

Regression note: #276 removed the `lane=` keyword from `record_respawn_guard`, which
switchroom's Kanban scenarios call, and nothing failed until the next switchroom pin PR. This
workflow runs those scenarios (the kanban legs) on every same-repository PR that can affect the
runtime, so the tests below pin that it keeps doing so and keeps the record switchroom reuses.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/fleet-integration.yml"
SCRIPT = ROOT / "scripts/ci/fleet_integration.py"
SHA = "a" * 40
LEGS = [{"lane": "kanban", "shard": "1/3", "name": "kanban 1/3"}, {"lane": "native", "shard": "", "name": "native"}]


def scope(event, *files):
    out = subprocess.run([sys.executable, str(SCRIPT), "scope", "--event", event, "--files", *files],
                         capture_output=True, text=True, check=True).stdout
    return out.strip()


class WorkflowShapeTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.wf = yaml.safe_load(self.text)
        self.on = self.wf.get("on", self.wf.get(True))

    def test_triggers_unfiltered_so_the_required_check_always_reports(self):
        self.assertEqual(set(self.on), {"pull_request", "push"})
        self.assertIsNone(self.on["pull_request"])
        self.assertEqual(self.on["push"], {"branches": ["main"]})

    def test_legs_come_from_switchroom_main_and_run_its_shared_leg_script(self):
        jobs = self.wf["jobs"]
        self.assertEqual(self.wf["env"]["SWITCHROOM_REF"], "main")
        self.assertIn("ref: ${{ env.SWITCHROOM_REF }}", self.text)
        self.assertIn("scripts/source-ci.py legs", self.text)
        leg = jobs["leg"]
        self.assertEqual(leg["name"], "fleet leg (${{ matrix.name }})")
        self.assertEqual(leg["strategy"]["matrix"]["include"], "${{ fromJSON(needs.scope.outputs.legs) }}")
        self.assertFalse(leg["strategy"]["fail-fast"])
        runs = "\n".join(s.get("run", "") for s in leg["steps"])
        self.assertIn('scripts/run-runtime-leg.sh "$LANE" "$SHARD" "$GITHUB_WORKSPACE/agent-source"', runs)
        refs = [s["with"].get("ref") for s in leg["steps"] if "checkout" in str(s.get("uses"))]
        self.assertEqual(refs, ["${{ needs.scope.outputs.switchroom_commit }}", None])

    def test_aggregate_name_and_artifact_are_what_switchroom_validates(self):
        passing = self.wf["jobs"]["pass"]
        self.assertEqual(passing["name"], "Fleet integration pass")
        self.assertEqual(passing["needs"], ["scope", "leg"])
        self.assertEqual(passing["if"], "always()")
        upload = [s for s in passing["steps"] if "upload-artifact" in str(s.get("uses"))]
        self.assertEqual(upload[0]["with"]["name"], "fleet-integration-result")
        self.assertTrue(upload[0]["with"]["path"].endswith("/fleet-integration.json"))


class ScopeTests(unittest.TestCase):
    def test_push_always_runs(self):
        self.assertEqual(scope("push", "README.md"), "run=true")

    def test_runtime_paths_run_and_doubt_runs(self):
        for path in ("hermes_cli/kanban_db_guard_wait.py", "plugins/x/README.md", "skills/a/SKILL.md",
                     "pyproject.toml", "uv.lock", "tests/test_x.py", ".github/workflows/fleet-integration.yml",
                     "scripts/ci/fleet_integration.py", "Dockerfile", "some-new-dir/x"):
            self.assertEqual(scope("pull_request", "README.md", path), "run=true", path)
        self.assertEqual(scope("pull_request"), "run=true")

    def test_front_end_docs_and_other_workflows_skip(self):
        self.assertEqual(scope("pull_request", "README.md", "website/docs/a.md", "ui-tui/src/x.ts",
                               "apps/desktop/a.ts", ".github/workflows/lint.yml", "pnpm-lock.yaml"), "run=false")


class ResultTests(unittest.TestCase):
    def result(self, **ctx_override):
        ctx = {"event": "push", "ref": "refs/heads/main", "repository": "switchroom/hermes-agent",
               "run_id": "50", "run_attempt": "1", "agent_commit": SHA, "agent_tree": "b" * 40, "pr_head": None,
               "switchroom_commit": "c" * 40, "switchroom_tree": "d" * 40, "legs": json.dumps(LEGS),
               "run": "true", "scope_result": "success", "leg_result": "success"}
        jobs = ctx_override.pop("jobs", [{"name": "fleet leg (kanban 1/3)", "conclusion": "success"},
                                         {"name": "fleet leg (native)", "conclusion": "success"}])
        ctx.update(ctx_override)
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "ctx.json").write_text(json.dumps(ctx))
            (t / "jobs.json").write_text(json.dumps({"jobs": jobs}))
            proc = subprocess.run([sys.executable, str(SCRIPT), "result", "--jobs", str(t / "jobs.json"),
                                   "--context", str(t / "ctx.json"), "--out", str(t / "out.json")],
                                  capture_output=True, text=True)
            record = json.loads((t / "out.json").read_text())
            notes = [line for line in proc.stdout.splitlines() if line.startswith("::notice title=fleet-integration-result::")]
            self.assertEqual(len(notes), 1)
            self.assertEqual(json.loads(notes[0].split("::", 2)[2]), record)
            return proc.returncode, record

    def test_green_push_records_every_field_switchroom_checks(self):
        code, record = self.result()
        self.assertEqual(code, 0)
        self.assertEqual(record["result"], "success")
        self.assertEqual(record["schema"], "fleet-integration/v1")
        self.assertEqual(record["workflow"], ".github/workflows/fleet-integration.yml")
        self.assertEqual(record["run_id"], 50)
        self.assertEqual(record["legs"], {"kanban 1/3": "success", "native": "success"})
        self.assertEqual(record["switchroom_repository"], "switchroom/switchroom")

    def test_missing_failed_or_skipped_legs_fail(self):
        cases = {
            "missing leg": {"jobs": [{"name": "fleet leg (native)", "conclusion": "success"}]},
            "failed leg": {"jobs": [{"name": "fleet leg (kanban 1/3)", "conclusion": "failure"},
                                    {"name": "fleet leg (native)", "conclusion": "success"}]},
            "matrix failed": {"leg_result": "failure"},
            "scope failed": {"scope_result": "failure"},
            "push may not skip": {"run": "false", "leg_result": "skipped"},
            "no legs": {"legs": "[]", "jobs": []},
            "no switchroom tree": {"switchroom_tree": ""},
        }
        for name, override in cases.items():
            with self.subTest(name=name):
                code, record = self.result(**override)
                self.assertEqual(code, 1)
                self.assertEqual(record["result"], "failure")

    def test_scoped_out_pr_passes_only_when_skipped(self):
        self.assertEqual(self.result(event="pull_request", run="false", leg_result="skipped")[0], 0)
        self.assertEqual(self.result(event="pull_request", run="false", leg_result="success")[0], 1)


if __name__ == "__main__":
    unittest.main()
