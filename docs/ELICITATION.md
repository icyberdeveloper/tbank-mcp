# Payment confirmations over OpenAI Secure MCP Tunnel

Direct MCP clients use the existing `elicitation/create` exchange. Registered
OpenAI connectors use MCP 2026-07-28 multi-round-trip requests (MRTR): capabilities
come from each request's `_meta`, and the server returns `input_required` instead
of sending a server-initiated request.

FastMCP 1.x only checks capabilities from `initialize`. A registered connector can
therefore support forms while the old payment guard reports them as unavailable.
Simply forcing that guard to return true would still use the wrong wire protocol.

The stdio entry point in `tbank_mcp/stdio.py` answers `server/discover` and
accepts self-contained modern requests without `initialize`. This happens before
the old SDK's request union and legacy initialization guard; otherwise discovery
fails and an automatic client falls back before it ever sends modern tool calls.
The original SDK session still handles legacy frames.

The compatibility layer in `tbank_mcp/elicitation.py` adapts modern `tools/call`
requests. It preserves the existing payment coroutines and confirmation checks:

- A form is returned in `inputRequests`; OpenAI's extended schema is placed under
  `_meta["openai/elicitation"].requestedSchema` when the host advertises that
  extension together with `elicitation.form`.
- Standard form support is independent of OpenAI's extended form support. If
  `elicitation.form` is declared but `openai/elicitation.form` is absent, send a
  standard MCP form using the real `requestedSchema`. An empty or URL-only OpenAI
  extension does not disable core forms. Use OpenAI's schema metadata carrier only
  when both form capabilities are declared.
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
moves money. The probe has one required boolean field with no default, rather
than an empty form. Confirm and decline separate test calls to check both paths.
This distinguishes displaying a normal form from handling an empty confirmation;
it does not change payment confirmation forms or prove that a host supports them.

Modern calls to this probe also write an `elicitation_test_response` JSON event to
stderr (visible in `docker compose logs bank`). It includes the response type,
core form support, selected encoding, number of input requests and schema fields,
and whether the request is a continuation. Tokens, form answers, caller identity
and raw metadata are omitted. An `input_required` event without a subsequent
`complete` continuation event shows that the server produced the form but has not
completed a continuation. It does not establish whether the host displayed it.

After changing protocol support, rescan the connector so its client can negotiate
again with discovery. An existing legacy connection may continue using its saved
protocol until it is recreated/refreshed.

Expected result for a registered connector: `transport: "mrtr"` with the action
chosen in the UI. `unavailable` means the current request did not declare the
required form capabilities. `error` means the form exchange failed. Direct
legacy MCP clients report `transport: "legacy"`. The `negotiation` object reports
only protocol dates and capability booleans from initialization and the current
request. Raw metadata, identities, headers, tokens and arbitrary extension values
are never returned. A legacy/unavailable result is evidence of the negotiated
path, not proof that every ChatGPT surface lacks form support.

Automated tests start with discovery and no initialization, then cover actual stdio wire messages and fake-bank transfers,
including accept/decline/cancel, multiple dialogs, changed arguments, cross-session
state, expiration and replay. A live ChatGPT test still needs the deployed image;
a successful offline test alone does not prove host UI behavior.

References:
[OpenAI form elicitation](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#openai-form-elicitation),
[MCP multi-round-trip requests](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr).
