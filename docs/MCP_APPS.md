# Confirmation cards in ChatGPT

Two confirmation transports share the existing payment guards and payment
coroutines. Select the transport for the server process, not through a tool
argument supplied by the model:

| Deployment | `TBANK_CONFIRMATION_MODE` | Confirmation |
| --- | --- | --- |
| Direct stdio harnesses (default) | `elicitation` | Existing legacy or MRTR elicitation |
| ChatGPT through the supplied Compose file | `mcp-apps` | MCP Apps card and app-only action |

An unknown value prevents startup. The default remains `elicitation`, so other
harnesses do not need to change their setup. To use elicitation in a custom
container configuration, explicitly set `TBANK_CONFIRMATION_MODE=elicitation`.
One process uses one mode; clients sharing the same tunnel must support that
mode. Do not run two stdio runtimes against one tunnel ID.

## Card flow

The initial payment tool performs the same preparation as before and pauses at
the same account/bank picker or money guard. It returns a **completed tool
response** containing a card, rather than MRTR `input_required`. No payment is
authorized by rendering the card.

The card displays the original server-generated confirmation message and any
required selection fields. Confirm/Cancel calls `confirmation_action`, a tool
with MCP Apps `visibility: ["app"]` and the ChatGPT private-visibility alias.
It is not available in the model's tool list. Its one-use random handle is only
in hidden result `_meta`, never in `content`, `structuredContent`, HTML, model
context updates or server logs. Tool visibility is not the authorization check:
the server also requires the unguessable handle, its original connection and
validated form data. The action accepts no replacement payment arguments.

The original suspended coroutine resumes. Recipient resolution, chosen accounts,
payment details and quotes are not reconstructed from model-generated arguments.
If another picker/guard is reached, the same card receives a new handle. A
consumed handle cannot authorize a second action. Expiry after five minutes,
process replacement, closing the connection, cancellation and invalid input do
not resume a debit. At most 32 tool coroutines can be pending per process.
The existing payment journal, threshold setting and duplicate-payment checks are
unchanged. A lost response after a payment must be reconciled through payment
status/operations; the UI never automatically retries an action.

## Deployment

Update both the Compose configuration and the prebuilt image when migrating an
existing installation. Updating only the image preserves the old local Compose
environment and therefore keeps elicitation:

```bash
cd ~/tbank
curl -fsSLO https://raw.githubusercontent.com/icyberdeveloper/tbank-mcp/master/compose.yaml
bash tbank update
```

Refresh the app in ChatGPT after the server starts. The tool catalog now links
confirming tools to a versioned `ui://` resource and includes an app-only action.
The HTML, CSS and official MCP Apps client SDK are bundled in the Python wheel
and Docker image. The resource travels through `resources/read` on the existing
OpenAI Tunnel; no public HTTP listener, CDN or external widget server is needed.
The resource CSP declares no external network/resource domains.

Call **only `elicitation_test`** to check the card without logging in, contacting
the bank or transferring money. In Apps mode its final payload reports
`transport: "mcp-apps"`. Check visible rendering and an explicit user click;
an action returned by a host is not, by itself, proof of a visible interface.
If a host cannot display the card, the operation remains pending and expires;
there is no automatic fallback that authorizes a payment.

## Development and checks

```bash
cd web
npm ci --ignore-scripts
npm run build
cd ..
python tests/test_mcp_apps.py
python tests/test_mcp_apps_stdio.py
```

Commit both the UI sources/lockfile and generated
`tbank_mcp/ui/confirmation.html`. Its content hash is part of the resource URI,
so UI changes invalidate the resource cache. CI rebuilds it and checks the file
matches, exercises fake-bank confirmation continuations, checks both transports
over actual stdio, and runs offline Chromium UI interactions in the image.

References: [MCP Apps](https://modelcontextprotocol.io/docs/extensions/apps),
[ChatGPT UI](https://developers.openai.com/plugins/build/chatgpt-ui),
[tool visibility and hidden metadata](https://developers.openai.com/plugins/reference).
