# OpenAI Secure MCP Tunnel on a GCP VM

T-Bank MCP already uses stdio. OpenAI's Secure MCP Tunnel supports that transport
directly: the tunnel client launches `tbank-mcp` as its child process. This guide
adds no HTTP listener, changes no tool registration, and does not bypass any
payment confirmation. It is a deployment recipe, not an end-to-end certification
of a particular ChatGPT workspace or tunnel-client release.

## Before deployment

- Use a dedicated, persistent GCP Compute Engine VM and a non-root service user.
  The VM needs outbound connectivity to OpenAI and the bank API hosts; no public
  inbound MCP port is required. Do not expose the tunnel's management endpoints.
- Review the project's [Security](../README.md#security) and
  [Privacy](../PRIVACY.md) notes before moving banking access to a hosted machine.
  The VM now holds the bank session and local diagnostic files. Restrict its
  operators, disk/snapshot access and backups accordingly.
- Obtain tunnel access for the intended OpenAI Platform organization and
  permission to use a custom MCP server in the target ChatGPT workspace. Those
  are separate permissions. See the official
  [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
- Install and review T-Bank MCP following the [README](../README.md#quick-install).
  Install the tunnel client only from its
  [official distribution](https://github.com/openai/tunnel-client/releases),
  following the release's verification instructions. Record the versions used.
- Perform bank login yourself in the VM's trusted terminal with the installed
  `tbank-mcp-login`, under the same user that will run the server. Do not paste
  bank passwords, PINs, session JSON or tunnel API keys into chat, a repository,
  a shell command argument, or a support log. Keep the bank session file at 0600.

## Configure the existing stdio entry point

Use an existing authorized tunnel ID and a runtime key with minimum required
permissions, not an organization admin key. Supply `CONTROL_PLANE_API_KEY` through
an appropriately protected runtime environment; never embed its value in this
repository. The following commands contain only placeholders, not credentials.
Replace the ID and executable path with your actual non-secret values:

```bash
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile tbank \
  --tunnel-id YOUR_TUNNEL_ID \
  --mcp-command "/absolute/path/to/venv/bin/tbank-mcp"
tunnel-client doctor --profile tbank --explain
tunnel-client run --profile tbank
```

Check the installed CLI's help and generated profile before starting. Keep
`TBANK_CONFIRM_ABOVE=0` for the child process. If using `TBANK_SESSION`, it must
point to the session file on this VM, not a path on your laptop. Run the tunnel
under the same service user's home/config environment used during setup.

Run one active tunnel client for this stdio tunnel ID. Stop the old process before
starting its replacement; avoid autoscaling and overlapping rolling restarts.
Use a supervisor with persistent storage and verify readiness after restart.
Do not automatically retry interrupted money-moving calls.

The command pattern and stdio lifecycle requirements come from the upstream
[tunnel client guide](https://github.com/openai/tunnel-client/blob/master/docs/end-user-guide.md).

## Validate before allowing banking operations

1. Check `doctor` and runtime readiness. A ready process is not proof that MCP
   discovery has succeeded.
2. Add the tunnel-backed MCP server in the intended client using that client's
   current setup flow. Restrict access to the bank account owner; this server
   uses one local bank session, not a separate bank account per connector user.
3. Confirm MCP initialization and `tools/list`, then call `flows` for a
   non-banking smoke test. Compare discovery with the local stdio tool list.
4. Verify the target client's form-elicitation support separately with a harmless
   test server. A generic tool-approval prompt is not the server's payment form.
   Do not test confirmation delivery with a real transfer or purchase.

## What “all tools” does and does not mean

No tool is removed by this setup. Actual usability also depends on the client,
tunnel behavior, bank session and optional dependencies:

- **Payments:** preserve the default zero confirmation threshold. Money-moving
  tools need the server's confirmation flow, including MCP elicitation where
  required. A tunnel must carry the exchange and the client must render it.
  If this is unsupported, keep payments blocked; raising the threshold is not a
  compatibility fix. `grocery_checkout` requires elicitation even below a
  nonzero threshold. See [Security](../README.md#security).
- **Grocery checkout:** the VM needs Playwright Chromium and its system
  dependencies. The repository's default Docker image does not install Chromium.
- **Downloads:** receipt and travel-document paths refer to files on the VM.
  A tunnel does not turn them into client attachments or public download URLs.
- **Slow operations:** hosted deadlines can interrupt a checkout or confirmation.
  Inspect the bank/payment status before deciding whether to retry. Large
  responses can also exceed hosted payload limits. See the current
  [connector limits](https://github.com/openai/tunnel-client/blob/master/docs/connectors.md#hosted-service-limits).
- **Client policy:** connecting every tool does not authorize every action or
  override the host's financial-action restrictions.

## Data boundary

“Private” describes network reachability, not local-only processing. Tool
arguments and results traverse OpenAI's tunnel infrastructure and the consuming
product. Banking credentials must remain in the approved login/session flow;
do not expose them as tunnel configuration or tool arguments. Consult OpenAI's
[architecture and data-flow documentation](https://github.com/openai/tunnel-client/blob/master/docs/architecture.md)
and your hosting/product policies before enabling access.

No GCP resources, credentials or live tunnel are created by this documentation.
