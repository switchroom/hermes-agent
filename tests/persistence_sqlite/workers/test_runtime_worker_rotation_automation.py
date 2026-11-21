"""Worker rotation carries automation in the publication transaction."""
import json

from tests.persistence_sqlite.workers._runtime_worker import worker as worker  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import


def test_publication_moves_automation_once_and_preserves_cleared_controls(worker):
    persistence, store = worker
    meta = persistence.state_meta
    values = {'goal': {'goal': 'retain objective', 'status': 'paused'},
              'loop': {'prompt': 'retain loop', 'status': 'active', 'interval_seconds': 60},
              'heartbeat': {'status': 'active', 'interval_seconds': 120}}
    for family, value in values.items():
        meta.set(f'{family}:owned', json.dumps(value))
    assert store.try_acquire_compression_lock('owned', 'compressor')
    store.publish_compression_child(parent_session_id='owned', child_session_id='child', source='cli',
        messages=[{'role': 'assistant', 'content': 'summary'}], compression_lock_holder='compressor')
    for family, value in values.items():
        raw = meta.get(f'{family}:child')
        assert raw is not None, family
        assert json.loads(raw)['status'] == value['status']
        assert json.loads(meta.get(f'{family}:owned'))['status'] == 'cleared'
    assert json.loads(meta.get('goal:child'))['goal'] == values['goal']['goal']
    assert json.loads(meta.get('loop:child'))['prompt'] == values['loop']['prompt']


def test_rotation_never_turns_a_retired_managed_check_into_a_user_heartbeat(worker):
    # HF-803: a row saved by the retired house active-work check must not reach the child as active.
    persistence, store = worker
    meta = persistence.state_meta
    meta.set('heartbeat:owned', json.dumps({
        'prompt': 'check active work', 'interval_seconds': 180, 'status': 'active', 'origin': 'active-work',
        'work_fingerprint': 'fp-2', 'woken_fingerprint': 'fp-1', 'created_at': 5.0}))
    assert store.try_acquire_compression_lock('owned', 'compressor')
    store.publish_compression_child(parent_session_id='owned', child_session_id='child', source='cli',
        messages=[{'role': 'assistant', 'content': 'summary'}], compression_lock_holder='compressor')
    child = meta.get('heartbeat:child')
    assert child is None or json.loads(child)['status'] == 'cleared'
