"""WAITING_CONFIRMATION is a resumable state, and /v1/confirm resumes it.

When a /v1/pay is held for a second factor, the bank answers WAITING_CONFIRMATION and
the MCP must treat it as a continuation — not a `failed` refusal (which would say the
money is safe and let a blind repeat double-pay). These tests pin the whole path:

  * /v1/pay -> {resultCode:WAITING_CONFIRMATION, operationTicket, initialOperation:"pay",
    confirmations:["SMSBYID"], confirmationData:{SMSBYID:{codeLength,paymentId,...}}}
  * POST /v1/confirm?sessionid&ccc&cpswc  (Cookie auth — NO Bearer, NO x-api-signature)
    body: device block + initialOperation + confirmationType + secretValue=<OTP>
           + initialOperationTicket=<ticket>
  * -> {resultCode:OK, payload:{paymentId, commissionInfo, extraFields}}

Every test EXECUTES the real code path (the LegalSession harness signs a body but
keeps it; a fake _http captures the /v1/confirm POST). Nothing greps source. All
values below are synthetic.

transfer_requisites confirms only through the «Перевести/Отмена» button and refuses
a client without elicitation before any journal write or HTTP — so every call that
must reach /v1/pay (pending, repeat, force, refusal) passes `ctx=accept_ctx()`: the
human pressed Accept, and what this file pins is what happens AFTER that.

    python3 tests/test_payment_confirmation.py
"""
import os
import sys
import tempfile
from urllib.parse import parse_qs

_TMP = tempfile.mkdtemp(prefix="tbank-confirm-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from src import journal, server                              # noqa: E402
from src import observability, trace                          # noqa: E402
from src.client import (MobileSession, TbankApiError,         # noqa: E402
                        PaymentConfirmationRequired)
from elicit_fake import accept_ctx                            # noqa: E402
from test_requisites import LegalSession, fixture, run_tool   # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ---- the real WAITING_CONFIRMATION envelope (synthetic ids) ----------------

# A realistic hex UUID: real operationTickets are hex (e.g. 0968de16-…), whose
# letters keep them clear of the journal's card-number value redaction. An
# all-numeric placeholder would be scrubbed to <card> — which is itself the proof
# that a real (hex) ticket survives the journal intact.
TICKET = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
AMT = 4321                 # synthetic; the pending path does not depend on the amount
UPID = 1700000000001       # synthetic userPaymentId (a ms-epoch-shaped id)
PAYMENT_ID = "100000000001"
WAITING = {
    "resultCode": "WAITING_CONFIRMATION", "operationTicket": TICKET,
    "initialOperation": "pay", "confirmations": ["SMSBYID"],
    "confirmationData": {"SMSBYID": {"codeLength": 4, "paymentId": PAYMENT_ID,
                                     "confirmationType": "SMSBYID", "codeType": "Numeric"}},
    "trackingId": "TRK",
}
# The /v1/confirm request body field set, verbatim from the capture.
CONFIRM_BODY_KEYS = {
    "deviceId", "initialOperation", "confirmationType", "appVersion",
    "mobile_device_model", "mobile_device_os_version", "secretValue", "root_flag",
    "screen_height", "appName", "fingerprint", "connectionType", "device_type",
    "origin", "screen_dpi", "device_location_availability", "mobile_device_os",
    "longitude", "latitude", "platform", "initialOperationTicket", "screen_width",
}
CONFIRM_OK = {"payload": {"paymentId": PAYMENT_ID, "commissionInfo": {},
                          "extraFields": {}}, "resultCode": "OK", "trackingId": "T"}


# ---- test doubles ----------------------------------------------------------

class PendingSession(LegalSession):
    """A /v1/pay the bank holds at WAITING_CONFIRMATION. Builds+keeps the real signed
    body (so the reused userPaymentId can be read), then raises as _unwrap would."""
    def _call_signed(self, template_key, body_str, extra_query=None):
        self.url, self.headers, self.body = self._signed_parts(
            template_key, body_str, extra_query)
        raise PaymentConfirmationRequired(
            "WAITING_CONFIRMATION", "", http_status=200, payload={"redacted": True},
            operation_ticket=TICKET, initial_operation="pay",
            confirmation_type="SMSBYID", confirmations=["SMSBYID"],
            code_length=4, payment_id=PAYMENT_ID, request_id="TRK")


class RefuseSession(LegalSession):
    def _call_signed(self, *a, **kw):
        super()._call_signed(*a, **kw)
        raise TbankApiError("INVALID_REQUEST_DATA", "поле заполнено неверно")


class _Resp:
    def __init__(self, body): self._b = body; self.status_code = 200; self.url = ""; self.headers = {}
    def json(self): return self._b


class _HTTP:
    def __init__(self, resp): self.resp = resp; self.calls = []
    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"m": "POST", "url": url, "data": data, "headers": headers or {}})
        return self.resp
    def get(self, url, headers=None, timeout=None):
        self.calls.append({"m": "GET", "url": url, "headers": headers or {}})
        return self.resp


