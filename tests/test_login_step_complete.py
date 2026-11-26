"""The login must finish an auth that ends in `step: complete`.

Observed live (2026-09-06, first login on a new device): otp accepted, password
accepted, and the bank answered the password step not with a code but with
{"action": "step", "step": "complete", ...}. Two things then went wrong:

  * confirm_step() knew no such step, so it raised NEXT_STEP 'complete' — the
    auth was alive and one hop from done, but nothing sent that hop;
  * the login CLI matched the substring "pin" against the generic hint, whose
    text names «confirm_otp / confirm_pin» for ANY unknown step, and sent a PIN
    the bank had not asked for: invalid_request, SMS spent.

The hop is a bare POST `step=complete` to the same cid — no value, no chained
token — and its reply carries {code, session_state}. That is the request
neolegoff_bank's auth_complete() sends, and the one pinned here.

Contract, executed against the real confirm_step() over a scripted transport:
  * password → complete → code: the hop is sent bare, the code is exchanged, the
    session is minted (three POSTs, in that order);
  * password → complete → another step: NEXT_STEP names THAT step, no PIN, no
    token exchange;
  * the complete hop's own error is reported as the bank's error;
  * a password answer that carries the code directly sends NO extra hop;
  * the CLI reads the step from the hint's head: the unknown-step hint is not
    a PIN request.

Also here, the `totp` step (2026-11-26, an account with «код из приложения-
аутентификатора» on): step=phone answers `step: totp`, not `otp`, and no SMS
is sent. The bank takes the code as `totpCode` — `totp=<code>` is a 400
invalid_request — and then asks for the password. Pinned:
  * confirm_step('totp') sends `step=totp&totpCode=<code>`;
  * the CLI asks for the authenticator code, not an SMS, when login() names
    totp, and then goes on to the password.

    python3 tests/test_login_step_complete.py
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tbank-complete-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_SESSION"] = os.path.join(_TMP, "session.json")
os.environ["TBANK_CA_BUNDLE"] = os.path.join(_TMP, "bundle.pem")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tbank_mcp import client as C                              # noqa: E402
from tbank_mcp.client import MobileSession, TbankApiError      # noqa: E402
from tbank_mcp import login_cli                                # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Http:
    """Answers POSTs from a script, in order, and records each one."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.cookies = []

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "data": data})
        if not self.answers:
            raise AssertionError(f"unexpected POST #{len(self.calls)}: {url}")
        return _Resp(self.answers.pop(0))


_TOKEN = {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 7199,
          "mobile": {"sessionid": "SID-1"}}


def _session(answers):
    s = MobileSession(mobile_sessionid="", refresh_token="", client_id="gorod-app")
    s._http = _Http(answers)
    s._login_cid = "CID-1"
    s._login_token = "TOK-otp"
    return s


def check_complete_hop_is_sent_bare_and_code_exchanged():
    s = _session([
        {"action": "step", "step": "complete", "cid": "CID-1", "step_back_allowed": False},
        {"code": "CODE-1", "session_state": "ss-1"},
        _TOKEN,
    ])
    try:
        s.confirm_step("password", "pw")
    except TbankApiError as e:
        check(False, f"complete must be finished, not raised: {e}")
        return
    calls = s._http.calls
    check(len(calls) == 3, f"password, complete, token — three POSTs, got {len(calls)}")
    if len(calls) < 3:
        return
    check("auth/step?cid=CID-1" in calls[1]["url"],
          "the complete hop goes to auth/step on the SAME cid")
    check(calls[1]["data"] == "step=complete",
          f"the hop is bare: 'step=complete', got {calls[1]['data']!r} "
          f"(no value, no chained token — the shape neolegoff_bank proves)")
    check(calls[2]["url"].endswith("auth/token/mobile?ccc=true&cpswc=true"),
          "the code from the complete reply is exchanged at auth/token/mobile")
    check("code=CODE-1" in calls[2]["data"], "the exchanged code is the one the hop returned")
    check(s.access_token == "AT-1" and s.mobile_sessionid == "SID-1",
          "the session is minted from the token answer")
    check(s._login_cid == "" and s._login_token == "", "login state is cleared after minting")
    print("  password → complete → code → session: the hop is sent bare, once")


def check_complete_followed_by_another_step_names_it():
    s = _session([
        {"action": "step", "step": "complete", "cid": "CID-1"},
        {"action": "step", "step": "card", "cid": "CID-1"},
    ])
    try:
        s.confirm_step("password", "pw")
        check(False, "a step after complete must surface as NEXT_STEP")
    except TbankApiError as e:
        check(e.result_code == "NEXT_STEP", f"expected NEXT_STEP, got {e.result_code}")
        check("card" in str(e.message), f"the hint must name the bank's step: {e.message}")
    check(len(s._http.calls) == 2, "no token exchange and no guessed step after that")
    print("  complete → another step: NEXT_STEP names it, nothing else is sent")


