"""A heartbeat row saved by the retired house active-work check never fires after upgrade (HF-803).

Older fleet images saved managed checks into the same ``heartbeat:<session>`` rows as /heartbeat, marked
with ``origin: "active-work"``. Without the origin field those rows would read as user heartbeats and wake
the chat every 180s forever. A house-written user row (``origin: "user"``) keeps working unchanged.
"""

import json
import time

from hermes_cli.heartbeat import (
    _META_PREFIX,
    HeartbeatManager,
    _get_session_db,
    load_heartbeat,
    store_has_active_heartbeat,
)

_MANAGED_PROMPT = "Check active tracked work that originated in this chat: delegated agents and background jobs."


def _db():
    db = _get_session_db()
    assert db is not None
    return db


def _saved_row(session_id: str, **fields) -> None:
    row = {"prompt": "watch CI", "interval_seconds": 180, "status": "active",
           "created_at": time.time() - 3600, "last_fired_at": 0.0, "fire_count": 0, **fields}
    _db().state_meta.set(_META_PREFIX + session_id, json.dumps(row))


def test_a_saved_managed_check_is_ignored_by_every_reader():
    _saved_row("hb-retired-sid", prompt=_MANAGED_PROMPT, origin="active-work",
               work_fingerprint="fp-2", woken_fingerprint="fp-1")
    assert load_heartbeat("hb-retired-sid") is None
    assert store_has_active_heartbeat(_db()) is False
    mgr = HeartbeatManager(session_id="hb-retired-sid")
    assert not mgr.has_heartbeat()
    assert mgr.due_prompt(now=time.time() + 86400) is None


def test_a_user_heartbeat_saved_with_the_house_origin_field_still_fires():
    _saved_row("hb-house-user-sid", origin="user", work_fingerprint="", woken_fingerprint="")
    mgr = HeartbeatManager(session_id="hb-house-user-sid")
    assert mgr.is_active()
    assert store_has_active_heartbeat(_db()) is True
    prompt = mgr.due_prompt()
    assert prompt is not None and "watch CI" in prompt


def test_a_user_heartbeat_set_over_a_retired_row_replaces_it():
    _saved_row("hb-replace-sid", prompt=_MANAGED_PROMPT, origin="active-work")
    mgr = HeartbeatManager(session_id="hb-replace-sid")
    mgr.set("my own check", 600)
    again = HeartbeatManager(session_id="hb-replace-sid")
    assert again.is_active()
    assert again.state is not None and again.state.prompt == "my own check"
    assert "origin" not in json.loads(_db().state_meta.get(_META_PREFIX + "hb-replace-sid"))