class ConfirmClientSession(MobileSession):
    """A real MobileSession (so PAY_DEVICE_PROFILE / device_model properties run) with
    the network faked, to inspect the /v1/confirm request confirm_payment builds."""
    def __init__(self, resp_body):
        self.mobile_sessionid = "sid.test"; self.device_id = "DEV-1"
        self.app_name = "mobile"; self.app_version = "7.39.1"
        self.connection_type = "WiFi"; self.origin = "mobile,ib5,loyalty,platform"
        self.platform = "ios"; self.ccc = "true"; self.cpswc = "true"
        self.base_url = "https://api.t-bank-app.ru"; self.device_profile = {}
        self._http = _HTTP(_Resp(resp_body))
    def _credentials_fingerprint(self): return "FP###1260x2736x32###-180###false###false###"
    def _mobile_ua(self): return "iPhone/iOS(26.5.2)/TCSMB/7.39.1(7391000)"
    def _wide_cookie(self): return "SSO=x"
    def _tpl(self, k): return {"host": "https://api.t-bank-app.ru"}
    def ensure_fresh(self, *a, **k): return None


class SrvSession:
    """Server-tool double: records the confirm call, returns the unwrapped payload."""
    def __init__(self): self.seen = None
    def ensure_fresh(self, *a, **k): return None
    def confirm_payment(self, *, operation_ticket, otp, initial_operation, confirmation_type):
        self.seen = {"ticket": operation_ticket, "otp": otp,
                     "io": initial_operation, "ct": confirmation_type}
        return {"paymentId": PAYMENT_ID, "extraFields": {}}


def _args(fx):
    return dict(fx["tool_args"], from_account="1111111111", comment="Счет 1 от 01.01.2026")

def _reset_logs():
    for p in (journal.ATTEMPTS_FILE, observability.EVENTS_FILE, trace.TRACE_FILE):
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").close()

def _attempt_id(out):
    for tok in out.replace("\n", " ").split():
        if tok.startswith("attemptId="):
            return tok.split("=", 1)[1]
    return ""

def _seed_waiting(fx):
    """A held payment already on the books — what _pending_confirmation would record."""
    aid = journal.new_attempt("transfer-legal", "••", "k", AMT)
    journal.record(aid, "pay", "waiting_confirmation", user_payment_ms=UPID,
                   provider="transfer-legal", amount=AMT, confirmation_type="SMSBYID",
                   operation_ticket=TICKET, initial_operation="pay",
                   code_length=4, payment_id=PAYMENT_ID)
    return aid


# ---- 1. the envelope is preserved as the real fields -----------------------

def test_waiting_confirmation_unwraps_with_the_real_envelope():
    class Resp:
        status_code = 200; url = "https://api.t-bank-app.ru/v1/pay?sessionid=SECRET"
        headers = {"X-Tracking-Id": "TRK-9"}
        def json(self): return dict(WAITING, extra_secret="eyJ.a.b")
    sess = MobileSession.__new__(MobileSession)
    try:
        sess._unwrap(Resp())
        failures.append("WAITING_CONFIRMATION did not raise"); return
    except PaymentConfirmationRequired as e:
        check(isinstance(e, TbankApiError), "must stay a TbankApiError subclass")
        check(e.operation_ticket == TICKET, f"operationTicket lost: {e.operation_ticket!r}")
        check(e.initial_operation == "pay", f"initialOperation lost: {e.initial_operation!r}")
        check(e.confirmation_type == "SMSBYID",
              f"confirmationType must stay LITERAL SMSBYID, got {e.confirmation_type!r}")
        check(e.code_length == 4, f"codeLength lost: {e.code_length}")
        check(e.payment_id == PAYMENT_ID, f"embedded paymentId lost: {e.payment_id!r}")
        check(e.http_status == 200 and e.request_id == "TRK-9", "http/correlation lost")
    print("  unwrap: real WAITING_CONFIRMATION envelope preserved (ticket, type, codeLen, paymentId)")


