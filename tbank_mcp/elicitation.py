"""MCP 2026-07-28 form elicitation for the existing FastMCP 1.x tools.

OpenAI registered connectors use request-scoped capabilities and MRTR, not
server-initiated requests. Keep a paused coroutine so bank preparation, selected
accounts and quoted totals are not repeated when the host supplies inputResponses.
Only an opaque, one-use handle crosses the wire. State is process-local and bound
to the original connection, tool and arguments; restart/expiry fails closed.

Protocol: modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
OpenAI: github.com/openai/mcp-extensions/blob/main/docs/spec.md#openai-form-elicitation
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import secrets
from types import SimpleNamespace

from mcp import types
from mcp.shared.exceptions import McpError
from pydantic import BaseModel

VERSION = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
OPENAI = "openai/elicitation"
CURRENT: ContextVar[Conversation | None] = ContextVar("tbank_mrtr", default=None)


def _object(value):
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    return value if isinstance(value, dict) else {}


def modern_meta(params):
    meta = _object(params.meta)
    version = meta.get(VERSION)
    try:
        modern = isinstance(version, str) and date.fromisoformat(version) >= date(2026, 7, 28)
    except ValueError:
        modern = False
    return meta if modern else None


def form_capabilities(capabilities):
    """Empty objects mean support; URL-only is never form support."""
    caps = _object(capabilities)
    form = isinstance(_object(caps.get("elicitation")).get("form"), dict)
    extensions = _object(caps.get("extensions"))
    openai = isinstance(_object(extensions.get(OPENAI)).get("form"), dict)
    # Plain MCP form elicitation remains valid when OpenAI's extension is absent.
    # If OpenAI advertises its extension, honor its stricter dual declaration.
    return form and (OPENAI not in extensions or openai), openai


def error(message):
    return McpError(types.ErrorData(code=-32602, message=message))


@dataclass
class Prompt:
    message: str
    schema: type[BaseModel]
    future: asyncio.Future
    key: str


class Conversation:
    def __init__(self, capabilities):
        self.capable, self.openai = form_capabilities(capabilities)
        self.prompts: asyncio.Queue[Prompt] = asyncio.Queue(maxsize=1)
        self.count = 0

    async def elicit(self, message, schema):
        self.count += 1
        future = asyncio.get_running_loop().create_future()
        await self.prompts.put(Prompt(message, schema, future, f"confirmation_{self.count}"))
        return await future

    def request(self, prompt):
        schema = prompt.schema.model_json_schema()
        params = {"mode": "form", "message": prompt.message, "requestedSchema": schema}
        if self.openai:
            params["requestedSchema"] = {"type": "object", "properties": {}}
            params["_meta"] = {OPENAI: {"requestedSchema": schema}}
        return {prompt.key: {"method": "elicitation/create", "params": params}}


@dataclass
class Pending:
    conversation: Conversation
    task: asyncio.Task
    prompt: Prompt
    owner: object
    binding: str
    timer: asyncio.TimerHandle | None = None


class RoundTrips:
    def __init__(self, *, ttl=300, capacity=32):
        self.ttl, self.capacity = ttl, capacity
        self.pending: dict[str, Pending] = {}
        self.active: set[asyncio.Task] = set()

    @staticmethod
    def binding(req):
        payload = json.dumps([req.params.name, req.params.arguments or {}],
                             ensure_ascii=False, sort_keys=True, allow_nan=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def expire(self, token):
        pending = self.pending.pop(token, None)
        if pending is not None:
            pending.task.cancel()

    async def close(self):
        for p in self.pending.values():
            if p.timer:
                p.timer.cancel()
        self.pending.clear()
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, req, owner, original):
        meta = modern_meta(req.params)
        extra = req.params.model_extra or {}
        if meta is None:
            if "requestState" in extra or "inputResponses" in extra:
                raise error("Elicitation continuation requires MCP 2026-07-28 metadata")
            return await original(req)
        capabilities = meta.get(CAPABILITIES)
        if not isinstance(capabilities, dict):
            raise error("Missing request-scoped client capabilities")
        binding = self.binding(req)
        if "requestState" in extra or "inputResponses" in extra:
            token, responses = extra.get("requestState"), extra.get("inputResponses")
            if not isinstance(token, str) or len(token) > 128:
                raise error("Invalid elicitation continuation")
            pending = self.pending.get(token)
            if pending is None:
                raise error("Elicitation expired, already consumed or server restarted; nothing resumed")
            if pending.owner is not owner or pending.binding != binding:
                raise error("Elicitation continuation does not match this connection, tool and arguments")
            capable, openai = form_capabilities(capabilities)
            if not capable or openai != pending.conversation.openai:
                raise error("Elicitation capabilities changed during confirmation")
            prompt = pending.prompt
            if not isinstance(responses, dict) or prompt.key not in responses:
                # Ask again without resuming, changing the deadline, or consuming state.
                return self.input_required(token, pending)
            answer = responses[prompt.key]
            if not isinstance(answer, dict) or answer.get("action") not in ("accept", "decline", "cancel"):
                raise error("Invalid elicitation response")
            action = answer["action"]
            data = None
            if action == "accept":
                content = answer.get("content")
                if content is None and not prompt.schema.model_fields:
                    content = {}  # empty confirmation forms have no inputs
                try:
                    data = prompt.schema.model_validate(content, strict=True)
                except Exception:
                    raise error("Invalid elicitation form content") from None
            # No await between lookup, validation and removal: exactly one retry wins.
            self.pending.pop(token)
            pending.timer.cancel()
            prompt.future.set_result(SimpleNamespace(action=action, data=data))
            return await self.advance(pending)

        if len(self.active) >= self.capacity:
            raise error("Too many pending tool calls; try again later")
        conversation = Conversation(capabilities)

        async def run():
            context_token = CURRENT.set(conversation)
            try:
                return await original(req)
            finally:
                CURRENT.reset(context_token)

        task = asyncio.create_task(run())
        self.active.add(task)
        task.add_done_callback(self.active.discard)
        return await self.advance(Pending(conversation, task, None, owner, binding))

    def input_required(self, token, pending):
        # FastMCP 1.x predates InputRequiredResult, but Result allows extension
        # fields. Use an actual SDK Result to avoid union serializer warnings
        # (which would otherwise write the continuation token into stderr).
        return types.ServerResult(types.EmptyResult(resultType="input_required",
            inputRequests=pending.conversation.request(pending.prompt), requestState=token))

    async def advance(self, pending):
        prompt_wait = asyncio.create_task(pending.conversation.prompts.get())
        try:
            done, _ = await asyncio.wait([pending.task, prompt_wait], return_when=asyncio.FIRST_COMPLETED)
            if pending.task in done:
                result = pending.task.result()
                result.root.resultType = "complete"
                return result
            pending.prompt = prompt_wait.result()
            token = secrets.token_urlsafe(32)
            pending.timer = asyncio.get_running_loop().call_later(self.ttl, self.expire, token)
            self.pending[token] = pending
            return self.input_required(token, pending)
        except BaseException:
            # A disconnected request must not leave a tool running without its host.
            if not pending.task.done():
                pending.task.cancel()
            raise
        finally:
            prompt_wait.cancel()
            await asyncio.gather(prompt_wait, return_exceptions=True)


def install(mcp):
    """Keep the normal SDK handler for legacy clients; adapt modern tools/call."""
    manager = RoundTrips()
    original = mcp._mcp_server.request_handlers[types.CallToolRequest]

    async def handler(req):
        owner = mcp._mcp_server.request_context.session
        return await manager.handle(req, owner, original)

    mcp._mcp_server.request_handlers[types.CallToolRequest] = handler
    return manager


def negotiation_snapshot(ctx):
    """Allowlisted protocol evidence only; never return raw metadata or headers."""
    def protocol(value):
        if not isinstance(value, str) or len(value) != 10:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None

    def capabilities(value):
        caps = _object(value)
        elicit = caps.get("elicitation")
        elicit = _object(elicit) if isinstance(elicit, (dict, BaseModel)) else None
        extensions = _object(caps.get("extensions"))
        return {"elicitation_declared": elicit is not None,
                "legacy_empty_elicitation": elicit == {},
                "form": isinstance((elicit or {}).get("form"), dict),
                "url": isinstance((elicit or {}).get("url"), dict),
                "openai_form": isinstance(_object(extensions.get(OPENAI)).get("form"), dict)}

    try:
        request = ctx.request_context
    except (AttributeError, ValueError):
        request = None
    session = getattr(request, "session", None)
    initialized = getattr(session, "client_params", None)
    meta = _object(getattr(request, "meta", None))
    return {"context_present": request is not None,
            "initialize_present": initialized is not None,
            "initialize_requested_protocol": protocol(getattr(initialized, "protocolVersion", None)),
            "initialize_capabilities": capabilities(getattr(initialized, "capabilities", None)),
            "request_protocol": protocol(meta.get(VERSION)),
            "request_capabilities_present": isinstance(meta.get(CAPABILITIES), dict),
            "request_capabilities": capabilities(meta.get(CAPABILITIES))}
