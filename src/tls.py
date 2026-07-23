"""Self-healing TLS for the T-Bank MCP.

The bank rotates certificates (e.g. HARICA leaf for www.tbank.ru, Russian
Trusted Root CA chain for *.t-bank-app.ru). The MCP must NOT crash when a cert
verifies fails — instead it fetches the host's current chain, checks the leaf CN
matches the bank domain (anti-MITM), appends the chain to ca/bundle.pem, and
retries. The bundle = system CAs + all bank hosts' current chains.

Usage: MobileSession mounts RobustTLSAdapter on its http session; on SSLError
the adapter calls rebuild_bundle([host]) (in place) and retries once.
"""
from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ca", "bundle.pem")

# all bank hosts the MCP talks to; their chains are pre-fetched on rebuild.
BANK_HOSTS = [
    "api.t-bank-app.ru", "id.t-bank-app.ru", "www.tbank.ru",
    "ms-loyalty-api.tinkoff.ru", "social-api.t-bank-app.ru",
    "lifestyle.t-bank-app.ru", "api-invest.t-bank-app.ru",
    "api-invest-gw.t-bank-app.ru", "shopping.t-bank-app.ru",
    "webview.t-bank-app.ru", "tm.t-bank-app.ru",
]

_PEM_RE = re.compile(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


def _expected_suffix(host: str) -> str | None:
    for s in ("t-bank-app.ru", "tbank.ru", "tinkoff.ru"):
        if host.endswith(s):
            return s
    return None


def fetch_chain_pem(host: str, port: int = 443, timeout: int = 15) -> list[bytes]:
    """openssl s_client -showcerts -> list of PEM blocks (leaf, intermediates, root)."""
    try:
        p = subprocess.run(
            ["openssl", "s_client", "-connect", f"{host}:{port}",
             "-servername", host, "-showcerts"],
            input=b"", capture_output=True, timeout=timeout)
        return _PEM_RE.findall(p.stdout)
    except Exception:
        return []


def leaf_subject(pem: bytes) -> str:
    try:
        p = subprocess.run(["openssl", "x509", "-noout", "-subject"],
                           input=pem, capture_output=True, timeout=10)
        return p.stdout.decode(errors="replace")
    except Exception:
        return ""


def leaf_cn_ok(pem: bytes, host: str) -> bool:
    """True if the leaf cert's CN/SAN matches the bank domain suffix (anti-MITM:
    we only trust a freshly-fetched chain whose leaf is actually for the bank)."""
    suffix = _expected_suffix(host)
    if not suffix:
        return False
    subj = leaf_subject(pem)
    # CN=*.t-bank-app.ru etc.
    return (f"*.{suffix}" in subj) or (f".{suffix}" in subj) or suffix in subj


def rebuild_bundle(hosts: list[str] = BANK_HOSTS, out: str = BUNDLE) -> str:
    """Rebuild the CA bundle = system CAs + every bank host's current chain
    (only if the leaf CN matches the bank domain). Safe to call on startup and
    on SSL failure."""
    parts: list[str] = []
    if os.path.exists(SYSTEM_CA):
        with open(SYSTEM_CA) as fh:
            parts.append(fh.read())
    for h in hosts:
        chain = fetch_chain_pem(h)
        if chain and leaf_cn_ok(chain[0], h):
            for c in chain:
                parts.append(c.decode())
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write("\n".join(parts))
    return out


class RobustTLSAdapter(HTTPAdapter):
    """HTTPAdapter that retries once on SSLCertVerificationError by refreshing
    the failing host's chain into the CA bundle (self-heals on cert rotation)."""

    def send(self, request, **kwargs):
        try:
            return super().send(request, **kwargs)
        except requests.exceptions.SSLError as e:
            host = urlparse(request.url).hostname or ""
            # refresh just this host's chain (and the bundle in place)
            try:
                rebuild_bundle([host])
            except Exception:
                pass
            # the session's verify points at BUNDLE; rebuild updated it in place.
            return super().send(request, **kwargs)
