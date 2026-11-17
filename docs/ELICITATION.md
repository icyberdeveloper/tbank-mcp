# Payment confirmations over OpenAI Secure MCP Tunnel

Direct MCP clients use the existing `elicitation/create` exchange. Registered
OpenAI connectors use MCP 2026-07-28 multi-round-trip requests (MRTR): capabilities
come from each request's `_meta`, and the server returns `input_required` instead
of sending a server-initiated request.

FastMCP 1.x only checks capabilities from `initialize`. A registered connector can
therefore support forms while the old payment guard reports them as unavailable.
Simply forcing that guard to return true would still use the wrong wire protocol.

The compatibility layer in `tbank_mcp/elicitation.py` adapts modern `tools/call`
requests. It preserves the existing payment coroutines and confirmation checks:

- A form is returned in `inputRequests`; OpenAI's extended schema is placed under
  `_meta["openai/elicitation"].requestedSchema` when the host advertises that
  extension together with `elicitation.form`.
- The host retries the same tool and arguments with `requestState` and
  `inputResponses`. Those are protocol fields, not model-visible tool arguments.
- The original coroutine resumes; recipient resolution, account selection and
  payment quotes are not replayed just to display the next form.
- Continuations use random one-use handles bound to the original connection,
  tool and arguments. They expire after five minutes. There are at most 32 active
  modern tool calls per server process. Invalid, changed, expired, consumed or
  lost-on-restart state never resumes a payment.
- Decline, cancel, malformed replies and missing form capabilities never authorize
  a debit. The bank's existing payment journal and duplicate guards still apply.

The deployment remains one process and one account per tunnel. Pending forms do
not survive process replacement. A lost response after a debit must be reconciled
through payment status and operations, rather than replaying the payment.

## Check a deployment without transferring money

After the fix has been merged and its image published:

```bash
bash tbank update
```

Refresh/reconnect the app in ChatGPT if its cached tool catalog does not yet
include `elicitation_test`. Ask it to run that tool. It only displays a harmless
confirmation form; it never logs in, calls the bank, writes a payment attempt or
moves money. Confirm and decline separate test calls to check both paths.

Expected result for a registered connector: `transport: "mrtr"` with the action
chosen in the UI. `unavailable` means the current request did not declare the
required form capabilities. `error` means the form exchange failed. Direct
legacy MCP clients report `transport: "legacy"`.

Automated tests cover actual stdio wire messages and fake-bank transfers,
including accept/decline/cancel, multiple dialogs, changed arguments, cross-session
state, expiration and replay. A live ChatGPT test still needs the deployed image;
a successful offline test alone does not prove host UI behavior.

References:
[OpenAI form elicitation](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#openai-form-elicitation),
[MCP multi-round-trip requests](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr).
