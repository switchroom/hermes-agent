"""The FIFO claim's pending-row reads wait off the owner loop.

Every turn's drain reads the session's pending admissions before its claim (and re-reads the head
after an awaited preclaim check). On slow storage that read used to stop the whole owner loop, every
other session's turn, Stop and socket included. Real SessionAuthority + state.db; reads are slowed
only for the session under test and only while the drain claims, and the stall is measured as
loop-tick lag. The claim still takes the head the read validated.
"""
import asyncio
from contextlib import closing
from dataclasses import replace
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority, _wire_turn_agent
from tests.gateway.test_session_authority_preadopt_stop import Agent
from tests.waiting import async_wait_until

SLOW_S = 2.0


@pytest.mark.asyncio
async def test_claim_pending_read_keeps_the_owner_loop_ticking(tmp_path, monkeypatch):
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from persistence.sqlite.authority.admissions import SQLiteAdmissionRepository
    db = storage_handle(tmp_path)
    seed_session(db, 's', 'cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False, adapters={}), profile_id='p', instance_id='t',
                                 persistence=db, epoch=db.runtime.begin_epoch(instance_id='t'))
    live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='u'), 'route')
    row, _ = db.admissions.admit(epoch=authority.epoch, principal_id='u', session_id='s',
                                 request_id='r1', payload={'text': 'x'})
    real, slow = SQLiteAdmissionRepository.list, threading.Event()

    def slow_list(*args, **kwargs):
        if slow.is_set():
            time.sleep(SLOW_S)  # health: allow HX013 -- the slow storage read IS the stall under test
        return real(*args, **kwargs)
    monkeypatch.setattr(SQLiteAdmissionRepository, 'list', slow_list)
    gaps = []

    async def tick():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)  # health: allow HX013 -- the ticker's period is the owner-loop stall measurement
            now = time.monotonic()
            gaps.append(now - last)
            last = now
    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.05)  # health: allow HX013 -- the ticker runs before the claim starts
    slow.set()
    try:
        claimed, first = await asyncio.wait_for(authority._claim_next(SessionRef('p', 's'), live), 30)
        # Let the ticker run once more: a stall at the claim's tail (its stamp) is measured too,
        # not hidden by cancelling the ticker before it wakes.
        await asyncio.sleep(0.1)  # health: allow HX013 -- one more ticker period after the claim returns
    finally:
        slow.clear()
        ticker.cancel()
        db.close()
    assert first['admission_id'] == row.admission_id
    assert claimed and claimed['admission_id'] == row.admission_id and claimed['status'] == 'started'
    assert max(gaps) < 0.2, f'owner loop stalled {max(gaps):.2f}s behind the claim read'


def _hold_first_claim_view(monkeypatch, authority):
    entered, release, overdue = threading.Event(), threading.Event(), threading.Event()
    owner = threading.get_ident()
    admissions = authority.persistence.admissions
    pending_snapshot = admissions.pending_snapshot

    def hold(*args, **kwargs):
        snapshot = pending_snapshot(*args, **kwargs)
        if threading.get_ident() != owner and not entered.is_set():
            entered.set()
            if not release.wait(4):  # bound a synchronous lock stall on the broken implementation
                overdue.set()
        return snapshot

    monkeypatch.setattr(admissions, 'pending_snapshot', hold)
    return entered, release, overdue


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['prompt.cancel', 'messaging /stop'])
async def test_claim_view_does_not_stall_cancel_or_restore_stale_queue(tmp_path, monkeypatch, path):
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.session_controls import AuthorityConnection

    db, authority = _authority(tmp_path, monkeypatch)
    runner = authority.runner
    runner.session_authority = authority
    runner.session_store = SimpleNamespace(peek_session_id=lambda route: REF.session_id)
    agent = Agent()
    runner._cached_agent_for = lambda route: agent
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    live = authority.sessions[REF.session_id]
    entered, release, overdue = _hold_first_claim_view(monkeypatch, authority)
    cancelled = asyncio.Event()
    views = []

    def observe(frame):
        params = frame['params']
        if params['type'] == 'session.info':
            views.append(params['payload'])
        if params['type'] == 'message.complete' and params['admission_id'] == follower.admission_id:
            cancelled.set()

    def admit(request_id, principal):
        row, _ = authority.persistence.admissions.admit(
            epoch=authority.epoch, principal_id=principal, session_id=REF.session_id,
            request_id=request_id, payload={'text': request_id})
        return row

    with db:
        active = admit('active', 'human')
        follower = admit('withdrawn', 'messaging:discord')
        survivor = admit('next', 'human')
        live.event_stream.observers.add(observe)
        claim = asyncio.create_task(authority._claim_next(REF, live))
        cancel = None
        try:
            await async_wait_until(entered.is_set, timeout=10, what='claim viewer read is held')
            if path == 'prompt.cancel':
                connection = AuthorityConnection(authority, SimpleNamespace(), {'user_id': 'human'})
                cancel = asyncio.create_task(connection.dispatch({
                    'jsonrpc': '2.0', 'method': path, 'id': 1,
                    'params': {'session_id': REF.session_id, 'admission_id': follower.admission_id},
                }))
            else:
                cancel = asyncio.create_task(GatewayInboundMixin._hm_stop_owner_turn(runner, 'route'))
            await asyncio.wait_for(cancelled.wait(), 10)
            assert not overdue.is_set(), f'{path} blocked the owner loop on claim publication'
        finally:
            release.set()
            await asyncio.wait_for(claim, 10)
            if cancel is not None:
                reply = await asyncio.wait_for(cancel, 10)
                assert reply is None or 'result' in reply, reply
        assert agent.interrupted == (path == 'messaging /stop')
        assert views
        for view in views:
            assert view['revision'] == authority._handle(REF).revision
            assert [row['admission_id'] for row in view['pending']] == [active.admission_id, survivor.admission_id]


