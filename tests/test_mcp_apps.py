"""App-only confirmation handles and real payment coroutines with a fake bank."""
import asyncio
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
TMP = tempfile.TemporaryDirectory(prefix="tbank-apps-")
for key in ("TBANK_EVENTS", "TBANK_ATTEMPTS", "TBANK_TRACE_FILE", "TBANK_CA_BUNDLE"):
    os.environ[key] = str(Path(TMP.name) / key)
os.environ.pop("TBANK_CONFIRM_ABOVE", None)
from mcp import types
from mcp.shared.exceptions import McpError
from tbank_mcp import server
from tbank_mcp.mcp_apps import AppConfirmations, META, ELIGIBLE, configured_mode
from test_elicitation_flows import TransferSession, _two_banks

OWNER = object()
ARGS = {"amount": 1000, "to_account": "+79991234567", "from_account": "1111111111", "force": True}


def request(**extra):
    return types.CallToolRequest(params=types.CallToolRequestParams.model_validate(
        {"name": "transfer", "arguments": ARGS, **extra}))


def wire(result):
    return result.model_dump(by_alias=True, mode="json", exclude_none=True)


async def rejected(awaitable):
    try:
        await awaitable
    except McpError:
        return
    raise AssertionError("unsafe confirmation accepted")


async def main():
    # Adding a new confirming public tool must not bypass the Apps transport.
    tree = ast.parse((ROOT / "tbank_mcp/server.py").read_text())
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.decorator_list:
            continue
        calls = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if calls & {"_money_gate", "_transfer_gate", "_transfer_prepare", "_elicit_or"}:
            assert node.name in ELIGIBLE, f"confirming tool {node.name} has no Apps route"
    assert configured_mode() == "elicitation", "direct harness default changed"
    os.environ["TBANK_CONFIRMATION_MODE"] = "invalid"
    try:
        configured_mode()
    except ValueError:
        pass
    else:
        raise AssertionError("invalid mode silently accepted")
    finally:
        os.environ.pop("TBANK_CONFIRMATION_MODE", None)
    recips = _two_banks()
    recips[0]["is_default_bank"] = True
    bank = TransferSession(recips)
    server._require = lambda: bank
    sent = []
    transfer = bank.transfer
    def counting(*args, **kwargs):
        sent.append(kwargs)
        return transfer(*args, **kwargs)
    bank.transfer = counting
    invoked = []
    async def original(req):
        invoked.append(req)
        out = await server.transfer(**req.params.arguments, ctx=SimpleNamespace())
        return types.ServerResult(types.CallToolResult(content=[types.TextContent(type="text", text=out)]))

    for action in ("decline", "cancel", "accept"):
        manager = AppConfirmations()
        try:
            before = len(sent)
            result = wire(await manager.start(request(), OWNER, original))
            assert result["resultType"] == "complete" and "inputRequests" not in result
            card = result["_meta"][META]
            token = card["token"]
            assert token not in json.dumps({k: v for k, v in result.items() if k != "_meta"})
            assert card["status"] == "pending" and len(sent) == before
            assert "1000" in card["message"] or "1 000" in card["message"]
            await rejected(manager.submit({"token": token, "action": "accept"}, object()))
            await rejected(manager.submit({"token": token, "action": "accept", "amount": 1}, OWNER))
            await rejected(manager.submit({"token": token, "action": "accept", "data": {"amount": 1}}, OWNER))
            await rejected(manager.submit({"token": token + "bad", "action": "accept"}, OWNER))
            calls = len(invoked)
            final = wire(await manager.submit({"token": token, "action": action}, OWNER))
            assert final["_meta"][META] == {"status": "complete", "action": action}
            assert len(invoked) == calls, "bank preparation was replayed"
            assert len(sent) == before + (action == "accept")
            await rejected(manager.submit({"token": token, "action": "accept"}, OWNER))
        finally:
            await manager.close()

    # Exactly one concurrent click can resume a debit.
    manager = AppConfirmations()
    before = len(sent)
    token = wire(await manager.start(request(), OWNER, original))["_meta"][META]["token"]
    results = await asyncio.gather(*[manager.submit({"token": token, "action": "accept"}, OWNER)
                                    for _ in range(2)], return_exceptions=True)
    assert sum(isinstance(r, McpError) for r in results) == 1 and len(sent) == before + 1
    await manager.close()

    # Expiry/restart/capacity cannot authorize a debit. MRTR cannot bypass a card.
    manager = AppConfirmations(ttl=0.01, capacity=1)
    before = len(sent)
    token = wire(await manager.start(request(), OWNER, original))["_meta"][META]["token"]
    await rejected(manager.start(request(), OWNER, original))
    await asyncio.sleep(0.04)
    await rejected(manager.submit({"token": token, "action": "accept"}, OWNER))
    assert len(sent) == before
    await rejected(manager.start(request(requestState=token, inputResponses={}), OWNER, original))
    await manager.close()
    restarted = AppConfirmations()
    await rejected(restarted.submit({"token": token, "action": "accept"}, OWNER))
    await restarted.close()

    # An overdue timer callback cannot make a late click valid when the loop was busy.
    manager = AppConfirmations(ttl=0.01)
    token = wire(await manager.start(request(), OWNER, original))["_meta"][META]["token"]
    before = len(sent)
    time.sleep(0.02)
    await rejected(manager.submit({"token": token, "action": "accept"}, OWNER))
    assert len(sent) == before
    await manager.close()

    # Bank selection -> money gate: validate choices, rotate handles, prepare once.
    bank = TransferSession(_two_banks())
    server._require = lambda: bank
    charged = []
    transfer = bank.transfer
    bank.transfer = lambda *a, **kw: (charged.append(kw), transfer(*a, **kw))[1]
    manager = AppConfirmations()
    try:
        first = wire(await manager.start(request(), OWNER, original))["_meta"][META]
        choices = first["schema"]["properties"]["choice"]["enum"]
        await rejected(manager.submit({"token": first["token"], "action": "accept",
                                       "data": {"choice": 42}}, OWNER))
        second = wire(await manager.submit({"token": first["token"], "action": "accept",
                            "data": {"choice": choices[0]}}, OWNER))["_meta"][META]
        assert second["status"] == "pending" and second["token"] != first["token"]
        assert len(bank.resolved) == 1 and not charged
        await rejected(manager.submit({"token": first["token"], "action": "accept"}, OWNER))
        await manager.submit({"token": second["token"], "action": "accept"}, OWNER)
        assert len(charged) == 1 and len(bank.resolved) == 1
    finally:
        await manager.close()
    print("MCP Apps: no debit before click; accept/decline/cancel, hidden tokens, binding, expiry, replay, concurrency and chained forms OK")


asyncio.run(main())
