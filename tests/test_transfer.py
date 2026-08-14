"""The transfer money path — the one place where a bug costs the user cash.

Five defects, all from the audit and all verified against the real signed /v1/pay
in the capture (captures.xml #1477, p2p-anybank via SBP, HTTP 200):

1. The payer account was hardcoded to "first Current RUB with a positive balance"
   and could not be chosen. The skill tells the agent to ask the user which account
   to debit; the answer was then ignored.
2. No userPaymentId. The app sends one on every payment; without it a retry after a
   timeout is a second, unlinked transfer.
3. The bank's response was thrown away and the tool echoed its own arguments back
   ("Sent: 1000₽ to …"), losing paymentId — which is the only handle for a receipt
   and appears nowhere else afterwards.
4. `description` was accepted, documented, and never sent. The app puts it in
   providerFields.message.
5. The body diverged from the app's: no device/anti-fraud block, no deviceId, a wuid
   the app does not send there, and a paymentType that belongs to
   payment_commission, not to pay.

    python3 tests/test_transfer.py
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "transfer.json")

# Point the attempt journal at a scratch file BEFORE importing the modules that
# resolve its path at import time.
_TMP = tempfile.mkdtemp()
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

from src import server  # noqa: E402
from src.client import MobileSession  # noqa: E402

failures = []


def _run(coro_or_val):
    """transfer is now an async tool; every call site here passes ctx=None so no
    dialog fires — just await the coroutine to reach the sync body."""
    return asyncio.run(coro_or_val) if inspect.iscoroutine(coro_or_val) else coro_or_val


def check(cond, msg):
    if not cond:
        failures.append(msg)


CAPTURE = os.environ.get("TBANK_CAPTURE", os.path.expanduser("~/tbank-app/captures.xml"))


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_the_fixture_still_matches_the_capture():
    """Everything below is checked against fixtures/transfer.json. That file is a
    scrub of one real signed /v1/pay, and the two OTHER fixtures each guard
    themselves against drifting away from the capture — this one did not, so the
    whole /v1/pay contract rested on a snapshot nothing re-derived.

    Regenerating is deterministic (the scrub substitutes fixed values), so the check
    is simply: does regen produce what is committed? A mismatch means either the app
    changed its request or the scrub changed, and both want a human, not a silent
    pass. Runs only where the capture lives — the assertions below run everywhere."""
    if not os.path.exists(CAPTURE):
        print(f"  (capture absent at {CAPTURE} — fixture-vs-capture drift check "
              f"skipped; the contract below was still verified against the fixture)")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "fixtures"))
    import regen                                            # noqa: E402
    regen.T.CAPTURE = CAPTURE
    try:
        fresh = regen.build_transfer()
    except Exception as e:                                  # noqa: BLE001
        failures.append(f"transfer.json can no longer be regenerated from the "
                        f"capture ({type(e).__name__}: {e})")
        return
    mine = fixture()
    for key in ("query_keys", "form_keys", "query_static", "pay_parameters"):
        check(mine.get(key) == fresh.get(key),
              f"fixtures/transfer.json {key} drifted from captures.xml #1477 — "
              f"rerun tests/fixtures/regen.py and read the diff before committing "
              f"(fixture={mine.get(key)!r} capture={fresh.get(key)!r})")
    print("  fixture vs capture: /v1/pay keys and protocol constants still match")


class CaptureSession(MobileSession):
    """Signs and builds the request exactly as production does, but keeps it."""

    def __init__(self, fail=False, payload=None):
        self.mobile_sessionid = "sid.authenticon-test"
        self.access_token = "tok"
        self.device_id = "00000000-1111-2222-3333-444444444444"
        self.old_device_id = "0123456789abcdef"
        self.cookie_str = ""
        # As server._require builds it. The User-Agent is DERIVED from these, so a
        # stub without them would exercise a session shape production never has.
        self.platform = "ios"
        self.app_name = "mobile"
        self.app_version = "7.31.6"
        self.fail = fail
        self.payload = payload
        self.url = None
        self.body = None
        self.headers = None

    def ensure_fresh(self, *a, **kw):
        return None

    def list_accounts(self):
        return [{"id": "1111111111", "accountType": "Current",
                 "moneyAmount": {"value": 5000, "currency": {"name": "RUB"}}}]

    def resolve_recipient(self, phone):
        return [{"bank_member_id": "100000000000", "masked_fio": "И. И.",
                 "pointer_link_id": "10000000000", "bank_name": "Банк",
                 "is_default_bank": True, "workflow_type": "SBPTransfer",
                 "is_tbank": False}]

    def _call_signed(self, template_key, body_str, extra_query=None):
        self.url, self.headers, self.body = self._signed_parts(
            template_key, body_str, extra_query)
        if self.fail:
            raise ConnectionError("connection reset by peer")
        # The REAL response shape, from the fixture. The stub here used to be
        # {"paymentId": …, "commissionInfo": {"value": 0}} — a shape the bank
        # never sends, invented to match the code. It made a renderer that read
        # the wrong money object out of commissionInfo look correct.
        return self.payload if self.payload is not None else {
            "payload": json.loads(json.dumps(fixture()["pay_response"]))}

    def sent_pay_parameters(self):
        return json.loads(urllib.parse.parse_qs(self.body)["payParameters"][0])

    def sent_form(self):
        return urllib.parse.parse_qs(self.body)

    def sent_query(self):
        return urllib.parse.parse_qs(self.url.split("?", 1)[1])


def test_body_matches_the_real_pay_request():
    fx = fixture()
    s = CaptureSession()
    s.transfer(1000, "+79991234567", account="0000000000")

    got, real = s.sent_pay_parameters(), fx["pay_parameters"]
    check(sorted(got) == sorted(real),
          f"payParameters keys differ\n    ours={sorted(got)}\n    real={sorted(real)}")
    for field in ("provider", "currency", "cellularService", "frontCamera",
                  "isTransferStatus", "isUrgentTransfer"):
        check(got.get(field) == real.get(field),
              f"payParameters.{field}: ours={got.get(field)!r} real={real.get(field)!r}")
    check(sorted(got["providerFields"]) == sorted(real["providerFields"]),
          f"providerFields keys differ\n    ours={sorted(got['providerFields'])}"
          f"\n    real={sorted(real['providerFields'])}")
    check(got["providerFields"]["pointerType"] == real["providerFields"]["pointerType"],
          "the SBP pointerType must stay 8276")

    # paymentType belongs to payment_commission; no real pay body carries it.
    check("paymentType" not in got,
          "paymentType is sent on /v1/pay — it belongs to payment_commission only")
    check(str(got["userPaymentId"]).isdigit() and len(str(got["userPaymentId"])) >= 13,
          f"userPaymentId must be a millisecond timestamp: {got.get('userPaymentId')!r}")

    # Anti-fraud/3DS block: the app sends the same keys in the query AND the form.
    form, query = s.sent_form(), s.sent_query()
    for k in fx["form_keys"]:
        if k in ("payParameters", "shortcutId"):
            continue
        check(k in form, f"form field {k!r} missing from the pay body")
        check(form[k][0] == fx["query_static"].get(k, form[k][0]),
              f"form {k}: ours={form[k][0]!r} real={fx['query_static'].get(k)!r}")
    for k in fx["query_keys"]:
        if k in ("sessionid", "deviceId", "oldDeviceId"):
            continue
        check(k in query, f"query param {k!r} missing (the signature covers the query)")
    check("deviceId" in query and "oldDeviceId" in query,
          f"the real pay carries deviceId + oldDeviceId: {sorted(query)}")
    check("wuid" not in query,
          "wuid is a web identifier and is not sent on the mobile /v1/pay")

    # EXTRA keys, not just missing ones. The loops above only prove the captured keys
    # are present, so anything added to PAY_DEVICE_PROFILE — which is spread into
    # both the query and the form — rode along invisibly. That happened: a device
    # `model` added for card_credentials silently appeared in the pay body, on the
    # one request the bank fraud-scores, and this test stayed green.
    extra_form = set(form) - set(fx["form_keys"])
    extra_query = set(query) - set(fx["query_keys"])
    check(not extra_form,
          f"the pay FORM carries keys the app does not send: {sorted(extra_form)}")
    check(not extra_query,
          f"the pay QUERY carries keys the app does not send: {sorted(extra_query)}")
    print("  body: matches the captured /v1/pay — keys, anti-fraud block, "
          "no paymentType, nothing extra")


def test_the_pay_request_looks_like_the_device_it_claims_to_be():
    """The query declares platform=ios and a whole iPhone anti-fraud profile. The
    headers said otherwise: the User-Agent came from the requests.Session default and
    read `okhttp/4.12.0` — an Android HTTP client posting an iPhone's 3DS block, on
    the one request the bank scores for fraud. The signature covers method, path,
    query and body, never the headers, so matching the app costs nothing."""
    s = CaptureSession()
    s.transfer(1000, "+79991234567", account="0000000000")
    h = {k.lower(): v for k, v in s.headers.items()}

    check("okhttp" not in h.get("user-agent", "").lower(),
          f"/v1/pay still goes out as an Android HTTP client: {h.get('user-agent')!r}")
    check(h.get("user-agent", "").startswith("iPhone/iOS("),
          f"the UA must be the app's mobile one: {h.get('user-agent')!r}")
    # The captured native /v1/pay: html Accept, ru, charset on the content type.
    check(h.get("accept", "").startswith("text/html"),
          f"Accept diverges from the captured pay: {h.get('accept')!r}")
    check(h.get("x-lang") == "ru", f"X-Lang missing: {sorted(h)}")
    check("charset=utf-8" in h.get("content-type", ""),
          f"Content-Type diverges from the captured pay: {h.get('content-type')!r}")
    # And the signature must still be the one over the query, unaffected by headers.
    check(h.get("x-api-signature"), "the request lost its signature")

    query = s.sent_query()
    check(query.get("platform", [""])[0] == "ios",
          "the query no longer declares the platform the UA claims")
    print("  headers: /v1/pay presents the same device its query describes")


def test_the_device_profile_is_configuration_not_a_constant():
    """The 3DS block mixes protocol constants with facts about ONE phone: a
    1260×2736 screen, the ru-CY locale (which also says what region its owner was in)
    and UTC+3. Baked in, every user of this MCP claims to be that device."""
    fresh = MobileSession("sid", "rt")
    base = fresh.PAY_DEVICE_PROFILE
    for key in ("colorDepth", "notificationUrl", "javaScriptEnabled", "emulator",
                "device_screen_height", "device_screen_width", "language", "timezone"):
        check(key in base, f"the pay device block lost {key!r}: {sorted(base)}")

    for env, key, value in (
            ("TBANK_DEVICE_SCREEN_HEIGHT", "device_screen_height", "1920"),
            ("TBANK_DEVICE_SCREEN_WIDTH", "device_screen_width", "1080"),
            ("TBANK_DEVICE_LANGUAGE", "language", "ru-RU"),
            ("TBANK_DEVICE_TIMEZONE", "timezone", "60")):
        os.environ[env] = value
        try:
            got = MobileSession("sid", "rt").PAY_DEVICE_PROFILE
        finally:
            os.environ.pop(env, None)
        check(got.get(key) == value,
              f"{env} did not reach the pay block: {key}={got.get(key)!r}")
        check(got["colorDepth"] == "24" and got["notificationUrl"].endswith("/v1/3ds"),
              "overriding a device fact must not disturb the protocol constants")
        check(len(got) == len(base),
              f"the block changed size under an override: {len(got)} vs {len(base)}")

    # A configured device must actually reach the wire — both copies of it.
    os.environ["TBANK_DEVICE_LANGUAGE"] = "ru-RU"
    try:
        s = CaptureSession()
        s.device_profile = {"language": "ru-RU"}
        s.transfer(1000, "+79991234567", account="0000000000")
        check(s.sent_query().get("language", [""])[0] == "ru-RU",
              f"the query kept the captured locale: {s.sent_query().get('language')}")
        check(s.sent_form().get("language", [""])[0] == "ru-RU",
              f"the form kept the captured locale: {s.sent_form().get('language')}")
    finally:
        os.environ.pop("TBANK_DEVICE_LANGUAGE", None)
    print("  device profile: constants fixed, device facts configurable, both copies sent")


def test_description_reaches_the_recipient():
    s = CaptureSession()
    s.transfer(10, "+79991234567", "За обед", account="0000000000")
    pf = s.sent_pay_parameters()["providerFields"]
    check(pf.get("message") == "За обед",
          f"description must ride in providerFields.message, got {pf.get('message')!r}")

    s2 = CaptureSession()
    s2.transfer(10, "+79991234567", account="0000000000")
    check("message" not in s2.sent_pay_parameters()["providerFields"],
          "an empty description must not add an empty message field")
    print("  description: delivered as providerFields.message (was silently dropped)")


def test_transfer_refuses_a_non_positive_amount_before_touching_a_session():
    """pay_bill already refused amount<=0 with a friendly message before calling
    the bank; transfer() (the sibling MONEY tool) had no such guard at all, so
    the identical mistake fell through to a raw bank error or an ambiguous
    outcome the retry-guard journal then had to reason about."""
    for bad in (0, -5, -0.01):
        out = _run(server.transfer(bad, "+79991234567"))
        check("Сумма должна быть больше нуля" in out,
              f"transfer({bad!r}, ...) must be refused with a clear message: {out!r}")
    print("  transfer: amount<=0 is refused before any session/API work, like pay_bill")


def test_the_chosen_account_is_the_one_debited():
    s = CaptureSession()
    s.transfer(10, "+79991234567", account="9999999999")
    check(s.sent_pay_parameters()["account"] == "9999999999",
          f"the caller's account was ignored: {s.sent_pay_parameters()['account']!r}")

    fallback = CaptureSession()
    fallback.transfer(10, "+79991234567")
    check(fallback.sent_pay_parameters()["account"] == "1111111111",
          "with no account given, the first Current RUB is the documented fallback")
    print("  account: the chosen payer account is honoured, fallback still works")


def test_a_lost_transfer_blocks_the_next_identical_one():
    """A timeout means the money MAY have moved. The next identical transfer must be
    refused until the user reconciles — and the retry must reuse the same
    userPaymentId, or it is a second payment rather than a repeat of the first."""
    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    saved = server._require
    dead = CaptureSession(fail=True)
    server._require = lambda: dead
    try:
        out = _run(server.transfer(500, "+79991234567", from_account="0000000000"))
        check("НЕИЗВЕСТЕН" in out, f"a dropped connection must not read as success: {out}")
        check("force=True" in out, f"the recovery path must be spelled out: {out}")
        check("list_operations" in out, f"the user must be told how to check: {out}")
        first_upid = dead.sent_pay_parameters()["userPaymentId"]

        again = _run(server.transfer(500, "+79991234567", from_account="0000000000"))
        check("ЗАБЛОКИРОВАН" in again,
              f"an identical transfer after an unknown outcome must be blocked: {again}")

        ok = CaptureSession()
        server._require = lambda: ok
        forced = _run(server.transfer(500, "+79991234567", from_account="0000000000", force=True))
        check("paymentId=100000000001" in forced, f"force must go through: {forced}")
        check(ok.sent_pay_parameters()["userPaymentId"] == first_upid,
              f"the retry must reuse the original userPaymentId "
              f"({first_upid} → {ok.sent_pay_parameters()['userPaymentId']}), "
              f"otherwise it is a second payment, not a repeat")

        # A CONFIRMED transfer must not block the next one: sending the same person
        # the same amount twice is ordinary.
        third = CaptureSession()
        server._require = lambda: third
        out3 = _run(server.transfer(500, "+79991234567", from_account="0000000000"))
        check("ЗАБЛОКИРОВАН" not in out3,
              f"a completed transfer must not block a later identical one: {out3}")
        # ...and it must be a NEW payment. Reusing the confirmed transfer's id makes
        # the bank deduplicate the second one away: the tool reports success and no
        # money moves. This test previously checked only that it was not blocked,
        # which is exactly how the defect survived.
        check(third.sent_pay_parameters()["userPaymentId"] != first_upid,
              f"a deliberate repeat must get a FRESH userPaymentId, reused "
              f"{first_upid} — the bank would treat it as the first payment again")

        # A retry of a still-unconfirmed attempt must STILL reuse the id.
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        lost = CaptureSession(fail=True)
        server._require = lambda: lost
        _run(server.transfer(700, "+79991234567", from_account="0000000000"))
        lost_upid = lost.sent_pay_parameters()["userPaymentId"]
        retry = CaptureSession()
        server._require = lambda: retry
        _run(server.transfer(700, "+79991234567", from_account="0000000000", force=True))
        check(retry.sent_pay_parameters()["userPaymentId"] == lost_upid,
              f"a forced retry of an UNCONFIRMED transfer must reuse the id "
              f"({lost_upid} -> {retry.sent_pay_parameters()['userPaymentId']})")
    finally:
        server._require = saved
    print("  idempotency: unknown outcome blocks + reuses the id, success does not block")


def test_the_result_carries_what_the_agent_needs_next():
    saved = server._require
    s = CaptureSession()
    server._require = lambda: s
    try:
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        out = _run(server.transfer(1936, "+79991234567", masked_fio="И. И.",
                              from_account="0000000000"))
        check("100000000001" in out, f"paymentId must be returned: {out}")
        check("payment_receipt" in out, f"the tool that uses it must be named: {out}")
        check("0000000000" in out, f"the debited account must be stated: {out}")
        # The amount must be readable AND carry its currency — a bare «1936.0» is
        # one misread away from a factor of ten.
        check("1\u00a0936.00 RUB" in out or "1 936.00 RUB" in out,
              f"the amount must be stated with its currency: {out!r}")

        # No paymentId back → say so plainly instead of implying success.
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        blank = CaptureSession(payload={"payload": {}})
        server._require = lambda: blank
        out2 = _run(server.transfer(50, "+79991234567", from_account="0000000000"))
        check("не вернул paymentId" in out2, f"a missing paymentId must be flagged: {out2}")
    finally:
        server._require = saved
    print("  result: paymentId, account and amount returned instead of an argument echo")


def _money_obj(value):
    return {"currency": {"name": "RUB", "strCode": "643", "code": 643}, "value": value}


def test_the_reported_commission_is_the_commission():
    """commissionInfo holds three money objects — amount (sent), commission (fee),
    amountWithCommission (debited). Reading the wrong one reports the transfer as
    its own fee: a live 10 ₽ transfer said «комиссия 10.0» on a free transfer, and
    a 150 000 ₽ one would have said «комиссия 150000»."""
    saved = server._require
    try:
        # Free transfer — the real fixture shape, fee 0 on a 1000 ₽ amount.
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        free = CaptureSession()
        server._require = lambda: free
        out = _run(server.transfer(1000, "+79991234567", from_account="0000000000"))
        check("комиссия 0.00 RUB" in out,
              f"a free transfer must report a ZERO fee: {out!r}")
        check("комиссия 1" not in out,
              f"the amount sent must never be printed as the fee: {out!r}")
        check("{" not in out,
              f"a raw money dict must never reach the user: {out!r}")

        # Charged transfer — fee and the amount actually debited both matter.
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        charged = CaptureSession(payload={"payload": {
            "paymentId": "100000000001",
            "commissionInfo": {"amount": _money_obj(1000.0),
                               "commission": _money_obj(90.0),
                               "amountWithCommission": _money_obj(1090.0)}}})
        server._require = lambda: charged
        out2 = _run(server.transfer(1000, "+79991234567", from_account="0000000000"))
        check("комиссия 90.00 RUB" in out2, f"the fee must be the fee: {out2!r}")
        check("1 090.00 RUB" in out2 or "1 090.00 RUB" in out2,
              f"what actually left the account must be stated: {out2!r}")

        # A bank that sends no commissionInfo must not make one up.
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        silent = CaptureSession(payload={"payload": {"paymentId": "100000000001"}})
        server._require = lambda: silent
        out3 = _run(server.transfer(1000, "+79991234567", from_account="0000000000"))
        check("комиссия" not in out3,
              f"no commissionInfo must mean no claim about the fee: {out3!r}")
    finally:
        server._require = saved
    print("  commission: the fee is reported, not the amount it was charged on")


def test_bill_sections_raise_the_session_level_before_asking():
    """A real unpaid bill was invisible through get_data while the identical call
    succeeded moments later — because session_status() had raised the session level
    in between.

    These endpoints validate the SESSIONID, whose CLIENT window is ~11 minutes,
    not the Bearer, which lasts ~2h. Past the window they answer
    INSUFFICIENT_PRIVILEGES / «Невозможно определить подписчика» — errors an agent
    reads as "you have no bills to pay". Verified live against the bank."""
    class Sec(MobileSession):
        def __init__(self):
            self.raised = 0
            self.seen = []

        def ensure_client_session(self):
            self.raised += 1
            return "CLIENT"

        def ensure_fresh(self, *a, **kw):
            return None

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            self.seen.append(key)
            return {"payload": []}

    for section in ("invoices", "subscription_bills", "subscriptions",
                    "templates", "autopayments", "sbp"):
        s = Sec()
        s.get_data(section)
        check(s.raised == 1,
              f"get_data({section!r}) must raise the session to CLIENT first "
              f"(raised {s.raised}×) — otherwise a lapsed sessionid reads as "
              f"'nothing to pay'")

    # And it must NOT pay that extra ping on sections that never needed it.
    s2 = Sec()
    s2.get_data("cars")
    check(s2.raised == 0,
          "a section that works on the Bearer alone must not cost an extra ping")
    print("  get_data: bill sections raise the session level, the others do not")


def test_filter_sections_refuse_to_pretend():
    """get_data("providers") hits /providers/compatible/filter, which the app calls
    with ?ids=fns-rf,… — with no ids it is a filter with no filter and returns
    nothing. Silently answering "empty" taught the agent the user has no providers."""
    from src.client import TbankApiError

    class Sec(MobileSession):
        def __init__(self):
            self.seen = None
            self.raised = 0

        def ensure_client_session(self):
            # `requisites` joined _SECTION_NEEDS_CLIENT after a live measurement
            # showed /v1/get_requisites refuses an ANONYMOUS session. Counted, not
            # ignored, so this test also pins that the raise happens.
            self.raised += 1
            return "CLIENT"

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            self.seen = (key, overrides)
            return {"payload": {"providers": []}}

    s = Sec()
    for section in ("providers", "requisites"):
        try:
            s.get_data(section)
            failures.append(f"get_data({section!r}) with no arg must raise, not return empty")
        except TbankApiError as e:
            check("ARG_REQUIRED" in str(e.result_code), f"wrong error for {section}: {e}")
            check(len(str(e.message)) > 60,
                  f"the error must explain what to pass for {section}: {e.message}")

    s.get_data("providers", "fns-rf,gibdd-online-rf")
    check(s.seen[1] == {"ids": "fns-rf,gibdd-online-rf"},
          f"the ids must reach the query: {s.seen}")
    check(s.raised >= 1,
          "get_data('requisites') must raise the session to CLIENT first — the "
          "endpoint answers an ANONYMOUS session with REQUEST_RATE_LIMIT_EXCEEDED, "
          "which reads as a volume limit and is not one")
    s.get_data("requisites", "+79991234567")
    check(s.seen[1] == {"pointer": "+79991234567"}, f"the pointer must reach the query: {s.seen}")

    # A section that needs no argument keeps working unchanged.
    s.get_data("loans")
    check(s.seen == ("active_loans", None), f"a plain section must not gain a param: {s.seen}")
    print("  get_data: filter sections demand their argument instead of faking an empty result")


def test_payment_commission_rejects_a_body_it_cannot_use():
    saved = server._require

    class Stub(MobileSession):
        def __init__(self):
            self.seen = None

        def ensure_fresh(self, *a, **kw):
            return None

        def payment_commission(self, b=None):
            self.seen = b
            return {"commission": 0}

    server._require = lambda: Stub()
    try:
        for bad, why in (("", "empty"), ("не json", "not json"), ('{"a": 1}', "no payParameters")):
            out = server.payment_commission(bad)
            check("payParameters" in out or "не JSON" in out,
                  f"a {why} body must be explained, got: {out[:90]}")
            check("Traceback" not in out, f"a {why} body must not raise: {out[:90]}")
        ok = server.payment_commission('{"payParameters": {"account": "1"}}')
        check("commission" in ok, f"a valid body must go through: {ok[:90]}")
    finally:
        server._require = saved
    print("  payment_commission: a body it cannot use is explained, not thrown")


def test_a_refusal_is_not_reported_as_a_possible_charge():
    """Saying "the outcome is unknown" costs the user: it claims money may have moved
    AND blocks the next attempt. A client-side refusal (unresolved recipient, several
    SBP banks, bad phone) and a bank rejection are neither."""
    from src.client import TbankApiError
    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    saved = server._require

    class Refusing(CaptureSession):
        def __init__(self, exc):
            super().__init__()
            self.exc = exc

        def transfer(self, *a, **kw):
            raise self.exc

    try:
        for exc, label in ((TbankApiError("RECIPIENT_MULTIPLE_BANKS", "две штуки"), "refusal"),
                           (TbankApiError("INSUFFICIENT_FUNDS", "нет денег"), "bank rejection")):
            open(os.environ["TBANK_ATTEMPTS"], "w").close()
            server._require = lambda e=exc: Refusing(e)
            out = _run(server.transfer(100, "+79991234567", from_account="0000000000"))
            check("НЕИЗВЕСТЕН" not in out,
                  f"a {label} must not claim the money may have moved: {out}")
            check("деньги на месте" in out.lower(),
                  f"a {label} must say the money is safe: {out}")

            # ...and it must NOT block the next attempt.
            server._require = lambda: CaptureSession()
            again = _run(server.transfer(100, "+79991234567", from_account="0000000000"))
            check("ЗАБЛОКИРОВАН" not in again,
                  f"a {label} must not block the retry: {again}")

        # A transport failure REMAINS unknown and blocking.
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        server._require = lambda: Refusing(ConnectionError("reset"))
        lost = _run(server.transfer(100, "+79991234567", from_account="0000000000"))
        check("НЕИЗВЕСТЕН" in lost, f"a dropped connection is still unknown: {lost}")
        server._require = lambda: CaptureSession()
        blocked = _run(server.transfer(100, "+79991234567", from_account="0000000000"))
        check("ЗАБЛОКИРОВАН" in blocked, f"an unknown outcome must still block: {blocked}")
    finally:
        server._require = saved
    print("  outcomes: refusals and rejections say the money is safe; only transport is unknown")


def test_transfer_inner_is_refused_until_captured():
    """provider='transfer-inner' builds a plausible body ({'bankContract':
    to_account}) but no captured /v1/pay exists to check it against — unlike
    p2p-anybank, which this whole file's fixture is verified against. Refused,
    not sent, until that capture exists."""
    from src.client import TbankApiError
    s = CaptureSession()
    try:
        s.transfer(1000, "40817810000000000000", provider="transfer-inner")
        check(False, "transfer-inner must be refused, not sent")
    except TbankApiError as e:
        check(e.result_code == "NOT_SUPPORTED",
              f"transfer-inner should raise NOT_SUPPORTED, got {e.result_code!r}")
    check(s.url is None, "a refused transfer-inner must never reach the wire")
    print("  transfer-inner: refused until a real /v1/pay capture exists")


def test_the_recipient_bank_the_user_picked_is_the_one_used():
    """The gate required three fields while every agent-facing string promises two, so
    an agent that followed the docs had its chosen SBP bank silently replaced."""
    class Resolver(CaptureSession):
        def __init__(self):
            super().__init__()
            self.resolved = 0

        def resolve_recipient(self, phone):
            self.resolved += 1
            return [
                {"bank_member_id": "111", "masked_fio": "Дефолтный Б.",
                 "pointer_link_id": "aaa", "bank_name": "Дефолт", "is_default_bank": True,
                 "workflow_type": "SBPTransfer", "is_tbank": False},
                {"bank_member_id": "222", "masked_fio": "Выбранный Б.",
                 "pointer_link_id": "bbb", "bank_name": "Выбор", "is_default_bank": False,
                 "workflow_type": "SBPTransfer", "is_tbank": False},
            ]

    # The two ids the docs promise, no masked_fio — the documented call.
    s = Resolver()
    s.transfer(100, "+79991234567", bank_member_id="222", pointer_link_id="bbb",
               account="0000000000")
    pf = s.sent_pay_parameters()["providerFields"]
    check(pf["bankMemberId"] == "222",
          f"the chosen bank was replaced by the default: {pf['bankMemberId']!r}")
    check(pf["pointerLinkId"] == "bbb", f"the chosen link id was replaced: {pf}")
    check(pf.get("maskedFIO") == "Выбранный Б.",
          f"the display name must be filled in for the CHOSEN bank: {pf.get('maskedFIO')!r}")

    # Passing all three still works and costs no lookup.
    s2 = Resolver()
    s2.transfer(100, "+79991234567", bank_member_id="222", masked_fio="Выбранный Б.",
                pointer_link_id="bbb", account="0000000000")
    check(s2.resolved == 0, "a fully specified recipient must not trigger a lookup")
    check(s2.sent_pay_parameters()["providerFields"]["bankMemberId"] == "222",
          "a fully specified recipient must be used verbatim")

    # Nothing passed → auto-resolve to the default, as documented.
    s3 = Resolver()
    s3.transfer(100, "+79991234567", account="0000000000")
    check(s3.sent_pay_parameters()["providerFields"]["bankMemberId"] == "111",
          "with no choice given, the default bank is the documented behaviour")
    print("  recipient: the caller's chosen SBP bank survives; auto-resolve only without one")


RECIPIENT_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "fixtures", "recipient.json")


def recipient_fixture():
    with open(RECIPIENT_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class ResolvingSession(CaptureSession):
    """Answers /v1/get_requisites from the fixture, per pointerSource, and records
    the query it was asked with."""

    # CaptureSession stubs the lookup out; here it is the thing under test.
    resolve_recipient = MobileSession.resolve_recipient

    def __init__(self):
        super().__init__()
        self.fx = recipient_fixture()
        self.asked = []

    def ensure_client_session(self, *a, **kw):
        return None

    def _call_read(self, template_key, *, overrides=None, **kw):
        check(template_key == "get_requisites",
              f"unexpected read during a recipient lookup: {template_key!r}")
        overrides = overrides or {}
        self.asked.append(dict(overrides))
        source = overrides.get("pointerSource")
        return {"payload": self.fx[source]["payload"] if source in self.fx else []}


def test_the_recipients_own_tbank_account_is_found():
    """A phone whose owner banks with T-Bank came back as «Sber and VTB, no T-Bank»:
    only pointerSource=external was ever asked, and withTinkoff=true on that call does
    not fold the internal answer in. The recipient the user could see in the app was
    unreachable through the tool, and the commission preview that seemed to say
    otherwise was an unfinishedFlag=true non-answer."""
    fx = recipient_fixture()
    s = ResolvingSession()
    cands = s.resolve_recipient(fx["phone"])

    check([a.get("pointerSource") for a in s.asked] == ["internal", "external"],
          f"both pointerSources must be asked, internal first: {s.asked}")
    # The client context (appVersion, origin, deviceId, sessionid…) comes from the
    # endpoint template; the overrides are the RECIPIENT half of the query, and that
    # is what must equal the app's — including the two flags the internal call does
    # NOT carry. Sending withTinkoff on it would be the same class of invention as
    # believing withTinkoff on the external one covers T-Bank.
    from src.endpoints import BUILTIN_ENDPOINTS
    template = set(BUILTIN_ENDPOINTS["get_requisites"]["params"])
    for asked, source in zip(s.asked, ("internal", "external")):
        check(set(asked) == set(fx[source]["query_keys"]) - template,
              f"{source} query differs from the app's: ours={sorted(asked)} "
              f"real={sorted(set(fx[source]['query_keys']) - template)}")

    check(len(cands) == 3, f"expected the T-Bank account + two SBP banks: {cands}")
    check(cands[0]["is_tbank"] and cands[0]["bank_name"] == "Т-Банк",
          f"the T-Bank account must be present and first: {cands}")
    check([c["is_tbank"] for c in cands[1:]] == [False, False],
          f"the SBP banks must not be marked as internal: {cands[1:]}")
    check("bankMemberId" not in cands[0]["provider_fields"],
          f"an internal recipient has no bankMemberId — the key must be ABSENT, not "
          f"empty: {cands[0]['provider_fields']}")
    for c in cands[1:]:
        check(c["provider_fields"].get("bankMemberId"),
              f"an SBP recipient must keep its bankMemberId: {c['provider_fields']}")
    check(all(c["masked_fio"] == fx["masked_fio"] for c in cands),
          f"every candidate carries the recipient's name: {cands}")
    print("  resolve: the recipient's own T-Bank account is found, not just SBP")


def test_paying_a_tbank_recipient_sends_the_body_the_app_sends():
    """The gate demanded bank_member_id AND pointer_link_id, so choosing the T-Bank
    account — which has no member id — read as "nothing chosen" and went back to
    auto-resolution. With no default among the SBP banks that surfaced as a refusal
    listing banks the user had not chosen; with one, it silently became a transfer to
    that bank instead (the test below). The body is checked against the REAL signed /v1/pay for exactly this
    recipient shape (captures-pay.xml, 200 + paymentId) — the SBP capture in
    transfer.json cannot stand in for it, because the one field in dispute is the one
    it carries and this one does not."""
    fx = recipient_fixture()
    real = fx["internal_pay"]["pay_parameters"]

    s = ResolvingSession()
    s.transfer(10000, fx["phone"], account="0000000000",
               pointer_link_id=real["providerFields"]["pointerLinkId"])
    got = s.sent_pay_parameters()
    pf = got["providerFields"]

    check(sorted(got) == sorted(real),
          f"payParameters keys differ from the app's internal transfer\n"
          f"    ours={sorted(got)}\n    real={sorted(real)}")
    for field in ("provider", "currency", "cellularService", "frontCamera",
                  "isTransferStatus", "isUrgentTransfer"):
        check(got.get(field) == real.get(field),
              f"payParameters.{field}: ours={got.get(field)!r} real={real.get(field)!r}")
    check("paymentType" not in got,
          "paymentType belongs to payment_commission, not to an internal /v1/pay")
    check(sorted(pf) == sorted(real["providerFields"]),
          f"providerFields keys differ from the app's internal transfer\n"
          f"    ours={sorted(pf)}\n    real={sorted(real['providerFields'])}")
    check("bankMemberId" not in pf,
          f"bankMemberId must not be sent for an internal transfer: {pf}")
    check(pf["pointerLinkId"] == real["providerFields"]["pointerLinkId"],
          f"the chosen T-Bank account was replaced: {pf}")
    check(pf["maskedFIO"] == fx["masked_fio"],
          f"the display name must be resolved for the chosen candidate: {pf}")
    check(pf["pointerType"] == real["providerFields"]["pointerType"],
          f"an internal transfer still uses the phone pointer type: {pf}")

    # The form the app posts alongside — it dropped shortcutId between the two
    # captures, and ours never sent one, so this is now checked rather than skipped.
    check(sorted(s.sent_form()) == sorted(fx["internal_pay"]["form_keys"]),
          f"pay form keys differ\n    ours={sorted(s.sent_form())}"
          f"\n    real={sorted(fx['internal_pay']['form_keys'])}")
    print("  pay: a T-Bank recipient is payable — body matches the real signed /v1/pay")


def test_choosing_the_tbank_account_is_not_overridden_by_a_default_sbp_bank():
    """The money-losing shape of the old gate, and the reason it is the link id alone
    that now counts as a choice.

    A recipient with a T-Bank account AND a DEFAULT SBP bank: the caller passes the
    T-Bank candidate's pointer_link_id, which is everything that candidate has. The
    old gate wanted a bank_member_id too, did not get one, treated the call as
    unspecified and resolved to the default — a completed transfer to the right
    person's OTHER bank, reported as success, with nothing in the result line to show
    the substitution."""
    fx = recipient_fixture()
    tbank_link = fx["internal_pay"]["pay_parameters"]["providerFields"]["pointerLinkId"]

    class WithDefault(ResolvingSession):
        def _call_read(self, template_key, *, overrides=None, **kw):
            payload = super()._call_read(template_key, overrides=overrides, **kw)
            for c in payload["payload"]:
                # The SBP side has a default here; the internal candidate never does
                # in any capture (isDefaultBank is false on all three).
                if c.get("workflowType") == "SBPTransfer":
                    c["isDefaultBank"] = True
                    break
            return payload

    s = WithDefault()
    s.transfer(10000, fx["phone"], account="0000000000", pointer_link_id=tbank_link)
    pf = s.sent_pay_parameters()["providerFields"]
    check(pf["pointerLinkId"] == tbank_link,
          f"the chosen T-Bank account was replaced by the default SBP bank: {pf}")
    check("bankMemberId" not in pf,
          f"the substitution would show up as a bankMemberId appearing: {pf}")
    print("  choice: a default SBP bank cannot override an explicitly chosen T-Bank account")


def test_a_recipient_with_several_accounts_is_never_picked_silently():
    """Three candidates and no default: the tool must refuse and show all three,
    T-Bank among them — picking one would send the money to the wrong account of the
    right person, invisibly."""
    from src.client import TbankApiError

    fx = recipient_fixture()
    s = ResolvingSession()
    try:
        s.transfer(10000, fx["phone"], account="0000000000")
        failures.append("three candidates with no default must not be resolved silently")
    except TbankApiError as e:
        check(e.result_code == "RECIPIENT_MULTIPLE_BANKS",
              f"expected RECIPIENT_MULTIPLE_BANKS, got {e.result_code!r}")
        for bank in ("Т-Банк", "Сбербанк", "ВТБ"):
            check(bank in str(e), f"{bank} missing from the choice offered: {e}")
    check(s.url is None, "a refused transfer must never reach the wire")
    print("  choice: all candidates including T-Bank are surfaced, none auto-picked")


def test_the_recipient_fixture_still_matches_the_capture():
    """Same contract as the /v1/pay fixture: regenerating from the capture must
    reproduce what is committed, or the app changed and a human should look."""
    if not os.path.exists(CAPTURE):
        print(f"  (capture absent at {CAPTURE} — recipient fixture-vs-capture drift "
              f"check skipped; the contract above was verified against the fixture)")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "fixtures"))
    import regen                                            # noqa: E402
    regen.T.CAPTURE = CAPTURE
    try:
        fresh = regen.build_recipient()
    except FileNotFoundError as e:
        # The internal /v1/pay lives in its own capture file. Absent, the drift check
        # cannot run — but say WHICH capture is missing rather than report a failure
        # that looks like the fixture went stale.
        print(f"  (capture absent at {e} — recipient fixture-vs-capture drift check "
              f"skipped; the contract above was verified against the fixture)")
        return
    except Exception as e:                                  # noqa: BLE001
        failures.append(f"recipient.json can no longer be regenerated from the "
                        f"capture ({type(e).__name__}: {e})")
        return
    mine = recipient_fixture()
    for key in ("internal", "external", "commissions", "internal_pay"):
        check(mine.get(key) == fresh.get(key),
              f"fixtures/recipient.json {key} drifted from captures.xml — rerun "
              f"tests/fixtures/regen.py and read the diff before committing")
    print("  fixture vs capture: both get_requisites calls still match")


MOSENERGO = {
    "id": "mosenergosb-moskva-fix", "name": "МосЭнергоСбыт",
    "groupId": "Коммунальные платежи", "paymentType": "Payment",
    "fields": [
        {"id": "account", "name": "Лицевой счет", "regexp": r"^\d{10}$",
         "hint": "10 цифр", "type": "Text",
         "usageTypes": [{"code": "Pay", "required": True, "visible": True,
                         "editable": True, "order": 0}]},
        {"id": "date", "name": "Период оплаты", "regexp": "", "hint": "", "type": "Date",
         "usageTypes": [{"code": "Pay", "required": False, "visible": True,
                         "editable": True, "order": 1}]},
        {"id": "internal", "name": "Служебное", "regexp": "", "hint": "", "type": "Text",
         "usageTypes": [{"code": "Template", "required": True, "visible": True,
                         "editable": True, "order": 0}]},
    ],
}


class BillSession(CaptureSession):
    """A payer whose catalogue and commission are canned, so the validation and the
    refusals can be driven without touching the bank.

    payment_commission is deliberately NOT stubbed: the real method runs and only
    the network hop under it is canned. The old stub took any `body` and never
    touched it, which is how server.pay_bill shipped a JSON *string* where the
    client wants a dict — the real method's first `.get()` raised AttributeError
    on every live bill payment while this suite stayed green."""

    def __init__(self, commission=None):
        super().__init__()
        self.commission_calls = 0
        self.commission_body = None
        self._commission = commission if commission is not None else {
            "value": {"value": 0}, "total": {"value": 100},
            "minAmount": 0.01, "maxAmount": 200000000.0}

    def find_provider(self, provider_id, group="", max_pages=7):
        return dict(MOSENERGO) if provider_id == MOSENERGO["id"] else {}

    def _call_read(self, template_key, *, overrides=None, body=None,
                   path_override=None):
        if template_key != "payment_commission":
            raise AssertionError(f"unexpected read call in a bill test: {template_key}")
        self.commission_calls += 1
        self.commission_body = body
        return self._commission


def test_a_bill_payment_is_refused_before_it_is_sent_when_the_fields_are_wrong():
    """The provider's own schema carries a regexp per field, and it is the only thing
    between a typo and a stranger's utility account being paid. Checked BEFORE the
    request, so a bad value costs nothing — and the refusal has to name what was
    wrong, or the agent will just try again with the same value."""
    import os

    from src import server

    saved = server._require
    try:
        # Wrong format: 9 digits where the provider's regexp demands 10.
        s = BillSession()
        server._require = lambda: s
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        out = _run(server.pay_bill(MOSENERGO["id"], '{"account": "123456789"}', 100,
                              group="ЖКХ", from_account="0000000000"))
        check(s.url is None, f"a refused payment must not reach the bank: {s.url}")
        check(s.commission_calls == 0, "a refused payment must not even be priced")
        check("account" in out and "^\\d{10}$" in out,
              f"the refusal must name the field and its format: {out!r}")

        # A field the provider does not accept at all.
        s2 = BillSession()
        server._require = lambda: s2
        out2 = _run(server.pay_bill(MOSENERGO["id"], '{"schet": "1234567890"}', 100,
                               group="ЖКХ", from_account="0000000000"))
        check(s2.url is None, "an unknown field must not be sent to the bank")
        check("schet" in out2 and "account" in out2,
              f"the refusal must list the fields the provider does accept: {out2!r}")

        # Only Pay-usage fields are required: `internal` is Template-only and its
        # absence must not block a payment.
        s3 = BillSession()
        server._require = lambda: s3
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        out3 = _run(server.pay_bill(MOSENERGO["id"], '{"account": "1234567890"}', 100,
                               group="ЖКХ", from_account="0000000000"))
        check(s3.commission_calls == 1,
              f"a valid payment must be priced exactly once first: {s3.commission_calls}")
        check(s3.url and "/v1/pay" in s3.url,
              f"a valid payment must reach the signed pay path: {s3.url!r}")
        pp = s3.sent_pay_parameters()
        check(pp["provider"] == MOSENERGO["id"], f"wrong provider sent: {pp}")
        check(pp["providerFields"] == {"account": "1234567890"},
              f"providerFields must carry exactly what was validated: {pp}")
        check("paymentType" not in pp,
              "paymentType belongs to the commission body, never to pay")
        check("МосЭнергоСбыт" in out3, f"the answer must name the payee: {out3!r}")

        # An unknown provider is refused with a way forward, not a traceback.
        s4 = BillSession()
        server._require = lambda: s4
        out4 = _run(server.pay_bill("no-such-provider", '{"account": "1"}', 100, group="ЖКХ"))
        check(s4.url is None, "an unknown provider must not be paid")
        check("payment_providers" in out4,
              f"the refusal must name the tool that lists providers: {out4!r}")
    finally:
        server._require = saved
    print("  pay_bill: wrong fields refused before sending, valid one priced then paid")


def test_pricing_a_bill_runs_the_real_commission_contract():
    """server.pay_bill prices through client.payment_commission, and that method
    takes a DICT. It used to be handed json.dumps(...) — a string — so every live
    bill payment died with `AttributeError: 'str' object has no attribute 'get'`
    before any money moved (2026-07-26, found on a real капремонт bill). The old
    BillSession stub swallowed the string, so the suite stayed green. Here the
    real method runs and only the network hop is canned: a type regression on
    this argument fails this test instead of the next live payment."""
    from src import server

    saved = server._require
    try:
        s = BillSession()
        server._require = lambda: s
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        out = _run(server.pay_bill(MOSENERGO["id"], '{"account": "1234567890"}', 100,
                              group="ЖКХ", from_account="0000000000"))
        check("AttributeError" not in out,
              f"pay_bill fed payment_commission something it cannot read: {out!r}")
        check("Оплачено" in out, f"a priced, valid payment must go through: {out!r}")
        # `total` (amount+fee) was computed from the commission preview but never
        # shown — the caller only ever saw the fee, never what actually gets debited.
        check("итого списано 100.00 RUB" in out,
              f"the commission's total must reach the user, not just the fee: {out!r}")
        pp = (s.commission_body or {}).get("payParameters") or {}
        check(pp.get("provider") == MOSENERGO["id"],
              f"the commission body lost the provider: {s.commission_body!r}")
        check(pp.get("providerFields") == {"account": "1234567890"},
              f"the commission body must price the validated fields: {pp!r}")
        check(pp.get("paymentType") == "Payment",
              f"the commission body carries paymentType=Payment: {pp!r}")
    finally:
        server._require = saved
    print("  pay_bill: prices through the REAL payment_commission — the dict "
          "contract is executed, not assumed")


def test_a_bill_payment_respects_the_provider_limits():
    """The commission preview returns the provider's own min/max. Sending an amount
    outside them is a rejection the bank would make anyway — better made here, where
    it costs no attempt and no journal entry."""
    import os

    from src import server

    saved = server._require
    try:
        s = BillSession(commission={"value": {"value": 0}, "total": {"value": 1},
                                    "minAmount": 10.0, "maxAmount": 5000.0})
        server._require = lambda: s
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        low = _run(server.pay_bill(MOSENERGO["id"], '{"account": "1234567890"}', 5,
                              group="ЖКХ", from_account="0000000000"))
        check(s.url is None and "Минимальная" in low,
              f"an amount under the provider minimum must be refused: {low!r}")
        high = _run(server.pay_bill(MOSENERGO["id"], '{"account": "1234567890"}', 9999,
                               group="ЖКХ", from_account="0000000000"))
        check(s.url is None and "Максимальная" in high,
              f"an amount over the provider maximum must be refused: {high!r}")
    finally:
        server._require = saved
    print("  pay_bill: the provider's own min/max are enforced before sending")


def main():
    print("transfer money path:")
    test_a_bill_payment_is_refused_before_it_is_sent_when_the_fields_are_wrong()
    test_pricing_a_bill_runs_the_real_commission_contract()
    test_a_bill_payment_respects_the_provider_limits()
    test_the_fixture_still_matches_the_capture()
    test_body_matches_the_real_pay_request()
    test_the_pay_request_looks_like_the_device_it_claims_to_be()
    test_the_device_profile_is_configuration_not_a_constant()
    test_description_reaches_the_recipient()
    test_transfer_refuses_a_non_positive_amount_before_touching_a_session()
    test_the_chosen_account_is_the_one_debited()
    test_a_lost_transfer_blocks_the_next_identical_one()
    test_the_result_carries_what_the_agent_needs_next()
    test_the_reported_commission_is_the_commission()
    test_transfer_inner_is_refused_until_captured()
    test_bill_sections_raise_the_session_level_before_asking()
    test_filter_sections_refuse_to_pretend()
    test_payment_commission_rejects_a_body_it_cannot_use()
    test_a_refusal_is_not_reported_as_a_possible_charge()
    test_the_recipient_bank_the_user_picked_is_the_one_used()
    test_the_recipient_fixture_still_matches_the_capture()
    test_the_recipients_own_tbank_account_is_found()
    test_paying_a_tbank_recipient_sends_the_body_the_app_sends()
    test_choosing_the_tbank_account_is_not_overridden_by_a_default_sbp_bank()
    test_a_recipient_with_several_accounts_is_never_picked_silently()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
