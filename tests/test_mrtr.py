"""Modern wire envelopes and money continuations; no bank access, all data synthetic."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
TMP = tempfile.mkdtemp(prefix='tbank-mrtr-')
for key, name in [('TBANK_EVENTS', 'events'), ('TBANK_ATTEMPTS', 'attempts'),
                  ('TBANK_TRACE_FILE', 'calls'), ('TBANK_CA_BUNDLE', 'bundle')]:
    os.environ[key] = str(Path(TMP) / name)
os.environ.pop('TBANK_CONFIRM_ABOVE', None)
from mcp import types
from mcp.shared.exceptions import McpError
from tbank_mcp import server
from tbank_mcp.elicitation import RoundTrips, CAPABILITIES, VERSION, OPENAI, CURRENT
from test_elicitation_flows import TransferSession, _two_banks

CAPS = {'elicitation': {'form': {}}, 'extensions': {OPENAI: {'form': {}}}}
OWNER = object()


def request(args=None, *, state=None, responses=None, caps=CAPS, name='transfer'):
    data = {'name': name, 'arguments': args or {}, '_meta': {
        VERSION: '2026-07-28', CAPABILITIES: caps,
        'io.modelcontextprotocol/clientInfo': {'name': 'test', 'version': '1'}}}
    if state is not None:
        data['requestState'] = state
    if responses is not None:
        data['inputResponses'] = responses
    return types.CallToolRequest(params=types.CallToolRequestParams.model_validate(data))


def wire(result):
    # Exercise actual ServerResult serialization (the SDK's union predates MRTR).
    encoded = types.JSONRPCResponse(jsonrpc='2.0', id=1, result=result.model_dump(by_alias=True, mode='json', exclude_none=True)).model_dump_json(by_alias=True, exclude_none=True)
    return json.loads(encoded)['result']


def resume_args(result, action='accept', content=None):
    return dict(state=result['requestState'], responses={next(iter(result['inputRequests'])):
                {'action': action, **({'content': content} if content is not None else {})}})


async def rejected(awaitable):
    try:
        await awaitable
    except McpError:
        return
    raise AssertionError('unsafe continuation accepted')


async def main():
    recips = _two_banks(); recips[0]['is_default_bank'] = True
    bank = TransferSession(recips)
    sent = []
    transfer = bank.transfer
    def counting(*a, **kw):
        sent.append(kw)
        return transfer(*a, **kw)
    bank.transfer = counting
    server._require = lambda: bank
    arguments = {'amount': 1000, 'to_account': '+79991234567', 'from_account': '1111111111', 'force': True}
    invoked = []
    async def original(req):
        invoked.append(req)
        out = await server.transfer(**req.params.arguments, ctx=SimpleNamespace())
        return types.ServerResult(types.CallToolResult(content=[types.TextContent(type='text', text=out)]))
    manager = RoundTrips()
    try:
        for action in ['decline', 'cancel', 'accept']:
            result = wire(await manager.handle(request(arguments), OWNER, original))
            assert result['resultType'] == 'input_required', result
            assert not sent
            prompt = next(iter(result['inputRequests'].values()))['params']
            assert '1000' in prompt['message'] or '1 000' in prompt['message'], prompt
            assert prompt['requestedSchema'] == {'type': 'object', 'properties': {}}
            assert OPENAI in prompt['_meta']
            continuation = request(arguments, **resume_args(result, action))
            if action == 'accept':
                await rejected(manager.handle(request(dict(arguments, amount=2000), **resume_args(result)), OWNER, original))
                await rejected(manager.handle(continuation, object(), original))
                await rejected(manager.handle(request(arguments, state=result['requestState']+'bad', responses={}), OWNER, original))
            before = len(invoked)
            final = wire(await manager.handle(continuation, OWNER, original))
            assert final['resultType'] == 'complete', final
            assert len(invoked) == before, 'original tool was re-executed'
            assert len(sent) == (1 if action == 'accept' else 0), final
            await rejected(manager.handle(continuation, OWNER, original))
        assert len(bank.resolved) == 3, 'recipient preparation was replayed'
        assert len(sent) == 1
    finally:
        await manager.close()

    # Capability-less and URL-only modern callers never prepare or post a payment.
    for caps in [{}, {'elicitation': {'url': {}}},
                 {'elicitation': {'form': {}}, 'extensions': {OPENAI: {}}}]:
        manager = RoundTrips()
        resolved = len(bank.resolved)
        final = wire(await manager.handle(request(arguments, caps=caps), OWNER, original))
        assert 'ПЛАТЁЖ НЕ ВЫПОЛНЕН' in str(final)
        assert len(sent) == 1 and len(bank.resolved) == resolved
        await manager.close()

    # Plain modern MCP uses the actual form schema without the OpenAI extension.
    manager = RoundTrips(ttl=0.01)
    result = wire(await manager.handle(request(arguments, caps={'elicitation': {'form': {}}}), OWNER, original))
    assert '_meta' not in next(iter(result['inputRequests'].values()))['params']
    await asyncio.sleep(0.03)
    await rejected(manager.handle(request(arguments, **resume_args(result)), OWNER, original))
    assert len(sent) == 1
    await manager.close()

    # A source-account picker followed by the money gate resumes the same tool.
    manager = RoundTrips()
    bank.accounts.append({'id': '2222222222', 'name': 'Second', 'balance': 3000})
    args = dict(arguments, from_account='')
    result = wire(await manager.handle(request(args), OWNER, original))
    schema = next(iter(result['inputRequests'].values()))['params']['_meta'][OPENAI]['requestedSchema']
    selected = schema['properties']['choice']['enum'][1]
    invalid = request(args, **resume_args(result, content={'choice': 7}))
    await rejected(manager.handle(invalid, OWNER, original))
    assert len(sent) == 1
    gate = wire(await manager.handle(request(args, **resume_args(result, content={'choice': selected})), OWNER, original))
    assert gate['resultType'] == 'input_required' and gate['requestState'] != result['requestState']
    final = wire(await manager.handle(request(args, **resume_args(gate)), OWNER, original))
    assert len(sent) == 2 and sent[-1]['account'] == '2222222222', final
    await manager.close()
    # Missing answers re-display the same prompt. Capacity is bounded, and two
    # simultaneous accepts of one handle still execute the debit only once.
    manager = RoundTrips(capacity=1)
    result = wire(await manager.handle(request(arguments), OWNER, original))
    again = wire(await manager.handle(request(arguments, state=result['requestState'], responses={}), OWNER, original))
    assert again == result
    await rejected(manager.handle(request(arguments), OWNER, original))
    answer = request(arguments, **resume_args(result))
    before = len(sent)
    outcomes = await asyncio.gather(manager.handle(answer, OWNER, original),
                                    manager.handle(answer, OWNER, original), return_exceptions=True)
    assert sum(isinstance(outcome, McpError) for outcome in outcomes) == 1
    assert len(sent) == before + 1
    await manager.close()
    assert CURRENT.get() is None
    print('MRTR: accept/decline/cancel, no preparation replay, capability checks, binding, expiry, replay and pickers OK')


if __name__ == '__main__':
    asyncio.run(main())
