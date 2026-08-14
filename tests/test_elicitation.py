"""Elicitation: first-class dialogs for money, with a byte-for-byte text fallback.

The money path may ask the HUMAN — not the agent — two questions when the client
supports MCP elicitation: «Перевести/Отмена» before a transfer_requisites POST
(from TBANK_CONFIRM_ABOVE, default 0 = every one), and the SMS code form when
confirm_payment is called without an otp. These tests EXECUTE the real tools
through the same fakes the rest of the suite uses and pin the contract:

  * no ctx / no capability -> today's flow exactly, nothing elicited;
  * accept -> the payment POSTs (or the code rides as secretValue);
  * decline / cancel / client error -> ZERO journal writes, ZERO HTTP for the
    gate; the pending payment stays waiting_confirmation for the code form;
  * the code from the form never reaches any log;
  * the schema agents see has no `ctx`, and `otp` is optional;
  * the injected ctx never reaches calls.jsonl.

All values are synthetic.

    python3 tests/test_elicitation.py
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="tbank-elicit-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from mcp.shared.exceptions import McpError                    # noqa: E402
from mcp.types import ErrorData                               # noqa: E402

from src import journal, observability, server, trace         # noqa: E402
from test_requisites import LegalSession, fixture, run_tool   # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


TICKET = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
AMT = 23600
UPID = 1700000000001
PAYMENT_ID = "100000000001"
# Same synthetic requisites the rest of the suite uses.
QR = ("ST00012|Name=ООО Тест|PersonalAcc=40702810000000000001"
     "|BIC=044525000|PayeeINN=7700000000|Purpose=Оплата счёта 1|Sum=2360000")


# ---- test doubles ----------------------------------------------------------

class _FakeServerSession:
    def __init__(self, capable=True):
        self.capable = capable

    def check_client_capability(self, cap):   # sync, like the real ServerSession
        return self.capable


class FakeCtx:
    """Quacks like fastmcp Context for exactly what the server touches:
    .request_context.session and async .elicit(message=, schema=)."""

    def __init__(self, action="accept", data=None, capable=True, exc=None):
        self.request_context = SimpleNamespace(session=_FakeServerSession(capable))
        self._action, self._data, self._exc = action, data or {}, exc
        self.asked = []                        # (message, schema) pairs

    async def elicit(self, message, schema):
        self.asked.append((message, schema))
        if self._exc:
            raise self._exc
        if self._action == "accept":
            return SimpleNamespace(action="accept", data=schema(**self._data))
        return SimpleNamespace(action=self._action)


class SrvSession:
    """confirm_payment double: records the confirm call, returns the payload."""

    def __init__(self):
        self.seen = None

    def ensure_fresh(self, *a, **k):
        return None

    def confirm_payment(self, *, operation_ticket, otp, initial_operation,
                        confirmation_type):
        self.seen = {"ticket": operation_ticket, "otp": otp,
                     "io": initial_operation, "ct": confirmation_type}
        return {"paymentId": PAYMENT_ID, "extraFields": {}}


def _mcp_error():
    return McpError(ErrorData(code=-32603, message="elicitation timed out"))


def _args(fx):
    return dict(fx["tool_args"], from_account="1111111111",
                comment="Счет 1 от 01.01.2026")


def _reset_logs():
    for p in (journal.ATTEMPTS_FILE, observability.EVENTS_FILE, trace.TRACE_FILE):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()


def _seed_waiting():
    aid = journal.new_attempt("transfer-legal", "••", "k", AMT)
    journal.record(aid, "pay", "waiting_confirmation", user_payment_ms=UPID,
                   provider="transfer-legal", amount=AMT,
                   confirmation_type="SMSBYID", operation_ticket=TICKET,
                   initial_operation="pay", code_length=4, payment_id=PAYMENT_ID)
    return aid


def _no_threshold():
    os.environ.pop("TBANK_CONFIRM_ABOVE", None)


# ---- 1. no ctx is today's flow exactly -------------------------------------

def test_no_ctx_is_todays_flow_exactly():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=AMT, **_args(fx))
    check("Отправлено" in out and "paymentId=" in out,
          f"without a ctx the payment must flow as before: {out}")
    check(s.body is not None, "the /v1/pay body must have been built and sent")
    print("  no ctx: today's flow, payment posted")


# ---- 2. no capability never elicits ----------------------------------------

def test_no_capability_never_elicits():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    fctx = FakeCtx(capable=False)
    out = run_tool(s, server.transfer_requisites, amount=AMT, ctx=fctx, **_args(fx))
    check(fctx.asked == [], f"no capability -> no elicitation: {fctx.asked}")
    check("Отправлено" in out, f"payment must proceed on the text path: {out}")
    print("  no capability: nothing elicited, payment proceeds")


# ---- 3. the threshold gates only from the configured amount ----------------

def test_threshold_gates_only_above():
    fx = fixture()
    try:
        os.environ["TBANK_CONFIRM_ABOVE"] = "1000"
        _reset_logs()
        fctx = FakeCtx(action="accept")
        run_tool(LegalSession(fx), server.transfer_requisites, amount=500,
                 ctx=fctx, **_args(fx))
        check(fctx.asked == [], f"500 < 1000 must not elicit: {fctx.asked}")
        _reset_logs()
        fctx = FakeCtx(action="accept")
        run_tool(LegalSession(fx), server.transfer_requisites, amount=1500,
                 ctx=fctx, **_args(fx))
        check(len(fctx.asked) == 1, f"1500 >= 1000 must elicit once: {fctx.asked}")
        if fctx.asked:
            msg = fctx.asked[0][0]
            check("1 500.00" in msg and "ПРИМЕР" in msg,
                  f"the question must name the amount and the payee: {msg}")

        _no_threshold()                          # default: gate every transfer
        _reset_logs()
        fctx = FakeCtx(action="accept")
        run_tool(LegalSession(fx), server.transfer_requisites, amount=100,
                 ctx=fctx, **_args(fx))
        check(len(fctx.asked) == 1, "default (unset) must gate any amount")

        os.environ["TBANK_CONFIRM_ABOVE"] = "мусор"   # garbage -> like 0
        _reset_logs()
        fctx = FakeCtx(action="accept")
        run_tool(LegalSession(fx), server.transfer_requisites, amount=100,
                 ctx=fctx, **_args(fx))
        check(len(fctx.asked) == 1, "a garbage threshold must fail safe (gate)")
    finally:
        _no_threshold()
    print("  threshold: gates from the configured amount, default gates all")


# ---- 4. accept sends the payment -------------------------------------------

def test_accept_sends_the_payment():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    fctx = FakeCtx(action="accept")
    out = run_tool(s, server.transfer_requisites, amount=AMT, ctx=fctx, **_args(fx))
    check(len(fctx.asked) == 1, "the button must have been shown")
    check(s.body is not None, "accept must POST the payment")
    check("Отправлено" in out and "paymentId=" in out, f"success text expected: {out}")
    print("  accept: button shown, payment posted")


# ---- 5. decline moves nothing ----------------------------------------------

def test_decline_moves_nothing():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=AMT,
                   ctx=FakeCtx(action="decline"), **_args(fx))
    check(s.body is None, "decline must not POST /v1/pay")
    check(s.commission_body is None, "decline must not even quote the commission")
    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", f"decline must leave zero journal writes: {blob!r}")
    check("отменён" in out and "деньги на месте" in out, f"honest refusal text: {out}")
    check("НЕ выполнен" not in out, f"a decline is not a failure: {out}")
    print("  decline: no HTTP, no journal, honest text")


# ---- 6. cancel and a client error move nothing either ----------------------

def test_cancel_and_error_move_nothing():
    fx = fixture()
    for fctx in (FakeCtx(action="cancel"), FakeCtx(exc=_mcp_error())):
        _reset_logs()
        s = LegalSession(fx)
        out = run_tool(s, server.transfer_requisites, amount=AMT, ctx=fctx, **_args(fx))
        check(s.body is None and s.commission_body is None,
              "cancel/error must not touch the network")
        blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
        check(blob.strip() == "", "cancel/error must leave zero journal writes")
        check("не получено" in out and "НЕ отправлен" in out,
              f"must say the confirmation was not received: {out}")
    print("  cancel/timeout: no HTTP, no journal, says not confirmed")


# ---- 7. a QR-only call still gates on the QR's own amount ------------------

def test_qr_only_amount_still_gates():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    fctx = FakeCtx(action="decline")
    out = run_tool(s, server.transfer_requisites, amount=0, qr=QR, ctx=fctx)
    check(len(fctx.asked) == 1, "the QR amount must reach the gate")
    if fctx.asked:
        check("23 600.00" in fctx.asked[0][0],
              f"the question must carry the QR sum: {fctx.asked[0][0]}")
    check(s.body is None and "отменён" in out, f"declined QR payment must stop: {out}")
    print("  qr-only: gate reads Sum from the QR locally")


# ---- 8. the OTP form confirms and never logs the code ----------------------

def test_otp_form_confirms_and_never_logs_the_code():
    _reset_logs()
    aid = _seed_waiting()
    OTP = "903175"
    srv = SrvSession()
    fctx = FakeCtx(action="accept", data={"code": " %s " % OTP})
    out = run_tool(srv, server.confirm_payment, aid, ctx=fctx)
    check(len(fctx.asked) == 1, "the code form must have been shown")
    if fctx.asked:
        check("4 цифр" in fctx.asked[0][0],
              f"the form must state the code length: {fctx.asked[0][0]}")
    check(srv.seen and srv.seen["otp"] == OTP,
          f"the trimmed form code must ride as the otp: {srv.seen}")
    check(srv.seen and srv.seen["ticket"] == TICKET, "the journalled ticket must ride")
    check(PAYMENT_ID in out and "подтвержд" in out.lower(), f"success text: {out}")
    ev = journal.latest_event_of_attempt(aid)
    check((ev or {}).get("status") == "paid", f"must journal paid: {ev}")
    for p in (journal.ATTEMPTS_FILE, observability.EVENTS_FILE, trace.TRACE_FILE):
        blob = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        check(OTP not in blob, f"the form code leaked into {os.path.basename(p)}")
    print("  otp form: code rides as secretValue, journal paid, no log leak")


# ---- 9. declining the form leaves the payment pending ----------------------

def test_otp_decline_cancel_error_leave_it_pending():
    for fctx in (FakeCtx(action="decline"), FakeCtx(action="cancel"),
                 FakeCtx(exc=_mcp_error())):
        _reset_logs()
        aid = _seed_waiting()
        srv = SrvSession()
        out = run_tool(srv, server.confirm_payment, aid, ctx=fctx)
        check(srv.seen is None, "no code -> /v1/confirm must not be called")
        ev = journal.latest_event_of_attempt(aid)
        check((ev or {}).get("status") == "waiting_confirmation",
              f"the payment must stay pending: {ev}")
        check("payment_status" in out and "не двинуты" in out,
              f"must point at payment_status and say money did not move: {out}")
    print("  otp decline/cancel/timeout: pending kept, money untouched")


# ---- 10. the text fallback without elicitation -----------------------------

def test_otp_fallback_without_elicitation():
    _reset_logs()
    aid = _seed_waiting()
    srv = SrvSession()
    out = run_tool(srv, server.confirm_payment, aid)          # no ctx, no otp
    check(srv.seen is None, "no code -> nothing sent")
    check(f"confirm_payment('{aid}'" in out and "otp=" in out,
          f"must ask the agent to collect the code: {out}")
    check("4-значный" in out, f"must state the journalled code length: {out}")
    ev = journal.latest_event_of_attempt(aid)
    check((ev or {}).get("status") == "waiting_confirmation", "state must not move")

    # An otp-less call on a non-waiting attempt never pops a form.
    aid2 = journal.new_attempt("transfer-legal", "••", "k2", AMT)
    journal.record(aid2, "confirm", "paid", user_payment_ms=UPID,
                   payment_id=PAYMENT_ID)
    fctx = FakeCtx(action="accept", data={"code": "0000"})
    out2 = run_tool(SrvSession(), server.confirm_payment, aid2, ctx=fctx)
    check(fctx.asked == [], "a paid attempt must not elicit a code")
    check("уже подтверждён" in out2, f"existing guard text expected: {out2}")
    print("  otp fallback: agent-text path intact, guards never elicit")


# ---- 11. an explicit otp never elicits -------------------------------------

def test_explicit_otp_never_elicits():
    _reset_logs()
    aid = _seed_waiting()
    srv = SrvSession()
    fctx = FakeCtx(action="accept", data={"code": "1111"})
    out = run_tool(srv, server.confirm_payment, aid, otp="7788", ctx=fctx)
    check(fctx.asked == [], "an explicit otp must skip the form")
    check(srv.seen and srv.seen["otp"] == "7788", f"the explicit otp must ride: {srv.seen}")
    check(PAYMENT_ID in out, f"must confirm as before: {out}")
    print("  explicit otp: no form, flows as before")


# ---- 12. the schema agents see: no ctx, otp optional -----------------------

def test_schema_has_no_ctx_and_otp_is_optional():
    import inspect
    listed = server.mcp._tool_manager.list_tools()
    if inspect.isawaitable(listed):
        listed = asyncio.run(listed)
    tools = {t.name: t for t in listed}
    for name in ("transfer_requisites", "confirm_payment"):
        props = tools[name].parameters.get("properties", {})
        check("ctx" not in props, f"{name}: ctx leaked into the agent schema: {list(props)}")
    tr = tools["transfer_requisites"].parameters.get("properties", {})
    check(set(tr) == {"amount", "qr", "comment", "account_number", "bik", "inn",
                      "name", "kpp", "corr_account", "bank_name", "nds",
                      "personal_account", "from_account", "force"},
          f"transfer_requisites property set changed: {sorted(tr)}")
    cp = tools["confirm_payment"].parameters
    check("otp" not in cp.get("required", []),
          f"otp must be optional now: {cp.get('required')}")
    check("attempt_id" in cp.get("required", []), "attempt_id must stay required")
    print("  schema: no ctx anywhere, otp optional, property sets pinned")


# ---- 13. the injected ctx never reaches the trace --------------------------

def test_ctx_never_reaches_the_trace():
    _reset_logs()
    fx = fixture()
    run_tool(LegalSession(fx), server.transfer_requisites, amount=AMT,
             ctx=FakeCtx(action="decline"), **_args(fx))
    aid = _seed_waiting()
    run_tool(SrvSession(), server.confirm_payment, aid,
             ctx=FakeCtx(action="accept", data={"code": "903175"}))
    raw = open(trace.TRACE_FILE, encoding="utf-8").read()
    check("FakeCtx" not in raw and "SimpleNamespace" not in raw,
          "a ctx repr leaked into calls.jsonl")
    for line in raw.splitlines():
        row = json.loads(line)
        check("ctx" not in (row.get("args") or {}),
              f"a ctx key leaked into the trace args: {row}")
    print("  trace: ctx dropped from calls.jsonl")


def main():
    for t in (test_no_ctx_is_todays_flow_exactly,
              test_no_capability_never_elicits,
              test_threshold_gates_only_above,
              test_accept_sends_the_payment,
              test_decline_moves_nothing,
              test_cancel_and_error_move_nothing,
              test_qr_only_amount_still_gates,
              test_otp_form_confirms_and_never_logs_the_code,
              test_otp_decline_cancel_error_leave_it_pending,
              test_otp_fallback_without_elicitation,
              test_explicit_otp_never_elicits,
              test_schema_has_no_ctx_and_otp_is_optional,
              test_ctx_never_reaches_the_trace):
        t()
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nall elicitation tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
