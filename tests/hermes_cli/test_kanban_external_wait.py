"""A wait a plugin will resume itself is board state, not a block someone must answer (HF-850).

A worker that parks on CI or on another tracker issue calls ``kanban_block`` with
``kind="dependency"``. With no open parent core re-kinds it to ``needs_input``: nothing
native would clear it. When a plugin (the Linear bridge) resumes such waits itself, every
park pinged "blocked; needs attention" and the second park on one card tripped the
block-loop breaker into triage. A plugin now claims the wait through ``kanban_wait_claim``;
core marks it ``source: external_wait`` and leaves it out of the loop count. A wait no
plugin claims, and every real question, still alerts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_db_guard_wait import is_board_hold_event
from hermes_cli.plugins import get_plugin_manager

CI_WAIT = "CI gate: example/repo@" + "a" * 40


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """A claimed card and the env of the worker that owns it."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix", assignee="test-worker")
    _run_again(tid, monkeypatch)
    return tid


def _run_again(tid, monkeypatch):
    with kbc.connect() as conn:
        if kb.get_task(conn, tid).status != "ready":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))


@pytest.fixture
def claims():
    """Register a ``kanban_wait_claim`` callback; returns the calls it saw."""
    hooks = get_plugin_manager()._hooks
    saved = list(hooks.get("kanban_wait_claim", []))
    calls: list[dict] = []

    def install(answer):
        def callback(**kw):
            calls.append(kw)
            if isinstance(answer, Exception):
                raise answer
            return answer(kw) if callable(answer) else answer
        hooks["kanban_wait_claim"] = [callback]
        return calls

    yield install
    hooks["kanban_wait_claim"] = saved


def _block(reason=CI_WAIT, kind="dependency"):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_block({"reason": reason, "kind": kind}))


def _last_block(tid):
    with kbc.connect() as conn:
        return [ev for ev in kb.list_events(conn, tid) if ev.kind in ("blocked", "block_loop_detected")][-1]


def _notice(tid, platform="telegram", chat_id="ken"):
    """What the gateway notifier would ping and wake for every event on ``tid`` so far."""
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _KanbanNotification

    with kbc.connect() as conn:
        if not kbn.list_notify_subs(conn, tid):
            kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id, delivery_mode="notify+wake")
            kbn.advance_notify_cursor(conn, task_id=tid, platform=platform, chat_id=chat_id, new_cursor=0)
        sub = kbn.list_notify_subs(conn, tid)[0]
        old, cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform=platform, chat_id=chat_id, kinds=TERMINAL_KINDS)
        task = kb.get_task(conn, tid)
    sent: list[str] = []
    notice = _KanbanNotification(
        SimpleNamespace(_kanban_sub_op=lambda *a, **k: True),
        {"sub": sub, "task": task, "events": events, "old_cursor": old, "cursor": cursor, "board": "default"},
        platform_cls=None, sub_fail_counts={})

    async def record(ev, msg):
        sent.append(msg)
        return True

    notice._send_event = record
    asyncio.run(notice._send_pings())
    notice.build_wake_text()
    return sent, notice.wake_kinds


def _resume(tid):
    with kbc.connect() as conn:
        assert kb.unblock_task(conn, tid)
        return kb.get_task(conn, tid).status


def test_claimed_parks_are_quiet_and_never_trip_the_block_loop(worker, claims, monkeypatch):
    calls = claims({"resumes": True})
    statuses = []
    for _ in range(3):
        landed = _block()
        assert (landed["status"], landed["block_kind"]) == ("blocked", "needs_input")
        assert "will unblock the task itself" in landed["note"]
        event = _last_block(worker)
        assert event.kind == "blocked" and event.payload["source"] == "external_wait"
        assert (event.payload["requested_kind"], event.payload["rekind_reason"]) == ("dependency", "no_open_parent")
        assert is_board_hold_event(event.kind, event.payload)
        statuses.append(_resume(worker))
        _run_again(worker, monkeypatch)

    assert statuses == ["ready"] * 3
    assert _notice(worker) == ([], set())
    with kbc.connect() as conn:
        assert kb.get_task(conn, worker).block_recurrences == 0
    assert calls[0]["task_id"] == worker and calls[0]["reason"] == CI_WAIT
    assert calls[0]["board"] and calls[0]["profile_name"]


