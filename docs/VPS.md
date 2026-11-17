# Run on a VPS with OpenAI Secure MCP Tunnel

No builds are needed on the VPS: Compose downloads a prebuilt image with Chromium.
The server uses stdio and `openai/tunnel-client`, with no published ports.

## 1. Prepare access once

Use a Linux VPS with **Docker and Compose v2** installed. You can select a Docker
OS template in your hosting provider's panel or follow the
[official installation guide](https://docs.docker.com/engine/install/).
Run the commands below as a user with Docker access.
The prebuilt image targets Linux amd64 (standard Intel/AMD VPS instances).

Create a tunnel in
[OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels)
and copy its **Tunnel ID**. Create a
[runtime API key](https://platform.openai.com/settings/organization/api-keys)
with Tunnels Read + Use permissions. Your account and ChatGPT workspace need
Secure MCP Tunnel access.

Download just two small files:

```bash
mkdir -p ~/tbank && cd ~/tbank
curl -fsSLO https://raw.githubusercontent.com/icyberdeveloper/tbank-mcp/master/compose.yaml
curl -fsSLO https://raw.githubusercontent.com/icyberdeveloper/tbank-mcp/master/tbank
```

## 2. Set up and start with one command

```bash
bash tbank setup
```

The wizard:

1. Asks for the Tunnel ID and OpenAI key; key input is hidden.
2. Downloads the prebuilt Docker image.
3. Asks for your T-Bank phone number, then starts the regular SMS and password/PIN login.
4. Saves the bank session and starts the container in the background.

Enter the password and SMS code only in your own terminal, outside ChatGPT.
The OpenAI key is saved in a local `.env` file with mode 0600; it is not published.
The bank session is stored in the `tbank-mcp-data` Docker volume and survives
container upgrades. One container serves one bank account.

## 3. Connect ChatGPT

Wait for the service to report `healthy`:

```bash
bash tbank status
```

Select your tunnel in
[ChatGPT app settings](https://chatgpt.com/#settings/Connectors).
If your workspace's interface differs, follow the
[OpenAI onboarding guide](https://github.com/openai/tunnel-client/blob/v0.0.21/docs/onboarding.md).
Keep the container running. First check the bank session status and read access.
Payments require the client to support MCP elicitation confirmations;
the existing confirmation checks remain enabled.

## Everyday commands

```bash
docker compose up -d  # Start; the prebuilt image is downloaded automatically
bash tbank login     # Log in again; the wizard stops and restarts the server
bash tbank update    # Download the latest image and restart
bash tbank logs      # Show recent logs
bash tbank stop      # Stop
```

Run these commands from `~/tbank`. Do not run multiple instances for the same
session and Tunnel ID. `docker compose down` preserves the session; deleting the
`tbank-mcp-data` volume removes it. Stop the container before backing up the volume.
If a payment call is interrupted, check its status before retrying.

## For repository maintainers

The `Docker` workflow runs the tests, builds the image, launches Chromium, and
checks stdio MCP. After a successful push to `master`, it publishes the tested image:

- `ghcr.io/icyberdeveloper/tbank-mcp:tunnel` — used by Compose.
- `ghcr.io/icyberdeveloper/tbank-mcp:sha-<commit>` — a specific version.

After the first publication, open GitHub → Packages → tbank-mcp → Package settings
and set visibility to **Public** once so VPS instances can download the image
without `docker login`. This is a package setting, not a step on every VPS.

Local development builds are still available:

```bash
docker build --target tunnel -t tbank-mcp:tunnel .
pip install .
python tests/docker_smoke.py
```

The tunnel uses OpenAI runtime `v0.0.21` without the Cloudflare companion.
Live ChatGPT and bank checks require your Tunnel ID, key, and login; CI does not
use them.