@pytest.mark.asyncio
async def test_claim_view_refreshes_after_attach_observes_newer_queue(tmp_path, monkeypatch):
    db, authority = _authority(tmp_path, monkeypatch)
    actor = replace(ACTOR, capabilities=ACTOR.capabilities | {'session:read'})
    live = authority.sessions[REF.session_id]
    entered, release, overdue = _hold_first_claim_view(monkeypatch, authority)
    frames = []
    live.event_stream.observers.add(frames.append)

    def admit(request_id):
        row, _ = authority.persistence.admissions.admit(
            epoch=authority.epoch, principal_id='human', session_id=REF.session_id,
            request_id=request_id, payload={'text': request_id})
        return row

    with db:
        active = admit('active')
        claim = asyncio.create_task(authority._claim_next(REF, live))
        try:
            await async_wait_until(entered.is_set, timeout=10, what='claim viewer read is held')
            # Write without an event to model an admission committed before its owner publishes.
            newer = await asyncio.to_thread(admit, 'newer')
            snapshot = await authority.attach(actor, REF, history=False)
            assert not overdue.is_set(), 'attach blocked the owner loop on claim publication'
        finally:
            release.set()
            await asyncio.wait_for(claim, 10)
        ids = [active.admission_id, newer.admission_id]
        assert [row.admission_id for row in snapshot.pending] == ids
        views = [frame['params']['payload'] for frame in frames if frame['params']['type'] == 'session.info']
        assert len(views) == 1
        assert [row['admission_id'] for row in views[0]['pending']] == ids
        assert views[0]['revision'] == snapshot.handle.revision


@pytest.mark.asyncio
async def test_claim_view_does_not_retry_for_identical_attachments(tmp_path, monkeypatch):
    db, authority = _authority(tmp_path, monkeypatch)
    actor = replace(ACTOR, capabilities=ACTOR.capabilities | {'session:read'})
    live = authority.sessions[REF.session_id]
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()
    admissions = authority.persistence.admissions
    pending_snapshot = admissions.pending_snapshot
    reads = 0
    attached = []

    def read_with_attach(*args, **kwargs):
        nonlocal reads
        snapshot = pending_snapshot(*args, **kwargs)
        if threading.get_ident() != owner:
            reads += 1
            if reads <= 3:
                attachment = asyncio.run_coroutine_threadsafe(authority.attach(actor, REF, history=False), loop)
                attached.append(attachment.result(timeout=4))
        return snapshot

    monkeypatch.setattr(admissions, 'pending_snapshot', read_with_attach)
    with db:
        authority.persistence.admissions.admit(
            epoch=authority.epoch, principal_id='human', session_id=REF.session_id,
            request_id='active', payload={'text': 'active'})
        row, _ = await asyncio.wait_for(authority._claim_next(REF, live), 10)
        assert row['status'] == 'started'
        assert reads == 2, 'only the first attach observed a changed pending view'
        first, second = attached
        assert first.handle == second.handle
        assert first.pending == second.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('event_type', ['session.updated', 'session.info'])
