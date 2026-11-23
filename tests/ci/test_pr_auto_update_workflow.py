"""Contract for .github/workflows/pr-auto-update.yml.

main's required checks are strict, so an auto-merge PR stalls once main moves.
This workflow merges main into same-repository PRs that have auto-merge armed.
It holds a write-scoped App token, so the tests pin what it may touch: no PR
trigger (it never runs PR code), no GITHUB_TOKEN grants, an App token scoped to
this repository with exactly three permissions, and only the update-branch API.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/pr-auto-update.yml"
APP_TOKEN = "${{ steps.app-token.outputs.token }}"


def _without_comments(script: str) -> str:
    return re.sub(r"(?m)^\s*#.*$", "", script)


class PrAutoUpdateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.wf = yaml.safe_load(self.text)
        self.on = self.wf.get("on", self.wf.get(True))
        self.job = self.wf["jobs"]["update"]
        self.steps = {step["name"]: step for step in self.job["steps"]}
        self.mint = next(step for step in self.job["steps"] if step.get("id") == "app-token")
        self.run = self.steps["Update behind auto-merge PRs"]

    def test_runs_after_main_moves_with_a_schedule_fallback_and_no_pr_trigger(self) -> None:
        self.assertEqual(set(self.on), {"push", "schedule", "workflow_dispatch"})
        self.assertEqual(self.on["push"], {"branches": ["main"]})
        self.assertRegex(self.on["schedule"][0]["cron"], r"^\*/(5|10|15) \* \* \* \*$")
        self.assertEqual(set(self.wf["jobs"]), {"update"})
        self.assertEqual(self.job["if"], "github.repository == 'switchroom/hermes-agent'")
        self.assertLessEqual(self.job["timeout-minutes"], 10)

    def test_one_sweep_at_a_time(self) -> None:
        self.assertEqual(self.wf["concurrency"], {"group": "pr-auto-update", "cancel-in-progress": False})

    def test_github_token_has_no_permissions_and_no_code_is_checked_out(self) -> None:
        self.assertEqual(self.wf["permissions"], {})
        self.assertEqual(self.job["permissions"], {})
        for forbidden in ("write-all", "GITHUB_TOKEN", "github.token", "actions/checkout", "skip-token-revoke"):
            self.assertNotIn(forbidden, _without_comments(self.text), forbidden)
        self.assertEqual([step.get("uses") for step in self.job["steps"] if "uses" in step], [self.mint["uses"]])

    def test_app_token_is_this_repository_only_with_least_privilege(self) -> None:
        self.assertRegex(self.mint["uses"], r"^actions/create-github-app-token@[0-9a-f]{40}$")
        self.assertEqual(self.mint["with"], {
            "client-id": "${{ vars.RELEASE_BOT_APP_ID }}",
            "private-key": "${{ secrets.RELEASE_BOT_PRIVATE_KEY }}",
            "owner": "switchroom",
            "repositories": "hermes-agent",
            "permission-contents": "write",
            "permission-pull-requests": "write",
            "permission-workflows": "write",
        })
        # The key is read once, by the mint step; the token flows to one sink: gh's env.
        self.assertEqual(self.text.count("secrets."), 1)
        self.assertEqual(len(self.job["steps"]), 2)
        self.assertEqual(self.text.count(APP_TOKEN), 1)
        self.assertEqual(self.run["env"]["GH_TOKEN"], APP_TOKEN)

    def test_only_same_repo_auto_merge_prs_that_are_behind_are_updated_without_force(self) -> None:
        script = _without_comments(self.run["run"])
        self.assertIn("--base main", script)
        self.assertIn(".autoMergeRequest != null", script)
        self.assertIn("(.isCrossRepository | not)", script)
        self.assertIn("(.isDraft | not)", script)
        self.assertIn('[ "$mergeable" = "CONFLICTING" ]', script)
        self.assertIn('[ "$state" != "BEHIND" ]', script)
        self.assertIn('gh api -X PUT "repos/$REPO/pulls/$number/update-branch"', script)
        self.assertIn('expected_head_sha="$head"', script)
        for forbidden in ("git ", "--force", "rebase", "gh pr merge", "--admin", "merge_method", "${{"):
            self.assertNotIn(forbidden, script, forbidden)


if __name__ == "__main__":
    unittest.main()
