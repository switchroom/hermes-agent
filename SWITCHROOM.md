# Switchroom and this fork

`switchroom/hermes-agent` is the Hermes Agent fork that [Switchroom](https://switchroom.ai) builds on. Hermes Agent itself is made by [Nous Research](https://github.com/NousResearch/hermes-agent), and almost everything in this repository is their work. This fork adds a small set of add-ons and packages the result as the agent image the Switchroom distribution uses. Switchroom is not affiliated with or endorsed by Nous Research.

## The flow

```text
NousResearch/hermes-agent          upstream Hermes Agent, unchanged
        │  regular upstream syncs (merge commits)
        ▼
switchroom/hermes-agent            this fork: upstream + Switchroom add-ons
        │  Fork Agent Image workflow (scan, provenance, SBOM)
        ▼
ghcr.io/switchroom/hermes-agent    the agent image, pinned by digest
        │  Release Handoff opens a pin PR
        ▼
switchroom/switchroom              the Switchroom distribution: a digest-pinned
                                   child image, sidecars, plugins and contracts
```

1. **Upstream.** Nous Research develops Hermes Agent in `NousResearch/hermes-agent`.
2. **This fork.** Upstream code, plus the add-ons below. The fork keeps its own CI on GitHub's standard hosted runners and turns off upstream product workflows it does not ship (desktop installers, the docs site, and similar).
3. **Agent image.** Every image-affecting push to `main` runs `.github/workflows/fork-agent-image.yml`, which builds the complete runtime from the root `Dockerfile`, checks it, scans it for critical vulnerabilities, and publishes `ghcr.io/switchroom/hermes-agent:sha-<commit>` with build-provenance and SBOM attestations. There is no `latest` tag. Consumers pin the digest. Details: [`website/docs/developer-guide/fork-agent-image.md`](website/docs/developer-guide/fork-agent-image.md).
4. **Distribution.** `.github/workflows/release-handoff.yml` hands the published digest to [`switchroom/switchroom`](https://github.com/switchroom/switchroom) as an auto-merging pin PR, gated by that repository's own checks. Switchroom then builds its child image on top of that exact digest.

Each release step is a separate change gated by its own repository's checks. Publishing the Agent image starts the normal fleet pin and deployment chain automatically.

## What the add-ons are

The add-ons are the reliability and operations work a long-running, one-container-per-agent deployment needs. They fall into a few groups:

- **Long-running work.** Kanban worker recovery across gateway restarts, respawn and block-loop guards, durable human checkpoints, and clarification routing for background tasks.
- **Messaging delivery.** Gateway fixes for restart drains, redelivery, completion delivery and notification transport, plus Slack and Telegram delivery fixes (thread handling, voice replies, per-topic voice mode).
- **Approvals and safety.** Approval flows that keep a turn's command authority, smart approval of computer-use actions, and a sandboxed local Chromium for managed profiles.
- **Bot Screen and Desktop.** Screen handoff and clipboard copy-out for an agent's own desktop session.
- **Image and release pipeline.** The Fork Agent Image workflow, the Release Handoff, and fork CI tuned to cheap hosted runners.

The canonical list is the history. With a remote named `upstream` pointing at Nous, `git log --no-merges upstream/main..main` shows every commit this fork has on top of upstream.

## Upstream syncs

- The fork regularly merges Nous `main` into this fork's `main` through a pull request. It lands as a merge commit, never a squash, so upstream stays an ancestor and its history stays intact.
- Conflicts are resolved in favour of upstream unless an add-on depends on the fork's behaviour. An add-on that upstream has since fixed is dropped rather than kept.
- A sync merges only on green CI for the exact head. A new agent image is published from the merged `main`, and Switchroom pins it like any other release.
- Generic fixes should go upstream. The aim is a small, shrinking set of add-ons.

## For agents working on this fork

Read this before changing anything here. Upstream's [`AGENTS.md`](AGENTS.md) still applies to the code itself.

- **The chain.** `NousResearch/hermes-agent` → this fork → `ghcr.io/switchroom/hermes-agent` (published by `fork-agent-image.yml`, pinned by digest) → `switchroom/switchroom` (pin PR from `release-handoff.yml`, then its Fleet child image) → a private overlay's Stage A pin PR → a private candidate image → a host-run rollout. Each arrow is a separate, machine-gated change. On the managed fleet, the host's automatic rollout service picks up published private candidates by digest. It rolls a canary first and then checks each agent's readiness before proceeding.
- **Upstream syncs** land as a merge commit of Nous `main`, never a squash or rebase, so the Nous parent stays in history. Keep every add-on unless upstream now does the same job. Resolve each conflict on purpose and say why in the PR. Merge only on green CI for the exact head; the merged `main` publishes a new image by itself. Do not start a sync unless one has been asked for.
- **Where add-ons live.** There is no patch directory. Add-ons are ordinary commits on `main` on top of upstream; `git log --no-merges upstream/main..main` lists them. Keep them small and in the area they change, and prefer a plugin or skill over a core edit so syncs stay cheap. Fork-only workflows (`fork-agent-image.yml`, `release-handoff.yml`) are gated on `github.repository == 'switchroom/hermes-agent'`.
- **Upstream files.** Keep edits to upstream-owned files such as `README.md` and `AGENTS.md` to a line or two at the top, so syncs rarely conflict. Fork context belongs here.
- **GitHub access.** The `switchroom` organization rejects fine-grained personal access tokens with a lifetime over 366 days. A 403 from a long-lived token on `switchroom/*` is that policy, not a broken login. Use an org-approved token for this org.
- **Old name.** This fork moved from `mekenthompson/hermes-agent` on 2026-11-18, and GitHub redirects the old URL. Never create a repository at the old name, because that breaks the redirect. Images already published under `ghcr.io/mekenthompson/hermes-agent` stay valid by digest. Never delete or retag them.

## CI on this fork (read before pushing)

- **Before push.** Run `python scripts/check`. Install it once with `python scripts/check --install-hook pre-push`. Then run `scripts/run_tests.sh tests/<area>/`, never bare `pytest`.
- **What CI runs.**
  - `detect` (`scripts/ci/classify_changes.py`, `affected_test_plan.py`, `ci_policy.py`) picks the lanes. For a local preview, set `base` to the PR base SHA and use the committed tree you want to test as `HEAD`. Run `git diff --name-only "$base" HEAD | BASE_SHA="$base" EVENT_NAME=pull_request REPO=switchroom/hermes-agent python3 -m scripts.ci.classify_changes`.
  - A `.github/`, `justfile` or `scripts/hermes-python` change runs every lane.
  - The Python plan can narrow for standalone test modules and edits to existing plain Markdown under `website/docs/`. The docs lane verifies both Git trees and runs the reader tests plus `tests/ci` and `tests/website`. Skills, agent instructions, executable docs and uncertain changes keep full coverage.
  - A push to `main` plans the full suite. It reuses a green PR run when the exact tree and full plan match (`reused-from:` in the gate). Narrowed PR coverage is completed on `main` before any image publication.
  - A nightly scheduled full run backstops `main`. It is also the primary feed of test durations.
- **Workflow edits.** A PR that changes `.github/workflows`, `.github/actions` or other CI-review files needs the `ci-reviewed` label. External `uses:` must be pinned to a full SHA. Adding the label reruns the gate by itself.
- **Commit identity.** Use an `<id>+<login>@users.noreply.github.com` email, which the contributor check skips.
- **Flakes.** A file shown as `FLAKY`, or one that fails twice, is a bug. Rerun the failed jobs once, then fix the test with the process_time-ratio pattern. The nightly keeps a "Nightly full CI: flake and failure tracker" issue.
- **Red `main`.** `main-health.yml` reruns the failed jobs once and waits. If the run is still `failure` (cancelled counts as unknown), it names the culprit: the oldest commit of the red streak, with a green parent and its own failed run. It reverts that commit only when its run failed in test or check steps (not setup or infrastructure), the commit is not itself a revert, and no newer main run is pending or green. The revert goes in as an auto-merging PR from the release bot, and an issue is filed. Anything else is only reported. Re-land the change with a fix.
- **Release path.** An image-affecting merge starts Fork Agent Image, which publishes behind the exact-SHA image gate. Release Handoff opens the fleet pin PR, arms auto-merge and waits up to 12 minutes. Handoffs run one at a time. If the wait ends while the exact pin is still armed, the PR can finish through its normal checks. Its checks show what is holding the pin.
- **Active workflows.** The upstream workflows listed in `scripts/ci/fork_disabled_workflows.txt` are disabled here. Do not edit them.
- **Branches.** Required checks are strict and `allow_update_branch` is off. Merge `main` into your own branch, and never force-push. A conflicting PR gets no CI. Merged branches are deleted automatically.

| CI is red in | Reproduce locally |
|---|---|
| Python lints (ruff, footguns, code health ratchet) | `python scripts/check` (`--only ruff`, `--only footguns,health`) |
| Unmapped contributor email | `python3 scripts/ci/check_contributor_emails.py` |
| A Python test slice | `scripts/run_tests.sh <failing file>` |

## Where issues go

- **Hermes Agent bugs and features** that reproduce on upstream: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and its contribution guide.
- **Switchroom distribution, packaging or this fork's add-ons**: [switchroom/switchroom](https://github.com/switchroom/switchroom). Switchroom is in early access, and [switchroom.ai](https://switchroom.ai) has the current contact route.
- **Security**: never in a public issue. For upstream Hermes, follow [`SECURITY.md`](SECURITY.md). For Switchroom, follow the [`switchroom/switchroom` security policy](https://github.com/switchroom/switchroom/blob/main/SECURITY.md).
