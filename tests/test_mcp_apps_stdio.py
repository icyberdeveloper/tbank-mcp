"""Installed/real stdio: app cards use complete results and hidden UI-only actions."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tbank_mcp.mcp_apps import ACTION, META, MIME, ELIGIBLE
from tbank_mcp.elicitation import CAPABILITIES, VERSION


async def main():
    with tempfile.TemporaryDirectory(prefix="tbank-app-wire-") as tmp:
        env = dict(os.environ, TBANK_CONFIRMATION_MODE="mcp-apps")
        for key in ("TBANK_TRACE_FILE", "TBANK_EVENTS", "TBANK_ATTEMPTS", "TBANK_CA_BUNDLE"):
            env[key] = str(Path(tmp) / key)
        command = (["docker", "run", "--rm", "-i", "-e", "TBANK_CONFIRMATION_MODE=mcp-apps",
                    "--entrypoint", "tbank-mcp", "tbank-mcp:tunnel"] if "--docker" in sys.argv
                   else [sys.executable, "-m", "tbank_mcp.server"])
        proc = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env,
            limit=4 * 1024 * 1024, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        tokens = []
        meta = {VERSION: "2026-07-28", CAPABILITIES: {},
                "io.modelcontextprotocol/clientInfo": {"name": "test-host", "version": "1"}}
        counter = 0
        async def call(method, params):
            nonlocal counter
            counter += 1
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": counter, "method": method,
                "params": dict(params, _meta=meta)}) + "\n").encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            assert line, "server closed stdout"
            result = json.loads(line)
            assert result["id"] == counter
            return result
        try:
            await call("server/discover", {})
            tools = (await call("tools/list", {}))["result"]["tools"]
            for tool in tools:
                if tool["name"] in ELIGIBLE:
                    assert tool["_meta"]["ui"]["resourceUri"] == tool["_meta"]["openai/outputTemplate"]
                    assert tool["_meta"]["ui"]["resourceUri"].startswith("ui://tbank/confirmation-")
            action = next(t for t in tools if t["name"] == ACTION)
            assert action["_meta"]["ui"]["visibility"] == ["app"]
            assert action["_meta"]["openai/visibility"] == "private"
            assert action["annotations"]["destructiveHint"] is True
            uri = next(t["_meta"]["ui"]["resourceUri"] for t in tools if t["name"] == "elicitation_test")
            resources = (await call("resources/list", {}))["result"]["resources"]
            assert uri in {r["uri"] for r in resources}
            resource = (await call("resources/read", {"uri": uri}))["result"]["contents"][0]
            assert resource["mimeType"] == MIME and "confirmation_action" in resource["text"]
            assert resource["_meta"]["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}

            for chosen in ("accept", "decline", "cancel"):
                card = (await call("tools/call", {"name": "elicitation_test", "arguments": {}}))["result"]
                assert card["resultType"] == "complete" and "inputRequests" not in card
                token = card["_meta"][META]["token"]
                tokens.append(token)
                assert token not in json.dumps({k: v for k, v in card.items() if k != "_meta"})
                assert set(card["structuredContent"]) == {"result"}, "existing outputSchema must stay valid"
                invalid = await call("tools/call", {"name": ACTION, "arguments": {
                    "token": token, "action": "accept", "amount": 100}})
                assert invalid["error"]["code"] == -32602
                final = (await call("tools/call", {"name": ACTION, "arguments": {
                    "token": token, "action": chosen, "data": {"confirmed": True}}}))["result"]
                payload = json.loads(final["content"][0]["text"])
                assert payload["transport"] == "mcp-apps" and payload["outcome"] == chosen
                assert META in final["_meta"] and "token" not in final["_meta"][META]
                duplicate = await call("tools/call", {"name": ACTION, "arguments": {
                    "token": token, "action": "accept"}})
                assert duplicate["error"]["code"] == -32602
            print("MCP Apps wire: packaged UI, complete cards, hidden handles, app-only actions, valid outputSchema and one-use continuations OK")
        finally:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            stderr = (await proc.stderr.read()).decode()
            assert not any(token in stderr for token in tokens), "handle leaked to server logs"
            if proc.returncode not in (0, -9):
                print(stderr, file=sys.stderr)


asyncio.run(main())
