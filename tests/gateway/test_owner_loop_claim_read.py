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
@pytest.mark.requires_wal
@pytest.mark.parametrize('interference', ['publish', 'attach', 'service_tier'])
async def test_claim_refresh_coexists_with_same_session_activity(tmp_path, monkeypatch, interference):
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_busy_controls import busy_config
    from gateway.session_contract import SessionActor, SessionRef
    from persistence.sqlite.authority.admissions import SQLiteAdmissionRepository

    db = storage_handle(tmp_path)
    seed_session(db, 's', 'gui')
    runner = SimpleNamespace(_draining=False, adapters={}, _set_session_service_tier_override=lambda route, tier: None)
    authority = SessionAuthority(runner, profile_id='p', instance_id='t',
                                 persistence=db, epoch=db.runtime.begin_epoch(instance_id='t'))
    live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='u'), 'route')
    ref = SessionRef('p', 's')
    head, _ = db.admissions.admit(epoch=authority.epoch, principal_id='u', session_id='s',
                                  request_id='r1', payload={'text': 'first'})

    def admit_follower():
        row, _ = db.admissions.admit(epoch=authority.epoch, principal_id='u', session_id='s',
                                    request_id='r2', payload={'text': 'second'})
        return row

    # The service tier affects no admission state. The other cases observe a new follower instead.
    follower = admit_follower() if interference == 'service_tier' else None
    frames = []
    live.event_stream.observers.add(lambda frame: frames.append(frame['params']))
    loop = asyncio.get_running_loop()
    owner_thread = threading.current_thread()
    reading = asyncio.Queue()
    done_reading, expired = threading.Event(), threading.Event()
    read = SQLiteAdmissionRepository.list

    def hold_claim_refresh(self, **kwargs):
        rows = read(self, **kwargs)
        if (threading.current_thread() is not owner_thread and not done_reading.is_set() and not expired.is_set()
                and any(row.admission_id == head.admission_id and row.status == 'started' for row in rows)):
            release = threading.Event()
            loop.call_soon_threadsafe(reading.put_nowait, release)
            # A broken owner-loop lock acquisition must let the test fail without hanging the file.
            if not release.wait(5):
                expired.set()
        return rows

    monkeypatch.setattr(SQLiteAdmissionRepository, 'list', hold_claim_refresh)
    claim = asyncio.create_task(authority._claim_next(ref, live))
    heartbeat = asyncio.Event()
    release = None
    try:
        release = await asyncio.wait_for(reading.get(), 10)
        if follower is None:
            follower = admit_follower()
        loop.call_soon(heartbeat.set)
        if interference == 'publish':
            authority._publish_pending(ref)  # takes the same stream lock on the owner loop
        else:
            actor = SessionActor('u', 'p', frozenset({'session:read', 'session:control'}), 'viewer')
            connection = SimpleNamespace(authority=authority, actor=actor)
            # Interfere with every paused attempt so a redundant retry cannot hide a lost refresh.
            for tier in ('auto', 'cold'):
                if interference == 'attach':
                    snapshot = await authority.attach(actor, ref, history=False)
                    assert [row.admission_id for row in snapshot.pending] == [head.admission_id, follower.admission_id]
                else:
                    await busy_config(connection, ref, {'key': 'service_tier', 'value': tier}, write=True)
                next_read = asyncio.create_task(reading.get())
                release.set()
                try:
                    done, _ = await asyncio.wait({claim, next_read}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
                    if claim in done:
                        break
                    assert next_read in done
                    release = next_read.result()
                finally:
                    next_read.cancel()
            assert claim.done(), 'same-session activity kept the committed claim waiting on refresh reads'
        await asyncio.wait_for(heartbeat.wait(), 2)
        assert not expired.is_set(), 'same-session activity blocked the heartbeat behind the refresh read'
    finally:
        done_reading.set()
        if release is not None:
            release.set()
        claimed, _ = await asyncio.wait_for(claim, 10)

    revision = db.sessions.get('s').runtime_revision
    assert frames[0]['type'] == 'message.start'
    assert all(frame['type'] == 'session.info' for frame in frames[1:])
    if interference == 'service_tier':
        assert frames[1]['payload'] == {'service_tier': 'auto', 'fast': False}
    snapshots = [frame for frame in frames[1:] if 'pending' in frame['payload']]
    assert snapshots == frames[-1:], 'the claim must publish its pending and running state after the intervening events'
    frame = snapshots[0]
    assert frame['execution_generation'] == claimed['generation']
    assert frame['payload']['running'] is True
    assert frame['payload']['revision'] == revision
    first, second = frame['payload']['pending']
    assert (first['admission_id'], first['status']) == (head.admission_id, 'started')
    assert (second['admission_id'], second['controls']) == (follower.admission_id, ['steer', 'replace'])


@pytest.mark.asyncio
@pytest.mark.requires_wal
@pytest.mark.parametrize('method', ['session.interrupt', 'prompt.cancel', 'retire'])
async def test_started_claim_can_be_stopped_while_viewer_refresh_waits(tmp_path, monkeypatch, method):
    from gateway import session_finite
    from gateway.run_runtime import _retire_profile_authority
    from gateway.session_controls import AuthorityConnection
    from persistence.core.errors import StoreContention
    from persistence.sqlite.authority.admissions import SQLiteAdmissionRepository
    from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit
    from tests.waiting import async_wait_until

    db, authority = _authority(tmp_path, monkeypatch)
    authority.runner._cached_agent_for = lambda route: None
    live = authority.sessions[REF.session_id]
    connection = AuthorityConnection(authority, object(), {'user_id': 'human'})
    frames, interrupted, executed = [], [], []
    live.event_stream.observers.add(lambda frame: frames.append(frame['params']))

    async def execute(owner, ref, row):
        agent = SimpleNamespace(interrupt=lambda *args, **kwargs: interrupted.append(row['generation']))
        owner.adopt_agent(ref.session_id, row['generation'], agent)
        executed.append(row['admission_id'])
        return 'done'

    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    loop, owner_thread = asyncio.get_running_loop(), threading.current_thread()
    reading = asyncio.Event()
    release = threading.Event()
    read = SQLiteAdmissionRepository.list

    def hold_viewer_read(self, **kwargs):
        rows = read(self, **kwargs)
        if (threading.current_thread() is not owner_thread and not release.is_set()
                and any(row.request_id == 'first' and row.status == 'started' for row in rows)):
            loop.call_soon_threadsafe(reading.set)
            release.wait(10)
            if method == 'prompt.cancel':
                raise StoreContention('viewer read lost its storage lock')
        return rows

    monkeypatch.setattr(SQLiteAdmissionRepository, 'list', hold_viewer_read)
    retirement, follower = None, None
    with db:
        head = await _submit(authority, 'first')
        try:
            await asyncio.wait_for(reading.wait(), 5)
            started = authority.persistence.admissions.get(admission_id=head.admission_id)
            if method == 'retire':
                retirement = asyncio.create_task(_retire_profile_authority(authority, timeout=8))
                await async_wait_until(lambda: authority.retiring, timeout=3, what='the retirement fence')
            else:
                params = {'execution_generation': started.generation} if method == 'session.interrupt' else {
                    'admission_id': head.admission_id}
                reply = await asyncio.wait_for(connection.dispatch({
                    'id': 1, 'method': method, 'params': {'session_id': REF.session_id, **params},
                }), 3)
                assert reply.get('result', {}).get('execution_generation') == started.generation, reply
                # Submission takes the mutation gate and publishes a complete, newer queue snapshot.
                follower = await asyncio.wait_for(_submit(authority, 'second'), 3)
            assert authority.pending_stops[REF.session_id] == started.generation
            assert not live.task.done() and not executed
        finally:
            release.set()
            await asyncio.wait_for(live.task, 10)
            if retirement is not None:
                assert await asyncio.wait_for(retirement, 10) and authority.retired

    expected = [head.admission_id] if follower is None else [head.admission_id, follower.admission_id]
    assert executed == expected
    assert interrupted == [started.generation]
    running = [frame['payload']['pending'] for frame in frames if frame['type'] == 'session.info'
               and frame['payload']['running'] and frame['execution_generation'] == started.generation]
    # Retirement suppresses the waiting cosmetic frame. Otherwise the one running snapshot was
    # already sent by the newer submission, and the older read must not publish behind it.
    assert [[row['admission_id'] for row in pending] for pending in running] == ([] if follower is None else [expected])


@pytest.mark.asyncio
@pytest.mark.parametrize('journal_mode', ['delete', pytest.param('wal', marks=pytest.mark.requires_wal)])
async def test_claim_without_a_pooled_reader_still_starts_and_settles(tmp_path, monkeypatch, journal_mode):
    from contextlib import ExitStack
    from gateway import session_finite
    from persistence.sqlite._state.readpool import _READ_POOL_MAX
    from tests.fixtures.storage_holder import raw_holder
    from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit

    (tmp_path / 'config.yaml').write_text(f'database:\n  journal_mode: {journal_mode}\n', encoding='utf-8')
    db, authority = _authority(tmp_path, monkeypatch)
    holder = raw_holder(authority.persistence)
    assert holder._wal_active == (journal_mode == 'wal')
    live = authority.sessions[REF.session_id]
    frames = []
    live.event_stream.observers.add(lambda frame: frames.append(frame['params']))

    async def execute(owner, ref, row):
        return 'done'

    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db, ExitStack() as readers:
        if journal_mode == 'wal':
            for _ in range(_READ_POOL_MAX):
                assert readers.enter_context(holder._read_ctx(pooled_only=True)) is not None
        receipt = await _submit(authority, 'input')
        await asyncio.wait_for(live.task, 10)
        settled = authority.persistence.admissions.get(admission_id=receipt.admission_id)

    assert (settled.status, settled.outcome) == ('terminal', 'completed')
    first = next(i for i, frame in enumerate(frames) if frame['type'] == 'message.start')
    assert [frame['type'] for frame in frames[first:]] == ['message.start', 'message.complete', 'session.info']
    assert frames[first]['execution_generation'] == settled.generation
    assert frames[-1]['payload']['running'] is False
