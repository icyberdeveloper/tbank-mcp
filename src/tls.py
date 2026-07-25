"""CA trust for the T-Bank MCP.

The bank's hosts split in two:
  * `*.t-bank-app.ru` chains to the **Russian Trusted Root CA** (Минцифры), which no
    Linux/macOS trust store ships. 13 of the 18 hosts fail without it.
  * `www.tbank.ru`, `*.tinkoff.ru`, `api.tbank.ru` chain to publicly-trusted roots
    already in the system store (they were HARICA, they are TrustAsia now — the CA
    changed under us and nothing needed to be done, which is the point).

So the bundle is: **system CAs + the pinned Russian root**, and nothing else.
Measured against all 18 hosts: system store alone fails 13; system store + this one
root passes 18/18.

WHY THIS FILE WAS REWRITTEN (audit, 2026-07-25). It used to "self-heal": on any TLS
failure it ran `openssl s_client` against the failing host — with no verification —
and appended whatever certificates came back into the file used as `verify=`. The
only gate was a substring match on the leaf's Subject DN, a field supplied by
whoever answered the connection. So any machine-in-the-middle presenting a
self-signed certificate with `*.t-bank-app.ru` in its subject got itself installed
as a trust anchor, and the retry then succeeded. TLS verification was not weakened,
it was *inverted*: the attacker chose the trust store. That is gone. Trust now comes
only from the system store and from root certificates committed to this repo and
pinned by SHA-256 — never from the network.

ROTATION, WITHOUT BREAKING ANYTHING. Pinning a *root* is what makes leaf and
intermediate rotation a non-event: the bank can (and does) reissue those whenever it
likes and verification keeps working, because the anchor never moved. The old code
re-fetched leaves on every failure precisely because it trusted no root — it was
solving a problem it had created. Roots are long-lived (this one runs to 2032). If a
root ever IS replaced, drop the new PEM into `ca/roots/` or point `TBANK_EXTRA_CA`
at it and it is picked up on the next start, no code change and no release needed;
the error you get until then names the file to add rather than failing obscurely.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import sys

import requests
from requests.adapters import HTTPAdapter

_HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
# Some distros/macOS put the system store elsewhere; first hit wins.
SYSTEM_CA_CANDIDATES = [
    SYSTEM_CA,
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
]
ROOTS_DIR = os.path.join(_HERE, "..", "ca", "roots")
BUNDLE = os.path.join(_HERE, "..", "ca", "bundle.pem")

# Roots committed to this repo, pinned by SHA-256 of their DER. A file whose
# fingerprint does not match is NOT trusted — that is the whole point of shipping
# them rather than fetching them.
PINNED_ROOTS = {
    # C=RU, O=The Ministry of Digital Development and Communications,
    # CN=Russian Trusted Root CA — self-signed, notAfter 2032-02-27.
    "russian-trusted-root-ca.pem":
        "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31",
}

# Every bank host the MCP talks to. Kept as the list the connectivity test walks;
# nothing is fetched from them at runtime any more.
BANK_HOSTS = [
    "api.t-bank-app.ru", "id.t-bank-app.ru", "www.tbank.ru",
    "ms-loyalty-api.tinkoff.ru", "social-api.t-bank-app.ru",
    "lifestyle.t-bank-app.ru", "api-invest.t-bank-app.ru",
    "api-invest-gw.t-bank-app.ru", "shopping.t-bank-app.ru",
    "webview.t-bank-app.ru", "tm.t-bank-app.ru",
    "api.tbank.ru", "csc.tbank.ru", "my-home.tinkoff.ru",
    "myauto.t-bank-app.ru", "shortcuts.t-bank-app.ru",
    "push-history-api.t-bank-app.ru", "api-common-gw.t-bank-app.ru",
]

_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.S)


class UntrustedRoot(RuntimeError):
    """A shipped root certificate does not match its pin."""


def _log(msg: str) -> None:
    print(f"[tbank-tls] {msg}", file=sys.stderr, flush=True)


def fingerprint(pem_text: str) -> str:
    """SHA-256 over the DER, i.e. the same value `openssl x509 -fingerprint -sha256`
    prints. Pure Python — no openssl process, so this works on a machine that has
    none, which the old code could not."""
    m = _PEM_RE.search(pem_text)
    if not m:
        raise UntrustedRoot("not a PEM certificate")
    try:
        der = base64.b64decode("".join(m.group(1).split()))
    except (binascii.Error, ValueError) as e:
        raise UntrustedRoot(f"malformed base64 in PEM: {e}") from e
    return hashlib.sha256(der).hexdigest()


def system_ca_path() -> str | None:
    for p in SYSTEM_CA_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def load_roots(roots_dir: str | None = None) -> list[str]:
    """PEM text of every trusted extra root.

    A file listed in PINNED_ROOTS must match its pin or it is refused — a tampered
    or swapped root is exactly the thing this module exists to prevent. Files not in
    PINNED_ROOTS are the user's own escape hatch for a future root rotation; they are
    accepted and announced, because refusing them would mean a root change bricks the
    MCP until someone ships a release."""
    # Resolved at CALL time, not baked into a default argument: the module globals
    # are the configuration point, and a default evaluated at def time silently
    # ignores anything set afterwards.
    roots_dir = roots_dir if roots_dir is not None else ROOTS_DIR
    out: list[str] = []
    for name in sorted(os.listdir(roots_dir) if os.path.isdir(roots_dir) else []):
        if not name.endswith((".pem", ".crt", ".cer")):
            continue
        path = os.path.join(roots_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            got = fingerprint(text)
        except (OSError, UntrustedRoot) as e:
            _log(f"REFUSED {name}: unreadable or not a certificate ({e})")
            continue
        expected = PINNED_ROOTS.get(name)
        if expected and got != expected:
            _log(f"REFUSED {name}: SHA-256 {got} does not match the pin {expected}. "
                 f"This file is NOT trusted. If the bank genuinely rotated its root, "
                 f"update PINNED_ROOTS in src/tls.py deliberately.")
            continue
        if not expected:
            _log(f"trusting unpinned root {name} ({got[:16]}…) — added locally, "
                 f"not shipped with this repo")
        out.append(text)

    extra = os.environ.get("TBANK_EXTRA_CA", "")
    for path in [p for p in extra.split(os.pathsep) if p]:
        try:
            with open(path, encoding="utf-8") as fh:
                out.append(fh.read())
            _log(f"trusting TBANK_EXTRA_CA root {path}")
        except OSError as e:
            _log(f"TBANK_EXTRA_CA {path}: {e}")
    return out


def rebuild_bundle(hosts=None, out: str | None = None) -> str:
    """Write the CA bundle: system store + pinned roots. No network, no subprocess.

    `hosts` is accepted and ignored — the old signature took the hosts to go and
    fetch certificates from, which is the behaviour that was removed."""
    out = out if out is not None else BUNDLE
    parts: list[str] = []
    sys_ca = system_ca_path()
    if sys_ca:
        with open(sys_ca, encoding="utf-8", errors="replace") as fh:
            parts.append(fh.read())
    else:
        _log("no system CA store found — verification will rely on ca/roots/ alone")
    roots = load_roots()
    parts.extend(roots)
    if not roots:
        _log(f"no extra roots loaded from {ROOTS_DIR} — *.t-bank-app.ru will FAIL to "
             f"verify (its root is not in any system store)")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    return out


class RobustTLSAdapter(HTTPAdapter):
    """Retries once on a TLS failure by REBUILDING the bundle from trusted material.

    This is not the old self-healing: nothing is learned from the peer. It recovers
    the one failure that actually happened in practice — `ca/bundle.pem` missing or
    truncated (it is a generated file, gitignored, and a fresh clone has none; see
    6f7274d) — and then re-raises with an explanation instead of retrying forever."""

    # Methods that are safe to send twice. A TLS error usually means the handshake
    # failed and nothing was transmitted — but `requests` also raises SSLError on a
    # mid-stream read, after the body has gone out. Replaying a POST there would
    # repeat /v1/pay or a ticket payment, so those are rebuilt-and-re-raised instead:
    # the caller decides, and the next call finds a healthy bundle either way.
    _REPLAYABLE = {"GET", "HEAD", "OPTIONS"}

    def send(self, request, **kwargs):
        try:
            return super().send(request, **kwargs)
        except requests.exceptions.SSLError:
            try:
                rebuild_bundle()
            except Exception as e:                          # noqa: BLE001
                _log(f"bundle rebuild failed: {e}")
                raise
            if (request.method or "").upper() not in self._REPLAYABLE:
                _log(f"CA bundle rebuilt, but not retrying a {request.method} — "
                     f"it may already have reached the server")
                raise
            try:
                return super().send(request, **kwargs)
            except requests.exceptions.SSLError as e:
                raise requests.exceptions.SSLError(
                    f"{e}\n[tbank-tls] Certificate verification failed against the "
                    f"system store plus the pinned roots in {os.path.abspath(ROOTS_DIR)}. "
                    f"This is NOT worked around by trusting the server's own "
                    f"certificate. If the bank rotated its root CA, add the new root "
                    f"PEM to that directory (or set TBANK_EXTRA_CA) and retry; "
                    f"otherwise the connection is being intercepted."
                ) from e
