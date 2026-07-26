"""Trust must come from pinned roots, never from whoever answers the connection.

src/tls.py used to "self-heal" a TLS failure by running `openssl s_client` against
the failing host — with no verification — and appending whatever certificates came
back into the file used as `verify=`. The only gate was a substring match on the
leaf's Subject DN, a field the peer chooses. A machine-in-the-middle presenting a
self-signed certificate naming the bank got installed as a trust anchor and the
retry then succeeded.

These tests execute the trust decisions rather than inspecting the source:
a real TLS server with a hostile self-signed certificate is stood up and the client
is pointed at it; a shipped root is tampered with byte-for-byte and reloaded.

    python3 tests/test_tls_trust.py
"""
import http.server
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from src import tls  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def make_self_signed(dirpath, cn="*.t-bank-app.ru"):
    """A certificate an interceptor would present: self-signed, naming the bank.
    This is precisely what the old leaf_cn_ok() accepted as proof of authenticity."""
    key = os.path.join(dirpath, "k.pem")
    crt = os.path.join(dirpath, "c.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", crt, "-days", "1",
         "-subj", f"/C=RU/O=TBank/CN={cn}",
         "-addext", f"subjectAltName=DNS:{cn},DNS:localhost,IP:127.0.0.1"],
        capture_output=True, check=True)
    return key, crt


class Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def serve_tls(key, crt):
    srv = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def test_a_hostile_certificate_is_not_learned():
    """The security property, executed: after a TLS failure the peer's certificate
    must not end up in the trust bundle, and the request must still fail."""
    tmp = tempfile.mkdtemp()
    try:
        key, crt = make_self_signed(tmp)
        srv, port = serve_tls(key, crt)
        try:
            bundle = os.path.join(tmp, "bundle.pem")
            roots = os.path.join(tmp, "roots")
            os.makedirs(roots)
            shutil.copy(os.path.join(tls.ROOTS_DIR, "russian-trusted-root-ca.pem"), roots)
            saved_bundle, saved_roots = tls.BUNDLE, tls.ROOTS_DIR
            tls.BUNDLE, tls.ROOTS_DIR = bundle, roots
            try:
                tls.rebuild_bundle()
                before = open(bundle, encoding="utf-8").read()

                sess = requests.Session()
                sess.mount("https://", tls.RobustTLSAdapter())
                sess.verify = bundle
                raised = None
                try:
                    sess.get(f"https://127.0.0.1:{port}/", timeout=10)
                except requests.exceptions.SSLError as e:
                    raised = e
                except requests.exceptions.RequestException as e:
                    raised = e

                check(raised is not None,
                      "a self-signed certificate naming the bank was ACCEPTED")
                after = open(bundle, encoding="utf-8").read()
                hostile = open(crt, encoding="utf-8").read().strip()
                check(hostile not in after,
                      "the hostile certificate was written into the trust bundle — "
                      "this is the exact self-healing hole the rewrite removed")
                check(before == after,
                      "the trust bundle changed after talking to an untrusted peer")
                check(isinstance(raised, requests.exceptions.SSLError),
                      f"the failure must be a TLS failure, got {type(raised).__name__}")
                check("roots" in str(raised) and "intercepted" in str(raised),
                      f"the error must explain how to add a genuine root and that "
                      f"interception is the other explanation: {raised}")
            finally:
                tls.BUNDLE, tls.ROOTS_DIR = saved_bundle, saved_roots
        finally:
            srv.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  hostile peer: rejected, and its certificate never entered the bundle")


