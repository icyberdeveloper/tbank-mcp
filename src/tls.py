"""CA trust for the T-Bank MCP.

The bank's hosts split in two, by suffix rather than by count:
  * `*.t-bank-app.ru` chains to the **Russian Trusted Root CA** (Минцифры), which no
    Linux/macOS trust store ships. Every one of them fails without it.
  * `*.tbank.ru`, `*.tinkoff.ru`, `api.tinsurance.ru` chain to publicly-trusted roots
    already in the system store (they were HARICA, they are TrustAsia now — the CA
    changed under us and nothing needed to be done, which is the point).

So the bundle is: **system CAs + the pinned Russian root**, and nothing else, which
passes every host in `BANK_HOSTS`. That list is derived from the endpoint table
rather than typed out — see bank_hosts() for what the typed-out version cost.

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

# Hosts reached outside the endpoint table: search posts straight through _http.
EXTRA_HOSTS = ["search.t-bank-app.ru"]


def bank_hosts() -> list[str]:
    """Every bank host the MCP talks to, DERIVED from the endpoint table.

    It used to be a hand-written list, and it drifted: it named 18 hosts while the
    code was calling 22, so the four newest (hotels, insurance, cx-evolution and
    search) were outside everything that walks this list — including the
    connectivity check that is supposed to prove the trust store is complete. A
    list that silently omits what it is meant to cover is worse than no list.
    Nothing is fetched from these at runtime; this is the set to TEST against."""
    from .endpoints import BUILTIN_ENDPOINTS
    hosts = {(ep.get("host") or "").split("://")[-1].strip("/")
             for ep in BUILTIN_ENDPOINTS.values() if isinstance(ep, dict)}
    return sorted(hosts.union(EXTRA_HOSTS) - {""})


BANK_HOSTS = bank_hosts()

_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.S)


class UntrustedRoot(RuntimeError):
    """A shipped root certificate does not match its pin."""


def _log(msg: str) -> None:
    print(f"[tbank-tls] {msg}", file=sys.stderr, flush=True)


def fingerprints(pem_text: str) -> list[str]:
    """SHA-256 over the DER of EVERY certificate in the text, in file order — i.e.
    the values `openssl x509 -fingerprint -sha256` prints. Pure Python — no openssl
    process, so this works on a machine that has none, which the old code could not."""
    blocks = _PEM_RE.findall(pem_text)
    if not blocks:
        raise UntrustedRoot("not a PEM certificate")
    out = []
    for body in blocks:
        try:
            der = base64.b64decode("".join(body.split()))
        except (binascii.Error, ValueError) as e:
            raise UntrustedRoot(f"malformed base64 in PEM: {e}") from e
        out.append(hashlib.sha256(der).hexdigest())
    return out


def fingerprint(pem_text: str) -> str:
    """The fingerprint of a file that holds exactly ONE certificate.

    More than one is refused rather than partially verified. A PEM file is a
    concatenation, and a pin is a single hash: a check that looks only at the first
    block trusts every later block unexamined. Appending a root of your choosing
    after a genuine one would then produce a file that "matches its pin" and installs
    an attacker-chosen anchor — the same inversion this module was rewritten to
    remove, just moved from the network to the filesystem."""
    got = fingerprints(pem_text)
    if len(got) != 1:
        raise UntrustedRoot(
            f"expected one certificate, found {len(got)} — a pin covers one "
            f"certificate and the rest would be trusted unverified")
    return got[0]


def system_ca_path() -> str | None:
    for p in SYSTEM_CA_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def load_roots(roots_dir: str | None = None) -> list[str]:
    """PEM text of every trusted extra root.

    A file listed in PINNED_ROOTS must hold exactly one certificate and it must match
    the pin, or the whole file is refused — a tampered or swapped root is exactly the
    thing this module exists to prevent, and so is one smuggled in behind a genuine
    one (see fingerprint() for why one-block-only is not enough). Files not in
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
            got = fingerprints(text)
        except (OSError, UnicodeDecodeError, UntrustedRoot) as e:
            # UnicodeDecodeError is a ValueError, not an OSError, so it slipped this
            # clause and crashed the whole login from codecs. A binary file with a
            # .pem name — a macOS AppleDouble `._root.pem` in a zip is the usual one —
            # is a bad certificate, not a fatal error: REFUSE it and read the rest.
            _log(f"REFUSED {name}: unreadable or not a certificate ({e})")
            continue
        expected = PINNED_ROOTS.get(name)
        if expected:
            # The whole file is trusted or none of it is: everything after the first
            # block would otherwise ride in on the first block's pin.
            if len(got) != 1:
                _log(f"REFUSED {name}: it holds {len(got)} certificates but a pin "
                     f"covers one. The extras would be trusted without ever being "
                     f"checked. Split them into separate files and pin each.")
                continue
            if got[0] != expected:
                _log(f"REFUSED {name}: SHA-256 {got[0]} does not match the pin "
                     f"{expected}. This file is NOT trusted. If the bank genuinely "
                     f"rotated its root, update PINNED_ROOTS in src/tls.py "
                     f"deliberately.")
                continue
        else:
            _log(f"trusting {len(got)} unpinned certificate(s) in {name} "
                 f"({', '.join(f'{f[:16]}…' for f in got)}) — added locally, not "
                 f"shipped with this repo")
        out.append(text)

    extra = os.environ.get("TBANK_EXTRA_CA", "")
    for path in [p for p in extra.split(os.pathsep) if p]:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            # Named explicitly by the operator, so it is trusted without a pin — but
            # every certificate in it is announced, because "I pointed at one root"
            # and "the file contained one root" are different statements.
            got = fingerprints(text)
        except (OSError, UnicodeDecodeError, UntrustedRoot) as e:
            # Same as above: a human typed this path, and a non-text file (wrong file,
            # wrong encoding) must be reported and skipped, not crash the process.
            _log(f"TBANK_EXTRA_CA {path}: {e}")
            continue
        _log(f"trusting {len(got)} certificate(s) from TBANK_EXTRA_CA {path} "
             f"({', '.join(f'{f[:16]}…' for f in got)})")
        out.append(text)
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
