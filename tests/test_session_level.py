"""The bank calls a lapsed session a rate limit, and that cost a real transfer.

An agent resolving an SBP recipient got

    API error (REQUEST_RATE_LIMIT_EXCEEDED):
    EPB9VLK01 - Слишком много попыток проверить банки получателя

and the owner reasonably read it as a volume limit and suspected a hardcoded
string. It is neither. Measured live: the message fires on the FIRST call in
fourteen minutes, from the same deviceId and the same IP, and the identical call
succeeds seconds after a session re-mint. It is what this bank answers an
ANONYMOUS-level sessionid on that endpoint.

The gap underneath it: the sessionid's CLIENT window is ~11 minutes, while
ensure_fresh() — all these tools guaranteed — re-mints on a ~100-minute schedule.
So for most of the interval between re-mints these endpoints were being called
with a dead session, and which of them noticed was a matter of which endpoint it
was.

The endpoint list and the code list here are MEASURED, not reasoned. With the
session let lapse to ANONYMOUS, each was called directly and then re-called after
a re-mint; ten refused and ten recovered:

    REQUEST_RATE_LIMIT_EXCEEDED   get_requisites, list_regular_payments
    INSUFFICIENT_PRIVILEGES       payment_templates, invoices_to_pay, autopayments,
                                  sbp_subscriptions, manager_info, client_offers
    OPERATION_REJECTED            subscription_all, subscription_all_bills
    INTERNAL_ERROR                card_credentials

while accounts_light, active_loans, operations, user_profile, contact_list,
bank_info and providers_compatible_page answered fine. That last half matters as
much: the guard costs a ping, so it has to stay on the endpoints that need it.

    python3 tests/test_session_level.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="tbank-level-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

import json                                                        # noqa: E402

from elicit_fake import accept_ctx                                 # noqa: E402
from src import server, trace                                      # noqa: E402
from src.client import MobileSession, TbankApiError                # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def run(session, fn, *a, **kw):
    import asyncio
    import inspect
    saved = server._require
    server._require = lambda: session
    try:
        out = fn(*a, **kw)
        if inspect.iscoroutine(out):
            out = asyncio.run(out)
        return out
    finally:
        server._require = saved


# The exact code/message pairs the bank returned during the live measurement.
MEASURED = {
    "get_requisites": ("REQUEST_RATE_LIMIT_EXCEEDED",
                       "EPB9VLK01 - Слишком много попыток проверить банки получателя"),
    "payment_templates": ("INSUFFICIENT_PRIVILEGES", "Недостаточно прав"),
    "subscription_all": ("OPERATION_REJECTED", "Операция отклонена"),
    "card_credentials": ("INTERNAL_ERROR", "Данные недоступны"),
}


class Lapsed(MobileSession):
    """A session whose CLIENT window has closed, and which counts the re-mint."""

    def __init__(self, code="REQUEST_RATE_LIMIT_EXCEEDED", message="…", heal=True):
        self._memo = {}
        self.raised = 0
        self.reads = []
        self.code = code
        self.message = message
        self.heal = heal

    def ensure_fresh(self, *a, **kw):
        return None

    def ensure_client_session(self):
        self.raised += 1
        return "CLIENT"

    def _call_read(self, key, **kw):
        self.reads.append(key)
        # Anonymous until the level is raised — the whole point of the guard.
        if not self.raised and self.heal:
            raise TbankApiError(self.code, self.message)
        return []


# ---- the fix itself -------------------------------------------------------

def test_the_sbp_lookup_raises_the_session_before_asking():
    """The guard sits in resolve_recipient, not in the tools: transfer()
    resolves a SECOND time on its own, and a tool-level guard would miss it."""
    s = Lapsed()
    s.resolve_recipient("+79991234567")
    check(s.raised == 1,
          f"the session level must be raised before /v1/get_requisites, raised "
          f"{s.raised}× — without it the bank answers REQUEST_RATE_LIMIT_EXCEEDED "
          f"to the first call in fourteen minutes")
    # TWO reads, one per pointerSource, and the level is raised ONCE for both: the
    # app asks internal (the recipient's own T-Bank account) and external (their SBP
    # banks) separately, and asking only the second is what hid a T-Bank recipient.
    # The guard living in the lookup rather than around each request is what keeps
    # the second call from paying for its own re-mint.
    check(s.reads == ["get_requisites", "get_requisites"],
          f"unexpected reads: {s.reads}")

    # ...and transfer()'s OWN resolve — the one an agent hits without ever calling
    # transfer_sbp_resolve — goes through the same guard. Ids omitted on purpose:
    # that is the call that decides where the money goes, and the only lookup
    # transfer still makes.
    deep = Lapsed()
    deep.list_accounts = lambda: [{"id": "1", "accountType": "Current",
                                   "moneyAmount": {"value": 100,
                                                   "currency": {"name": "RUB"}}}]
    deep._call_signed = lambda *a, **kw: {"payload": {"paymentId": "1"}}
    try:
        deep.transfer(10, "+79991234567", account="1")
    except TbankApiError:
        pass                      # the stub resolves to nothing; the raise is the point
    check(deep.raised >= 1,
          "transfer()'s own recipient lookup must raise the level too — it is the "
          "one an agent hits without ever calling transfer_sbp_resolve")
    print("  sbp: the level is raised inside the lookup, so both call sites are covered")


def test_every_measured_endpoint_is_declared():
    """The list is the measurement. A section that quietly leaves it goes back to
    being called with a dead session."""
    need = MobileSession._SECTION_NEEDS_CLIENT
    for section in ("invoices", "subscription_bills", "subscriptions", "templates",
                    "autopayments", "sbp", "requisites", "manager", "offers"):
        check(section in need,
              f"section {section!r} refused an ANONYMOUS session in the live "
              f"measurement and must raise the level first")
    # And the ones that did NOT refuse must stay out: the guard costs a ping.
    for section in ("loans", "profile", "contacts", "providers"):
        check(section not in need,
              f"section {section!r} answered fine while ANONYMOUS — guarding it "
              f"buys nothing and costs a request on every call")
    print(f"  sections: {len(need)} declared, and the ones that do not need it stay out")


# ---- the explanation the agent gets --------------------------------------

def test_a_lapsed_session_is_explained_instead_of_read_as_a_rate_limit():
    """Five different codes, one cause. Two of them — the most common — were
    missing from the list that triggers the hint, which is exactly why the agent
    reported a rate limit and nobody could act on it."""
    for code, message in MEASURED.values():
        s = Lapsed(code=code, message=message)

        def boom(*a, **kw):
            raise TbankApiError(code, message)

        s.resolve_recipient = boom
        out = run(s, server.transfer_sbp_resolve, "+79991234567")
        check(code in out, f"the bank's own code must survive: {out[:120]!r}")
        check("CLIENT" in out and "refresh_session" in out,
              f"{code}: the agent must be told this is the session window and what "
              f"to call — it got a bare rate limit instead: {out!r}")
    # ...and the hint says plainly that waiting is useless, because the text
    # («слишком много попыток») invites exactly that.
    s = Lapsed()
    s.resolve_recipient = lambda *a, **kw: (_ for _ in ()).throw(
        TbankApiError(*MEASURED["get_requisites"]))
    out = run(s, server.transfer_sbp_resolve, "+79991234567")
    check("НЕ значат перебор" in out or "не значат перебор" in out.lower(),
          f"the false rate-limit reading must be named: {out!r}")
    print(f"  hint: all {len(MEASURED)} measured codes are explained as the session window")


def test_an_ordinary_failure_is_not_dressed_up_as_a_session_problem():
    """The hint must not fire on everything, or it becomes noise and the real
    session case stops standing out."""
    s = Lapsed()
    s.resolve_recipient = lambda *a, **kw: (_ for _ in ()).throw(
        TbankApiError("INVALID_REQUEST_DATA", "поле заполнено неверно"))
    out = run(s, server.transfer_sbp_resolve, "+79991234567")
    check("refresh_session" not in out,
          f"a malformed request is not a session problem: {out!r}")
    print("  hint: an unrelated bank error is not blamed on the session")


def test_the_resolved_name_reaches_both_the_signed_body_and_the_user():
    """The recipient's name is the only human-readable check on a transfer.

    A phone number does not survive being read: +79991234567 and +79991234576 look
    alike and belong to different people. «Мария П.» is what a person recognises.

    The client looks that name up — one request to /v1/get_requisites — whenever the
    caller passes the two routing ids and no name, and puts it in the SIGNED body.
    It used to die there: server.transfer built its confirmation line from its own
    masked_fio ARGUMENT, still empty, so the line a person reads before the money
    moves showed a phone number and nothing else. In the trace, 309 of 333 transfers
    were made that way.

    An earlier version of this test PINNED that gap, asserting the name was absent
    and saying in its own message that a failure would mean the gap had been closed.
    It failed on the fix, which is what the marker was for.

    Every transfer here carries ctx=accept_ctx(): money moves only after the
    elicitation button, so without it the tool refuses before the body runs."""
    seen = {}

    class Named(MobileSession):
        def __init__(self):
            self._memo = {}

        def ensure_fresh(self, *a, **kw):
            return None

        def ensure_client_session(self):
            return None

        def list_accounts(self):
            return [{"id": "1", "accountType": "Current",
                     "moneyAmount": {"value": 100, "currency": {"name": "RUB"}}}]

        def resolve_recipient(self, phone):
            seen["resolved"] = seen.get("resolved", 0) + 1
            return [{"bank_member_id": "1", "masked_fio": "И. И.",
                     "pointer_link_id": "2", "bank_name": "Б",
                     "is_default_bank": True, "workflow_type": "SBPTransfer",
                     "is_tbank": False}]

        def _call_signed(self, key, body_str, extra_query=None):
            import urllib.parse
            seen["pf"] = json.loads(
                urllib.parse.parse_qs(body_str)["payParameters"][0])["providerFields"]
            return {"payload": {"paymentId": "1"}}

    s = Named()
    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    # accept_ctx(): the human pressed «Перевести». pointer_link_id is passed, so
    # the tool's own pre-dialog resolve stays out of the way — the ONE lookup
    # counted below is the client's, made for the name in the signed body.
    out = run(s, server.transfer, 10, "+79991234567",
              bank_member_id="1", pointer_link_id="2", from_account="1",
              ctx=accept_ctx())

    check(seen.get("resolved") == 1,
          f"the name lookup must happen — it fills the signed body: "
          f"{seen.get('resolved')}")
    check(seen["pf"].get("maskedFIO") == "И. И.",
          f"the resolved name must reach the SIGNED body: {seen['pf']!r}")
    check("(И. И.)" in out,
          f"the name that went into the payment must appear in the line the user "
          f"reads before it: {out!r}")

    # An explicitly passed name still wins — the caller may have shown the user a
    # specific bank's holder and must not have it replaced.
    seen.clear()
    chosen = Named()
    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    out2 = run(chosen, server.transfer, 12, "+79991234567",
               bank_member_id="1", pointer_link_id="2", masked_fio="Пётр П.",
               from_account="1", ctx=accept_ctx())
    check("(Пётр П.)" in out2, f"an explicit name must win: {out2!r}")
    check(seen.get("resolved") is None,
          f"...and must not cost a lookup at all: {seen.get('resolved')}")

    # A lookup that fails must not block a confirmed transfer, and must not invent
    # a name for the signed body.
    seen.clear()

    class NoName(Named):
        def resolve_recipient(self, phone):
            seen["resolved"] = seen.get("resolved", 0) + 1
            raise TbankApiError(*MEASURED["get_requisites"])

    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    out3 = run(NoName(), server.transfer, 13, "+79991234567",
               bank_member_id="1", pointer_link_id="2", from_account="1",
               ctx=accept_ctx())
    check("paymentId=1" in out3,
          f"a failed NAME lookup must not stop a transfer whose routing is already "
          f"decided by the two ids: {out3!r}")
    check(seen["pf"].get("maskedFIO") == "",
          f"...and must not invent a name for the signed body: {seen['pf']!r}")
    print("  transfer: the name reaches the signed body AND the user's line; an "
          "explicit one wins; a failed lookup neither blocks nor invents")


# ---- what made it uninvestigable ------------------------------------------

def test_the_bank_code_is_recorded_so_the_next_one_is_findable():
    """This incident left no trace: 4701 rows, 346 failures, and the code appeared
    only inside a redacted prose field of the OUTERMOST tool."""
    path = os.path.join(_TMP, "level.jsonl")
    trace.TRACE_FILE = path
    s = Lapsed()
    s.resolve_recipient = lambda *a, **kw: (_ for _ in ()).throw(
        TbankApiError(*MEASURED["get_requisites"]))
    run(s, server.transfer_sbp_resolve, "+79991234567")

    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    check(rows, "nothing was recorded at all")
    last = rows[-1]
    check(last.get("err_code") == "REQUEST_RATE_LIMIT_EXCEEDED",
          f"the bank's code must be a FIELD, not prose: err_code="
          f"{last.get('err_code')!r}")
    check(last.get("err") == "TbankApiError",
          f"the exception class must still be recorded: {last.get('err')!r}")

    # A success must not carry a stale code from an earlier failure.
    ok = Lapsed()
    ok.resolve_recipient = lambda *a, **kw: []
    run(ok, server.transfer_sbp_resolve, "+79991234567")
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    check(rows[-1].get("err_code") is None,
          f"a successful call must not inherit the previous code: "
          f"{rows[-1].get('err_code')!r}")
    print("  trace: the bank's code is a queryable field, and does not leak forward")


def test_a_403_raises_the_level_and_retries_once():
    """The travel endpoints validate the sessionid, and a lapsed one answers 403
    with an EMPTY body — no code, no message to key off.

    Which endpoints check is not knowable by inspection and a hand-kept list of
    them rots, so `_call_read` treats the 403 itself as the signal: raise the
    level, send again, once. Measured live on travel/checkout/accounts, which
    answered 403 and then 200 seconds later after a re-mint.
    """
    import types

    class Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._payload = payload
            self.headers = {}
            self.content = b"{}"
            self.text = "{}"

        def json(self):
            return self._payload

    class Flaky(MobileSession):
        """403 until the level is raised, 200 after — and it counts both."""

        def __init__(self):
            self.access_token = "tok"
            self.mobile_sessionid = "old"
            self.sent = 0
            self.raised = 0
            self._http = types.SimpleNamespace(
                get=lambda *a, **kw: self._answer(),
                post=lambda *a, **kw: self._answer(),
                cookies=types.SimpleNamespace(get_dict=dict))

        def _answer(self):
            self.sent += 1
            if self.mobile_sessionid == "old":
                return Resp(403, {})
            return Resp(200, {"resultCode": "OK", "payload": [{"id": "1"}]})

        def ensure_client_session(self):
            self.raised += 1
            self.mobile_sessionid = "fresh"
            return "CLIENT"

        def ensure_fresh(self):
            return None

        def _cookie_for(self, host):
            return ""

        def _tpl(self, key):
            return {"method": "GET", "host": "https://www.tbank.ru",
                    "path": "/api/common/v1/travel/checkout/accounts",
                    "params": {}, "session_param": "sessionId",
                    "headers": {"X-Travel-Context": "mb"}}

    s = Flaky()
    out = s._call_read("travel_accounts")
    check(s.raised == 1, f"the level must be raised exactly once, was {s.raised}×")
    check(s.sent == 2, f"exactly one retry after the 403, sent {s.sent}×")
    check(out == [{"id": "1"}], f"the retry's answer must be returned: {out}")

    # And it must not loop: ensure_client_session pings through this same method,
    # so a 403 answered during the raise has to fall through, not recurse.
    class Stubborn(Flaky):
        def ensure_client_session(self):
            self.raised += 1
            return "ANONYMOUS"           # the raise did not take

    s2 = Stubborn()
    try:
        s2._call_read("travel_accounts")
    except Exception:
        pass
    check(s2.raised == 1, f"a failed raise must not be retried in a loop, {s2.raised}×")
    check(s2.sent == 1, f"no retry when the level did not rise, sent {s2.sent}×")
    print("  a 403 raises the session level and retries once, without looping")


def test_session_status_raises_the_client_level_before_it_reports():
    """session_status() is the tool an agent calls to CHECK the session — so it must
    raise the portal (~11 min) level to CLIENT ITSELF first, else it reports a lapsed
    session as the steady state and the agent «recovers» by re-logging in for nothing.
    The raise must happen BEFORE the status read, not after."""
    order = []

    class Recording(MobileSession):
        def __init__(self):
            super().__init__("sid", "rt")
        def ensure_client_session(self):
            order.append("raise")
            return "CLIENT"
        def session_status(self):
            order.append("read")
            return {"level": "CLIENT"}

    saved = server._require
    server._require = lambda: Recording()
    try:
        out = server.session_status()
    finally:
        server._require = saved
    check("raise" in order, "session_status must raise the client level itself")
    check(order == ["raise", "read"],
          f"the level must be raised BEFORE the status is read: {order}")
    check("CLIENT" in out, f"the raised status must be what gets reported: {out!r}")
    print("  session_status: raises the CLIENT level itself, before reporting")


def test_wait_for_propagation_polls_then_gives_up_without_raising():
    """A freshly-minted session needs a moment before mobile reads accept it. This
    replaces a blind sleep: it returns the instant the probe stops raising, and on
    timeout it degrades to «waited the deadline and proceeded» — never worse than the
    sleep, and it must never propagate the probe's exception."""
    import time as _time
    from src.client import _wait_for_propagation

    # (a) succeeds on the third probe → returns as soon as it stops raising.
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("not ready")
    _wait_for_propagation(flaky, timeout_s=1.0, interval_s=0.01)
    check(calls["n"] == 3, f"must stop polling the moment the probe succeeds: {calls}")

    # (b) always raises → returns after the deadline, WITHOUT raising, and bounded.
    started = _time.monotonic()
    raised = {"n": 0}
    def never():
        raised["n"] += 1
        raise RuntimeError("still not ready")
    try:
        _wait_for_propagation(never, timeout_s=0.15, interval_s=0.02)
    except Exception as e:
        failures.append(f"a timed-out propagation wait must not raise: {e!r}")
    took = _time.monotonic() - started
    check(0.15 <= took < 1.0, f"it must wait ~the deadline, then proceed: {took:.3f}s")
    check(raised["n"] >= 2, f"it must actually poll more than once before giving up: {raised}")
    print("  _wait_for_propagation: returns on first success, else waits the deadline and proceeds")