# ---- 2. transfer_requisites reports a pending payment, not a charge/fail ----

def test_transfer_requisites_reports_a_pending_confirmation():
    _reset_logs()
    fx = fixture()
    out = run_tool(PendingSession(fx), server.transfer_requisites, amount=AMT,
                   ctx=accept_ctx(), **_args(fx))
    check("ПОДТВЕРЖДЕНИ" in out.upper(), f"must announce confirmation: {out}")
    check("confirm_payment" in out, f"must name the next tool: {out}")
    check("4-значный" in out, f"must state the code length from the envelope: {out}")
    check("НЕ выполнен" not in out and "деньги на месте" not in out,
          f"a pending payment is neither a refusal nor 'money safe': {out}")
    aid = _attempt_id(out)
    ev = journal.latest_event_of_attempt(aid)
    check((ev or {}).get("status") == "waiting_confirmation", f"journal status: {ev}")
    check((ev or {}).get("operation_ticket") == TICKET, f"ticket must be journalled: {ev}")
    evs = [e for e in observability.for_attempt(aid) if e.get("step") == "payment_http"]
    check(len(evs) == 1 and evs[0].get("resultCode") == "WAITING_CONFIRMATION",
          f"one safe payment_http trace expected: {evs}")
    print("  transfer_requisites: pending, ticket journalled, safe trace emitted")


# ---- 3. a pending payment blocks a blind repeat and reuses the id ----------

def test_pending_blocks_repeat_and_reuses_user_payment_id():
    _reset_logs()
    fx = fixture()
    s1 = PendingSession(fx)
    run_tool(s1, server.transfer_requisites, amount=AMT, ctx=accept_ctx(), **_args(fx))
    upid1 = s1.sent_pay_parameters()["userPaymentId"]
    # The human presses Accept again — the journal, not the button, blocks the repeat.
    out2 = run_tool(PendingSession(fx), server.transfer_requisites, amount=AMT,
                    ctx=accept_ctx(), **_args(fx))
    check("ЗАБЛОКИРОВАН" in out2, f"identical repeat must be blocked: {out2}")
    s3 = PendingSession(fx)
    out3 = run_tool(s3, server.transfer_requisites, amount=AMT, force=True,
                    ctx=accept_ctx(), **_args(fx))
    check("ПОДТВЕРЖДЕНИ" in out3.upper(), f"forced retry still needs confirmation: {out3}")
    check(s3.sent_pay_parameters()["userPaymentId"] == upid1,
          "the retry must reuse the original userPaymentId, not mint a second payment")
    print("  idempotency: repeat blocked, forced retry reuses the id")


# ---- 4. client.confirm_payment builds the exact /v1/confirm request --------

def test_confirm_payment_builds_the_captured_request():
    s = ConfirmClientSession(CONFIRM_OK)
    out = s.confirm_payment(operation_ticket=TICKET, otp="7788",
                            initial_operation="pay", confirmation_type="SMSBYID")
    call = s._http.calls[0]
    check(call["m"] == "POST" and "/v1/confirm?" in call["url"],
          f"must POST /v1/confirm: {call['url']}")
    q = parse_qs(call["url"].split("?", 1)[1])
    check(q.get("sessionid") == ["sid.test"] and q.get("ccc") == ["true"]
          and q.get("cpswc") == ["true"], f"confirm query: {q}")
    form = {k: v[0] for k, v in parse_qs(call["data"]).items()}
    check(set(form) == CONFIRM_BODY_KEYS,
          f"confirm body field set drifted from the capture:\n"
          f"  extra={set(form) - CONFIRM_BODY_KEYS}\n  missing={CONFIRM_BODY_KEYS - set(form)}")
    check(form.get("initialOperationTicket") == TICKET, "ticket must ride as initialOperationTicket")
    check(form.get("secretValue") == "7788", "OTP must ride as secretValue")
    check(form.get("confirmationType") == "SMSBYID", "confirmationType must be echoed literally")
    check(form.get("initialOperation") == "pay", "initialOperation must be sent")
    check(form.get("mobile_device_os") == "iOS", "mobile_device_os must be iOS")
    hk = {k.lower() for k in call["headers"]}
    check("authorization" not in hk and "x-api-signature" not in hk,
          f"/v1/confirm must NOT be Bearer/signature-authorised: {sorted(hk)}")
    check("cookie" in hk, "/v1/confirm authorises on the cookie")
    check(out.get("paymentId") == PAYMENT_ID, f"must unwrap to the paymentId: {out}")
    print("  client.confirm_payment: exact /v1/confirm body, cookie-auth, unwraps paymentId")


