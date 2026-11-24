"""Wake-only (delivery_mode='wake') push-adapter delivery ordering.

For a push-adapter subscription in wake-only mode the visible text ping is
intentionally skipped (the ``send_passive`` gate), so the wake injection IS
the sole delivery. The cursor must therefore only advance AFTER the wake
succeeds: advancing first and running the wake best-effort afterwards let a
failed wake permanently lose the event — the exact bug class the non-push
(api_server) self-post branch already guards against with rewind/retry.

Residual insight extracted from closed PR #84191 (@MaximCrabbe).
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    """Push-capable adapter (no supports_async_delivery attr => push)."""

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


class FailingWakeAdapter(RecordingAdapter):
    """Push adapter whose wake injection (handle_message) always fails."""

    async def handle_message(self, event):
        self.handled.append(event)
        raise RuntimeError("simulated wake failure")


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _make_completed_task(delivery_mode, summary="done"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="wake ordering task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode=delivery_mode,
        )
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def _subs(tid):
    conn = kbc.connect()
    try:
        return kbn.list_notify_subs(conn, tid)
    finally:
        conn.close()


def test_wake_only_success_advances_cursor_single_wake(tmp_path, monkeypatch):
    """Wake succeeds: exactly one wake, no text ping, cursor advanced."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-ok.db"))
    kb.init_db()
    tid = _make_completed_task("wake")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == [], "wake-only mode must not send a text ping"
    assert len(adapter.handled) == 1, "wake injection is the sole delivery"
    assert _unseen_terminal_events(tid) == [], (
        "cursor must advance after a successful wake-only delivery"
    )
    assert runner._kanban_sub_fail_counts == {}


def test_wake_only_failure_rewinds_and_redelivers(tmp_path, monkeypatch):
    """Wake fails: cursor rewound, counter bumped, event retried next tick."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-fail.db"))
    kb.init_db()
    tid = _make_completed_task("wake")

    adapter = FailingWakeAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.handled) == 1, "one wake attempt on the first tick"
    events = _unseen_terminal_events(tid)
    assert len(events) == 1, (
        "a failed wake-only delivery must REWIND the claim so the event "
        "is redelivered on a later tick — it was permanently lost before "
        "this fix (cursor advanced before the best-effort wake)"
    )
    assert list(runner._kanban_sub_fail_counts.values()) == [1], (
        "failure counter must bump on a failed wake-only delivery"
    )
    assert len(_subs(tid)) == 1, "one transient failure must not drop the sub"

    # Next tick: the same event is claimed and the wake retried.
    runner2 = _make_runner(adapter)
    runner2._kanban_sub_fail_counts = runner._kanban_sub_fail_counts
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner2))
    assert len(adapter.handled) == 2, "event must be redelivered next tick"
    assert list(runner2._kanban_sub_fail_counts.values()) == [2]


def test_notify_wake_failure_retries_without_repeating_ping(tmp_path, monkeypatch):
    """notify+wake requires both deliveries, retaining the sent-ping checkpoint."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "notify-wake.db"))
    kb.init_db()
    tid = _make_completed_task("notify+wake")

    adapter = FailingWakeAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "text ping delivered"
    assert len(adapter.handled) == 1, "wake attempted after the ping"
    assert len(_unseen_terminal_events(tid)) == 1
    assert list(runner._kanban_sub_fail_counts.values()) == [1]
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 2
    assert list(runner._kanban_sub_fail_counts.values()) == [2]


def test_wake_only_failure_cap_drops_subscription(tmp_path, monkeypatch):
    """After MAX_SEND_FAILURES consecutive wake failures the sub is dropped."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-cap.db"))
    kb.init_db()
    tid = _make_completed_task("wake")

    adapter = FailingWakeAdapter()
    runner = _make_runner(adapter)
    # Simulate 11 prior consecutive failures (MAX_SEND_FAILURES = 12).
    runner._kanban_sub_fail_counts = {
        (tid, "telegram", "chat-1", ""): 11,
    }
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.handled) == 1
    assert _subs(tid) == [], (
        "subscription must drop after MAX_SEND_FAILURES consecutive "
        "wake-only delivery failures, like text sends do"
    )
    assert runner._kanban_sub_fail_counts == {}, (
        "counter entry must clear when the subscription is dropped"
    )


def test_wake_only_completion_still_uploads_artifacts_once(tmp_path, monkeypatch):
    """Wake-only skips the text ping, not the handoff's files: they upload once,
    and a wake retry does not upload them again."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-artifacts.db"))
    kb.init_db()
    tid = _make_completed_task("wake")

    adapter = FailingWakeAdapter()
    runner = _make_runner(adapter)
    uploads = []

    async def record_upload(**kwargs):
        uploads.append(kwargs)

    runner._deliver_kanban_artifacts = record_upload
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == [], "wake-only mode must not send a text ping"
    assert len(uploads) == 1 and uploads[0]["chat_id"] == "chat-1"
    assert len(_unseen_terminal_events(tid)) == 1, "failed wake is retried"

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 2, "wake retried"
    assert len(uploads) == 1, "artifacts must not re-upload on a wake retry"


