"""Plain-language copy for chat-turn failures shown in the CLI response panel.

The chat panel used to echo ``Error: HTTP 401: Invalid API key`` as the assistant's answer. These
helpers map the classifier verdict (``agent/error_classifier.py``) to WHAT happened + WHAT TO DO,
and demote the raw provider text to a ``Details:`` line. Copy lives in the i18n catalog under
``cli.error.*`` and is resolved at render time for the active language.
"""

from __future__ import annotations

from agent.i18n import t

_SUMMARY_LIMIT = 120

# FailoverReason.value -> catalog key suffix. Reasons sharing copy point at one key.
_REASON_KEYS: dict[str, str] = {
    "auth": "auth",
    "auth_permanent": "auth",
    "billing": "billing",
    "model_not_found": "model_not_found",
    "rate_limit": "rate_limit",
    "upstream_rate_limit": "rate_limit",
    "upstream_blocked": "upstream_blocked",
    "overloaded": "overloaded",
    "server_error": "server_error",
    "timeout": "timeout",
}
_UNKNOWN_KEY = "unknown"


def _short(text: str, limit: int | None = _SUMMARY_LIMIT) -> str:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return first if limit is None or len(first) <= limit else first[: limit - 1].rstrip() + "…"


def refusal_text(error: BaseException | str) -> str:
    """The refusal as the client shows it: the reason code, plus the check that fired when the owner
    named one (``invalid_params (toolsets: unknown or invalid toolset name: 'mcp-codegraph')``).

    The gateway sends ``field``/``detail`` in the JSON-RPC error's ``data`` for a handler's check, or
    ``fields`` (the refused key paths) when the method's wire contract rejects the request
    (``gateway/session_controls.py``); ``hermes_cli.gateway_client.rpc_error`` carries the bounded
    scalars onto ``GatewayRPCError.data``. A refusal from anywhere else has no ``data`` and reads
    exactly as before."""
    if str(error) == "launch_credentials_unavailable":
        # The chat's launch key changed: every retry repeats the refusal, so name the way out.
        from agent.turn_failure_copy import launch_credentials_copy

        return launch_credentials_copy(error)
    data = getattr(error, "data", None)
    data = data if isinstance(data, dict) else {}
    field, detail = str(data.get("field") or "").strip(), str(data.get("detail") or "").strip()
    field = field or ", ".join(_refused_fields(error))
    if not field and not detail:
        return str(error)
    return f"{error} ({field or 'unnamed field'}{': ' + detail if detail else ''})"


def _refused_fields(error: BaseException | str) -> list[str]:
    """The key paths a wire contract refused (``data.fields``), deduplicated in order."""
    data = getattr(error, "data", None)
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in fields if str(item or "").strip()))


def _accepted_values(key: str) -> tuple[str, ...]:
    """The closed value set ``session.create`` declares for ``key`` (``source``), else none."""
    from typing import Literal, get_args, get_origin

    from tui_gateway.contracts.canonical_sessions import CanonicalCreateParams

    field = CanonicalCreateParams.model_fields.get(key)
    annotation = field.annotation if field is not None else None
    return tuple(str(value) for value in get_args(annotation)) if get_origin(annotation) is Literal else ()


def launch_option_refusal(error: BaseException) -> str | None:
    """Copy for a ``session.create`` contract refusal of a flag the user typed (``--source uat``):
    a bad option, not a model connection the user should debug with ``hermes doctor``. ``None``
    for anything else, including handler checks (``field``), which may come from config.yaml."""
    from hermes_cli.gateway_chat import _POLICY, _RESUME_FLAG_HINTS
    from hermes_cli.gateway_client import GatewayRPCError

    if not isinstance(error, GatewayRPCError) or str(error) != "invalid_params":
        return None
    launch_keys = {*_POLICY, "source"}
    keys = [key for key in dict.fromkeys(path.split(".")[0] for path in _refused_fields(error)) if key in launch_keys]
    if not keys:
        return None
    flags = [_RESUME_FLAG_HINTS.get(key, ("--" + key.replace("_", "-"), None))[0] for key in keys]
    accepted = "; ".join(f"{flag} accepts {', '.join(values)}"
                         for flag, key in zip(flags, keys) if (values := _accepted_values(key)))
    return (f"Hermes refused {', '.join(flags)}: the gateway does not accept that value ({refusal_text(error)})."
            f"{' ' + accepted + '.' if accepted else ''} Your message was not sent; fix the option and run "
            f"the command again (`hermes chat --help`).")


def chat_error_response(
    error: Exception | str, *, provider: str = "", model: str = "", failure_reason: str | None = None,
) -> str:
    """Two-line panel text: plain sentence with the fix command, then ``Details: <raw>``.

    ``failure_reason`` is the verdict the turn loop already stamped on its result
    (``agent/turn_failure_copy.stamp_failure``). When present it is used as-is instead of
    re-classifying a summarised string (which has no status code and almost always lands on
    'unknown'); for the loop's own site codes the ``error`` text is already the user-facing copy,
    so it is returned verbatim rather than wrapped and demoted to a Details line."""
    reason = str(failure_reason or "").strip()
    if reason:
        from agent.turn_failure_copy import SITE_FAILURE_CODES

        text = str(error or "").strip()
        if reason in SITE_FAILURE_CODES and text:
            return text
    else:
        from agent.error_classifier import classify_api_error

        exc = error if isinstance(error, Exception) else Exception(str(error))
        reason = classify_api_error(exc, provider=provider or "", model=model or "").reason.value
    lead = t(
        f"cli.error.{_REASON_KEYS.get(reason, _UNKNOWN_KEY)}",
        provider=provider or t("cli.error.the_provider"),
        model=model or t("cli.error.the_current_model"),
    )
    return t("cli.error.with_details", lead=lead, details=_short(refusal_text(error), 300))


def agent_init_failure_message(error: BaseException) -> str:
    """Copy for a failed AIAgent build on first message: the user's turn was dropped.

    A gateway that cannot be reached keeps its whole first line: its discovery detail is already
    bounded (``DiscoveryError.bounded_detail``) and ends in the repair command, which the summary
    limit would cut mid-path. A refused launch flag names the flag instead (``launch_option_refusal``)."""
    from hermes_cli.gateway_client import GatewayUnavailableError

    if (bad_option := launch_option_refusal(error)) is not None:
        return bad_option
    limit = None if isinstance(error, GatewayUnavailableError) else _SUMMARY_LIMIT
    return t("cli.error.agent_init_failed", error=_short(refusal_text(error), limit) or type(error).__name__)
