"""A native Stop is the interrupt plus a best-effort kill of the session's own background processes.

Nothing about a Stop is durable: no process-Stop fence, no typed Stop row. A failed kill is
logged and never gates the next claim, and rows an older build left behind (a queued typed
Stop, a pending process-Stop fence) cannot wedge a session after restart.
"""
import asyncio
import json
import shlex
import sys
from types import SimpleNamespace
import uuid

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_unattended_unknown import settle_recovered_unattended
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import storage_handle
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority, _submit, _wire_turn_agent
from tests.gateway.test_session_authority_preadopt_stop import Agent
from tests.waiting import async_wait_until, wait_until

FENCE_PREFIX = 'gateway.process_stop.v1:'


@pytest.fixture
def registry(monkeypatch):
    from tools import process_registry as module
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    yield registry
    for process in list(registry._running.values()):
        registry.kill_process(process.id)


def _spawn(registry, tmp_path):
    ready = tmp_path / ('ready-' + uuid.uuid4().hex)
    code = f'import pathlib,threading; pathlib.Path({str(ready)!r}).touch(); threading.Event().wait(120)'
    command = 'exec ' + shlex.join([sys.executable, '-c', code])
    process = registry.spawn_local(command, cwd=str(tmp_path), task_id='s', owner_task_id='s', session_key='route')
    wait_until(ready.exists, timeout=20, what='owned fixture ready')
    return process


def _durable_stop_state(store):
    with raw_holder(store)._read_ctx() as conn:
        fences = conn.execute('SELECT COUNT(*) FROM state_meta WHERE key GLOB ?', (FENCE_PREFIX + '*',)).fetchone()[0]
        controls = conn.execute("SELECT COUNT(*) FROM session_admissions WHERE principal_id='native-process-stop' "
                                "OR json_type(payload_json,'$.native_stop_v1') IS NOT NULL").fetchone()[0]
    return fences, controls


def _running_turn(authority, monkeypatch, agent, spawn=None):
    """Patch execution: 'stopped' spawns (optionally) and waits for its Stop; anything else runs at once."""
    from gateway import session_finite
    started, executed = asyncio.Event(), []

    async def execute(authority, ref, row):
        _wire_turn_agent(authority, row['generation'], agent)
        executed.append(row['request_id'])
        if row['request_id'] == 'stopped':
            if spawn is not None:
                spawn()
            started.set()
            await async_wait_until(lambda: agent.interrupted, timeout=10, what='the Stop reached the turn')
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    return started, executed


@pytest.mark.asyncio
async def test_stop_interrupts_the_turn_and_kills_its_background_process_without_a_fence(
        tmp_path, monkeypatch, registry):
    db, authority = _authority(tmp_path, monkeypatch)
    agent = Agent()
    agent.session_id = 's'
    authority.runner._cached_agent_for = lambda route: agent
    children = []
    started, _executed = _running_turn(authority, monkeypatch, agent,
                                      spawn=lambda: children.append(_spawn(registry, tmp_path)))
    with db:
        await _submit(authority, 'stopped')
        await asyncio.wait_for(started.wait(), 20)
        await authority.interrupt(ACTOR, REF, authority._handle(REF).execution_generation)
        assert agent.interrupted
        await async_wait_until(lambda: children[0].exited, timeout=20, what='the background process was killed')
        assert children[0].completion_reason == 'killed'
        # Its completion never wakes the stopped chat.
        assert registry.is_completion_consumed(children[0].id)
        await asyncio.wait_for(authority.sessions['s'].task, 20)
        assert _durable_stop_state(authority.persistence) == (0, 0)


@pytest.mark.asyncio
async def test_a_failed_kill_is_logged_and_never_blocks_the_next_claim(tmp_path, monkeypatch, registry, caplog):
    db, authority = _authority(tmp_path, monkeypatch)
    agent = Agent()
    agent.session_id = 's'
    authority.runner._cached_agent_for = lambda route: agent

    def refuse(*_args, **_kwargs):
        raise OSError('signal refused')
    child = []
    started, executed = _running_turn(authority, monkeypatch, agent,
                                      spawn=lambda: child.append(_spawn(registry, tmp_path)))
    with db:
        await _submit(authority, 'stopped')
        await asyncio.wait_for(started.wait(), 20)
        with monkeypatch.context() as patched:
            patched.setattr(registry, 'kill_process', refuse)
            await authority.interrupt(ACTOR, REF, authority._handle(REF).execution_generation)  # never raises
            assert 'could not kill process' in caplog.text
            await asyncio.wait_for(authority.sessions['s'].task, 20)
        agent.interrupted = False
        await _submit(authority, 'successor')
        await asyncio.wait_for(authority.sessions['s'].task, 20)
        assert executed == ['stopped', 'successor']
        assert _durable_stop_state(authority.persistence) == (0, 0)
        assert not child[0].exited  # nothing pretended the refused kill happened


@pytest.mark.asyncio
async def test_restart_with_a_legacy_stop_row_and_fence_claims_followers_and_settles_unattended(
        tmp_path, monkeypatch):
    """Rows an older build left behind: a queued typed Stop and a pending process-Stop fence."""
    from gateway import session_finite
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = storage_handle(tmp_path)
    seed_session(store, 's', 'test')
    old = store.runtime.begin_epoch(instance_id='old')
    # An unattended automation turn in flight when the old owner died.
    store.admissions.admit(epoch=old, principal_id='automation:route', session_id='s', request_id='wake',
                           payload={'text': 'wake'})
    assert store.admissions.claim(epoch=old, session_id='s')

    def legacy(conn):
        payload = {'text': '', 'native_stop_v1': {'kind': 'stop', 'generation': 1, 'cutoff': 1, 'paused': False}}
        conn.execute("INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,"
                     "lineage_json,payload_json,payload_digest,intent,status,owner_epoch,generation) "
                     "VALUES(?,?,?,?,?,?,?,'queue','queued',?,1)",
                     (uuid.uuid4().hex, 'stop-1', 'messaging:legacy', 's', json.dumps(['s']), json.dumps(payload),
                      'legacy-digest', old))
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (FENCE_PREFIX + 's', f'{old}:1'))
    raw_holder(store)._execute_write(legacy)

    # Boot recovery, as initialize_session_authority runs it.
    epoch = store.runtime.begin_epoch(instance_id='current')
    store.admissions.recover(epoch=epoch)
    assert settle_recovered_unattended(store, epoch) == 1
    assert _durable_stop_state(store)[0] == 0

    executed = []

    async def execute(authority, ref, row):
        executed.append(row['request_id'])
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='current', epoch=epoch, persistence=store)
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='human'), 'route')
    with store:
        await _submit(authority, 'follower')
        await asyncio.wait_for(authority.sessions['s'].task, 20)
        assert executed == ['follower']  # the legacy Stop row never ran as model work, nor held the FIFO
        statuses = {r.request_id: (r.status, r.outcome)
                    for r in store.admissions.list(session_id='s', pending_only=False)}
        assert statuses == {'wake': ('terminal', 'interrupted'), 'stop-1': ('terminal', 'cancelled'),
                            'follower': ('terminal', 'completed')}