# --- HF-824: a wake-only notice must not wait behind the destination's running turn ---

_SUMMARY = (
    "**Delivered**\nThe notifier now sends the plain notice while the chat is busy.\n\n"
    "**Evidence**\nPR and CI links.\n\n**Remaining**\nNothing"
)


def _mark_destination_busy(monkeypatch, runner):
    """Put the wake destination's session key in ``_running_agents`` (a turn is running)."""
    from gateway.session import SessionSource

    key = runner._session_key_for_source(
        SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm"))
    assert key
    monkeypatch.setattr(GatewayRunner, "_running_agents", {key: object()}, raising=False)
    return key


def test_wake_only_busy_destination_sends_plain_notice_now(tmp_path, monkeypatch):
    """Busy chat: the plain notice (with the summary's first section) is sent right away,
    and the wake is still queued for after the turn, told the notice already went out."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-busy.db"))
    kb.init_db()
    tid = _make_completed_task("wake", summary=_SUMMARY)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    _mark_destination_busy(monkeypatch, runner)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "a busy wake-only chat gets the plain notice immediately"
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "The notifier now sends the plain notice while the chat is busy." in text
    assert "**Evidence**" not in text, "only the first section, not the whole summary"
    assert len(adapter.handled) == 1, "the wake is still delivered (queued behind the turn)"
    wake_text = adapter.handled[0].text
    assert "already got the plain notice" in wake_text
    assert _unseen_terminal_events(tid) == []


def test_wake_only_idle_destination_still_sends_no_ping(tmp_path, monkeypatch):
    """Idle chat: unchanged single-message behaviour (the wake reply is the notice)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-idle.db"))
    kb.init_db()
    _make_completed_task("wake", summary=_SUMMARY)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    monkeypatch.setattr(GatewayRunner, "_running_agents", {"agent:main:telegram:dm:other": object()},
                        raising=False)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert len(adapter.handled) == 1
    assert "already got the plain notice" not in adapter.handled[0].text
    assert "The notifier now sends the plain notice while the chat is busy." in adapter.handled[0].text


def test_wake_only_busy_ping_not_resent_on_wake_retry(tmp_path, monkeypatch):
    """The early notice is checkpointed: a failed wake retries without a second ping."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-busy-retry.db"))
    kb.init_db()
    tid = _make_completed_task("wake", summary=_SUMMARY)

    adapter = FailingWakeAdapter()
    runner = _make_runner(adapter)
    _mark_destination_busy(monkeypatch, runner)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(_unseen_terminal_events(tid)) == 1, "failed wake is retried"

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 2, "wake retried"
    assert len(adapter.sent) == 1, "the notice is not sent twice"


def test_first_section_takes_heading_and_its_body():
    from gateway.kanban_watchers_notifier import _first_section

    assert _first_section(_SUMMARY) == (
        "**Delivered**\nThe notifier now sends the plain notice while the chat is busy.")
    assert _first_section("# Done\n\nAll good.\n## Next\nmore") == "# Done\n\nAll good."
    assert _first_section("plain one-liner") == "plain one-liner"
    assert _first_section("**Delivered**") == "**Delivered**"
    # A bold lead-in followed by text on the same line is content, not a heading.
    assert _first_section("**UAT passed.** Release is live.\n**Evidence**\nx") == "**UAT passed.** Release is live."


def test_first_section_caps_at_600_on_a_word_boundary():
    from gateway.kanban_watchers_notifier import HANDOFF_SECTION_CHARS, _first_section

    body = "**Delivered**\n" + " ".join(["word"] * 400)
    out = _first_section(body)
    assert HANDOFF_SECTION_CHARS == 600
    assert out.endswith("…") and len(out) <= 601
    assert out[:-1].endswith("word")
