"""Real stdio JSON-RPC: modern MRTR and legacy push forms, without bank access."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tbank_mcp.elicitation import CAPABILITIES, VERSION, OPENAI


async def main():
    with tempfile.TemporaryDirectory(prefix='tbank-wire-') as tmp:
        env = dict(os.environ)
        for key in ('TBANK_TRACE_FILE', 'TBANK_EVENTS', 'TBANK_ATTEMPTS', 'TBANK_CA_BUNDLE'):
            env[key] = str(Path(tmp) / key)
        command = (["docker", "run", "--rm", "-i", "--entrypoint", "tbank-mcp", "tbank-mcp:tunnel"]
                   if "--docker" in sys.argv else [sys.executable, "-m", "tbank_mcp.server"])
        proc = await asyncio.create_subprocess_exec(*command,
            cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        counter = 0
        async def send(message):
            proc.stdin.write((json.dumps(message)+'\n').encode())
            await proc.stdin.drain()
        async def read():
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            assert line, 'server closed stdout'
            return json.loads(line)
        async def call(method, params):
            nonlocal counter
            counter += 1
            await send({'jsonrpc': '2.0', 'id': counter, 'method': method, 'params': params})
            return await read()
        try:
            init = await call('initialize', {'protocolVersion': '2025-11-25',
                'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
            assert 'result' in init, init
            await send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            meta = {VERSION: '2026-07-28', CAPABILITIES: {
                'elicitation': {'form': {}}, 'extensions': {OPENAI: {'form': {}}}},
                'io.modelcontextprotocol/clientInfo': {'name': 'test', 'version': '1'}}
            for action in ('accept', 'decline', 'cancel'):
                params = {'name': 'elicitation_test', 'arguments': {}, '_meta': meta}
                result = await call('tools/call', params)
                assert 'method' not in result and result['result']['resultType']=='input_required', result
                required = result['result']
                key = next(iter(required['inputRequests']))
                params.update(requestState=required['requestState'], inputResponses={key:{'action':action}})
                result = await call('tools/call', params)
                assert result['result']['resultType']=='complete', result
                payload = json.loads(result['result']['content'][0]['text'])
                assert payload == {'transport':'mrtr', 'outcome':action}, payload
                replay = await call('tools/call', params)
                assert replay['error']['code']==-32602, replay

            # The legacy push path still works for existing direct MCP clients.
            await call('initialize', {'protocolVersion': '2025-11-25',
                'capabilities': {'elicitation': {'form': {}}},
                'clientInfo': {'name':'legacy', 'version':'1'}})
            await send({'jsonrpc':'2.0','method':'notifications/initialized'})
            prompt = await call('tools/call', {'name':'elicitation_test','arguments':{}})
            assert prompt['method']=='elicitation/create', prompt
            await send({'jsonrpc':'2.0','id':prompt['id'],'result':{'action':'accept','content':{}}})
            result = await read()
            payload = json.loads(result['result']['content'][0]['text'])
            assert payload == {'transport':'legacy','outcome':'accept'}, payload
            print('Stdio wire: request-scoped capabilities override empty initialize; modern accept/decline/cancel/replay and legacy form OK')
        finally:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            err = (await proc.stderr.read()).decode()
            if err:
                print(err, file=sys.stderr)

if __name__ == '__main__':
    asyncio.run(main())