def test_invest_portfolio_asks_for_dates_in_moscow_not_millisecond_timestamps():
    """This endpoint wants ISO DATES, unlike every other read here that sends
    millisecond timestamps — passing ms returned nothing. And the window's upper
    bound is Moscow «today»: computed in UTC, the 21:00–24:00 UTC band silently
    dropped the current Moscow trading day. Both are untested; pin the shape."""
    import re as _re
    from datetime import datetime, timedelta
    from src.client import MobileSession
    try:
        from src.server import MSK
    except ImportError:
        from src.client import MSK

    seen = {}

    class RecordDates(MobileSession):
        def __init__(self):
            super().__init__("sid", "rt")
        def ensure_client_session(self):
            return None
        def invest_portfolio(self, account, date_from, date_to, **kw):
            seen["from"], seen["to"] = date_from, date_to
            return {"months": []}

    saved = server._require
    server._require = lambda: RecordDates()
    try:
        server.invest_portfolio("acc-1", days=30)
    finally:
        server._require = saved

    iso = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if not (iso.match(seen.get("from", "")) and iso.match(seen.get("to", ""))):
        failures.append(
            f"dates must be ISO YYYY-MM-DD, not millisecond timestamps: {seen}")
        return                      # nothing more to parse — the shape is already wrong
    d_from = datetime.strptime(seen["from"], "%Y-%m-%d").date()
    d_to = datetime.strptime(seen["to"], "%Y-%m-%d").date()
    check((d_to - d_from).days == 30, f"the window must span the requested days: {seen}")
    # Upper bound is Moscow today (the test computes it the same way; a UTC «today»
    # would differ only in the late-UTC band, but equality here is still the contract).
    check(d_to == datetime.now(MSK).date(),
          f"the window must end on Moscow today, not UTC today: {seen}, "
          f"msk={datetime.now(MSK).date()}")
    print("  invest_portfolio: ISO dates in Moscow time, not millisecond timestamps")


