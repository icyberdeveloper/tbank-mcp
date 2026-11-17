"""Protocol diagnostics expose allowlisted facts, never caller data or secrets."""
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tbank_mcp.elicitation import negotiation_snapshot, VERSION, CAPABILITIES, OPENAI

secret = 'SYNTHETIC_PRIVATE_METADATA_NOT_FOR_OUTPUT'
ctx = SimpleNamespace(request_context=SimpleNamespace(
    session=SimpleNamespace(client_params=SimpleNamespace(protocolVersion='2025-11-25',
        capabilities={'extensions':{OPENAI:{'form':{}, 'private':secret}}, 'token':secret})),
    meta={VERSION:'2026-07-28', CAPABILITIES:{'elicitation':{'form':{}}, 'token':secret},
          'authorization':secret, 'io.modelcontextprotocol/clientInfo':{'name':secret, 'version':secret}}))
snapshot = negotiation_snapshot(ctx)
assert snapshot['context_present'] is True
assert snapshot['initialize_requested_protocol']=='2025-11-25'
assert snapshot['initialize_capabilities']['openai_form'] is True
assert snapshot['initialize_capabilities']['elicitation_declared'] is False
assert snapshot['request_protocol']=='2026-07-28'
assert snapshot['request_capabilities']['form'] is True
assert secret not in str(snapshot)
ctx.request_context.meta[VERSION] = secret
ctx.request_context.session.client_params.protocolVersion = secret
snapshot = negotiation_snapshot(ctx)
assert snapshot['request_protocol'] is None and snapshot['initialize_requested_protocol'] is None
assert secret not in str(snapshot)
assert negotiation_snapshot(None)['context_present'] is False
print('Negotiation diagnostics: protocol/capability facts only; metadata and secrets excluded')
