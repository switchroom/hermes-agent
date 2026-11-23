"""A launch flag the gateway's ``session.create`` contract refuses is named as that flag.

``hermes chat --source uat`` printed "Hermes couldn't start the model connection: invalid_params ...
Run `hermes doctor`": the canonical dispatcher's refusal carries the refused key paths as a
``fields`` list (``gateway/session_controls.py``), which the copy dropped, and a bad option is not a
model-connection failure.
"""

from hermes_cli.cli_chat_error_copy import agent_init_failure_message, refusal_text
from hermes_cli.gateway_client import GatewayRPCError, rpc_error


def _contract_refusal(*fields):
    # The exact frame the canonical dispatcher sends, decoded the way the client decodes it.
    return rpc_error({'code': 4001, 'message': 'invalid_params',
                      'data': {'reason': 'invalid_params', 'fields': list(fields)}})


def test_refusal_text_names_the_fields_list():
    assert refusal_text(_contract_refusal('source')) == 'invalid_params (source)'
    assert refusal_text(_contract_refusal('source', 'max_turns')) == 'invalid_params (source, max_turns)'


def test_refusal_text_keeps_the_single_field_shape():
    refused = GatewayRPCError('invalid_params', data={'field': 'toolsets', 'detail': 'bad'})
    assert refusal_text(refused) == 'invalid_params (toolsets: bad)'
    assert refusal_text(GatewayRPCError('invalid_params', data={'reason': 'invalid_params'})) == 'invalid_params'


def test_a_refused_source_flag_reads_as_a_bad_option_not_a_model_connection_failure():
    message = agent_init_failure_message(_contract_refusal('source'))
    assert '--source' in message, message
    assert "couldn't start the model connection" not in message and 'hermes doctor' not in message, message
    # The accepted labels come from the create contract, so the user can fix the flag without a search.
    for label in ('cli', 'tool', 'oneshot'):
        assert label in message, message


def test_a_refused_flag_without_a_closed_value_set_still_names_the_flag():
    message = agent_init_failure_message(_contract_refusal('max_turns'))
    assert '--max-turns' in message and 'hermes doctor' not in message, message


def test_a_refused_key_that_is_no_launch_flag_keeps_the_connection_copy_with_the_key():
    # ``request_id`` is the client's own retry token, never something the user typed.
    message = agent_init_failure_message(_contract_refusal('request_id'))
    assert "couldn't start the model connection" in message and 'request_id' in message, message


def test_a_handler_refusal_of_a_field_keeps_its_copy():
    # Handler checks (``field`` + ``detail``) may come from config.yaml, not a typed flag: unchanged.
    refused = GatewayRPCError('invalid_params', 4001, {
        'reason': 'invalid_params', 'field': 'toolsets', 'detail': "unknown or invalid toolset name: 'gone'"})
    message = agent_init_failure_message(refused)
    assert "couldn't start the model connection" in message and 'toolsets' in message, message