def test_a_tampered_shipped_root_is_refused():
    """The pin is the whole reason the roots can be committed. Flip bytes inside the
    certificate body and it must stop being trusted."""
    tmp = tempfile.mkdtemp()
    try:
        roots = os.path.join(tmp, "roots")
        os.makedirs(roots)
        name = "russian-trusted-root-ca.pem"
        good = open(os.path.join(tls.ROOTS_DIR, name), encoding="utf-8").read()
        shutil.copy(os.path.join(tls.ROOTS_DIR, name), os.path.join(roots, name))
        check(len(tls.load_roots(roots)) == 1, "the genuine shipped root must load")

        body = good.split("-----BEGIN CERTIFICATE-----")[1].split("-----END")[0]
        lines = [ln for ln in body.strip().splitlines() if ln]
        swapped = "A" if lines[1][0] != "A" else "B"
        tampered = good.replace(lines[1], swapped + lines[1][1:], 1)
        check(tampered != good, "the tamper did not change the file")
        open(os.path.join(roots, name), "w", encoding="utf-8").write(tampered)
        check(tls.load_roots(roots) == [],
              "a root whose SHA-256 no longer matches the pin was still trusted")

        # And a truncated / non-certificate file must be refused, not crash.
        open(os.path.join(roots, name), "w", encoding="utf-8").write("garbage")
        check(tls.load_roots(roots) == [], "a non-certificate file was trusted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  pinning: the shipped root loads, a byte-flipped or junk one is refused")


def test_a_pinned_file_cannot_smuggle_a_second_certificate():
    """A pin is one hash; a PEM file is a concatenation.

    Verifying only the first block means everything after it is trusted unexamined —
    append your own root behind the genuine one and the file still "matches its pin",
    which installs an attacker-chosen anchor. Same inversion as the old self-healing,
    moved from the network to the filesystem."""
    tmp = tempfile.mkdtemp()
    try:
        roots = os.path.join(tmp, "roots")
        os.makedirs(roots)
        name = "russian-trusted-root-ca.pem"
        good = open(os.path.join(tls.ROOTS_DIR, name), encoding="utf-8").read()
        _, crt = make_self_signed(tmp, cn="Attacker Root CA")
        hostile = open(crt, encoding="utf-8").read()

        path = os.path.join(roots, name)
        open(path, "w", encoding="utf-8").write(good.rstrip() + "\n" + hostile)
        blocks = tls.fingerprints(open(path, encoding="utf-8").read())
        # Without this the test would prove nothing: the genuine root must really be
        # first, so that a first-block-only check would have accepted the file.
        check(len(blocks) == 2 and blocks[0] == tls.PINNED_ROOTS[name],
              f"the genuine root must be the FIRST of two blocks, got {len(blocks)}")
        check(tls.load_roots(roots) == [],
              "a pinned file with a second certificate appended was trusted — the "
              "appended certificate rides in on the genuine root's pin")

        # And it must not reach the bundle by any other path.
        out = os.path.join(tmp, "bundle.pem")
        saved = tls.ROOTS_DIR
        tls.ROOTS_DIR = roots
        try:
            tls.rebuild_bundle(out=out)
        finally:
            tls.ROOTS_DIR = saved
        check(hostile.strip() not in open(out, encoding="utf-8").read(),
              "the smuggled certificate reached the CA bundle anyway")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  pinning: a certificate appended behind the pinned root is refused")


