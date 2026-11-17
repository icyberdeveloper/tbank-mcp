"""Dual-era stdio entry point for FastMCP 1.x.

Route self-contained MCP 2026-07-28 requests before the old SDK's ClientRequest
union and initialization guard. Keep its original session loop for legacy frames.
This is essential: implementing MRTR tools/call alone leaves server/discover
unsupported, so hosts fall back to legacy and never send request capabilities.
"""
from contextlib import asynccontextmanager
from importlib.metadata import version

import anyio
from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.server.stdio import stdio_server
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from .elicitation import VERSION, CAPABILITIES

PROTOCOL = "2026-07-28"
CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
SERVER_INFO = "io.modelcontextprotocol/serverInfo"


class ModernStdio:
    def __init__(self, mcp, lifespan_context=None):
        self.mcp = mcp
        self.owner = object()  # one authenticated single-account stdio connection
        self.lifespan_context = lifespan_context
        self.info = {"name": mcp.name, "version": version("tbank-mcp")}

    @staticmethod
    def routes(message):
        if not isinstance(message, types.JSONRPCRequest):
            return False
        meta = (message.params or {}).get("_meta")
        declared = meta.get(VERSION) if isinstance(meta, dict) else None
        return message.method == "server/discover" or (
            isinstance(declared, str) and declared >= PROTOCOL)

    async def respond(self, message):
        token = None
        try:
            params = message.params or {}
            meta = params.get("_meta")
            if not isinstance(meta, dict):
                raise McpError(types.ErrorData(code=-32602, message="Missing MCP request metadata"))
            if meta.get(VERSION) != PROTOCOL:
                raise McpError(types.ErrorData(code=-32022, message="Unsupported protocol version",
                    data={"supportedVersions": [PROTOCOL]}))
            identity = meta.get(CLIENT_INFO)
            if (not isinstance(meta.get(CAPABILITIES), dict) or not isinstance(identity, dict)
                    or not isinstance(identity.get("name"), str)
                    or not isinstance(identity.get("version"), str)):
                raise McpError(types.ErrorData(code=-32602, message="Missing MCP client identity or capabilities"))
            if message.method == "server/discover":
                options = self.mcp._mcp_server.create_initialization_options()
                result = {"supportedVersions": [PROTOCOL],
                          "capabilities": options.capabilities.model_dump(by_alias=True, exclude_none=True)}
                if options.instructions:
                    result["instructions"] = options.instructions
            else:
                if message.method in ("initialize", "ping"):
                    raise McpError(types.ErrorData(code=-32601, message="Method is not part of the modern lifecycle"))
                try:
                    req = types.ClientRequest.model_validate(message.model_dump(by_alias=True, exclude_none=True)).root
                except ValidationError:
                    # Unknown method and invalid parameters are distinct protocol errors.
                    known = {getattr(t.model_fields.get("method"), "default", None)
                             for t in self.mcp._mcp_server.request_handlers}
                    code = -32602 if message.method in known else -32601
                    raise McpError(types.ErrorData(code=code, message="Unknown method or invalid request parameters")) from None
                handler = self.mcp._mcp_server.request_handlers.get(type(req))
                if handler is None:
                    raise McpError(types.ErrorData(code=-32601, message="Method not found"))
                token = request_ctx.set(RequestContext(request_id=message.id,
                    meta=req.params.meta if req.params else None, session=self.owner,
                    lifespan_context=self.lifespan_context))
                response = await handler(req)
                result = response.model_dump(by_alias=True, mode="json", exclude_none=True)
            result.setdefault("resultType", "complete")
            result.setdefault("_meta", {})[SERVER_INFO] = self.info
            return types.JSONRPCResponse(jsonrpc="2.0", id=message.id, result=result)
        except McpError as exc:
            return types.JSONRPCError(jsonrpc="2.0", id=message.id, error=exc.error)
        except Exception:
            # Never reflect arguments, metadata, credentials or exception bodies.
            return types.JSONRPCError(jsonrpc="2.0", id=message.id,
                error=types.ErrorData(code=-32603, message="Internal MCP request error"))
        finally:
            if token is not None:
                request_ctx.reset(token)


async def run(mcp, round_trips):
    server = mcp._mcp_server
    original_lifespan = server.lifespan
    ready = anyio.Event()
    modern = ModernStdio(mcp)

    @asynccontextmanager
    async def lifespan(instance):
        async with original_lifespan(instance) as context:
            modern.lifespan_context = context
            ready.set()
            yield context

    server.lifespan = lifespan
    legacy_in, legacy_read = anyio.create_memory_object_stream(0)
    legacy_write, legacy_out = anyio.create_memory_object_stream(0)
    try:
        async with stdio_server() as (read, write):
            async def relay_legacy():
                async with legacy_out:
                    async for message in legacy_out:
                        await write.send(message)

            async def handle_modern(message):
                response = await modern.respond(message)
                await write.send(SessionMessage(types.JSONRPCMessage(response)))

            try:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(server.run, legacy_read, legacy_write, server.create_initialization_options())
                    tasks.start_soon(relay_legacy)
                    await ready.wait()
                    async for incoming in read:
                        if isinstance(incoming, SessionMessage) and modern.routes(incoming.message.root):
                            tasks.start_soon(handle_modern, incoming.message.root)
                        else:
                            await legacy_in.send(incoming)
                    # Match the SDK's EOF behavior: no orphaned requests after disconnect.
                    tasks.cancel_scope.cancel()
            finally:
                with anyio.CancelScope(shield=True):
                    await round_trips.close()
                    await legacy_in.aclose()
                    await write.aclose()
    finally:
        server.lifespan = original_lifespan
