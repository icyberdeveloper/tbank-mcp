"""Fixes that worked but nothing guarded.

Every defect below was found and repaired by an earlier audit. The code is right.
What was missing is the part that keeps it right: a full audit pass reverted each one
in a scratch copy, ran the whole suite, and got «all 14 passed» every time. A fix no
test holds down is a fix that leaves on the next refactor, and the leaving is silent.

So this file is not about new behaviour. It executes six repairs that had no
assertion of their own — three of them on paths where the cost of losing them is a
second payment or a card number in a chat transcript:

1. journal._BLOCKING — the set that stops grocery_checkout paying twice for one cart
   after an unknown outcome. Tested on the transfer side only; emptying it left the
   suite green.
2. grocery_checkout's crash classification. A runtime crash PAST the order/create
   point-of-no-return must be journalled UNKNOWN (blocks retry), not FAILED (invites
   one).
3. card_requisites' masking. The tool appeared in zero test files, so both `if
   reveal:` becoming `if True:` and the audit event disappearing were free changes.
4. grocery_set_cart's CART_READ_FAILED. cart/set is a full replace, so proceeding
   with an unreadable pre-read empties the cart.
5. RobustTLSAdapter's refusal to replay a non-idempotent request after an SSL error.
   The one test that touched the adapter drove a GET, so the branch that protects
   /v1/pay was never entered.
6. orders() printing paymentId — the only place payment_receipt's argument comes from.

    python3 tests/test_unpinned_fixes.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tbank-unpinned-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

import requests                                                    # noqa: E402

from src import journal, observability as obs, server, tls          # noqa: E402
from src.client import MobileSession, TbankApiError                 # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def fresh_journal():
    open(os.environ["TBANK_ATTEMPTS"], "w").close()


def run(session, fn, *a, **kw):
    saved = server._require
    server._require = lambda: session
    try:
        return fn(*a, **kw)
    finally:
        server._require = saved


# ---- 1: the cart that could be paid for twice -----------------------------

def test_an_unknown_checkout_blocks_the_next_one_for_the_same_cart():
    """Executed through journal's own API, with a real attempts file.

    Each status is checked separately, because the blocking set is not one rule: a
    crash at `order_posting` (about to POST order/create) and a confirmed `paid` are
    blocking for opposite reasons, and `started` must NOT block or a checkout that
    died before doing anything could never be retried."""
    fresh_journal()
    cart = "cart-hash-1"

    blocked, _ = journal.is_retry_blocked(cart)
    check(not blocked, "an unknown cart must not be blocked before anything happened")

    for status, must_block in (("started", False), ("order_posting", True),
                               ("order_posted", True), ("unknown", True),
                               ("paid", True), ("failed", False)):
        fresh_journal()
        aid = journal.new_attempt("204", "5980", cart, 100.0)
        journal.record(aid, "checkout", status)
        blocked, last = journal.is_retry_blocked(cart)
        check(blocked is must_block,
              f"status {status!r}: blocked={blocked}, expected {must_block} — "
              f"{'a second payment for the same cart is now possible'
                 if must_block else 'a recoverable failure can never be retried'}")
        if must_block:
            check((last or {}).get("status") == status,
                  f"the blocking event must be reported back: {last}")

    # A DIFFERENT cart is a different question and must go through.
    fresh_journal()
    aid = journal.new_attempt("204", "5980", cart, 100.0)
    journal.record(aid, "checkout", "unknown")
    other, _ = journal.is_retry_blocked("cart-hash-2")
    check(not other, "an unknown outcome for one cart must not block a different cart")
    print("  journal: 4 blocking statuses block, 2 recoverable ones do not, per cart")


def test_the_tool_refuses_the_repeat_and_says_how_to_reconcile():
    """The guard is only worth anything if it reaches the agent as a refusal."""
    fresh_journal()
    goods = [{"id": "1", "count": 1, "price": {"value": 100}}]

    class Stub(MobileSession):
        def __init__(self):
            self._memo = {}
            self.checked_out = 0

        def ensure_fresh(self, *a, **kw):
            return None

        def grocery_cart_get(self, **kw):
            return {"cart": {"goods": goods, "goodsSum": 100.0}}

    chash = journal.cart_hash_of(goods)
    aid = journal.new_attempt("204", "5980", chash, 100.0)
    journal.record(aid, "checkout", "unknown")

    out = run(Stub(), server._do_grocery_checkout, "204", "5980", False)
    check("BLOCKED" in out, f"the repeat must be refused: {out[:200]!r}")
    check("grocery_attempts" in out, f"the refusal must name the reconciliation call: {out!r}")
    check(aid in out, f"the blocking attempt must be identifiable: {out!r}")
    print("  checkout: a repeat of an unknown cart is refused, naming the attempt")


# ---- 2: a crash past the point of no return -------------------------------

def test_a_crash_after_order_create_is_unknown_not_failed():
    """`failed` invites an automatic retry; past order/create an order may exist.

    Driven by making the checkout body raise AFTER the journal has been moved to a
    blocking status — which is exactly the shape of a Playwright or urllib crash
    between order/create and payment."""
    for last_status, expect_unknown in (("order_posting", True),
                                        ("started", False)):
        fresh_journal()
        goods = [{"id": "1", "count": 1, "price": {"value": 100}}]

        class Crashing(MobileSession):
            def __init__(self):
                self._memo = {}

            def ensure_fresh(self, *a, **kw):
                return None

            def grocery_cart_get(self, **kw):
                return {"cart": {"goods": goods, "goodsSum": 100.0}}

            def grocery_checkout(self, **kw):
                # Move the attempt to where the crash happens, then die the way a
                # Playwright/urllib failure does: a bare RuntimeError the checkout
                # module never classified.
                journal.record(kw["attempt_id"], "checkout", last_status)
                raise RuntimeError("browser died")

        out = run(Crashing(), server._do_grocery_checkout, "204", "5980", False)

        aid = journal.latest_for_cart(journal.cart_hash_of(goods))["attempt_id"]
        final = journal.last_status_of_attempt(aid)
        if expect_unknown:
            check("UNKNOWN RESULT" in out,
                  f"a crash past order/create must read as unknown: {out[:180]!r}")
            check(final == "unknown",
                  f"...and be journalled as unknown, not {final!r} — otherwise the "
                  f"next call retries a checkout that may have placed an order")
            blocked, _ = journal.is_retry_blocked(journal.cart_hash_of(goods))
            check(blocked, "an unknown crash must block the next attempt")
        else:
            check("UNKNOWN RESULT" not in out,
                  f"a crash BEFORE order/create is an ordinary failure: {out[:180]!r}")
            check(final == "failed",
                  f"...and must stay retryable, got {final!r}")
    print("  checkout: a crash past order/create is UNKNOWN and blocks; before it, "
          "FAILED and retryable")


# ---- 3: the card number that must not appear ------------------------------

CARD = {"cardHolder": "IVAN IVANOV", "expireDate": "1230",
        "cardNumber": "5536913812345678", "cvv2": "123"}


def test_card_requisites_masks_by_default_and_audits_the_reveal():
    """The only control this repo has over full payment credentials entering a
    transcript. Both halves were free to remove: no test called the tool."""

    class Cards(MobileSession):
        def __init__(self):
            pass

        def ensure_client_session(self):
            return None

        def card_credentials(self, ucid):
            return dict(CARD)

    open(os.environ["TBANK_EVENTS"], "w").close()
    masked = run(Cards(), server.card_requisites, "1236003428")
    # Compared with spaces stripped: the reveal branch prints the PAN in groups of
    # four, so searching for the raw digits misses it entirely — the first version
    # of this line did, and the mutation was caught only by the audit assertion.
    check(CARD["cardNumber"] not in masked.replace(" ", ""),
          f"the full PAN is printed on the DEFAULT call: {masked!r}")
    check(CARD["cvv2"] not in masked, f"the CVV is printed by default: {masked!r}")
    check("**** ****" in masked, f"the number must be shown masked: {masked!r}")
    check(CARD["cardNumber"][-4:] in masked,
          f"the last four are what identify the card: {masked!r}")
    check(not [e for e in obs.recent(50) if e.get("step") == "card_reveal"],
          "a masked call must not record a reveal")

    open(os.environ["TBANK_EVENTS"], "w").close()
    shown = run(Cards(), server.card_requisites, "1236003428", reveal=True)
    check(CARD["cardNumber"] in shown.replace(" ", ""),
          f"reveal=True must actually reveal: {shown!r}")
    check(CARD["cvv2"] in shown, f"reveal=True must include the CVV: {shown!r}")
    check("⚠" in shown, f"the user must be told the data is now in the chat: {shown!r}")
    revealed = [e for e in obs.recent(50) if e.get("step") == "card_reveal"]
    check(len(revealed) == 1,
          f"a reveal must be auditable after the fact: {revealed}")
    check(CARD["cardNumber"] not in json.dumps(revealed, ensure_ascii=False),
          f"the audit record must note THAT it happened, never the values: {revealed}")
    print("  card: masked by default, CVV withheld, the reveal is audited without values")


# ---- 4: the cart that emptied itself --------------------------------------

def test_setting_a_cart_refuses_when_the_current_one_cannot_be_read():
    """cart/set is a full replace, so the pre-read decides what survives."""
    writes = []

    class Broken(MobileSession):
        def __init__(self):
            self._memo = {}

        def grocery_cart_get(self, **kw):
            raise TbankApiError("500", "Сервис временно недоступен")

        def grocery_cart_set(self, *a, **kw):
            writes.append(kw)
            return {"goodsSum": 1.0}

    try:
        Broken().grocery_set_cart([{"id": "42", "count": 2}],
                                  app_id="204", point_id="5980")
        failures.append("an unreadable cart was replaced with just the new items")
    except TbankApiError as e:
        check(e.result_code == "CART_READ_FAILED",
              f"the refusal must name itself: {e.result_code}")
    check(not writes, f"a write went out after an unreadable read: {writes}")
    print("  cart: an unreadable current cart refuses the full-replace write")


# ---- 5: the payment that must not be sent twice ---------------------------

def test_a_tls_error_does_not_replay_a_payment():
    """requests raises SSLError on a mid-stream read — AFTER the body has gone out.
    Replaying that is a second /v1/pay. GET is safe to replay and must still be."""
    sent = []

    class Adapter(tls.RobustTLSAdapter):
        def __init__(self):
            pass

        def send(self, request, **kwargs):
            return tls.RobustTLSAdapter.send(self, request, **kwargs)

    class Req:
        def __init__(self, method):
            self.method = method

    saved_super = requests.adapters.HTTPAdapter.send
    saved_rebuild = tls.rebuild_bundle
    tls.rebuild_bundle = lambda *a, **kw: None

    def always_ssl_error(self, request, **kwargs):
        sent.append(request.method)
        raise requests.exceptions.SSLError("certificate verify failed")

    requests.adapters.HTTPAdapter.send = always_ssl_error
    try:
        for method, expected_attempts in (("POST", 1), ("PUT", 1),
                                          ("GET", 2), ("HEAD", 2)):
            sent.clear()
            try:
                Adapter().send(Req(method))
            except requests.exceptions.SSLError:
                pass
            check(len(sent) == expected_attempts,
                  f"{method}: sent {len(sent)}×, expected {expected_attempts} — "
                  + ("a non-idempotent request was replayed after the body had "
                     "already reached the server"
                     if expected_attempts == 1 else
                     "a safe method lost its retry, so a genuine CA rotation now "
                     "fails instead of recovering"))
    finally:
        requests.adapters.HTTPAdapter.send = saved_super
        tls.rebuild_bundle = saved_rebuild
    print("  tls: POST/PUT are never replayed after an SSL error; GET/HEAD still are")


# ---- 6: the id a receipt cannot be fetched without ------------------------

def test_orders_prints_the_payment_id_a_receipt_needs():
    """payment_receipt(payment_id) has exactly one producer in the read tools, and
    its own docstring points here."""

    class Orders(MobileSession):
        def __init__(self):
            pass

        def ensure_fresh(self, *a, **kw):
            return None

        def orders(self, **kw):
            return [
                {"orderId": "1", "objectType": "grocery", "status": "DONE",
                 "amount": 100, "created": "2026-08-01", "paymentId": "100000000002",
                 "fields": {"applicationName": "ВкусВилл"}},
                {"orderId": "2", "objectType": "grocery", "status": "NEW",
                 "amount": 200, "created": "2026-08-02",
                 "fields": {"applicationName": "Лента"}},
            ]

    out = run(Orders(), server.orders)
    check("paymentId=100000000002" in out,
          f"the paymentId must be printed — without it payment_receipt has no "
          f"argument and no other tool produces one: {out!r}")
    lines = [l for l in out.splitlines() if l.startswith("- ")]
    check(len(lines) == 2, f"both orders must render: {out!r}")
    check("paymentId" not in lines[0] or "paymentId" not in lines[1],
          "an order with no paymentId must not print an empty one")
    print("  orders: the paymentId column is there, and absent when the order has none")


def main():
    print("previously unpinned fixes:")
    test_an_unknown_checkout_blocks_the_next_one_for_the_same_cart()
    test_the_tool_refuses_the_repeat_and_says_how_to_reconcile()
    test_a_crash_after_order_create_is_unknown_not_failed()
    test_card_requisites_masks_by_default_and_audits_the_reveal()
    test_setting_a_cart_refuses_when_the_current_one_cannot_be_read()
    test_a_tls_error_does_not_replay_a_payment()
    test_orders_prints_the_payment_id_a_receipt_needs()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
