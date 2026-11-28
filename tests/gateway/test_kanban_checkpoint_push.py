"""Human checkpoints reach push chats (Telegram/Slack) through the real gateway sub-op path.

Regression: ``GatewayKanbanWatchersMixin._kanban_sub_op`` discarded the return value of
``begin_checkpoint_ping``, so the notifier read ``None`` as "an earlier send is uncertain",
left every checkpoint pending forever and never sent one to a push chat. Earlier tests used
a ``Mock``/local ``sub_op`` that did return the value, hiding the defect.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _KanbanNotification
from gateway.platforms.base import SendResult
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tools import kanban_tools  # noqa: F401  registers the real producer
from tools.registry import registry


class _Runner(GatewayKanbanWatchersMixin):
    """Only the real cursor/subscription helpers; no gateway start-up."""

    def __init__(self):
        self._deliver_kanban_artifacts = AsyncMock()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()


def _deliver_once(runner, task_id, adapter):
    with kbc.connect_closing() as conn:
        sub = kbn.list_notify_subs(conn, task_id)[0]
        old, cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=task_id, platform=sub["platform"], chat_id=sub["chat_id"],
            thread_id=sub.get("thread_id") or "", kinds=TERMINAL_KINDS)
        task = kb.get_task(conn, task_id)
    notice = _KanbanNotification(runner, {"sub": sub, "task": task, "events": events,
                                           "old_cursor": old, "cursor": cursor, "board": None},
                                 platform_cls=None, sub_fail_counts={})
    notice.adapter = adapter
    return events, asyncio.run(notice._send_pings())


def test_real_sub_op_returns_begin_checkpoint_answer(board):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="sub op")
        kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="42")
        sub = kbn.list_notify_subs(conn, task_id)[0]
    runner = _Runner()
    assert runner._kanban_sub_op(None, "begin_checkpoint_ping", sub, event_id=10) is True
    # Same event again is an uncertain earlier send: refuse, never resend.
    assert runner._kanban_sub_op(None, "begin_checkpoint_ping", sub, event_id=10) is False


@pytest.mark.parametrize("mode", ["wake", "notify", "notify+wake"])
def test_checkpoint_reaches_telegram_once_and_comments_stay_out(board, mode):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="Fleet cutover")
        kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="42",
                           thread_id="7", delivery_mode=mode)
        kb.add_comment(conn, task_id, "worker", "## Plan and progress (agent)\nStatus: working")
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "heartbeat", {"note": "still going"})
    body = "**Five agents live**\n**What happened:** rolled.\n**Next:** Klanker."
    args = {"task_id": task_id, "body": body, "human_checkpoint": True, "request_id": "stage-live"}
    assert json.loads(registry.dispatch("kanban_comment", args)).get("comment_id")

    runner = _Runner()
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True, message_id="m1")))
    events, ok = _deliver_once(runner, task_id, adapter)
    assert ok is True
    assert [e.kind for e in events] == ["checkpoint"]
    assert adapter.send.await_count == 1
    assert adapter.send.await_args.args[:2] == ("42", body)
    assert adapter.send.await_args.kwargs["metadata"]["thread_id"] == "7"
    with kbc.connect_closing() as conn:
        sub = kbn.list_notify_subs(conn, task_id)[0]
    assert sub["checkpoint_pending_event_ids"] == []
    assert sub["last_ping_event_id"] == events[0].id

    # A retried tool call with the same request_id adds no event and sends nothing new.
    assert json.loads(registry.dispatch("kanban_comment", args)).get("comment_id")
    events, ok = _deliver_once(runner, task_id, adapter)
    assert ok is True and events == [] and adapter.send.await_count == 1


def test_worker_created_child_checkpoint_reaches_parent_chat(board):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        kbn.add_notify_sub(conn, task_id=parent, platform="telegram", chat_id="42",
                           delivery_mode="wake", notifier_profile="default")
        child = kb.create_task(conn, title="split-out stage", creator_task_id=parent)
        subs = kbn.list_notify_subs(conn, child)
        assert [(s["platform"], s["chat_id"]) for s in subs] == [("telegram", "42")]
        kb.add_comment(conn, child, "worker", "**Child stage done**", human_checkpoint=True,
                       request_id="child-1")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
    _events, ok = _deliver_once(_Runner(), child, adapter)
    assert ok is True
    assert adapter.send.await_args.args[:2] == ("42", "**Child stage done**")