@pytest.mark.parametrize("answer", [None, {"resumes": False}, {"resumes": "yes"}, RuntimeError("plugin down")])
def test_an_unclaimed_wait_still_alerts_and_the_second_reaches_triage(worker, claims, monkeypatch, answer):
    claims(answer)
    _block()
    first = _last_block(worker)
    assert "source" not in first.payload and not is_board_hold_event(first.kind, first.payload)
    sent, wake = _notice(worker)
    assert len(sent) == 1 and wake == {"blocked"}

    _resume(worker)
    _run_again(worker, monkeypatch)
    _block()
    assert _last_block(worker).kind == "block_loop_detected"
    with kbc.connect() as conn:
        assert kb.get_task(conn, worker).status == "triage"


def test_without_any_plugin_the_wait_alerts_as_before(worker):
    hooks = get_plugin_manager()._hooks
    saved = hooks.pop("kanban_wait_claim", None)
    try:
        _block()
    finally:
        if saved is not None:
            hooks["kanban_wait_claim"] = saved
    assert len(_notice(worker)[0]) == 1


def test_a_real_question_after_quiet_parks_counts_and_alerts_as_usual(worker, claims, monkeypatch):
    claims({"resumes": True})
    _block()
    _resume(worker)
    _run_again(worker, monkeypatch)
    _notice(worker)  # the park: nothing

    _block("Which account should this use?", kind="needs_input")
    sent, wake = _notice(worker)
    assert len(sent) == 1 and wake == {"blocked"}
    with kbc.connect() as conn:
        assert kb.get_task(conn, worker).block_recurrences == 1


def test_the_claim_is_asked_only_for_dependency_waits(worker, claims):
    calls = claims({"resumes": True})
    _block("need a decision", kind="needs_input")
    assert calls == []
    assert "source" not in _last_block(worker).payload


def test_a_claimed_wait_on_an_open_parent_stays_a_native_dependency(worker, claims, monkeypatch):
    claims({"resumes": True})
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream", assignee="other")
        # A worker may link its own running card right before a dependency block.
        kb.link_tasks(conn, parent_id=parent, child_id=worker,
                      expected_child_run_id=kb._current_run_id(conn, worker))
        assert kb.parent_ids(conn, worker) == [parent]

    landed = _block()
    assert landed["status"] == "todo", landed
    with kbc.connect() as conn:
        last = kb.list_events(conn, worker)[-1]
    assert last.kind == "dependency_wait" and "source" not in (last.payload or {})


def test_a_park_from_review_resumes_into_review(worker, claims, monkeypatch):
    claims({"resumes": True})
    with kbc.connect() as conn:
        assert kb.request_review(conn, worker, summary="ready for review",
                                 expected_run_id=kb._current_run_id(conn, worker))
        reviewing = kb.claim_review_task(conn, worker)  # the reviewer's run
        assert reviewing is not None
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, worker)))
    _block()
    event = _last_block(worker)
    assert event.payload["source"] == "external_wait"
    assert event.payload["source_status"] == "review"
    assert _resume(worker) == "review"


def test_a_stop_during_a_quiet_park_wins_and_alerts(worker, claims):
    claims({"resumes": True})
    _block()
    park = _last_block(worker)
    _notice(worker)
    with kbc.connect() as conn:
        # Stop: an operator's guarded needs_input on the parked card.
        assert kb.block_task(conn, worker, reason="Stopped by Ken", kind="needs_input", expected_event_id=park.id)
        stop = _last_block(worker)
        assert stop.id > park.id and not is_board_hold_event(stop.kind, stop.payload)
        # The plugin's resume, guarded on the park it saw, now refuses.
        assert not kb.unblock_task(conn, worker, expected_event_id=park.id)
        assert kb.get_task(conn, worker).status == "blocked"
    sent, wake = _notice(worker)
    assert len(sent) == 1 and wake == {"blocked"}