# ---- 5. the confirm tool completes the payment and never logs the OTP ------

def test_confirm_payment_tool_completes_and_never_logs_the_otp():
    _reset_logs()
    fx = fixture()
    aid = _seed_waiting(fx)
    OTP = "903175"
    srv = SrvSession()
    out = run_tool(srv, server.confirm_payment, aid, otp=OTP)
    check(srv.seen and srv.seen["ticket"] == TICKET, "the tool must pass the journalled ticket")
    check(srv.seen and srv.seen["otp"] == OTP, "the tool must pass the OTP through")
    check(PAYMENT_ID in out and "подтвержд" in out.lower(), f"must confirm with paymentId: {out}")
    ev = journal.latest_event_of_attempt(aid)
    check((ev or {}).get("status") == "paid", f"must journal paid: {ev}")
    for p in (journal.ATTEMPTS_FILE, observability.EVENTS_FILE, trace.TRACE_FILE):
        blob = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        check(OTP not in blob, f"the OTP leaked into {os.path.basename(p)}")
    red = observability._redact_value({"secretValue": OTP, "otp": OTP, "keep": "ok"})
    check(red.get("secretValue") == "<redacted>" and red.get("otp") == "<redacted>",
          f"secretValue and otp must both redact: {red}")
    print("  confirm_payment tool: completes, journals paid, OTP absent from every log")


# ---- 6. login OTP stays login-only -----------------------------------------

def test_confirm_otp_is_still_login_only():
    saved = server._session
    server._session = None
    try:
        out = server.confirm_otp("424242")
    finally:
        server._session = saved
    check("login" in out.lower() or "NO_SESSION" in out or "сессия" in out.lower(),
          f"confirm_otp without a login must point at login: {out}")
    check("confirm_payment" not in out, "confirm_otp must not redirect to the payment tool")
    print("  confirm_otp: still login-only")


# ---- 7. payment_status shows the pending state -----------------------------

def test_payment_status_shows_pending():
    _reset_logs()
    fx = fixture()
    aid = _seed_waiting(fx)
    st = run_tool(SrvSession(), server.payment_status, aid)
    check("ЖДЁТ ПОДТВЕРЖДЕНИЯ" in st.upper() or "ждёт подтверждения" in st.lower(),
          f"must show pending: {st}")
    check("list_operations" in st and "confirm_payment" in st, f"must show how to reconcile/resume: {st}")
    print("  payment_status: shows pending + how to reconcile/resume")


# ---- 8. regression: a plain refusal is still a refusal ----------------------

def test_a_genuine_refusal_is_still_a_refusal():
    _reset_logs()
    fx = fixture()
    out = run_tool(RefuseSession(fx), server.transfer_requisites, amount=99,
                   ctx=accept_ctx(), **_args(fx))
    check("НЕ выполнен" in out, f"a refusal must be reported as one: {out}")
    check("ПОДТВЕРЖДЕНИ" not in out.upper(), f"a refusal is not a confirmation: {out}")
    check("деньги на месте" in out, f"a refusal moved nothing — say so: {out}")
    print("  regression: a plain refusal stays a refusal")


def main():
    print("payment confirmation (WAITING_CONFIRMATION → /v1/confirm):")
    test_waiting_confirmation_unwraps_with_the_real_envelope()
    test_transfer_requisites_reports_a_pending_confirmation()
    test_pending_blocks_repeat_and_reuses_user_payment_id()
    test_confirm_payment_builds_the_captured_request()
    test_confirm_payment_tool_completes_and_never_logs_the_otp()
    test_confirm_otp_is_still_login_only()
    test_payment_status_shows_pending()
    test_a_genuine_refusal_is_still_a_refusal()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
