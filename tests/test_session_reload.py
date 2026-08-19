"""_require() must not serve a session older than the file on disk.

The bug this pins (observed live): the server caches the session in memory forever,
so a re-login in another process (login_cli.py) rewrites session.json but never
reaches the running server — every tool keeps failing on the stale copy until an MCP
reconnect that nothing tells the user to do. The fix (ported from the myt server's
_require_myt) re-stats the file each call and reloads when it is newer.

Contract, executed against the real _require / _save_session / _load_session:
  * a newer file on disk is picked up;
  * an unchanged mtime serves the cached session (no reload);
  * an unreadable/corrupt file must NOT destroy a working in-memory session;
  * the server's OWN save must not later read as a foreign re-login;
  * login()'s fresh blank session is not clobbered by the old file before confirm.

    python3 tests/test_session_reload.py
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tbank-reload-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_SESSION"] = os.path.join(_TMP, "session.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import server                                        # noqa: E402
from src.client import TbankApiError                          # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def _write_file(sid, mtime):
    """Simulate an EXTERNAL write (login_cli.py), bypassing the server's _save_session
    so the test controls the mtime itself."""
    s = server._blank_session()
    s.mobile_sessionid = sid
    d = {k: v for k, v in s.__dict__.items()
         if not k.startswith("_") or k == "_minted_at"}
    with open(server._SESSION_FILE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.utime(server._SESSION_FILE, (mtime, mtime))


def _fresh():
    server._session = None
    server._session_mtime = None


def check_newer_file_is_reloaded():
    _fresh()
    _write_file("SID-A", 1000.0)
    s1 = server._require()
    check(s1.mobile_sessionid == "SID-A", f"first load must read the file: {s1.mobile_sessionid!r}")

    # An external re-login writes a newer file.
    _write_file("SID-B", 2000.0)
    s2 = server._require()
    check(s2.mobile_sessionid == "SID-B",
          f"a newer file must be reloaded, not the cached SID-A: {s2.mobile_sessionid!r}")
    print("  newer file on disk is picked up (the live bug)")


def check_unchanged_mtime_serves_the_cache():
    _fresh()
    _write_file("SID-A", 1000.0)
    a = server._require()
    b = server._require()                    # nothing changed on disk
    check(a is b, "an unchanged mtime must serve the same cached object, not reload")
    print("  unchanged mtime → cached session, no needless reload")


def check_unreadable_file_keeps_the_working_session():
    _fresh()
    _write_file("SID-A", 1000.0)
    good = server._require()
    check(good.mobile_sessionid == "SID-A", "sanity: working session loaded")

    # A newer but corrupt file (a mid-write, a truncated JSON).
    with open(server._SESSION_FILE, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    os.utime(server._SESSION_FILE, (3000.0, 3000.0))
    kept = server._require()
    check(kept is good and kept.mobile_sessionid == "SID-A",
          "an unreadable newer file must NOT destroy the working session")
    print("  corrupt/half-written file keeps the working in-memory session")


def check_own_save_is_not_a_foreign_relogin():
    _fresh()
    _write_file("SID-A", 1000.0)
    s = server._require()
    # The server re-mints its token and saves through the real path.
    s.mobile_sessionid = "SID-A2"
    ok = server._save_session(s)
    check(ok, "the save itself must succeed")
    after = server._require()
    check(after is s and after.mobile_sessionid == "SID-A2",
          "the server's own write must not trigger a reload over itself")
    print("  the server's own save is not seen as someone else's re-login")


def check_login_blank_session_survives_the_old_file():
    """login() must pin the mtime so _require does not reload the OLD valid session
    over the fresh blank one before confirm_otp saves the new credentials."""
    class FakeBlank:
        def __init__(self):
            self.mobile_sessionid = ""
            self._on_persist = None

        def login(self, phone):
            return "Следующий шаг — otp."

    _fresh()
    _write_file("SID-OLD", 5000.0)          # an old, valid session sits on disk
    saved = server._blank_session
    server._blank_session = lambda: FakeBlank()
    try:
        out = server.login("+79991234567")  # sets _session = blank, pins the mtime
        check("otp" in out, f"login must return the next-step hint: {out}")
        raised = False
        try:
            server._require()               # blank has an empty sessionid
        except TbankApiError as e:
            raised = e.result_code == "NO_SESSION"
        check(raised, "the blank login session must NOT be replaced by the old file "
                      "(a reload would return SID-OLD and skip login)")
    finally:
        server._blank_session = saved
    print("  login: the fresh blank session is not clobbered by the old file")


def main():
    for fn in (check_newer_file_is_reloaded,
               check_unchanged_mtime_serves_the_cache,
               check_unreadable_file_keeps_the_working_session,
               check_own_save_is_not_a_foreign_relogin,
               check_login_blank_session_survives_the_old_file):
        print(f"{fn.__name__}:")
        fn()
    if failures:
        print(f"\n{len(failures)} FAILED")
        return 1
    print("\nall session-reload tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
