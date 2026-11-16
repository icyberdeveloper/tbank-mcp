FROM ghcr.io/openai/tunnel-client-runtime:v0.0.21 AS tunnel-runtime

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/tbank PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app
# Allowlist: captures, sessions, .env and local notes never enter the image.
COPY pyproject.toml README.md LICENSE ./
COPY tbank_mcp ./tbank_mcp
RUN pip install --no-cache-dir . \
    && apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 tbank \
    && mkdir -p /home/tbank/.local/share/tbank-mcp \
    && chown -R tbank:tbank /home/tbank
USER tbank
# stdio remains the default; Compose selects the OpenAI tunnel runtime.
ENTRYPOINT ["tbank-mcp"]

# The runtime flavor contains no Cloudflare companion. It owns the MCP child.
FROM base AS tunnel
COPY --from=tunnel-runtime /usr/bin/tunnel-client-runtime /usr/local/bin/tunnel-client-runtime
ENTRYPOINT ["tunnel-client-runtime", "run"]

# Keep the catalog/default build compatible with existing stdio clients.
FROM base AS stdio
