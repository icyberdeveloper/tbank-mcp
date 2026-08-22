"""A cart that requests more than the shelf holds must be stopped by MCP, named, before
delivery — never handed to the store to fail as a generic code=211.

Incident 2026-08-21: пелёнки ×3 (в наличии 2) и вода ×10 (в наличии 9) прошли все проверки
(число строк, goodsSum, показанные count) и четыре раза уронили checkout на `HTTP 200 /
code=211`, по три delivery-повтора каждый — 12 бесполезных вызовов до сверки остатка. Поле
`countAvailable` было в payload корзины всё это время, но MCP его выбрасывал. Эти тесты
пиннят собственный инвариант корзины (`count > countAvailable`) во всех точках, где корзина
уходит в оформление, и отказ на запись, которая создаёт такой перекос.

    python3 tests/test_grocery_stock_preflight.py
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # repo root, for `src`
sys.path.insert(0, HERE)                     # tests/, for elicit_fake

# Every log the server writes resolves its path at IMPORT time. run_all.py redirects them
# per process; a STANDALONE run of this file must do it too, or the test seeds the user's
# real ~/.local/share/tbank-mcp/.
_TMP = tempfile.mkdtemp(prefix="tbank-stock-preflight-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")

from src import client as client_mod                            # noqa: E402
from src import server                                          # noqa: E402
from src.checkout import CheckoutError                          # noqa: E402
from src.client import MobileSession, TbankApiError             # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def run(session, fn, *a, **kw):
    """Drive a server tool / helper against `session`. Handles async tools."""
    saved = server._require
    server._require = lambda: session
    try:
        out = fn(*a, **kw)
        if inspect.iscoroutine(out):
            out = asyncio.run(out)
        return out
    finally:
        server._require = saved


# ── Cart shapes ──────────────────────────────────────────────────────────────

def overstock_cart():
    """The incident cart: two lines over the shelf, one fine. `delivery.address` present
    so _grocery_delivery needs no client/info round trip."""
    return {"cart": {
        "goodsSum": 3899.74, "sum": 4382.72, "minOrderSum": 500.0,
        "delivery": {"address": {"value": "ул Примерная",
                                 "details": {"street": "Примерная"}},
                     "pointId": "5980"},
        "goods": [
            {"id": "4292616", "name": "Пелёнки 60x90", "count": 3.0,
             "countAvailable": 2.0, "price": {"value": 439.99}},
            {"id": "4408426", "name": "Билаги 500 мл", "count": 10.0,
             "countAvailable": 9.0, "price": {"value": 42.99}},
            {"id": "382032", "name": "Товар в наличии", "count": 1.0,
             "countAvailable": 7.0, "price": {"value": 538.0}},
        ]}}


def clean_cart():
    """Everything within stock, below the free-delivery threshold."""
    return {"cart": {
        "goodsSum": 3000.0, "sum": 3000.0, "minOrderSum": 500.0,
        "nextStepDelivery": {"deliveryPrice": 0.0, "minOrderSum": 3499.0},
        "delivery": {"address": {"value": "ул Примерная",
                                 "details": {"street": "Примерная"}},
                     "pointId": "5980"},
        "goods": [
            {"id": "382032", "name": "Товар", "count": 1.0,
             "countAvailable": 7.0, "price": {"value": 538.0}},
        ]}}


class QuoteSession:
    """Minimal session for the read/quote/checkout HELPERS: they only ever call
    grocery_cart_get and grocery_checkout. grocery_checkout blows up if reached — the whole
    point is that a conflict returns before it."""

    def __init__(self, cart):
        self.cart = cart
        self.checkout_calls = 0

    def ensure_fresh(self, *a, **k):
        return None

    def grocery_cart_get(self, app_id="", point_id=""):
        return self.cart

    def grocery_checkout(self, *a, **k):
        self.checkout_calls += 1
        raise AssertionError("grocery_checkout must not run on a stock conflict")


class CartSession(MobileSession):
    """A real client whose reads come from `cart` and which records every write. __init__
    skips super() on purpose (the real one opens a requests Session and reads session.json);
    _memo is seeded so areaId resolves without downloading the retailers catalogue."""

    def __init__(self, cart):
        self._memo = {"areaId:204:5980": "17040911"}
        self._cart = cart
        self.reads = []
        self.bodies = {}

    def ensure_fresh(self, *a, **k):
        return None

    def ensure_client_session(self, *a, **k):
        return None

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        self.reads.append(key)
        if body is not None:
            self.bodies.setdefault(key, []).append(body)
        if key == "grocery_cart_get":
            return self._cart
        if key == "grocery_client_info":
            return {"deliveryInfo": {"address": {"value": "ул Примерная",
                    "details": {"street": "Примерная"}}}}
        if key == "grocery_cart_set":
            return {"goodsSum": 1.0}
        raise AssertionError("unexpected read: " + key)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_cart_surfaces_all_count_available_conflicts():
    """grocery_cart must name every over-stock SKU and NOT invite checkout — the deficit
    was invisible because countAvailable was dropped from the line render."""
    out = run(QuoteSession(overstock_cart()), server.grocery_cart,
              app_id="204", point_id="5980")
    check("CART_QUANTITY_CONFLICT" in out, f"no conflict block: {out!r}")
    for gid, req, avail in (("4292616", "3", "2"), ("4408426", "10", "9")):
        check(gid in out and f"запрошено={req}" in out and f"в наличии={avail}" in out,
              f"line {gid} requested={req} available={avail} not surfaced: {out!r}")
    check("→ дальше: grocery_checkout" not in out,
          f"a doomed cart still points the agent at checkout: {out!r}")
    print("  grocery_cart: both over-stock SKUs named, checkout hint withheld")


def test_clean_cart_still_offers_checkout():
    """The guard is a no-op on a healthy cart: the normal checkout hint stays."""
    out = run(QuoteSession(clean_cart()), server.grocery_cart,
              app_id="204", point_id="5980")
    check("CART_QUANTITY_CONFLICT" not in out, f"false conflict on a clean cart: {out!r}")
    check("→ дальше: grocery_checkout" in out, f"clean cart lost its hint: {out!r}")
    print("  grocery_cart: a within-stock cart keeps the checkout hint")


def test_quote_and_dry_run_never_call_checkout_on_conflict():
    """_grocery_quote_sum and _do_grocery_checkout(dry_run=True) must refuse BEFORE the
    delivery POST — zero grocery_checkout calls, so no 3×4 s retry burn, no button."""
    s = QuoteSession(overstock_cart())
    quote = run(s, server._grocery_quote_sum, "204", "5980", "")
    check(isinstance(quote, str) and "CART_QUANTITY_CONFLICT" in quote,
          f"quote did not refuse: {quote!r}")

    dry = run(s, server._do_grocery_checkout, "204", "5980", False, "", 0, True)
    check("CART_QUANTITY_CONFLICT" in dry, f"dry-run did not refuse: {dry!r}")
    check(s.checkout_calls == 0,
          f"checkout was invoked {s.checkout_calls}× despite the conflict")
    print("  quote/dry-run: conflict refused before any delivery call")


def test_real_checkout_rechecks_stock_after_confirmation():
    """The real checkout body re-reads the cart and re-checks: a stock drop between the
    button and the order is caught here, so no order is created (dry_run=False)."""
    s = QuoteSession(overstock_cart())
    out = run(s, server._do_grocery_checkout, "204", "5980", False, "", 3907.74, False)
    check("CART_QUANTITY_CONFLICT" in out, f"real checkout did not refuse: {out!r}")
    check(s.checkout_calls == 0,
          f"real checkout posted despite the conflict ({s.checkout_calls} calls)")
    print("  real checkout: fresh-read guard stops an order after the button")


def test_set_cart_refuses_over_stock_write_and_never_posts():
    """grocery_set_cart with a count over the shelf raises CART_QUANTITY_CONFLICT before
    any POST — the write is refused, not silently clamped."""
    s = CartSession(overstock_cart())
    raised = None
    try:
        s.grocery_set_cart([{"id": "4408426", "count": 10}], app_id="204", point_id="5980")
    except TbankApiError as e:
        raised = e
    check(raised is not None and getattr(raised, "result_code", "") == "CART_QUANTITY_CONFLICT",
          f"an over-stock set was not refused: {raised!r}")
    check("grocery_cart_set" not in s.reads,
          f"the cart was WRITTEN despite the conflict: {s.reads}")
    check(raised is not None and "4408426" in raised.message and "в наличии=9" in raised.message,
          f"the refusal did not name the offending SKU: {getattr(raised, 'message', None)!r}")
    print("  grocery_set_cart: an over-stock write is refused, nothing posted")


def test_set_cart_to_available_counts_succeeds():
    """The remedy the incident needed — set exactly the shelf — must go through, so the
    guard never blocks the fix (equal counts are fine)."""
    s = CartSession(overstock_cart())
    s.grocery_set_cart([{"id": "4292616", "count": 2}, {"id": "4408426", "count": 9}],
                       app_id="204", point_id="5980")
    check("grocery_cart_set" in s.reads,
          f"reducing to the shelf was blocked — the fix cannot be applied: {s.reads}")
    posted = {g["id"]: g["count"] for g in s.bodies["grocery_cart_set"][-1]["goods"]}
    check(posted.get("4292616") == 2 and posted.get("4408426") == 9,
          f"the posted counts are not the requested shelf counts: {posted}")
    print("  grocery_set_cart: reducing to countAvailable posts normally")


def test_equal_and_missing_count_available_never_false_positive():
    """requested == available passes; a fractional weight compares without float drift; a
    missing/non-numeric countAvailable is unknown, never a false out-of-stock."""
    goods = [
        {"id": "eq", "count": 2.0, "countAvailable": 2.0},          # equal — ok
        {"id": "weight", "count": 0.57, "countAvailable": 0.57},    # fractional equal — ok
        {"id": "nofield", "count": 99.0},                           # unknown — skip
        {"id": "null", "count": 5.0, "countAvailable": None},       # unknown — skip
        {"id": "text", "count": 5.0, "countAvailable": "n/a"},      # unknown — skip
        {"id": "real", "count": 0.60, "countAvailable": 0.55},      # over — the only hit
    ]
    conflicts = client_mod.cart_quantity_conflicts(goods)
    ids = [c["id"] for c in conflicts]
    check(ids == ["real"], f"expected only the genuine deficit, got {ids}")
    check(str(conflicts[0]["missing"]) == "0.05",
          f"weight deficit drifted off Decimal: {conflicts[0]['missing']!r}")
    print("  detector: equal/fractional/missing never becomes a false conflict")


def test_web_checkout_race_guard_blocks_before_deliveries():
    """A web cart over the shelf (a change after the mobile guard, or a direct client call)
    is stopped BEFORE /deliveries — no order, no payment."""
    result, exc, page = run_web_checkout(cart_goods=[
        {"id": "4292616", "name": "Пелёнки", "count": 3.0, "countAvailable": 2.0},
        {"id": "4408426", "name": "Вода", "count": 10.0, "countAvailable": 9.0},
    ])
    check(isinstance(exc, CheckoutError), f"an over-stock web cart must raise: {exc!r}")
    check(exc is not None and "CART_QUANTITY_CONFLICT" in str(exc),
          f"the web guard did not name the conflict: {exc!r}")
    for step in ("deliveries", "order/create", "payment-gate/payments"):
        check(step not in page.log,
              f"{step} ran despite the stock conflict: {page.log}")
    print("  web checkout: race guard blocks before deliveries/order/payment")


def test_delivery_tier_text_cannot_be_read_as_current_fee():
    """The next-tariff line must be labelled «Следующий тариф» and point at dry_run for the
    real delivery — reading it as the current fee is how goodsSum+8 was typed as the total."""
    out = run(QuoteSession(clean_cart()), server.grocery_cart,
              app_id="204", point_id="5980")
    check("Следующий тариф" in out, f"the tariff line is not labelled as the NEXT step: {out!r}")
    check("checkout dry_run" in out,
          f"the output does not say the real delivery comes from dry_run: {out!r}")
    print("  grocery_cart: the next tariff cannot be mistaken for the current fee")


# ── Minimal fake-Playwright harness for the web checkout ──────────────────────

_CART_RESP = None  # set per test


class _FakePage:
    def __init__(self, cart_goods):
        self.log = []
        self._cart = {"status": 200, "body": {"payload": {"cart": {
            "goodsSum": 3899.74, "goods": cart_goods}}}}

    def goto(self, url, **kw):
        self.log.append("goto")

    def evaluate(self, js, arg=None):
        if "grocery/cart?" in js:
            self.log.append("grocery/cart?")
            return self._cart
        for step in ("deliveries", "order/create", "payment-gate/payments"):
            if step in js:
                self.log.append(step)
                return {"status": 200, "body": {"payload": {}}}
        raise AssertionError(f"unrouted in-page request: {js[:120]}")


class _FakeBrowser:
    def __init__(self, page):
        self.page = page

    def new_context(self, **kw):
        return self

    def add_cookies(self, cookies):
        pass

    def new_page(self):
        return self.page

    def close(self):
        pass


class _FakePlaywright:
    def __init__(self, page):
        self.chromium = self
        self.browser = _FakeBrowser(page)

    def launch(self, **kw):
        return self.browser

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_web_checkout(cart_goods):
    """Drive the REAL src.checkout.checkout() against a fake browser whose web cart carries
    `cart_goods`. Returns (result, exc, page)."""
    import contextlib
    import io

    page = _FakePage(cart_goods)
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywright(page)
    saved = sys.modules.get("playwright.sync_api")
    sys.modules["playwright.sync_api"] = fake_module

    class S:
        mobile_sessionid = "sid"
        access_token = "tok"
        device_id = "DEV-1"
        cookie_str = "__P__wuid=W; api_sso_id=A; sso_used=true"
        sso_login_cookie = "api_sso_id=A"

        def _wide_cookie(self):
            from src.client import wide_cookies
            return wide_cookies(self.cookie_str)

    from src import checkout as co
    result, exc = None, None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = co.checkout(S(), app_id="204", point_id="5980", sum_val=100.0)
    except Exception as e:                                       # noqa: BLE001
        exc = e
    finally:
        if saved is not None:
            sys.modules["playwright.sync_api"] = saved
        else:
            sys.modules.pop("playwright.sync_api", None)
    return result, exc, page


def main():
    print("grocery stock preflight:")
    test_cart_surfaces_all_count_available_conflicts()
    test_clean_cart_still_offers_checkout()
    test_quote_and_dry_run_never_call_checkout_on_conflict()
    test_real_checkout_rechecks_stock_after_confirmation()
    test_set_cart_refuses_over_stock_write_and_never_posts()
    test_set_cart_to_available_counts_succeeds()
    test_equal_and_missing_count_available_never_false_positive()
    test_web_checkout_race_guard_blocks_before_deliveries()
    test_delivery_tier_text_cannot_be_read_as_current_fee()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