def test_the_login_next_step_hint_never_routes_a_password_through_the_chat():
    """_next_step_hint turns the bank's `step` into the next call. For otp/pin it
    names the confirm tool; for PASSWORD it must NOT — the password does not travel
    through the agent (login() and the README say so), so the hint sends the user to
    login_cli.py. It once said «Вызови confirm_password(<пароль>)», inviting the
    exact leak the rest of the flow avoids."""
    from src.client import _next_step_hint

    otp = _next_step_hint({"step": "otp"})
    check("confirm_otp" in otp, f"otp step must name confirm_otp: {otp!r}")

    pin = _next_step_hint({"step": "pin"})
    check("confirm_pin" in pin, f"pin step must name confirm_pin: {pin!r}")

    pwd = _next_step_hint({"step": "password"})
    check("login_cli.py" in pwd,
          f"the password step must route to login_cli.py: {pwd!r}")
    check("confirm_password" not in pwd,
          f"the password hint must NOT tell the agent to call confirm_password: {pwd!r}")

    # An unknown step falls back to naming the confirm tools — but still not
    # confirm_password (a password never goes through the chat, whatever the step).
    other = _next_step_hint({"step": "captcha"})
    check("confirm_password" not in other,
          f"even the fallback must not offer confirm_password: {other!r}")
    check("login_cli.py" in other or "confirm" in other,
          f"the fallback must still point somewhere useful: {other!r}")
    print("  login: the next-step hint routes password to login_cli.py, never the chat")


def main():
    print("session level:")
    test_the_sbp_lookup_raises_the_session_before_asking()
    test_every_measured_endpoint_is_declared()
    test_a_lapsed_session_is_explained_instead_of_read_as_a_rate_limit()
    test_an_ordinary_failure_is_not_dressed_up_as_a_session_problem()
    test_the_resolved_name_reaches_both_the_signed_body_and_the_user()
    test_the_bank_code_is_recorded_so_the_next_one_is_findable()
    test_a_403_raises_the_level_and_retries_once()
    test_session_status_raises_the_client_level_before_it_reports()
    test_wait_for_propagation_polls_then_gives_up_without_raising()
    test_invest_portfolio_asks_for_dates_in_moscow_not_millisecond_timestamps()
    test_the_login_next_step_hint_never_routes_a_password_through_the_chat()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