async def test_claim_view_does_not_rewind_a_newer_session_revision(tmp_path, monkeypatch, event_type):
    from persistence.authority.runtime import Rename

    db, authority = _authority(tmp_path, monkeypatch)
    live = authority.sessions[REF.session_id]
    entered, release, overdue = _hold_first_claim_view(monkeypatch, authority)
    frames = []
    live.event_stream.observers.add(frames.append)
    repo = authority.persistence.admissions

    def admit(request_id):
        row, _ = repo.admit(epoch=authority.epoch, principal_id='human', session_id=REF.session_id,
                           request_id=request_id, payload={'text': request_id})
        return row

    with db:
        active = admit('active')
        follower = admit('follower')
        claim = asyncio.create_task(authority._claim_next(REF, live))
        try:
            await async_wait_until(entered.is_set, timeout=10, what='claim viewer read is held')
            await asyncio.to_thread(repo.cancel, epoch=authority.epoch, admission_id=follower.admission_id)
            before = authority._handle(REF).revision
            receipt = await asyncio.to_thread(
                authority.persistence.runtime.mutate, authority.epoch, principal_id='human',
                session_id=REF.session_id, request_id='rename', expected_revision=before,
                command=Rename(title='Renamed'), owner=None)
            revision = receipt.result['revision']
            assert revision > before
            # A mutation's partial event can move the viewer's CAS revision without publishing pending.
            live.event_stream.publish(REF.session_id, dict(receipt.result), event_type=event_type)
            assert not overdue.is_set()
        finally:
            release.set()
            await asyncio.wait_for(claim, 10)
        payloads = [frame['params']['payload'] for frame in frames]
        pending = [payload for payload in payloads if 'pending' in payload]
        assert len(pending) == 1
        assert pending[0]['revision'] == revision
        assert [row['admission_id'] for row in pending[0]['pending']] == [active.admission_id]


@pytest.mark.asyncio
async def test_claim_view_does_not_retry_for_unrelated_stream_events(tmp_path, monkeypatch):
    db, authority = _authority(tmp_path, monkeypatch)
    live = authority.sessions[REF.session_id]
    loop = asyncio.get_running_loop()
    admissions = authority.persistence.admissions
    pending_snapshot = admissions.pending_snapshot
    reads = 0

    def read_with_notification(*args, **kwargs):
        nonlocal reads
        snapshot = pending_snapshot(*args, **kwargs)
        reads += 1
        if reads <= 2:
            published = threading.Event()

            def notify():
                live.event_stream.publish(REF.session_id, {'text': 'preview'}, event_type='notification.show')
                published.set()

            loop.call_soon_threadsafe(notify)
            assert published.wait(4), 'notification could not publish while the claim read ran'
        return snapshot

    monkeypatch.setattr(admissions, 'pending_snapshot', read_with_notification)
    with db:
        authority.persistence.admissions.admit(
            epoch=authority.epoch, principal_id='human', session_id=REF.session_id,
            request_id='active', payload={'text': 'active'})
        row, _ = await asyncio.wait_for(authority._claim_next(REF, live), 10)
        assert row['status'] == 'started'
        assert reads == 1, 'unrelated notifications invalidated the pending snapshot'