def test_an_operator_can_add_a_root_without_a_release():
    """Root rotation must never brick the MCP: a locally added root is honoured, so
    the fix is 'drop the PEM in' rather than 'wait for a release'."""
    tmp = tempfile.mkdtemp()
    try:
        roots = os.path.join(tmp, "roots")
        os.makedirs(roots)
        _, crt = make_self_signed(tmp, cn="Some New Root CA")
        shutil.copy(crt, os.path.join(roots, "future-root.pem"))
        loaded = tls.load_roots(roots)
        check(len(loaded) == 1,
              f"an unpinned, locally added root must be usable, got {len(loaded)}")

        # TBANK_EXTRA_CA is the same escape hatch without touching the repo.
        os.environ["TBANK_EXTRA_CA"] = crt
        try:
            check(len(tls.load_roots(os.path.join(tmp, "nonexistent"))) == 1,
                  "TBANK_EXTRA_CA was ignored")
        finally:
            os.environ.pop("TBANK_EXTRA_CA", None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  rotation: an operator-supplied root is honoured (repo dir or TBANK_EXTRA_CA)")


def test_bundle_contains_only_trusted_material():
    """The bundle must be system store + roots, and nothing that came off a wire."""
    tmp = tempfile.mkdtemp()
    try:
        out = os.path.join(tmp, "bundle.pem")
        tls.rebuild_bundle(out=out)
        text = open(out, encoding="utf-8").read()
        n = text.count("-----BEGIN CERTIFICATE-----")
        sys_ca = tls.system_ca_path()
        sys_n = open(sys_ca, encoding="utf-8", errors="replace").read().count(
            "-----BEGIN CERTIFICATE-----") if sys_ca else 0
        check(n == sys_n + len(tls.load_roots()),
              f"bundle has {n} certs, expected {sys_n} system + "
              f"{len(tls.load_roots())} roots — something extra got in")
        # A server leaf has no business being a trust anchor.
        check("CN=*.t-bank-app.ru" not in text and "CN = *.t-bank-app.ru" not in text,
              "a bank LEAF certificate is present as a trust anchor")
        check(tls.fingerprint(open(
            os.path.join(tls.ROOTS_DIR, "russian-trusted-root-ca.pem"),
            encoding="utf-8").read())
            == tls.PINNED_ROOTS["russian-trusted-root-ca.pem"],
            "the shipped root no longer matches its recorded pin")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  bundle: system store + pinned roots only, no leaf anchors")


def test_a_built_wheel_actually_carries_the_pinned_root():
    """Trust that ships only in a git checkout is not shipped.

    `packages = ["src"]` with no package-data built a wheel containing src/*.py and
    nothing else, while tls.py resolves ROOTS_DIR as `<src>/../ca/roots` — so an
    installed copy had no roots at all, every bank host failed verification, and
    tls.py:264 told the user the connection was being INTERCEPTED. A missing data
    file reported as an attack is the worst possible failure message, and only
    `pip install -e` hid it.

    This builds the real wheel and looks inside, because that is the only place the
    question is decided."""
    try:
        from setuptools import build_meta
    except ImportError:                                     # pragma: no cover
        print("  (setuptools build backend unavailable — wheel contents not checked)")
        return
    import contextlib
    import io
    import zipfile
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(root)
        # setuptools narrates every file it adds; that is its business, not this
        # suite's output.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            name = build_meta.build_wheel(tmp)
        names = zipfile.ZipFile(os.path.join(tmp, name)).namelist()
    except Exception as e:                                  # noqa: BLE001
        failures.append(f"the wheel could not be built, so what it ships is unknown: "
                        f"{type(e).__name__}: {e}")
        return
    finally:
        os.chdir(cwd)
        shutil.rmtree(os.path.join(root, "build"), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    for pinned in tls.PINNED_ROOTS:
        check(f"ca/roots/{pinned}" in names,
              f"the wheel ships no {pinned} — an install would fail every bank host "
              f"and blame interception. Wheel has: {sorted(names)[:8]}")
    check("docs/FLOWS.md" in names,
          "the wheel ships no docs/FLOWS.md — flows(), the documented discovery entry "
          "point, answers 'not found' on an installed copy")
    check(any(n.startswith("src/") and n.endswith(".py") for n in names),
          "the wheel ships no python at all")
    print(f"  packaging: the wheel carries {len(tls.PINNED_ROOTS)} pinned root(s) "
          f"and FLOWS.md, not just src/*.py")


def main():
    print("TLS trust:")
    test_a_tampered_shipped_root_is_refused()
    test_a_pinned_file_cannot_smuggle_a_second_certificate()
    test_an_operator_can_add_a_root_without_a_release()
    test_bundle_contains_only_trusted_material()
    test_a_built_wheel_actually_carries_the_pinned_root()
    test_a_hostile_certificate_is_not_learned()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
