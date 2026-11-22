"""A ready card whose PR is already open waits in ``ready`` without spamming its event log.

The native ``active_pr`` respawn guard refuses the card every tick while the PR
comment is fresh. Only one ``respawn_guarded`` event is written per change of
reason, an explicit unblock lets exactly one run through, and a card an older
build parked as blocked is handed back to ``ready``, where the guard holds it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR_COMMENT = "Opened https://github.com/example/repo/pull/44 for review."


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    kb.init_db()
    return home


def _spawned():
    calls: list[str] = []

    def spawn(task, workspace, board=None):
        calls.append(task.id)

    return calls, spawn


def _events(conn, tid, kind):
    return [json.loads(r["payload"] or "null") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _status(conn, tid):
    return conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()


def _crash_back_to_ready(conn, tid, run_id):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
            (kb.get_task(conn, tid).created_at, run_id))
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))


def test_guarded_card_waits_in_ready_with_one_event_not_one_per_tick(kanban_home):
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)

        for _ in range(5):
            res = kbd.dispatch_once(conn, spawn_fn=spawn)

        assert calls == []
        assert _status(conn, tid)["status"] == "ready"
        assert _events(conn, tid, "blocked") == []
        assert _events(conn, tid, "respawn_guarded") == [{"reason": "active_pr"}]
        assert dict(res.respawn_guarded).get(tid) == "active_pr"


def test_dry_run_reports_the_guard(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        res = kbd.dispatch_once(conn, dry_run=True)
        assert dict(res.respawn_guarded).get(tid) == "active_pr"
        assert _status(conn, tid)["status"] == "ready"


def test_explicit_unblock_lets_one_run_through_then_guard_holds(kanban_home):
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []

        # Ken (or an authorized chat) says go: block, then unblock, and the guard must not refuse it.
        assert kb.block_task(conn, tid, reason="run it anyway", kind="needs_input")
        kb.add_comment(conn, tid, author="ken", body="UNBLOCK: fix review on " + PR_COMMENT)
        assert kb.unblock_task(conn, tid)
        assert kbd.check_respawn_guard(conn, tid) is None
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]

        # That run ends without finishing and the card is back in ready: the
        # unblock was spent, so the duplicate-PR guard holds again.
        _crash_back_to_ready(conn, tid, kb.get_task(conn, tid).current_run_id)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert _status(conn, tid)["status"] == "ready"


def test_guard_still_prevents_a_duplicate_spawn_without_an_unblock(kanban_home):
    """The worker that opened the PR is never re-spawned against it, even after
    an old unblock that a later run already consumed."""
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worked once", assignee="dev")
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        claimed = kb.claim_task(conn, tid)  # the run the unblock authorised
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        _crash_back_to_ready(conn, tid, claimed.current_run_id)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []


def test_other_guard_reasons_are_recorded_once_not_every_tick(kanban_home, monkeypatch):
    _calls, spawn = _spawned()
    monkeypatch.setattr(kbd, "check_respawn_guard", lambda *_a, **_k: "rate_limit_cooldown")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cooling down", assignee="dev")
        for _ in range(4):
            kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _status(conn, tid)["status"] == "ready"
        assert _events(conn, tid, "respawn_guarded") == [{"reason": "rate_limit_cooldown"}]


def _legacy_park(conn, tid):
    """The park an older build wrote for ``active_pr``."""
    assert kb.block_task(conn, tid, reason="waiting for PR #44 to merge", kind="needs_input",
                         count_recurrence=False, source="respawn_guard")


def test_a_legacy_park_is_handed_back_to_ready_where_the_guard_holds_it(kanban_home):
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="parked by an older build", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        _legacy_park(conn, tid)
        assert _status(conn, tid)["status"] == "blocked"

        kbd.dispatch_once(conn, spawn_fn=spawn)

        assert _status(conn, tid)["status"] == "ready"
        assert _events(conn, tid, "promoted") == [{"source": "respawn_guard"}]
        # The release is not an explicit unblock: it grants no one-run consent.
        assert _events(conn, tid, "unblocked") == []
        assert calls == []
        assert not kb._has_sticky_block(conn, tid)


def test_a_human_block_on_a_legacy_park_is_not_released(kanban_home):
    calls, spawn = _spawned()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="parked, then held by a human", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        _legacy_park(conn, tid)
        last = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
        assert kb.block_task(conn, tid, reason="hold this one", kind="needs_input", expected_event_id=last)

        kbd.dispatch_once(conn, spawn_fn=spawn)

        assert calls == []
        assert _status(conn, tid)["status"] == "blocked"
        assert _events(conn, tid, "promoted") == []
