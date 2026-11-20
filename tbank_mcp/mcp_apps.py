"""MCP Apps confirmations; the default elicitation transport is left intact.

A card suspends the *original* tool coroutine. Only its widget receives the
one-use handle, in hidden result metadata. An app-only action resumes that
coroutine; neither the model nor the widget can replace its payment arguments.
State is connection-bound, bounded and process-local. Expiry/restart cancels it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from mcp import types

from . import elicitation

MIME = "text/html;profile=mcp-app"
META = "tbank/confirmation"
ACTION = "confirmation_action"
ELIGIBLE = frozenset({"transfer", "transfer_requisites", "pay_bill", "ticket_pay",
                      "grocery_checkout", "train_pay", "flight_book", "elicitation_test"})
ASSET = Path(__file__).with_name("ui") / "confirmation.html"


def configured_mode():
    mode = os.environ.get("TBANK_CONFIRMATION_MODE", "elicitation")
    if mode not in ("elicitation", "mcp-apps"):
        raise ValueError("TBANK_CONFIRMATION_MODE must be elicitation or mcp-apps")
    return mode


def resource_uri():
    digest = hashlib.sha256(ASSET.read_bytes()).hexdigest()[:16]
    return f"ui://tbank/confirmation-{digest}.html"


class AppConversation(elicitation.Conversation):
    transport = "mcp-apps"

    def __init__(self):
        super().__init__({})
        self.capable = True
        self.last_action = None
        self.tool_name = None


class AppConfirmations(elicitation.RoundTrips):
    """Reuse lifecycle/expiry, not MRTR request or response encoding."""

    async def start(self, req, owner, original):
        if any(key in (req.params.model_extra or {}) for key in ("requestState", "inputResponses")):
            raise elicitation.error("MCP Apps confirmations must use the card's action")
        if len(self.active) >= self.capacity:
            raise elicitation.error("Too many pending confirmations; finish or cancel a card first")
        conversation = AppConversation()
        conversation.tool_name = req.params.name

        async def run():
            token = elicitation.CURRENT.set(conversation)
            try:
                return await original(req)
            finally:
                elicitation.CURRENT.reset(token)

        task = asyncio.create_task(run())
        self.active.add(task)
        task.add_done_callback(self.active.discard)
        return await self.advance(elicitation.Pending(conversation, task, None, owner, self.binding(req)))

    def input_required(self, token, pending):
        # structuredContent matches FastMCP's existing outputSchema for str tools.
        # The handle is intentionally absent from all model-visible content.
        message = pending.prompt.message
        expires = time.time() + max(0, pending.timer.when() - asyncio.get_running_loop().time())
        print(json.dumps({"event": "mcp_app_confirmation", "stage": "card_ready",
                          "tool": pending.conversation.tool_name}), file=sys.stderr, flush=True)
        return types.ServerResult(types.CallToolResult(
            content=[types.TextContent(type="text", text=(
                "Требуется действие пользователя в карточке подтверждения. "
                "Операция ожидает, не повторяй исходный вызов.\n" + message))],
            structuredContent={"result": "Ожидается подтверждение в карточке.\n" + message},
            _meta={META: {"status": "pending", "token": token,
                         "expiresAt": int(expires * 1000), "message": message,
                         "schema": pending.prompt.schema.model_json_schema()}},
            resultType="complete"))

    async def advance(self, pending):
        result = await super().advance(pending)
        root = result.root
        meta = dict(root.meta or {})
        if META not in meta:
            meta[META] = {"status": "complete", "action": pending.conversation.last_action}
            root.meta = meta
        return result

    async def submit(self, arguments, owner):
        if not isinstance(arguments, dict) or set(arguments) - {"token", "action", "data"}:
            raise elicitation.error("Invalid confirmation action")
        token, action = arguments.get("token"), arguments.get("action")
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            raise elicitation.error("Invalid confirmation handle")
        if action not in ("accept", "decline", "cancel"):
            raise elicitation.error("Invalid confirmation action")
        pending = self.pending.get(token)
        if pending is None:
            raise elicitation.error("Confirmation expired, consumed or server restarted; nothing resumed")
        if pending.owner is not owner:
            raise elicitation.error("Confirmation belongs to another connection")
        data = None
        if action == "accept":
            content = arguments.get("data", {})
            if not isinstance(content, dict) or set(content) - set(pending.prompt.schema.model_fields):
                raise elicitation.error("Invalid confirmation form content")
            try:
                data = pending.prompt.schema.model_validate(content, strict=True)
            except Exception:
                raise elicitation.error("Invalid confirmation form content") from None
        # No await until the token has been consumed: only one click can win.
        self.pending.pop(token)
        pending.timer.cancel()
        pending.conversation.last_action = action
        print(json.dumps({"event": "mcp_app_confirmation", "stage": "action_received",
                          "tool": pending.conversation.tool_name, "action": action}),
              file=sys.stderr, flush=True)
        pending.prompt.future.set_result(SimpleNamespace(action=action, data=data))
        return await self.advance(pending)


def install(mcp, original):
    """Select explicitly by deployment; direct harnesses default to elicitation."""
    manager = AppConfirmations()
    if configured_mode() == "elicitation":
        return manager

    uri = resource_uri()
    server = mcp._mcp_server
    fallback = server.request_handlers[types.CallToolRequest]
    list_tools = server.request_handlers[types.ListToolsRequest]
    list_resources = server.request_handlers[types.ListResourcesRequest]
    read_resource = server.request_handlers[types.ReadResourceRequest]

    async def tools(req):
        result = await list_tools(req)
        for tool in result.root.tools:
            if tool.name in ELIGIBLE:
                tool.meta = {**(tool.meta or {}), "ui": {"resourceUri": uri},
                             "openai/outputTemplate": uri}
        result.root.tools.append(types.Tool(name=ACTION,
            description="Submit a confirmation from the card. App-only; never call from the model.",
            inputSchema={"type": "object", "properties": {
                "token": {"type": "string"},
                "action": {"type": "string", "enum": ["accept", "decline", "cancel"]},
                "data": {"type": "object"}}, "required": ["token", "action"],
                "additionalProperties": False},
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                               idempotentHint=False, openWorldHint=True),
            _meta={"ui": {"visibility": ["app"]}, "openai/visibility": "private",
                   "openai/widgetAccessible": True}))
        return result

    async def resources(req):
        result = await list_resources(req)
        result.root.resources.append(types.Resource(uri=uri, name="payment-confirmation",
            title="Подтверждение операции", mimeType=MIME))
        return result

    async def read(req):
        if str(req.params.uri) != uri:
            return await read_resource(req)
        return types.ServerResult(types.ReadResourceResult(contents=[types.TextResourceContents(
            uri=uri, mimeType=MIME, text=ASSET.read_text(encoding="utf-8"),
            _meta={"ui": {"prefersBorder": True,
                           "csp": {"connectDomains": [], "resourceDomains": []}},
                   "openai/widgetPrefersBorder": True,
                   "openai/widgetCSP": {"connect_domains": [], "resource_domains": []}})]))

    async def call(req):
        owner = server.request_context.session
        if req.params.name == ACTION:
            return await manager.submit(req.params.arguments, owner)
        if req.params.name in ELIGIBLE:
            return await manager.start(req, owner, original)
        return await fallback(req)

    server.request_handlers[types.ListToolsRequest] = tools
    server.request_handlers[types.ListResourcesRequest] = resources
    server.request_handlers[types.ReadResourceRequest] = read
    server.request_handlers[types.CallToolRequest] = call
    return manager
