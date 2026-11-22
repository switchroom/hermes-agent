"""Stop snapshots never absorb successors; interrupted in-flight spawns are reaped at the turn boundary."""
import asyncio
from dataclasses import replace
import shlex
import sys
import threading
import uuid

import pytest

from persistence.core.errors import AuthorityRefusal
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority, _submit, _wire_turn_agent
from tests.gateway.test_session_authority_preadopt_stop import Agent
from tests.waiting import wait_until


@pytest.fixture
def registry(monkeypatch):
    from tools import process_registry as module
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    yield registry
    for process in list(registry._running.values()):
        registry.kill_process(process.id)


def spawn(registry, tmp_path):
    ready = tmp_path / ('ready-' + uuid.uuid4().hex)
    code = f'import pathlib,threading; pathlib.Path({str(ready)!r}).touch(); threading.Event().wait(120)'
    # These tests exercise native FIFO boundaries, not interactive bash's
    # SIGTERM handling. Exec and readiness keep the owned fixture in Python.
    command = 'exec ' + shlex.join([sys.executable, '-c', code])
    process = registry.spawn_local(command, cwd=str(tmp_path), task_id='s', owner_task_id='s', session_key='route')
    wait_until(ready.exists, timeout=20, what='owned fixture ready')
    return process


@pytest.mark.asyncio
async def test_stop_reaps_a_spawn_that_crossed_initial_snapshot_before_successor(tmp_path, monkeypatch, registry):
    from gateway import session_finite
    db, authority = _authority(tmp_path, monkeypatch)
    agent = Agent()
    agent.session_id = 's'
    authority.runner._cached_agent_for = lambda route: agent
    started, allow_spawn = asyncio.Event(), asyncio.Event()
    children = []

    async def execute(authority, ref, row):
        _wire_turn_agent(authority, row['generation'], agent)
        if row['request_id'] == 'stopped':
            started.set()
            await asyncio.wait_for(allow_spawn.wait(), 10)
            children.append(spawn(registry, tmp_path))
        else:
            assert children[0].exited, 'the FIFO handed off while the interrupted spawn could still act'
            children.append(spawn(registry, tmp_path))
        return 'done'

    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        await _submit(authority, 'stopped')
        await asyncio.wait_for(started.wait(), 10)
        generation = authority._handle(REF).execution_generation
        await authority.interrupt(ACTOR, REF, generation)
        await _submit(authority, 'successor')
        allow_spawn.set()
        await asyncio.wait_for(authority.sessions['s'].task, 20)
        assert children[0].completion_reason == 'killed'
        assert not children[1].exited, 'the previous generation Stop reached its successor'
        with pytest.raises(AuthorityRefusal, match='stale_generation'):
            await authority.interrupt(ACTOR, REF, generation)
        assert not children[1].exited


@pytest.mark.asyncio
async def test_slow_stop_keeps_its_fixed_process_ids(tmp_path, monkeypatch, registry):
    db, authority = _authority(tmp_path, monkeypatch)
    old = spawn(registry, tmp_path)
    killing, finish_kill = threading.Event(), threading.Event()
    original = registry.kill_process

    def blocked(pid, **kwargs):
        if pid == old.id:
            killing.set()
            assert finish_kill.wait(10)
        return original(pid, **kwargs)

    monkeypatch.setattr(registry, 'kill_process', blocked)
    stop = asyncio.create_task(authority.interrupt(ACTOR, REF, authority._handle(REF).execution_generation))
    try:
        assert await asyncio.to_thread(killing.wait, 10)
        new = spawn(registry, tmp_path)
        finish_kill.set()
        await asyncio.wait_for(stop, 20)
        assert old.exited and not new.exited
    finally:
        finish_kill.set()
        await stop
        db.close()


@pytest.mark.asyncio
async def test_unauthorized_or_stale_stop_does_not_signal_processes(tmp_path, monkeypatch, registry):
    db, authority = _authority(tmp_path, monkeypatch)
    process = spawn(registry, tmp_path)
    for actor, generation, reason in [
        (replace(ACTOR, profile_id='foreign'), 0, 'profile_mismatch'),
        (replace(ACTOR, capabilities=frozenset()), 0, 'permission_denied'),
        (ACTOR, -1, 'stale_generation'),
    ]:
        with pytest.raises(AuthorityRefusal, match=reason):
            await authority.interrupt(actor, REF, generation)
        assert not process.exited
    db.close()
