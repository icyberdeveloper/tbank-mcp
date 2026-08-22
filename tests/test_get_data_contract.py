"""get_data(section) is one string standing in front of sixty endpoints.

That is a lot of surface behind one argument, and it failed in three directions at
once — each invisible, because every one of them produced a plausible-looking
answer:

1. An unrecognised section fell THROUGH to the internal template key, so the tool
   addressed any of the 155 templates by name: `get_data("v1_pay")` issued
   POST /v1/pay, `get_data("grocery_cart_set")` rewrote the cart,
   `get_data("order_cancel")` cancelled an order. The tool is annotated
   readOnlyHint=True and idempotentHint=True — a host may run it without asking.
2. `arg` was honoured for exactly three sections and silently dropped for the rest,
   while docs/FLOWS §7 told the agent to call three FILTER endpoints that need one.
   `get_data("full_debt_amount", account)` therefore asked the bank about the debt
   of no account, and the empty answer read as «долгов нет».
3. `subscription_bills` selects by `subscriptionIds` and was sent none, so the one
   place an unpaid utility bill actually lives answered about no subscriptions —
   «неоплаченных счетов нет» while a real ГИБДД fine sat there with its
   paymentFields.

    python3 tests/test_get_data_contract.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tbank-getdata-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

from src.client import MobileSession, TbankApiError            # noqa: E402
from src.endpoints import BUILTIN_ENDPOINTS                     # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


class Recorder(MobileSession):
    """A session whose transport records instead of sending."""

    def __init__(self, answers=None):
        super().__init__("sid", "rt")
        self.sent = []
        self.answers = answers or {}
        self.client_raises = 0
        outer = self

        class Resp:
            status_code = 200

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        class Http:
            def _go(self, method, url, **kw):
                path = url.split("t-bank-app.ru")[-1].split("?")[0]
                outer.sent.append((method, path, dict(kw.get("params") or {})))
                return Resp(outer.answers.get(path, {"payload": {}}))

            def get(self, url, **kw):
                return self._go("GET", url, **kw)

            def post(self, url, **kw):
                return self._go("POST", url, **kw)

            def put(self, url, **kw):
                return self._go("PUT", url, **kw)

        self._http = Http()

    def ensure_client_session(self):
        self.client_raises += 1


# The verb is not the signal — the app POSTs to read in one place. Listing the
# exception by name is the point: a section that turns into a POST without landing
# here is either a write behind a read-only tool, or an exception nobody reviewed.
POST_READS = {
    # POST /v1/contact/list — a filter body (`ids=` from the phone's address book),
    # not a mutation. Called with no ids it answers {"isSynchronized": false,
    # "contacts": []}, which states WHY it is empty rather than implying there are
    # none. Verified live.
    "contacts",
    # POST /api/promocodes with an EMPTY body — the client is identified by the
    # session alone, and the answer is a list of promo codes to spend in Город.
    # Nothing is created or consumed by asking. Verified live: it answers the same
    # header and list on every call.
    "promocodes",
}


def sections():
    """Every section name the tool advertises — read off the RUNNING tool, not its
    source. The old version regexed the `_SECTIONS = {` literal for `"key": "` and
    silently dropped any entry the pattern did not match (it lost `sbp_me2me`),
    so the enum tests below were validating a strict SUBSET of the real registry.

    get_data refuses an unknown section with «…Доступные: a, b, c…», where the list
    is `", ".join(sorted(_SECTIONS))` built from the same dict the tool resolves
    against. Parsing that refusal drives the tests from what the code actually
    offers, and cannot fall out of sync with it the way a source regex can."""
    s = Recorder()
    try:
        s.get_data("§not-a-real-section§")
    except TbankApiError as e:
        msg = str(e)
        after = msg.split("Доступные:", 1)[1]
        listed = after.split(".", 1)[0]
        names = sorted(n.strip() for n in listed.split(",") if n.strip())
        assert "sbp_me2me" in names, (
            "the live refusal no longer lists sbp_me2me — the parser or the "
            f"message format changed: {msg!r}")
        return names
    raise AssertionError("get_data accepted a nonsense section instead of refusing")


# ---- 1: the enum is closed ------------------------------------------------

def test_an_unknown_section_cannot_address_a_write_endpoint():
    """The four below are real template keys, and three of them WRITE. Under the old
    fall-through each one was reachable from a tool a host may run unprompted."""
    s = Recorder()
    # grocery_order_create/payment_gate_pay templates were removed with their dead
    # methods; the surviving write-capable keys are what get_data must still refuse.
    dangerous = ["v1_pay", "grocery_cart_set", "order_cancel", "messenger_mark_read"]
    for key in dangerous:
        if key not in BUILTIN_ENDPOINTS:
            continue
        s.sent.clear()
        try:
            s.get_data(key)
            failures.append(
                f"get_data({key!r}) reached {s.sent[-1][0]} {s.sent[-1][1]} — a "
                f"read-only tool addressed a write endpoint by its template name")
        except TbankApiError as e:
            check(e.result_code == "UNKNOWN_SECTION",
                  f"{key}: wrong refusal {e.result_code}")
            check(not s.sent, f"{key}: a request went out anyway: {s.sent}")

    # The refusal has to be actionable — an agent that guessed wrong needs the list.
    try:
        s.get_data("не-такой-секции")
    except TbankApiError as e:
        for name in ("loans", "invoices", "templates"):
            check(name in str(e), f"the refusal must list the valid sections: {e}")
    print(f"  enum: {len(dangerous)} write templates refused, the refusal names the "
          f"sections that exist")


def test_every_advertised_section_actually_resolves():
    """A closed enum is only an improvement if nothing legitimate fell outside it."""
    s = Recorder()
    for name in sections():
        s.sent.clear()
        try:
            s.get_data(name, "1")
        except TbankApiError as e:
            check(e.result_code != "UNKNOWN_SECTION",
                  f"section {name!r} is advertised but refused as unknown")
            continue
        check(s.sent, f"section {name!r} issued no request at all")
        if s.sent:
            method = s.sent[0][0]
            check(method == "GET" or name in POST_READS,
                  f"section {name!r} is a {method} and is not in POST_READS — either "
                  f"it writes (and has no business behind a readOnlyHint tool) or "
                  f"the exception needs stating there with its reason")
    print(f"  enum: all {len(sections())} advertised sections resolve; "
          f"{len(POST_READS)} documented POST-read(s), the rest GET")


# ---- 2: the argument reaches the request ---------------------------------

def test_a_filter_section_sends_its_filter():
    """Parameter names are the app's own, from the captures: account_details?id=,
    full_debt_amount?account=, statement_exist?account=."""
    s = Recorder()
    for name, arg, param in (("account_details", "5000000001", "id"),
                             ("full_debt_amount", "5000000002", "account"),
                             ("statement_exist", "5000000001", "account"),
                             ("statements", "5000000001", "account")):
        s.sent.clear()
        s.get_data(name, arg)
        params = s.sent[0][2]
        check(params.get(param) == arg,
              f"get_data({name!r}, {arg!r}) sent {param}={params.get(param)!r} — "
              f"the filter was dropped and the endpoint asked about nothing")
    print("  arg: four filter sections carry the argument the app sends")


def test_a_filter_section_refuses_without_its_filter():
    """Refusing beats asking about nothing: the empty answer is what read as
    «долгов нет»."""
    s = Recorder()
    for name in ("account_details", "full_debt_amount", "statement_exist",
                 "statements", "providers", "requisites", "sbp_me2me"):
        s.sent.clear()
        try:
            s.get_data(name)
            failures.append(f"get_data({name!r}) without an argument was SENT: {s.sent}")
        except TbankApiError as e:
            check(e.result_code == "ARG_REQUIRED",
                  f"{name}: wrong refusal {e.result_code}")
            check(not s.sent, f"{name}: a request went out anyway")
    # Cover EVERY arg-required section, not a hand-picked subset: the old
    # source-regex sections() dropped sbp_me2me, and this test's list had dropped
    # it too, so a whole arg-required section went unchecked in both places at once.
    covered = {"account_details", "full_debt_amount", "statement_exist",
               "statements", "providers", "requisites", "sbp_me2me"}
    required = set(MobileSession("sid", "rt")._SECTION_ARG)
    check(covered == required,
          f"arg-required sections not all covered by the refusal test: "
          f"missing {required - covered}, stale {covered - required}")
    print("  arg: seven filter sections refuse instead of querying nothing")


# ---- 3: the bills that were never asked for ------------------------------

def test_unpaid_bills_are_asked_for_without_a_filter():
    """`subscriptionIds` NARROWS the answer — it does not enable it.

    An audit finding said the reverse: that sending none made the endpoint answer
    about no subscriptions, and that the app's two-request chain had to be copied.
    Implementing that chain returned nothing at all, and the live numbers say why:

        /v1/subscription/all_bills                    → 2 records, 4 bills
        /v1/subscription/all_bills?subscriptionIds=…  → 0 records
        /v1/subscription/all                          → 0 subscriptions

    So this pins the unfiltered call. If someone re-reads that finding and adds the
    chain back, the tool silently stops reporting bills — the exact failure the
    finding was about — and this is what says so."""
    s = Recorder(answers={
        "/v1/subscription/all_bills": {"payload": [
            {"bills": [{"money": {"value": 5000}}]}]},
        "/v1/subscription/all": {"payload": []},
    })
    out = s.get_data("subscription_bills")

    paths = [p for _, p, _ in s.sent]
    check(paths == ["/v1/subscription/all_bills"],
          f"exactly one request, unfiltered, is the contract: {paths}")
    check("/v1/subscription/all" not in paths,
          "the subscription list is being chained in again — with 0 subscriptions "
          "live, that makes every answer empty")
    check(not (s.sent and s.sent[0][2].get("subscriptionIds")),
          f"subscriptionIds narrows the answer and must not be sent: {s.sent}")
    check(out == [{"bills": [{"money": {"value": 5000}}]}],
          f"the bills must come back: {out!r}")

    # The CLIENT window is the real reason a bill went missing, and it still has to
    # be raised: a lapsed sessionid answers this endpoint with a privileges error
    # that reads exactly like «nothing to pay».
    check(s.client_raises == 1,
          f"the session must be raised to CLIENT first (raised {s.client_raises}×)")
    print("  bills: one unfiltered request, and the CLIENT window is raised first")


def main():
    print("get_data contract:")
    test_an_unknown_section_cannot_address_a_write_endpoint()
    test_every_advertised_section_actually_resolves()
    test_a_filter_section_sends_its_filter()
    test_a_filter_section_refuses_without_its_filter()
    test_unpaid_bills_are_asked_for_without_a_filter()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
