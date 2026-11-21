"""A foreground terminal command is never the chat's tracked work (HF-799).

Real local spawns through the native job path the terminal tool uses. A foreground command is a
registry job while it runs and, when it starts a daemon, after its shell returns too. Counting it
flipped the managed check's fingerprint on every command and kept the check alive (30 minute
liveness wakes) after the chat's real work had ended.
"""
import contextvars
import json
import shlex
import sys
import threading

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli.active_work_progress import (
    ACTIVE_WORK_INTERVAL_SECONDS,
    ACTIVE_WORK_PROMPT,
    _scan_background_sessions,
    collect_active_work,
    reconcile_active_work_result,
)
from hermes_cli.heartbeat import ACTIVE_WORK_HEARTBEAT_ORIGIN, HeartbeatManager, load_heartbeat
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.waiting import wait_until
from tools import process_registry as module
from tools.environments.local import LocalEnvironment
from tools.interrupt import consume_yield, request_yield
from tools.process_registry_job import job_quiescent
from tools.terminal_tool_background import yield_to_background_handler

pytestmark = pytest.mark.platforms('linux')

CHAT = 'chat-session'


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from hermes_cli import goals

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    token = set_hermes_home_override(tmp_path)
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    goals._DB_CACHE.clear()
    session_vars = set_session_vars(session_id=CHAT, session_key='route')
    try:
        yield registry
    finally:
        clear_session_vars(session_vars)
        for session in list(registry._running.values()):
            registry.kill_process(session.id, source='session.interrupt')
        goals._DB_CACHE.clear()
        reset_hermes_home_override(token)


def _run(command, root, **kwargs):
    handler = yield_to_background_handler(command=command, env_type='local', cwd=str(root),
                                          effective_task_id='task', task_id='task', session_key='route')
    return LocalEnvironment(cwd=str(root)), handler


def _in_chat_thread(target):
    """A tool thread inherits the turn's context, as the agent's executor does."""
    context = contextvars.copy_context()
    return threading.Thread(target=lambda: context.run(target))


def _install_check(fingerprint='fp-before'):
    HeartbeatManager(CHAT).ensure_managed(
        ACTIVE_WORK_PROMPT, ACTIVE_WORK_INTERVAL_SECONDS, origin=ACTIVE_WORK_HEARTBEAT_ORIGIN,
        work_fingerprint=fingerprint)


def test_a_returned_foreground_command_whose_daemon_lives_on_clears_the_check(tmp_path, registry):
    # The live case: a probe started `op daemon`, the shell returned, the daemon held the job scope.
    command = 'sleep 30 & echo started'
    env, handler = _run(command, tmp_path)
    try:
        result = env.execute(command, timeout=15, yield_handler=handler)
        assert result['returncode'] == 0 and 'started' in result['output']
        job = next(s for s in registry._running.values() if s.command == command)
        assert job.parent_session_id == CHAT and not job_quiescent(job)  # still a running registry job

        assert _scan_background_sessions(registry).ids == set()
        _install_check()
        active, complete = collect_active_work(delegation_ids=set())
        result = reconcile_active_work_result(active, scans_complete=complete, fingerprints=active)
        assert result.cleared == [CHAT]
        assert load_heartbeat(CHAT) is None
        checkpoint = json.loads((tmp_path / 'processes.json').read_text())
        assert next(e for e in checkpoint if e['session_id'] == job.id)['inline_foreground'] is True
    finally:
        env.cleanup()


def test_a_running_foreground_command_does_not_move_the_fingerprint(tmp_path, registry):
    gate = tmp_path / 'gate'
    command = shlex.join([sys.executable, '-c', (
        'import time\nfrom pathlib import Path\nprint("running", flush=True)\n'
        f'end=time.monotonic()+12\nwhile not Path({str(gate)!r}).exists() and time.monotonic()<end: time.sleep(.01)\n')])
    env, handler = _run(command, tmp_path)
    result = {}
    worker = _in_chat_thread(lambda: result.update(env.execute(command, timeout=15, yield_handler=handler)))
    worker.start()
    try:
        wait_until(lambda: any(s.command == command for s in registry._running.values()), timeout=5)
        job = next(s for s in registry._running.values() if s.command == command)
        assert job.parent_session_id == CHAT and not job.exited
        assert _scan_background_sessions(registry).ids == set()
    finally:
        gate.touch()
        worker.join(15)
        env.cleanup()
    assert result['returncode'] == 0


def test_a_yielded_foreground_command_is_tracked_work_from_the_yield(tmp_path, registry):
    gate = tmp_path / 'gate'
    command = shlex.join([sys.executable, '-c', (
        'import time\nfrom pathlib import Path\nprint("prefix", flush=True)\n'
        f'end=time.monotonic()+12\nwhile not Path({str(gate)!r}).exists() and time.monotonic()<end: time.sleep(.01)\n')])
    env, handler = _run(command, tmp_path)
    result = {}
    worker = _in_chat_thread(lambda: result.update(env.execute(command, timeout=15, yield_handler=handler)))
    worker.start()
    try:
        wait_until(lambda: any(s.command == command for s in registry._running.values()), timeout=5)
        before = _scan_background_sessions(registry)
        assert next(s for s in registry._running.values() if s.command == command).parent_session_id == CHAT
        request_yield(worker.ident)
        worker.join(10)
        assert not worker.is_alive() and result.get('yielded_session_id')
        job = registry._running[result['yielded_session_id']]
        assert job.inline_foreground is False and job.notify_on_complete
        after = _scan_background_sessions(registry)
        assert CHAT not in before.ids and after.ids == {CHAT}
        checkpoint = json.loads((tmp_path / 'processes.json').read_text())
        assert next(e for e in checkpoint if e['session_id'] == job.id)['inline_foreground'] is False
    finally:
        gate.touch()
        consume_yield(worker.ident)
        worker.join(15)
        env.cleanup()


def test_background_spawn_is_still_tracked_and_its_exit_wakes_once(tmp_path, registry):
    gate = tmp_path / 'gate'
    command = shlex.join([sys.executable, '-c', (
        'import time\nfrom pathlib import Path\n'
        f'end=time.monotonic()+12\nwhile not Path({str(gate)!r}).exists() and time.monotonic()<end: time.sleep(.01)\n')])
    job = registry.spawn_local(command, cwd=str(tmp_path), task_id='task', session_key='route')
    try:
        running, _ = collect_active_work(delegation_ids=set())
        assert set(running) == {CHAT}
        reconcile_active_work_result(running, fingerprints=running)  # installed: the start is already known
        created = load_heartbeat(CHAT).created_at
        assert HeartbeatManager(CHAT).due_prompt(now=created + ACTIVE_WORK_INTERVAL_SECONDS) is None

        with registry._lock:
            job.exited = True  # the scan sees the exit before the reader retires the session
        exited, _ = collect_active_work(delegation_ids=set())
        reconcile_active_work_result(exited, fingerprints=exited)
        assert HeartbeatManager(CHAT).due_prompt(now=created + ACTIVE_WORK_INTERVAL_SECONDS) is not None
        assert HeartbeatManager(CHAT).due_prompt(now=created + 2 * ACTIVE_WORK_INTERVAL_SECONDS) is None
    finally:
        gate.touch()
