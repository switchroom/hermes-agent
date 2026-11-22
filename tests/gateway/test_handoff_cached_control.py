"""Cached native controls revalidate committed process handoff before signalling."""
import pytest
from tests.gateway.test_process_handoff_stop import Parent, _child, _spawn

@pytest.mark.parametrize('control', ['old-child-cleanup', 'new-parent-cleanup', 'birth-uncertain', 'pre-stopped-child'])
def test_stale_native_control_cannot_use_pre_handoff_owner(tmp_path, monkeypatch, control):
    from tools import process_registry as module
    from tools.delegate_tool import _unregister_subagent
    from tools.process_registry_handoff import handoff_process
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    parent = Parent()
    sid, _child_agent = _child(parent)
    release = tmp_path / 'finish'
    process = _spawn(registry, tmp_path, sid, 'native-child-session', 'route', logged=True, release=release)
    stale = module.ProcessRegistry()
    stale.recover_from_checkpoint()
    cached = stale._running[process.id]
    try:
        assert cached.parent_session_id == 'native-child-session'
        if control == 'pre-stopped-child':
            _child_agent._hard_interrupt_requested.set()
            assert 'error' in handoff_process(process.id, {'data': 'parent owns stopped job'}, sid)
            assert process.parent_session_id == 'native-child-session' and process.ownership_generation == 0
            assert process.owner_task_id == sid
            assert registry.kill_all(sid, source='session.interrupt') == 1
            return
        assert handoff_process(process.id, {'data':'parent owns work'}, sid)['status'] == 'handed_off'
        assert process.parent_session_id == 's'
        monkeypatch.setattr(module, 'process_registry', stale)
        if control == 'birth-uncertain':
            from tools.process_registry_handoff import ownership_identity
            assert stale.refresh_ownership()
            expected = (*ownership_identity(cached)[:-2], None, None)
            result = stale.kill_process(process.id, source='session.interrupt', expected_ownership=expected)
            assert result['status'] == 'error' and result['error'] == 'process_stop_unknown'
            assert not cached.exited and not stale.is_completion_consumed(process.id)
        elif control in {'old-child-cleanup', 'new-parent-cleanup'}:
            owner = sid if control == 'old-child-cleanup' else 'parent-turn'
            result = stale.kill_all(owner, source='session.interrupt')
            assert result == int(control == 'new-parent-cleanup'), 'cleanup used stale native ownership'
    finally:
        _unregister_subagent(sid)
        release.touch()
        registry.kill_process(process.id)
