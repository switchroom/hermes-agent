"""A planned stop whose drain times out interrupts an interactive turn. The old owner settles that
turn's canonical admission ``interrupted`` (terminal) and marks the session ``resume_pending``
(``shutdown_timeout``). Nothing is left queued or unknown, so canonical recovery has nothing to
run; startup auto-resume must post the interrupted-turn note in that chat instead of skipping it
because the session merely has admission history. Only open work (queued, started or unknown) is
owned by the canonical FIFO, and a deliberate skip says why in the log."""
import asyncio
import logging
from contextlib import closing
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionEntry
from gateway.session_authority import SessionAuthority
from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, profile_scope
from tests.gateway.fixtures.storage_handles import storage_handle
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _authority(tmp_path, runner, *, settle_outcome=None, leave_queued=False):
    """The previous owner admitted and started one turn; ``settle_outcome`` is how its drain ended it."""
    db = open_persistence(profile_scope(home=tmp_path))
    epoch = db.runtime.begin_epoch(instance_id='stopped')
    db.sessions.create(NewSession(session_id='sid', source='telegram'))  # what the route's register reserves
    row, _ = db.admissions.admit(epoch=epoch, principal_id='human', session_id='sid',
                                 request_id='turn', payload={'text': 'force yourself and then continue'})
    started = db.admissions.claim(epoch=epoch, session_id='sid')
    if settle_outcome is not None:
        db.admissions.settle(epoch=epoch, admission_id=row.admission_id,
                             generation=started.generation, outcome=settle_outcome)
    if leave_queued:
        db.admissions.admit(epoch=epoch, principal_id='human', session_id='sid',
                            request_id='next', payload={'text': 'sent during the drain'})
    epoch = db.runtime.begin_epoch(instance_id='restarted')
    db.admissions.recover(epoch=epoch)
    return db, SessionAuthority(runner, profile_id='profile', instance_id='restarted', epoch=epoch,
                                persistence=storage_handle(tmp_path))


def _drain_interrupted_entry():
    return SessionEntry(session_key='agent:main:telegram:dm:c', session_id='sid', created_at=datetime.now(),
                        updated_at=datetime.now(), origin=make_restart_source(chat_id='c'),
                        platform=Platform.TELEGRAM, chat_type='dm', resume_pending=True,
                        resume_reason='shutdown_timeout', last_resume_marked_at=datetime.now())


def _runner():
    runner, _adapter = make_restart_runner()
    runner._persist_active_agents = MagicMock()
    runner._run_startup_resume_event = AsyncMock()
    entry = _drain_interrupted_entry()
    runner.session_store._entries = {entry.session_key: entry}
    return runner, entry


@pytest.mark.asyncio
async def test_a_drain_interrupted_native_turn_is_resumed_at_startup(tmp_path):
    runner, entry = _runner()
    db, authority = _authority(tmp_path, runner, settle_outcome='interrupted')
    with closing(db), closing(authority.persistence):
        runner.session_authorities = [authority]
        assert runner._schedule_resume_pending_sessions() == 1
        await asyncio.sleep(0)
    runner._run_startup_resume_event.assert_awaited_once()
    _adapter, event, key = runner._run_startup_resume_event.await_args.args
    assert key == entry.session_key and event.internal and event.text == ''
    assert event.source.chat_id == 'c'


@pytest.mark.asyncio
@pytest.mark.parametrize('settle_outcome,leave_queued', [(None, False), ('interrupted', True)],
                         ids=['started-turn-recovers-unknown', 'queued-input-behind-it'])
async def test_open_canonical_work_still_owns_the_restart_and_the_skip_is_logged(
        tmp_path, caplog, settle_outcome, leave_queued):
    runner, _entry = _runner()
    db, authority = _authority(tmp_path, runner, settle_outcome=settle_outcome, leave_queued=leave_queued)
    with closing(db), closing(authority.persistence), caplog.at_level(logging.INFO, logger='gateway.run'):
        runner.session_authorities = [authority]
        assert runner._schedule_resume_pending_sessions() == 0
        await asyncio.sleep(0)
    runner._run_startup_resume_event.assert_not_called()
    assert any('Skipping auto-resume for agent:main:telegram:dm:c' in r.getMessage()
               and 'canonical admission' in r.getMessage() for r in caplog.records)