@pytest.mark.asyncio
@pytest.mark.parametrize('newer_view', ['none', 'event', 'attach'])
async def test_delete_journal_claim_view_publishes_started_queue_during_writes(tmp_path, monkeypatch, newer_view):
    from gateway import session_bot_mailbox, session_finite
    from gateway.session_controls import AuthorityConnection
    from persistence.authority.runtime import Rename
    from persistence.sqlite import _holder
    from tui_gateway.owner_forward import _Relay

    def use_delete(conn, **_kwargs):
        return conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0]

    monkeypatch.setattr(_holder, 'apply_wal_with_fallback', use_delete)
    db, authority = _authority(tmp_path, monkeypatch)
    agent = Agent()
    authority.runner._cached_agent_for = lambda route: agent
    live = authority.sessions[REF.session_id]
    loop = asyncio.get_running_loop()
    first_read, release = threading.Event(), threading.Event()
    admissions = authority.persistence.admissions
    pending_snapshot = admissions.pending_snapshot
    wake_bot_receipts = session_bot_mailbox.wake_bot_receipts
    frames, wakeups = [], []
    reads = 0
    live.event_stream.observers.add(frames.append)

    def read_during_writes(*args, **kwargs):
        nonlocal reads
        snapshot = pending_snapshot(*args, **kwargs)
        reads += 1
        if reads == 1:
            first_read.set()
            assert release.wait(5)
        if reads <= 6:  # Bound the fixture even against an implementation that keeps retrying.
            with closing(sqlite3.connect(tmp_path / 'state.db', isolation_level=None)) as writer:
                writer.execute("UPDATE sessions SET title = ? WHERE id = 'unrelated'", (str(reads),))
        if reads == 1 and newer_view != 'none':
            receipt = authority.persistence.runtime.mutate(
                authority.epoch, principal_id='human', session_id=REF.session_id,
                request_id='rename', expected_revision=snapshot.runtime_revision,
                command=Rename(title='Renamed'), owner=None)
            if newer_view == 'event':
                live.event_stream.publish(REF.session_id, dict(receipt.result), event_type='session.updated')
            else:
                actor = replace(ACTOR, capabilities=ACTOR.capabilities | {'session:read'})
                attached = asyncio.run_coroutine_threadsafe(authority.attach(actor, REF, history=False), loop)
                assert attached.result(timeout=4).handle.revision == receipt.result['revision']
        return snapshot

    def record_wake(owner, session_id):
        wakeups.append(session_id)
        return wake_bot_receipts(owner, session_id)

    async def execute(owner, ref, row):
        _wire_turn_agent(owner, row['generation'], agent)
        await async_wait_until(lambda: agent.interrupted, timeout=5, what='active Stop reached the agent')
        return 'stopped'

    monkeypatch.setattr(admissions, 'pending_snapshot', read_during_writes)
    monkeypatch.setattr(session_bot_mailbox, 'wake_bot_receipts', record_wake)
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        seed_session(db, 'unrelated', 'test')
        with closing(sqlite3.connect(tmp_path / 'state.db')) as reader:
            assert reader.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        with db.messages.read_snapshot() as consistent:
            assert not consistent.pinned
        row, _ = authority.persistence.admissions.admit(
            epoch=authority.epoch, principal_id='human', session_id=REF.session_id,
            request_id='active', payload={'text': 'active'})
        authority._publish_pending(REF)
        authority._schedule(REF)
        connection = AuthorityConnection(authority, SimpleNamespace(), {'user_id': 'human'})
        try:
            await async_wait_until(first_read.is_set, timeout=10, what='the DELETE-mode snapshot started')
            generation = live.event_stream.execution['execution_generation']
            stop = asyncio.create_task(connection.dispatch({
                'jsonrpc': '2.0', 'method': 'session.interrupt', 'id': 1,
                'params': {'session_id': REF.session_id, 'execution_generation': generation},
            }))
            # Stop is now runnable while the claim holds its gate and the writer is ready.
            await asyncio.sleep(0)  # health: allow HX013 -- let dispatch reach the claim gate
        finally:
            release.set()
        assert 'result' in await asyncio.wait_for(stop, 10)
        await asyncio.wait_for(live.task, 10)
        assert agent.interrupted
        assert reads == (1 if newer_view == 'none' else 2)
        assert db.sessions.get('unrelated').title == str(reads)
        assert wakeups.count(REF.session_id) == 3  # Initial, claim, and terminal publications.
        assert db.admissions.get(admission_id=row.admission_id).status == 'terminal'

    events = [frame['params'] for frame in frames
              if frame['params']['type'] in {'message.start', 'session.info', 'message.complete'}]
    assert [event['type'] for event in events] == [
        'session.info', 'message.start', 'session.info', 'message.complete', 'session.info',
    ]
    assert events[1]['payload'] == {}  # The ordinary start frame has no text for Ink.
    started, terminal = events[2]['payload'], events[-1]['payload']
    assert started['running'] is True and started['execution_generation'] == generation
    assert [(entry['admission_id'], entry['status'], entry['text']) for entry in started['pending']] == [
        (row.admission_id, 'started', 'active')]
    assert terminal['pending'] == [] and terminal['running'] is False
    assert started['revision'] == terminal['revision'] - 1
    forwarded = []
    relay = _Relay('s', {}, REF.session_id, tmp_path, None, 'active', lambda *event: forwarded.append(event))
    relay._pending(events[0], row.admission_id)
    relay._pending(events[2], row.admission_id)
    assert [[entry['status'] for entry in payload['pending_submissions']] for _, _, payload in forwarded] == [
        ['queued'], [],
    ]
