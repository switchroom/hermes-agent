"""``messages.read_snapshot`` on SQLite: every read inside sees one generation on a WAL store; a
DELETE-journal store (reads go through the writer connection under the write lock) is not pinned, so a
write from the same thread cannot wait on its own lock; there ``consistent()`` turns false once any write
lands, this process's or another connection's."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import sqlite3
import threading

import hermes_yaml as yaml
import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import seed_messages, seed_session


def _count(store):
    return raw_holder(store)._read_one("SELECT COUNT(*) FROM messages WHERE session_id = 's1'")[0]


def _append(store, role, content):
    seed_messages(store, "s1", {"role": role, "content": content})


@pytest.mark.requires_wal
@pytest.mark.parametrize('pinned_only', [False, True])
def test_reads_inside_a_snapshot_see_one_generation(tmp_path, pinned_only):
    store = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(store, "s1", "cli")
        assert raw_holder(store)._wal_active
        _append(store, "user", "hello")
        with store.messages.read_snapshot(pinned_only=pinned_only) as consistent:
            assert _count(store) == 1
            _append(store, "assistant", "committed meanwhile")
            assert _count(store) == 1  # the snapshot's generation
            with store.messages.read_snapshot() as inner:  # re-entrant: the same transaction
                assert _count(store) == 1 and inner()
            assert consistent()
        assert _count(store) == 2
    finally:
        store.close()


@pytest.mark.parametrize('journal_mode', ['delete', pytest.param('wal', marks=pytest.mark.requires_wal)])
def test_optional_pinned_snapshot_never_waits_for_the_writer(tmp_path, monkeypatch, journal_mode):
    from persistence.sqlite._state.readpool import _READ_POOL_MAX

    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'database': {'journal_mode': journal_mode}}),
                                         encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with open_persistence(profile_scope(home=tmp_path)) as store:
        holder = raw_holder(store)
        assert holder._wal_active == (journal_mode == 'wal')

        def try_pin():
            with store.messages.read_snapshot(pinned_only=True) as consistent:
                return consistent.pinned, consistent()

        with ExitStack() as readers, ThreadPoolExecutor(max_workers=1) as workers:
            if journal_mode == 'wal':
                # Borrow the real pool's full capacity without pinning a connection on the worker.
                for _ in range(_READ_POOL_MAX):
                    assert readers.enter_context(holder._read_ctx(pooled_only=True)) is not None
            with holder._lock:
                assert workers.submit(try_pin).result(timeout=3) == (False, False)


def test_a_delete_journal_store_is_not_pinned_and_any_write_makes_it_inconsistent(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"database": {"journal_mode": "delete"}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = open_persistence(profile_scope(home=home))
    try:
        seed_session(store, "s1", "cli")
        assert raw_holder(store)._wal_active is False
        done = []

        # Another connection on the same file (one holder serves every handle of a path in this process).
        other = sqlite3.connect(str(home / "state.db"), timeout=10, check_same_thread=False)

        def read_and_write():
            with store.messages.read_snapshot() as consistent:
                done.append((consistent.pinned, consistent()))
                _append(store, "user", "hello")
                done.append((_count(store), consistent()))
            with store.messages.read_snapshot() as consistent:
                assert consistent()
                with other:
                    other.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'assistant', ?, 0)",
                                  ("from another connection",))
                done.append((_count(store), consistent()))
        worker = threading.Thread(target=read_and_write, daemon=True)
        worker.start()
        worker.join(10)
        assert done == [(False, True), (1, False), (2, False)]
        other.close()
    finally:
        store.close()
