"""The FIFO claim's pending-row reads wait off the owner loop.

Every turn's drain reads the session's pending admissions before its claim (and re-reads the head
after an awaited preclaim check). On slow storage that read used to stop the whole owner loop, every
other session's turn, Stop and socket included. Real SessionAuthority + state.db; reads are slowed
only for the session under test and only while the drain claims, and the stall is measured as
loop-tick lag. The claim still takes the head the read validated.
"""
import asyncio
from dataclasses import replace
import threading
import time
from types import SimpleNamespace

import pytest

from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority
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


def _hold_first_claim_view(monkeypatch):
    from gateway import session_queued_controls

    entered, release, overdue = threading.Event(), threading.Event(), threading.Event()
    owner = threading.get_ident()
    pending_rows = session_queued_controls.pending_rows

    def hold(*args, **kwargs):
        rows = pending_rows(*args, **kwargs)
        if threading.get_ident() != owner and not entered.is_set():
            entered.set()
            if not release.wait(4):  # bound a synchronous lock stall on the broken implementation
                overdue.set()
        return rows

    monkeypatch.setattr(session_queued_controls, 'pending_rows', hold)
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
    entered, release, overdue = _hold_first_claim_view(monkeypatch)
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
    entered, release, overdue = _hold_first_claim_view(monkeypatch)
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
