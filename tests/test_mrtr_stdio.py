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
            cwd=ROOT, env=env, limit=4 * 1024 * 1024, stdin=asyncio.subprocess.PIPE,
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
            # Start exactly as a modern host does: discovery BEFORE initialize.
            meta = {VERSION: '2026-07-28', CAPABILITIES: {
                'elicitation': {'form': {}}, 'extensions': {OPENAI: {'form': {}}}},
                'io.modelcontextprotocol/clientInfo': {'name': 'test', 'version': '1'}}
            discovery = await call('server/discover', {'_meta': meta})
            assert discovery['result']['supportedVersions'] == ['2026-07-28'], discovery
            assert discovery['result']['resultType'] == 'complete', discovery
            assert discovery['result']['_meta']['io.modelcontextprotocol/serverInfo']['name'] == 'tbank'
            tools = await call('tools/list', {'_meta': meta})
            assert 'elicitation_test' in {t['name'] for t in tools['result']['tools']}, tools
            unsupported = await call('server/discover', {'_meta': dict(meta, **{VERSION: '2027-01-01'})})
            assert unsupported['error']['code'] == -32022, unsupported
            unknown = await call('unknown/method', {'_meta': meta})
            assert unknown['error']['code'] == -32601, unknown
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
                assert payload['transport']=='mrtr' and payload['outcome']==action, payload
                assert payload['negotiation']['request_protocol']=='2026-07-28', payload
                assert payload['negotiation']['initialize_present'] is False, payload
                replay = await call('tools/call', params)
                assert replay['error']['code']==-32602, replay

            # Match the live host: core form and URL capabilities, no OpenAI form.
            for settings in ({}, {'url': {}}):
                core_meta = dict(meta, **{CAPABILITIES: {
                    'elicitation': {'form': {}, 'url': {}},
                    'extensions': {OPENAI: settings}}})
                for action in ('accept', 'decline', 'cancel'):
                    params = {'name':'elicitation_test', 'arguments':{}, '_meta':core_meta}
                    result = await call('tools/call', params)
                    required = result['result']
                    assert required['resultType']=='input_required', required
                    key = next(iter(required['inputRequests']))
                    form = required['inputRequests'][key]['params']
                    assert '_meta' not in form, form
                    assert form['requestedSchema']['type']=='object', form
                    params.update(requestState=required['requestState'], inputResponses={key:{'action':action}})
                    result = await call('tools/call', params)
                    payload = json.loads(result['result']['content'][0]['text'])
                    assert payload['transport']=='mrtr' and payload['outcome']==action, payload
                    assert payload['negotiation']['request_capabilities']['form'] is True
                    assert payload['negotiation']['request_capabilities']['openai_form'] is False

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
            assert payload['transport']=='legacy' and payload['outcome']=='accept', payload
            assert payload['negotiation']['initialize_present'] is True, payload
            # Legacy capabilities must not bleed into a later modern request.
            no_caps = dict(meta, **{CAPABILITIES: {}})
            result = await call('tools/call', {'name':'elicitation_test', 'arguments':{}, '_meta':no_caps})
            payload = json.loads(result['result']['content'][0]['text'])
            assert payload['outcome']=='unavailable' and payload['transport']=='mrtr', payload
            assert payload['negotiation']['initialize_present'] is False, payload
            # A legacy peer without elicitation receives a useful, safe snapshot.
            await call('initialize', {'protocolVersion':'2025-11-25', 'capabilities':{},
                'clientInfo':{'name':'legacy','version':'1'}})
            result = await call('tools/call', {'name':'elicitation_test','arguments':{}})
            payload = json.loads(result['result']['content'][0]['text'])
            assert payload['transport']=='legacy' and payload['outcome']=='unavailable', payload
            assert payload['negotiation']['initialize_requested_protocol']=='2025-11-25', payload
            assert payload['negotiation']['request_capabilities_present'] is False, payload
            print('Stdio wire: discovery-first without initialize, modern forms, legacy fallback and negotiation diagnostics OK')
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
