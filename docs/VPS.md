# Linux VPS → OpenAI Secure MCP Tunnel → ChatGPT

The container runs OpenAI's `tunnel-client-runtime` and starts `tbank-mcp` as
its stdio child. There is no HTTP MCP listener, published port, domain, reverse
proxy, or Cloudflare dependency. The runtime makes outbound HTTPS connections to
OpenAI; the MCP also needs outbound access to the bank. One deployment serves one
bank account. Run only one instance per tunnel ID and session volume.

References: [OpenAI tunnel-client](https://github.com/openai/tunnel-client),
[stdio configuration](https://github.com/openai/tunnel-client/blob/v0.0.21/docs/configuration.md#stdio-mcp-command),
[OpenAI tunnel setup](https://platform.openai.com/settings/organization/tunnels).
The runtime image is pinned to `v0.0.21`, without the Cloudflare companion.

## 1. Prepare the VPS

Install Git, Docker Engine and the Docker Compose v2 plugin using the
[Docker Linux installation guide](https://docs.docker.com/engine/install/).
The commands below assume your user can run Docker.

```bash
git clone https://github.com/icyberdeveloper/tbank-mcp.git
cd tbank-mcp
cp .env.example .env
```

Create a tunnel in OpenAI Platform's Organization → Tunnels settings. Set
`CONTROL_PLANE_TUNNEL_ID` in `.env`. Create a runtime API key for a principal with
Tunnels Read + Use; use a runtime key, not an organization admin key. Your account
and ChatGPT workspace must have Secure MCP Tunnel access.

Store that key directly on the VPS, outside shell history and chat:

```bash
mkdir -p secrets
chmod 700 secrets
read -rsp 'OpenAI runtime API key: ' runtime_key; echo
(umask 077; printf '%s' "$runtime_key" > secrets/openai_runtime_key)
unset runtime_key
sudo chown 10001:10001 secrets/openai_runtime_key
sudo chmod 400 secrets/openai_runtime_key
```

Compose mounts the file read-only; the image runs as UID 10001. `.env` and
`secrets/` are excluded from git and the build context. Run Compose from this
checkout so the relative secret path resolves correctly.

## 2. Build and log in to the bank

```bash
docker compose build
# Create the persistent volume from the image so ownership is initialized.
docker run --rm -it \
  --mount source=tbank-mcp-data,target=/home/tbank/.local/share/tbank-mcp \
  --entrypoint tbank-mcp-login tbank-mcp:tunnel +7XXXXXXXXXX
```

Enter SMS/password/PIN in your own terminal. They do not pass through ChatGPT.
The login writes `session.json` inside the volume with mode 0600. The same volume
holds the regenerated CA bundle, diagnostics and downloaded files. It survives
container replacement. The image contains the public pinned bank CA roots.

Chromium and its OS dependencies are installed by default in Compose for grocery
checkout. Set `INSTALL_CHROMIUM=0` in `.env` before building for a smaller image;
grocery checkout then needs a rebuild with Chromium enabled.

## 3. Start and connect ChatGPT

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 bank
# The runtime's loopback-only readiness endpoint, inside the container:
docker compose exec bank python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read().decode())"
```

Wait for the service to become healthy, then add/select this tunnel in
[ChatGPT connector settings](https://chatgpt.com/#settings/Connectors). Follow the
[OpenAI onboarding guide](https://github.com/openai/tunnel-client/blob/v0.0.21/docs/onboarding.md)
for your workspace's tunnel connector flow. Keep the container running during
discovery and use. First verify tool discovery and ask for the flows reference;
then check session status before using banking tools.

Payment confirmation checks are unchanged. A client that does not advertise the
required MCP elicitation capability is refused according to the existing payment
policy; do not raise the confirmation threshold to work around missing support.
A successful tunnel connection alone does not prove payment UI compatibility.

## Operations

```bash
# Stop before re-login, backup, or replacement; never run two bank sessions.
docker compose down
# Re-login using the docker run command above, then:
docker compose up -d
# Upgrade source and rebuild (after reviewing the update):
git pull --ff-only
docker compose build
docker compose up -d
```

The `unless-stopped` policy restarts the service after process failure and Docker
restart. Stdio child failure exits the runtime so Docker replaces the whole pair.
An unhealthy status alone does not trigger a Docker restart; inspect logs before
restarting. Interrupted money calls must be reconciled, never blindly replayed.
Keep encrypted backups of `tbank-mcp-data` while the service is stopped. Removing
that volume deletes the saved session; `docker compose down` preserves it.

## Build validation

The Docker workflow builds both browser variants and tests real container stdio
initialization, tool discovery and an offline flows call. It does not log in or
call the bank. To repeat it locally after building:

```bash
pip install .
python tests/docker_smoke.py
```

The ordinary `docker build -t tbank-mcp .` still produces the stdio image without
the tunnel. `docker build --target tunnel -t tbank-mcp:tunnel .` builds the tunnel
variant; Compose also enables Chromium unless configured otherwise.
