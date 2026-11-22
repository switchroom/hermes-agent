"""Native messaging admission through the production inbound method; no gateway/model bootstrap.

The runner settings, agent and transport below are finite fixtures (shared by the native
settlement, redelivery and Kanban prompt tests). Admission and epoch recovery use Core.
"""
import asyncio
from datetime import UTC, datetime
from types import MethodType, SimpleNamespace
import sys
import unittest

import pytest

from persistence.core.errors import AuthorityRefusal


# Admission-row helpers over the storage interface: the old dict row shape, the authority's own handle.
def claim_session_input(authority, **kwargs):
    from gateway.session_authority import admission_row
    return admission_row(authority.persistence.admissions.claim(**kwargs))


def settle_session_input(authority, **kwargs):
    from gateway.session_authority import admission_row
    if '_transfers' in kwargs:
        kwargs['transfers'] = kwargs.pop('_transfers')
    return admission_row(authority.persistence.admissions.settle(**kwargs))


def list_session_admissions(authority, **kwargs):
    from gateway.session_authority import admission_row
    return [admission_row(record) for record in authority.persistence.admissions.list(**kwargs)]


def get_session_admission(authority, **kwargs):
    from gateway.session_authority import admission_row
    return admission_row(authority.persistence.admissions.get(**kwargs))


def begin_runtime_epoch(authority, *, instance_id):
    return authority.persistence.runtime.begin_epoch(instance_id=instance_id)


@pytest.fixture(autouse=True)
def no_bootstrap(monkeypatch):
    class DenyBootstrap:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in {'gateway.run', 'run_agent'}:
                raise AssertionError('native seam must not bootstrap ' + fullname)
    monkeypatch.setattr(sys, 'meta_path', [DenyBootstrap(), *sys.meta_path])
    violations = []
    def calls(frame, event, arg):
        if event != 'call':
            return
        module, name = frame.f_globals.get('__name__', ''), frame.f_code.co_name
        if (module == 'hermes_cli.auth' and name in {'_load_auth_store', '_load_global_auth_store',
                '_save_auth_store', '_auth_store_lock'}
                or module == 'agent.credential_pool' and name in {'load_pool', 'select', '_refresh_entry', '_persist'}
                or module == 'gateway.session_local_inventory' and name in {'snapshot_info', 'session_inventory'}
                or module.startswith(('openai.resources.', 'anthropic.resources.')) and name in {'create', 'stream'}):
            violations.append(module + '.' + name)
            raise AssertionError('forbidden native seam activation: ' + violations[-1])
    previous = sys.getprofile()
    sys.setprofile(calls)
    yield
    sys.setprofile(previous)
    assert not violations


class FiniteAgent:
    """Only an in-memory model endpoint; never executes tools or contacts a provider."""
    _supports_active_turn_redirect = True
    def __init__(self):
        self.effects, self.pending = [], set()
        self.failure = None
    def steer(self, text, *, ticket=None):
        if self.failure == 'refuse':
            return False
        self.effects.append(('steer', text, ticket))
        self.pending.add(ticket)
        if self.failure == 'unknown':
            raise RuntimeError('finite endpoint lost its acknowledgement after enqueue')
        return True
    def withdraw_steer(self, ticket):
        if ticket not in self.pending:
            return False
        self.pending.remove(ticket)
        return True
    def redirect(self, text):
        self.effects.append(('redirect', text))
        return True
    def interrupt(self, text=None):
        self.effects.append(('interrupt', text))


class NativeInboundRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertNotIn('gateway.run', sys.modules)
        self.assertNotIn('run_agent', sys.modules)
        import tempfile
        from pathlib import Path
        from gateway.config import GatewayConfig, Platform
        from gateway.session import SessionStore, SessionSource
        from gateway.session_authority import initialize_session_authority
        from gateway.run_busy import GatewayBusySessionMixin
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.addCleanup(reset_hermes_home_override, set_hermes_home_override(self.home))
        self.store = SessionStore(self.home / 'sessions', GatewayConfig())
        self.persistence = self.store._scoped_persistence()
        self.addCleanup(self.persistence.close)
        self.runner = SimpleNamespace(session_store=self.store, _session_db=self.persistence, adapters={},
            _draining=False, config=GatewayConfig(), _cached_agent_for=lambda key: None,
            _peek_session_state=lambda key: None, _primary_profile_name='default')
        self.allowed = {'fixture-author', 'other-author'}
        self.runner._adapter_for_source = lambda source: self.runner.adapters.get(source.platform)
        self.runner._adapter_flag = lambda *args: False
        self.runner._adapter_profile_for_source = lambda source: None
        self.runner._is_user_authorized_for_source = lambda source, **kwargs: source.user_id in self.allowed
        self.authority = await initialize_session_authority(self.runner, profile_id=str(self.home), instance_id='first')
        self.scheduled = []
        self.authority._schedule = lambda ref: self.scheduled.append(ref)
        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id='fixture-chat', user_id='fixture-author')
        self.route = self.store._generate_session_key(self.source)
        self.adapter = SimpleNamespace()
        self.runner.adapters[self.source.platform] = self.adapter
        self.runner._intake_adapter_for = lambda source: self.runner.adapters.get(source.platform)
        self.runner._delivery_adapter_for = lambda source: self.adapter
        self.mode = 'steer'
        self.runner._effective_busy_input_mode = lambda source: self.mode
        self.runner._effective_busy_text_mode = lambda source: 'interrupt'
        self.runner._agent_has_active_subagents = lambda agent: False
        async def no_compression(route):
            return False
        self.runner._session_has_compression_in_flight = no_compression
        self.runner._steer_text_with_origin = lambda text, event: text
        self.runner._steer_active_subagents = GatewayBusySessionMixin._steer_active_subagents
        self.runner._steer_running_agent = MethodType(GatewayBusySessionMixin._steer_running_agent, self.runner)
        self.runner._fold_into_running_turn = lambda *args: None
        self.acks = []
        async def ack(*args, **kwargs):
            self.acks.append(args[0].message_id)
        self.runner._busy_interrupt_and_ack = ack
        from gateway.run_inbound import GatewayInboundMixin
        # The authority path admits through the runner's own FIFO step (upstream ``_hm_admit_to_fifo``).
        self.runner._hm_admit_to_fifo = MethodType(GatewayInboundMixin._hm_admit_to_fifo, self.runner)
        async def no_consume(*args):
            return False, None
        self.runner._hm_post_admission_consume = no_consume
        self.runner._is_session_running = lambda route: True
        self.old_callbacks = []
        async def legacy_callback(event, *args):
            self.old_callbacks.append(event.message_id)
            return True
        self.runner._route_plaintext_approval_while_busy = legacy_callback
        self.runner._hm_busy_preempt = legacy_callback
        self.runner._plaintext_approval_words = lambda: {'yes': ('approve', '')}
        async def approval_confirmation(*args):
            return None  # finite transport presentation only
        self.runner._deliver_approval_confirmation = approval_confirmation

    def event(self, message_id, text='bounded instruction', *, source=None, control=False):
        from gateway.platforms.event import MessageEvent
        return MessageEvent(text=text, source=source or self.source, message_id=message_id,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC), allow_gateway_control=control)

    async def start(self, message_id='opening'):
        receipt = await self.authority.admit_native(self.event(message_id))
        row = claim_session_input(self.authority, epoch=self.authority.epoch, session_id=receipt.ref.session_id)
        self.ref, self.running = receipt.ref, row
        self.agent = FiniteAgent()
        self.authority.executing[self.ref.session_id] = row['generation']
        self.authority.adopt_agent(self.ref.session_id, row['generation'], self.agent)
        return row

    def settle(self, outcome='completed'):
        from gateway.session_queued_controls import steer_transfers
        with self.authority.sessions[self.ref.session_id].event_stream.lock:
            transfers = steer_transfers(self.authority, self.ref.session_id, self.running['generation'])
        return settle_session_input(self.authority, epoch=self.authority.epoch,
            admission_id=self.running['admission_id'], generation=self.running['generation'],
            outcome=outcome, _transfers=transfers)

    async def receive(self, event):
        from gateway.run_inbound import GatewayInboundMixin
        return await GatewayInboundMixin._hm_authority_admission(
            self.runner, event, event.source, self.store._generate_session_key(event.source), False)

    def row(self, message_id):
        return next(row for row in list_session_admissions(self.authority,
            session_id=self.ref.session_id, pending_only=False) if row['request_id'] == message_id)

    async def test_foreign_active_profile_cannot_reconcile_or_preempt_this_owner(self):
        from gateway.session_authorities import SessionAuthorities
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        await self.start()
        registry = SessionAuthorities(self.home, multiplexed=True)
        registry.add(self.home, self.authority)
        self.runner.session_authorities = registry
        other = self.home / 'unserved-profile'
        other.mkdir()
        token = set_hermes_home_override(other)
        try:
            with self.assertRaises(AuthorityRefusal) as mismatch:
                await self.receive(self.event('foreign-profile'))
        finally:
            reset_hermes_home_override(token)
        self.assertEqual(mismatch.exception.reason, 'profile_mismatch')
        self.assertEqual(self.agent.effects, [])

    async def test_busy_input_keeps_the_runner_busy_callback_for_every_adapter(self):
        self.assertEqual(await self.receive(self.event('legacy-busy')), (True, None))
        self.assertEqual(self.old_callbacks, ['legacy-busy'])
