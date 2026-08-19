"""Shared fake of the fastmcp Context, for every test that drives a paying tool.

Money tools (transfer, transfer_requisites, pay_bill, ticket_pay, grocery_checkout)
confirm ONLY through the elicitation button: a client without the capability is
refused before any journal write or HTTP. So a test that wants the payment body to
run must hand the tool a ctx whose button says «accept» — that is what
`accept_ctx()` is for. `FakeCtx` itself covers every outcome the server maps:
decline / cancel / an McpError (timeout) / no capability at all.

Quacks like fastmcp Context for exactly what the server touches:
`.request_context.session.check_client_capability(...)` (sync) and async
`.elicit(message=, schema=)`. `pick` selects options[pick] from a choice schema's
enum, so picker tests do not depend on the exact label text the tool builds.
"""
from types import SimpleNamespace

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData


class FakeCtx:
    def __init__(self, action="accept", data=None, capable=True, exc=None, pick=None):
        self.request_context = SimpleNamespace(
            session=SimpleNamespace(check_client_capability=lambda cap: capable))
        self._action, self._data, self._exc, self._pick = action, data or {}, exc, pick
        self.asked = []                        # (message, schema) pairs, in order

    async def elicit(self, message, schema):
        self.asked.append((message, schema))
        if self._exc:
            raise self._exc
        if self._action != "accept":
            return SimpleNamespace(action=self._action)
        data = dict(self._data)
        if self._pick is not None:
            props = schema.model_json_schema().get("properties", {})
            for fname, spec in props.items():
                if "enum" in spec:
                    data[fname] = spec["enum"][self._pick]
        return SimpleNamespace(action="accept", data=schema(**data))


def accept_ctx(**kw) -> FakeCtx:
    """A client whose human presses Accept on every button (and, with pick=,
    chooses that option in every picker). The default for tests that only care
    about what happens AFTER the confirmation."""
    return FakeCtx(action="accept", **kw)


def decline_ctx(**kw) -> FakeCtx:
    return FakeCtx(action="decline", **kw)


def cancel_ctx(**kw) -> FakeCtx:
    return FakeCtx(action="cancel", **kw)


def incapable_ctx() -> FakeCtx:
    """A ctx whose client never declared elicitation — must be refused for money."""
    return FakeCtx(capable=False)


def mcp_error(message="elicitation timed out") -> McpError:
    return McpError(ErrorData(code=-32603, message=message))