def check_complete_hop_error_is_the_banks_error():
    s = _session([
        {"action": "step", "step": "complete", "cid": "CID-1"},
        {"error": "invalid_request", "cid": "CID-1", "action": "step", "step": "complete"},
    ])
    try:
        s.confirm_step("password", "pw")
        check(False, "an error on the complete hop must raise")
    except TbankApiError as e:
        check(e.result_code == "invalid_request",
              f"the bank's error code is kept, got {e.result_code}")
    check(len(s._http.calls) == 2, "no token exchange after an error")
    print("  complete hop error: reported as the bank's error, nothing exchanged")


def check_direct_code_sends_no_extra_hop():
    s = _session([
        {"code": "CODE-2"},
        _TOKEN,
    ])
    s.confirm_step("password", "pw")
    calls = s._http.calls
    check(len(calls) == 2, f"code in the password answer: password + token only, got {len(calls)}")
    check(all("step=complete" not in (c["data"] or "") for c in calls),
          "no complete hop when the code came directly (the capture's flow is unchanged)")
    print("  direct code: the old two-POST flow is byte-for-byte what it was")


def check_cli_reads_the_step_from_the_hint_head():
    def err(step):
        return TbankApiError("NEXT_STEP", C._next_step_hint({"step": step, "cid": "x"}))

    check(login_cli._next_step(err("password")) == "password", "password hint → password")
    check(login_cli._next_step(err("pin")) == "pin", "pin hint → pin")
    check(login_cli._next_step(err("otp")) == "otp", "otp hint → otp")
    check(login_cli._next_step(err("totp")) == "totp", "totp hint → totp")
    # The regression: the unknown-step hint says «confirm_otp / confirm_pin» and
    # used to read as a PIN request by substring.
    check(login_cli._next_step(err("complete")) == "complete",
          "the generic hint for 'complete' is NOT a PIN request")
    check("pin" in C._next_step_hint({"step": "complete"}).lower(),
          "(precondition) the generic hint really does contain the word pin")
    check(login_cli._next_step(TbankApiError("invalid_request", "pin something")) == "",
          "a non-NEXT_STEP error names no step, whatever its text says")
    print("  CLI: the step comes from the hint's head, not from a substring")


def check_totp_is_sent_as_totp_code():
    s = _session([{"action": "step", "step": "password", "cid": "CID-1"}])
    s._login_token = ""          # the totp answer to step=phone carries no token
    try:
        s.confirm_step("totp", "123456")
        check(False, "the password after totp must surface as NEXT_STEP")
    except TbankApiError as e:
        check(login_cli._next_step(e) == "password", f"totp → password, got {e.message}")
    check(s._http.calls[0]["data"] == "step=totp&totpCode=123456",
          f"the field is totpCode, got {s._http.calls[0]['data']!r}")
    print("  totp: the code goes as totpCode")


def check_cli_asks_for_the_authenticator_code_on_totp():
    class _Fake:
        access_token = ""

        def __init__(self):
            self.sent = []

        def login(self, phone):
            return C._next_step_hint({"step": "totp"})

        def confirm_step(self, kind, value):
            self.sent.append((kind, value))
            if kind == "totp":
                raise TbankApiError("NEXT_STEP", C._next_step_hint({"step": "password"}))

    fake = _Fake()
    prompts = []

    def getpass(prompt):
        prompts.append(prompt)
        return "pw" if "Пароль" in prompt else "123456"

    saved = (login_cli.srv._blank_session, login_cli.srv._save_session, login_cli.getpass.getpass)
    login_cli.srv._blank_session = lambda: fake
    login_cli.srv._save_session = lambda s: True
    login_cli.getpass.getpass = getpass
    try:
        rc = login_cli.login("+79991234567")
    finally:
        login_cli.srv._blank_session, login_cli.srv._save_session, login_cli.getpass.getpass = saved
    check(rc == 0, f"the totp → password login succeeds, rc={rc}")
    check(fake.sent == [("totp", "123456"), ("password", "pw")],
          f"totp code, then password; got {fake.sent}")
    check("аутентификатора" in prompts[0] and "SMS" not in prompts[0],
          f"the first prompt asks for the authenticator code: {prompts[0]!r}")
    print("  CLI: totp asks for the authenticator code, then the password")


def main():
    saved = C._wait_for_propagation
    C._wait_for_propagation = lambda probe, **kw: None   # no live keepalive poll
    try:
        for fn in (check_complete_hop_is_sent_bare_and_code_exchanged,
                   check_complete_followed_by_another_step_names_it,
                   check_complete_hop_error_is_the_banks_error,
                   check_direct_code_sends_no_extra_hop,
                   check_cli_reads_the_step_from_the_hint_head,
                   check_totp_is_sent_as_totp_code,
                   check_cli_asks_for_the_authenticator_code_on_totp):
            print(f"{fn.__name__}:")
            fn()
    finally:
        C._wait_for_propagation = saved
    if failures:
        print(f"\n{len(failures)} FAILED")
        return 1
    print("\nall login-step-complete tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
