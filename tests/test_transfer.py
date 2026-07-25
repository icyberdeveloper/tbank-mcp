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

    def resolve_sbp_recipient(self, phone):
        return [{"bank_member_id": "100000000000", "masked_fio": "И. И.",
                 "pointer_link_id": "10000000000", "bank_name": "Банк",
                 "is_default_bank": True}]

    def _call_signed(self, template_key, body_str, extra_query=None):
        self.url, self.headers, self.body = self._signed_parts(
            template_key, body_str, extra_query)
        if self.fail:
            raise ConnectionError("connection reset by peer")
        return self.payload if self.payload is not None else {
            "payload": {"paymentId": "125301542205", "commissionInfo": {"value": 0}}}

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
    print("  body: matches the captured /v1/pay — keys, anti-fraud block, no paymentType")


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
        out = server.transfer(500, "+79991234567", from_account="0000000000")
        check("НЕИЗВЕСТЕН" in out, f"a dropped connection must not read as success: {out}")
        check("force=True" in out, f"the recovery path must be spelled out: {out}")
        check("list_operations" in out, f"the user must be told how to check: {out}")
        first_upid = dead.sent_pay_parameters()["userPaymentId"]

        again = server.transfer(500, "+79991234567", from_account="0000000000")
        check("ЗАБЛОКИРОВАН" in again,
              f"an identical transfer after an unknown outcome must be blocked: {again}")

        ok = CaptureSession()
        server._require = lambda: ok
        forced = server.transfer(500, "+79991234567", from_account="0000000000", force=True)
        check("paymentId=125301542205" in forced, f"force must go through: {forced}")
        check(ok.sent_pay_parameters()["userPaymentId"] == first_upid,
              f"the retry must reuse the original userPaymentId "
              f"({first_upid} → {ok.sent_pay_parameters()['userPaymentId']}), "
              f"otherwise it is a second payment, not a repeat")

        # A CONFIRMED transfer must not block the next one: sending the same person
        # the same amount twice is ordinary.
        third = CaptureSession()
        server._require = lambda: third
        out3 = server.transfer(500, "+79991234567", from_account="0000000000")
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
        server.transfer(700, "+79991234567", from_account="0000000000")
        lost_upid = lost.sent_pay_parameters()["userPaymentId"]
        retry = CaptureSession()
        server._require = lambda: retry
        server.transfer(700, "+79991234567", from_account="0000000000", force=True)
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
        out = server.transfer(1936, "+79991234567", masked_fio="И. И.",
                              from_account="0000000000")
        check("125301542205" in out, f"paymentId must be returned: {out}")
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
        out2 = server.transfer(50, "+79991234567", from_account="0000000000")
        check("не вернул paymentId" in out2, f"a missing paymentId must be flagged: {out2}")
    finally:
        server._require = saved
    print("  result: paymentId, account and amount returned instead of an argument echo")


def test_filter_sections_refuse_to_pretend():
    """get_data("providers") hits /providers/compatible/filter, which the app calls
    with ?ids=fns-rf,… — with no ids it is a filter with no filter and returns
    nothing. Silently answering "empty" taught the agent the user has no providers."""
    from src.client import TbankApiError

    class Sec(MobileSession):
        def __init__(self):
            self.seen = None

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
            out = server.transfer(100, "+79991234567", from_account="0000000000")
            check("НЕИЗВЕСТЕН" not in out,
                  f"a {label} must not claim the money may have moved: {out}")
            check("деньги на месте" in out.lower(),
                  f"a {label} must say the money is safe: {out}")

            # ...and it must NOT block the next attempt.
            server._require = lambda: CaptureSession()
            again = server.transfer(100, "+79991234567", from_account="0000000000")
            check("ЗАБЛОКИРОВАН" not in again,
                  f"a {label} must not block the retry: {again}")

        # A transport failure REMAINS unknown and blocking.
        open(os.environ["TBANK_ATTEMPTS"], "w").close()
        server._require = lambda: Refusing(ConnectionError("reset"))
        lost = server.transfer(100, "+79991234567", from_account="0000000000")
        check("НЕИЗВЕСТЕН" in lost, f"a dropped connection is still unknown: {lost}")
        server._require = lambda: CaptureSession()
        blocked = server.transfer(100, "+79991234567", from_account="0000000000")
        check("ЗАБЛОКИРОВАН" in blocked, f"an unknown outcome must still block: {blocked}")
    finally:
        server._require = saved
    print("  outcomes: refusals and rejections say the money is safe; only transport is unknown")


def test_the_recipient_bank_the_user_picked_is_the_one_used():
    """The gate required three fields while every agent-facing string promises two, so
    an agent that followed the docs had its chosen SBP bank silently replaced."""
    class Resolver(CaptureSession):
        def __init__(self):
            super().__init__()
            self.resolved = 0

        def resolve_sbp_recipient(self, phone):
            self.resolved += 1
            return [
                {"bank_member_id": "111", "masked_fio": "Дефолтный Б.",
                 "pointer_link_id": "aaa", "bank_name": "Дефолт", "is_default_bank": True},
                {"bank_member_id": "222", "masked_fio": "Выбранный Б.",
                 "pointer_link_id": "bbb", "bank_name": "Выбор", "is_default_bank": False},
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


def main():
    print("transfer money path:")
    test_the_fixture_still_matches_the_capture()
    test_body_matches_the_real_pay_request()
    test_the_pay_request_looks_like_the_device_it_claims_to_be()
    test_the_device_profile_is_configuration_not_a_constant()
    test_description_reaches_the_recipient()
    test_the_chosen_account_is_the_one_debited()
    test_a_lost_transfer_blocks_the_next_identical_one()
    test_the_result_carries_what_the_agent_needs_next()
    test_filter_sections_refuse_to_pretend()
    test_payment_commission_rejects_a_body_it_cannot_use()
    test_a_refusal_is_not_reported_as_a_possible_charge()
    test_the_recipient_bank_the_user_picked_is_the_one_used()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
