"""The FIFO claim's pending-row reads wait off the owner loop.

Every turn's drain reads the session's pending admissions before its claim (and re-reads the head
after an awaited preclaim check). On slow storage that read used to stop the whole owner loop, every
other session's turn, Stop and socket included. Real SessionAuthority + state.db; reads are slowed
only for the session under test and only while the drain claims, and the stall is measured as
loop-tick lag. The claim still takes the head the read validated.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle

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


@pytest.mark.asyncio
async def test_claim_refresh_allows_same_session_publication_while_storage_waits(tmp_path, monkeypatch):
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from persistence.sqlite.authority.admissions import SQLiteAdmissionRepository

    db = storage_handle(tmp_path)
    seed_session(db, 's', 'gui')
    authority = SessionAuthority(SimpleNamespace(_draining=False, adapters={}), profile_id='p', instance_id='t',
                                 persistence=db, epoch=db.runtime.begin_epoch(instance_id='t'))
    live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='u'), 'route')
    ref = SessionRef('p', 's')
    head, _ = db.admissions.admit(epoch=authority.epoch, principal_id='u', session_id='s',
                                  request_id='r1', payload={'text': 'first'})
    frames = []
    live.event_stream.observers.add(lambda frame: frames.append(frame['params']))
    loop = asyncio.get_running_loop()
    owner_thread = threading.current_thread()
    reading = asyncio.Event()
    release, expired = threading.Event(), threading.Event()
    read = SQLiteAdmissionRepository.list

    def hold_claim_refresh(self, **kwargs):
        rows = read(self, **kwargs)
        if (threading.current_thread() is not owner_thread and not release.is_set() and not expired.is_set()
                and any(row.admission_id == head.admission_id and row.status == 'started' for row in rows)):
            loop.call_soon_threadsafe(reading.set)
            # A broken owner-loop lock acquisition must let the test fail without hanging the file.
            if not release.wait(5):
                expired.set()
        return rows

    monkeypatch.setattr(SQLiteAdmissionRepository, 'list', hold_claim_refresh)
    claim = asyncio.create_task(authority._claim_next(ref, live))
    heartbeat = asyncio.Event()
    try:
        await asyncio.wait_for(reading.wait(), 10)
        follower, _ = db.admissions.admit(epoch=authority.epoch, principal_id='u', session_id='s',
                                         request_id='r2', payload={'text': 'second'})
        loop.call_soon(heartbeat.set)
        authority._publish_pending(ref)  # takes the same stream lock on the owner loop
        await asyncio.wait_for(heartbeat.wait(), 2)
        assert not expired.is_set(), 'same-session publication blocked the heartbeat behind the refresh read'
    finally:
        release.set()
        claimed, _ = await asyncio.wait_for(claim, 10)

    revision = db.sessions.get('s').runtime_revision
    assert [frame['type'] for frame in frames] == ['message.start', 'session.info', 'session.info']
    for frame in frames[1:]:
        assert frame['execution_generation'] == claimed['generation']
        assert frame['payload']['running'] is True
        assert frame['payload']['revision'] == revision
        first, second = frame['payload']['pending']
        assert (first['admission_id'], first['status']) == (head.admission_id, 'started')
        assert (second['admission_id'], second['controls']) == (follower.admission_id, ['steer', 'replace'])
