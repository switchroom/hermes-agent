"""A coding delegate starts only inside its own linked worktree, and a resume is never refused for reusing its id."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools import delegate_tool


class _Started(Exception):
    """Raised once a child passes the worktree preflight (the credential lease is the next step)."""


def _run_stub(worktree_info):
    class Run:
        def __init__(self, *_args):
            self.worktree_info = None

        def seed_workspace(self):
            self.worktree_info = worktree_info

        def elapsed(self):
            return 0.0

        def attach_worktree(self, entry):
            return entry

        def await_child(self):
            pytest.fail("a refused coding delegate must not start")

    return Run


def test_coding_delegate_without_a_worktree_is_refused_before_start(monkeypatch):
    released: list[object] = []
    monkeypatch.setattr(delegate_tool, "_ChildRun", _run_stub(None))
    monkeypatch.setattr("tools.delegate_tool_resumable.release_unstarted", released.append)
    monkeypatch.setattr(
        delegate_tool, "_fabricated_entry", lambda _i, status, error, *_a: {"status": status, "error": error},
    )
    monkeypatch.setattr(
        delegate_tool, "_Heartbeat",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("heartbeat must follow the worktree preflight")),
    )
    child = SimpleNamespace(_delegate_worktree_required=True, _subagent_id="sa-unisolated")

    result = delegate_tool._run_single_child(0, "do not run", child=child)

    assert result == {"status": "error", "error": "no isolated worktree for a coding delegate"}
    assert released == [child]


def test_same_subagent_id_passes_preflight_again_for_continue_and_auto_resume(monkeypatch):
    """action='continue' and delegation.auto_resume reuse the subagent id; a second launch must not be refused."""
    monkeypatch.setattr(delegate_tool, "_ChildRun", _run_stub({"path": "/isolated/worktree"}))
    monkeypatch.setattr("tools.delegate_tool_resumable.release_unstarted", lambda _child: None)

    def _lease(_child):
        raise _Started

    monkeypatch.setattr(delegate_tool, "_lease_child_credential", _lease)
    child = SimpleNamespace(_delegate_worktree_required=True, _subagent_id="sa-resumed")

    for _attempt in range(2):
        with pytest.raises(_Started):
            delegate_tool._run_single_child(0, "resume", child=child)


def test_worktree_boundary_preserves_direct_read_only_seams():
    """A worktree needs a stamp or a resolved checkout, not write tools alone."""
    from run_agent import AIAgent

    direct_writer = object.__new__(AIAgent)
    direct_writer.valid_tool_names = {"write_file"}
    direct_reader = object.__new__(AIAgent)
    direct_reader.valid_tool_names = {"read_file"}

    assert delegate_tool._requires_isolated_worktree(SimpleNamespace(_delegate_worktree_required=True))
    # Write tools without a resolved checkout need none: Slack children have terminal and must still start.
    assert not delegate_tool._requires_isolated_worktree(direct_writer)
    direct_writer._delegate_repo_root = "/repo"
    assert delegate_tool._requires_isolated_worktree(direct_writer)
    assert not delegate_tool._requires_isolated_worktree(direct_reader)
    assert not delegate_tool._requires_isolated_worktree(
        SimpleNamespace(_subagent_id="subagent-test", valid_tool_names={"terminal"})
    )
