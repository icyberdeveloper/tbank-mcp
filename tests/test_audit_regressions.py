"""Three defects found by the 2026-07-25 audit. Each was invisible in normal use.

1. spending_categories always returned Total 0. `payload["spending"]` is a TREE
   ({summary, intervals:[{aggregated:[…]}]}), not a list of categories, so the old
   `for c in spending:` iterated the two dict KEYS, failed isinstance(dict) on both
   and produced an empty report. Nothing raised: the tool printed "Total: 0.0 RUB"
   over a period whose real spending was 3.87M RUB. An earlier "live validation"
   missed it because it checked for HTTP 200, not for a plausible number.

2. _err() leaked the mobile sessionid — the HMAC key for /v1/pay — into the model's
   context on any network error. Secrets ride in the query string, and requests puts
   the whole URL in the exception text. The existing blob-redactor does NOT catch a
   sessionid: it is 61 chars, but the '.' inside splits it into 32- and 28-char runs,
   both under the 40-char threshold. So this needs a scrub by parameter NAME.

3. cinema_schedule never printed objectId, which cinema_seats/cinema_book require.
   The docs told the agent to take it from there; the tool never emitted one, so the
   whole cinema booking flow dead-ended. concert_schedule had always printed it.

    python3 tests/test_audit_regressions.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from src import server  # noqa: E402
from src.client import MobileSession  # noqa: E402
from src.observability import redact_text  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def money(v):
    return {"value": v, "currency": {"code": 643, "name": "RUB", "strCode": "643"}}


# The real shape, trimmed from captures.xml item 52 (GET /v1/operations_histogram,
# 200, groupBy=category, 31 day-intervals). Kept inline so the test runs on a clean
# clone — the captures are gitignored secrets and are not there.
HISTOGRAM = {"payload": {
    "spending": {
        "summary": money(1500.0),
        "intervals": [
            {"summary": money(1000.0), "start": 1782853200000, "end": 1782939599999,
             "aggregated": [
                 {"groupBy": "24", "groupByKey": "24", "amount": money(700.0),
                  "amountPercent": 70.0, "category": {"id": "24", "name": "Переводы"}},
                 {"groupBy": "13", "groupByKey": "13", "amount": money(300.0),
                  "amountPercent": 30.0, "category": {"id": "13", "name": "Супермаркеты"}},
             ]},
            {"summary": money(500.0), "start": 1782939600000, "end": 1783025999999,
             "aggregated": [
                 {"groupBy": "13", "groupByKey": "13", "amount": money(500.0),
                  "amountPercent": 100.0, "category": {"id": "13", "name": "Супермаркеты"}},
             ]},
        ]},
    "earning": {"summary": money(9000.0), "intervals": [
        {"summary": money(9000.0), "aggregated": [
            {"groupBy": "24", "amount": money(9000.0), "category": {"id": "24", "name": "Переводы"}}]}]},
}}


class HistogramSession(MobileSession):
    """Returns the captured histogram shape instead of calling the API."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def _call_read(self, name, **kw):
        self.calls.append((name, kw))
        return self.payload


def test_spending_categories_walks_the_interval_tree():
    s = HistogramSession(HISTOGRAM)
    rep = s.spending_categories("0000000000", 1782853200000, 1785531599999)

    check(rep["total_spent"] == 1500.0,
          f"total must come from spending.summary.value, got {rep['total_spent']}")
    check(rep["total_earned"] == 9000.0,
          f"earnings must be summed too, got {rep['total_earned']}")

    cats = {c["category"]: c["amount"] for c in rep["categories"]}
    check(cats == {"Супермаркеты": 800.0, "Переводы": 700.0},
          f"categories must be summed ACROSS intervals, got {cats}")
    check([c["category"] for c in rep["categories"]][0] == "Супермаркеты",
          "categories must be sorted by amount, biggest first")
    shares = {c["category"]: c["share_pct"] for c in rep["categories"]}
    check(abs(shares["Супермаркеты"] - 53.33) < 0.01,
          f"share must be of the real total, got {shares}")

    # The exact bug: iterating the dict yields its KEYS and silently produces nothing.
    check(rep["categories"], "the tree walk produced no categories at all")

    # A period with no spending must report zero honestly, not crash.
    empty = HistogramSession({"payload": {"spending": {}, "earning": {}}})
    z = empty.spending_categories(None, 0, 1)
    check(z["total_spent"] == 0.0 and z["categories"] == [],
          f"an empty period must be a clean zero, got {z}")

    # A malformed side must not raise either — this is a read tool on a live API.
    junk = HistogramSession({"payload": {"spending": [], "earning": None}})
    j = junk.spending_categories(None, 0, 1)
    check(j["total_spent"] == 0.0, f"malformed payload must degrade, got {j}")
    print("  spending_categories: sums across intervals, honest zero, survives junk")


def test_err_redacts_the_sessionid():
    # Synthetic, but the SHAPE is what matters and it mirrors a real one: 32 chars,
    # a '.', then the pod suffix. That '.' is exactly why the blob redactor misses it.
    sid = "AbCdEfGhIjKlMnOpQrStUvWxYz012345.authenticon-0123456789-abcde"
    url = ("https://api.t-bank-app.ru/v1/accounts_light?appName=mobile"
           f"&sessionid={sid}&deviceId=00000000-1111-2222-3333-444444444444")

    # Exactly what requests raises when the bank is unreachable.
    out = server._err(requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='api.t-bank-app.ru', port=443): "
        f"Max retries exceeded with url: {url} (Caused by NewConnectionError())"))
    check(sid not in out, f"sessionid leaked verbatim into the tool result: {out}")
    check("<redacted>" in out, f"redaction marker missing: {out}")
    check("ConnectionError" in out, f"the error TYPE must survive redaction: {out}")

    # The blob pattern alone cannot catch it — proving the by-name scrub is required.
    from src.observability import _RE_BLOB
    check(_RE_BLOB.sub("<blob>", sid) == sid,
          "sessionid is now blob-matchable; this test's premise needs revisiting")

    # An API error carrying a URL must be scrubbed on its branch too.
    from src.client import TbankApiError
    api = server._err(TbankApiError("INTERNAL_ERROR", f"failed calling {url}"))
    check(sid not in api, f"sessionid leaked through the TbankApiError branch: {api}")

    from src.client import SessionExpired
    exp = server._err(SessionExpired("SESSION_IS_ABSENT", f"at {url}"))
    check(sid not in exp, f"sessionid leaked through the SessionExpired branch: {exp}")
    check("refresh_session" in exp, "the expiry branch must still name the recovery tool")

    # The user's phone must not survive either — it rides as ?pointer= on SBP lookups.
    check("+79991234567" not in redact_text("https://x/v1/get_requisites?pointer=+79991234567"),
          "the SBP pointer (a real phone number) must be redacted")
    print("  _err: sessionid, phone and URL secrets scrubbed on all three branches")


class ScheduleSession(MobileSession):
    def __init__(self, venues):
        self.venues = venues

    def ensure_fresh(self, *a, **kw):
        return None

    def cinema_schedule(self, event_id="", date="", city="", object_id="", **kw):
        return self.venues


def test_cinema_schedule_emits_objectid():
    """Field names verified against captures2.xml item 733: every one of the 139
    venues carries info.objectId, and the slot carries slotId."""
    venues = [{
        "info": {"objectId": "10031", "objectName": "Синема Парк Метрополис",
                 "geo": {"address": "Ленинградское ш., 16А", "distance": 4200}},
        "events": [{"slots": [
            {"slotId": "132988597", "startTime": "17:30", "hallName": "ЗАЛ №7",
             "prices": {"fix": 880}}]}],
    }]
    original = server._require
    server._require = lambda: ScheduleSession(venues)
    try:
        out = server.cinema_schedule("103693", "2026-07-26")
    finally:
        server._require = original

    check("objectId=10031" in out,
          f"cinema_schedule must print the venue objectId — cinema_seats needs it:\n{out}")
    check("slotId=132988597" in out, f"slotId must still be printed:\n{out}")

    # Both ids in one output is the whole point: either alone is useless downstream.
    check("objectId=10031" in out and "slotId=132988597" in out,
          "slotId without objectId is a dead end — cinema_seats requires both")

    # The docstring must say so, since a skill may not be loaded.
    doc = server.cinema_schedule.__doc__ or ""
    check("objectId" in doc, f"cinema_schedule's docstring never mentions objectId: {doc!r}")
    print("  cinema_schedule: objectId + slotId both emitted, and documented")


def node_bin():
    import glob
    hits = glob.glob(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".venv", "lib", "python*", "site-packages", "playwright", "driver", "node"))
    return hits[0] if hits and os.access(hits[0], os.X_OK) else None


def test_in_page_fetch_gives_up_instead_of_hanging():
    """page.evaluate has no timeout of its own, so before this fix a request that
    never settled hung the checkout forever — worst case between order/create and
    payment, with the money committed and the result unknowable.

    Run the REAL helper the checkout injects, with the REAL fetch, against a real
    server that never answers. A stub fetch would prove nothing here: the whole
    mechanism rests on fetch honouring AbortSignal, so stubbing fetch stubs out the
    thing under test (the first version of this test did exactly that and passed a
    hang off as a timeout)."""
    import json as _json
    import subprocess
    import tempfile
    from src.checkout import _js

    node = node_bin()
    if not node:
        print("  in-page fetch: SKIPPED (no node binary)")
        return

    program = """
import http from 'node:http';
const srv = http.createServer((req, res) => {
  if (req.url.startsWith('/hang')) return;                       // never answers
  if (req.url.startsWith('/junk')) { res.writeHead(500); res.end('<html>not json'); return; }
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({payload: {cart: {goodsSum: 7}}}));
});
await new Promise(r => srv.listen(0, '127.0.0.1', r));
const base = 'http://127.0.0.1:' + srv.address().port;
const run = __RUN__;
const out = {};
out.hung = await run({url: base + '/hang', ms: 300});
out.ok   = await run({url: base + '/ok',   ms: 5000});
out.junk = await run({url: base + '/junk', ms: 5000});
console.log(JSON.stringify(out));
srv.close();
""".replace("__RUN__", _js("return await _f(a.url, {}, a.ms);"))

    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(program)
        tmp = fh.name
    try:
        r = subprocess.run([node, tmp], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        failures.append(f"in-page helper crashed: {r.stderr.strip()[:300]}")
        return
    res = _json.loads(r.stdout.strip() or "{}")

    hung = res.get("hung", {})
    check(hung.get("timedOut") is True,
          f"a server that never answers must time out, got {hung}")
    check(hung.get("status") == 0,
          f"a timed-out request must report status 0, not a real code: {hung}")
    check(hung.get("body") == {}, f"a timed-out request must carry no body: {hung}")

    ok = res.get("ok", {})
    check(ok.get("status") == 200 and ok.get("body") == {"payload": {"cart": {"goodsSum": 7}}},
          f"a healthy request must pass status and body through unchanged: {ok}")
    check(not ok.get("timedOut"), f"a healthy request must not be flagged timed out: {ok}")

    junk = res.get("junk", {})
    check(junk.get("status") == 500 and junk.get("body") == {},
          f"an unparseable body must degrade to {{}} while keeping the status: {junk}")
    check(not junk.get("timedOut"),
          f"a server error is not a timeout and must not be labelled one: {junk}")
    print("  in-page fetch: real server that never answers is aborted; healthy ones pass")


class FakePage:
    """A checkout page whose in-page fetches are scripted per endpoint.

    Routes on the URL the real code builds, so the test exercises the actual request
    sequence — if a step stops being issued, or is issued in the wrong order, the
    recorded log shows it."""

    def __init__(self, routes):
        self.routes = routes
        self.log = []
        # What was SENT, per route key — the request bodies the real code builds and
        # hands to the page. Without these a test can only assert that a step ran,
        # never that it carried the right numbers.
        self.bodies = {}

    def goto(self, url, **kw):
        self.log.append("goto")

    def evaluate(self, js, arg=None):
        for key, responses in self.routes.items():
            if key not in js:
                continue
            self.log.append(key)
            if isinstance(arg, dict) and "body" in arg:
                self.bodies.setdefault(key, []).append(arg["body"])
            r = responses.pop(0) if len(responses) > 1 else responses[0]
            return r
        raise AssertionError(f"unrouted in-page request: {js[:200]}")


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_context(self, **kw):
        return self

    def add_cookies(self, cookies):
        # Recorded, not discarded, and hung off the PAGE so the caller of
        # run_checkout can read it: what lands in the browser jar is a security
        # question. Every part of it used to be re-written as a `.tbank.ru` cookie,
        # including SSO_SESSION — the host-only credential that mints a session
        # without an SMS.
        self.cookies = getattr(self, "cookies", []) + list(cookies)
        self.page.browser_cookies = self.cookies

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, page):
        self.chromium = self
        self._page = page
        self.browser = FakeBrowser(page)

    def launch(self, **kw):
        return self.browser

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_cart_readiness_poll_uses_a_real_clock():
    """The checkout waits for the page's cart API before doing anything, and the
    wait is bounded — because it happens BEFORE the order exists, so timing out
    there is safe and timing out later is not.

    The bound was the sum of the loop's own sleeps, which ignored the probe. Each
    probe is a real in-page fetch capped at FETCH_TIMEOUT_MS (30 s), so a page that
    hung twice blew a "20 second" budget past a minute while the caller sat
    mid-checkout. Executed here with a probe that is genuinely slow."""
    from src.checkout import _poll_until_ready

    # A probe slower than the whole budget: one call and the deadline is gone.
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.25)
        return {"status": 500}

    started = time.monotonic()
    result, waited = _poll_until_ready(slow, lambda r: r["status"] == 200,
                                       timeout_ms=200, interval_ms=50)
    elapsed = time.monotonic() - started
    check(result is None, "a never-ready probe must time out, not return a value")
    check(elapsed < 1.0,
          f"the deadline ignored the probe's own duration: {elapsed:.2f}s for a "
          f"200 ms budget after {len(calls)} probes")
    check(waited >= 200, f"the reported wait must be real time, got {waited} ms")

    # And the result of the probe that succeeded is RETURNED, not thrown away: the
    # caller used to reissue the identical request.
    seen = []

    def flaky():
        seen.append(1)
        return {"status": 200 if len(seen) >= 2 else 503, "n": len(seen)}

    got, _ = _poll_until_ready(flaky, lambda r: r["status"] == 200,
                               timeout_ms=5000, interval_ms=10)
    check(got == {"status": 200, "n": 2},
          f"the successful probe's own result must come back, got {got!r}")
    check(len(seen) == 2, f"expected 2 probes, made {len(seen)}")

    # A probe that raises is a "not ready", not a crash.
    def boom():
        raise RuntimeError("page not up")

    out, _ = _poll_until_ready(boom, lambda r: True, timeout_ms=60, interval_ms=10)
    check(out is None, "a raising probe must read as not-ready, not propagate")
    print("  checkout poll: wall-clock deadline, result reused, errors are not-ready")


def test_the_checkout_browser_never_receives_the_no_otp_credential():
    """checkout() seeds the page's jar from the session, and every part of that
    string was re-written with `domain: ".tbank.ru"` — so SSO_SESSION, which is
    host-only to id.t-bank-app.ru and mints a session with NO SMS, was handed to
    every host under tbank.ru the checkout page talks to."""
    paid = {"status": 200, "body": {"paymentId": "PAY-1",
                                    "stage": {"status": "SUCCESS"}}}
    _, _, page, _ = run_checkout(routes(paid))
    jar = getattr(page, "browser_cookies", None)
    check(jar is not None, "the fake browser recorded no cookies at all")
    names = {c["name"] for c in (jar or [])}
    for secret in ("SSO_SESSION", "SSO_SESSION_STATE", "SSO_CONVERSATION_CSRF_ab"):
        check(secret not in names,
              f"{secret} was written into the browser jar as a .tbank.ru cookie: "
              f"{sorted(names)}")
    check({"api_sso_id", "sso_used"} <= names,
          f"the cookies the checkout actually needs are missing: {sorted(names)}")
    print("  checkout cookies: the no-OTP credential stays out of the browser jar")


def run_checkout(routes, **kw):
    """Drive the REAL checkout() against a fake browser. Returns (result, exc, page, stdout).
    Extra kwargs go straight to checkout() (expected_sum, account, ...)."""
    import contextlib
    import io
    import types

    page = FakePage(routes)
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: FakePlaywright(page)
    saved = sys.modules.get("playwright.sync_api")
    sys.modules["playwright.sync_api"] = fake_module

    class S:
        mobile_sessionid = "sid"
        access_token = "tok"
        device_id = "DEV-1"
        # The real shapes: the login jar holds SSO_SESSION, the per-host string is
        # what every ordinary host receives.
        cookie_str = "__P__wuid=W; api_sso_id=A; sso_used=true; SSO_SESSION=SECRET"
        sso_login_cookie = "SSO_SESSION=SECRET; SSO_SESSION_STATE=st; api_sso_id=A"

        def _wide_cookie(self):
            from src.client import wide_cookies
            return wide_cookies(self.cookie_str)

    from src import checkout as co
    out = io.StringIO()
    result, exc = None, None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            result = co.checkout(S(), app_id="204", point_id="700", sum_val=100.0, **kw)
    except Exception as e:                                  # noqa: BLE001
        exc = e
    finally:
        if saved is not None:
            sys.modules["playwright.sync_api"] = saved
        else:
            sys.modules.pop("playwright.sync_api", None)
    return result, exc, page, out.getvalue()


CART_OK = {"status": 200, "body": {"payload": {"cart": {
    "goodsSum": 1630.0, "goods": [{"id": "g1"}, {"id": "g2"}]}}}}
DELIV_OK = {"status": 200, "body": {"payload": {
    "cartPrice": 1600.2, "delivery": {"selected": {"pointId": "700"}}}}}
AGREEMENT_OK = {"status": 200, "body": {"payload": {"accountId": "1234567890"}}}
EMAIL_OK = {"status": 200, "body": {"email": "user@example.com"}}
ORDER_OK = {"status": 200, "body": {"payload": {"order": {"id": "ORD-1"}}}}


def routes(payment, order_lookup=None, cart=None):
    return {
        "order/create": [ORDER_OK],
        "payment-gate/payments": [payment],
        "grocery/order?": [order_lookup or {"status": 200, "body": {"payload": {"order": {"status": "NEW"}}}}],
        "grocery/cart?": cart or [CART_OK],
        "deliveries": [DELIV_OK],
        "user/payment/account/last": [AGREEMENT_OK],
        "get-customer-information": [EMAIL_OK],
    }


def test_checkout_uses_the_post_delivery_sum_and_stays_off_stdout():
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    res, exc, page, out = run_checkout(routes(paid))
    check(exc is None, f"the happy path must not raise: {exc!r}")
    check(res and res["order_id"] == "ORD-1" and res["payment_id"] == "PAY-1",
          f"happy path must return the order and payment ids: {res}")
    check(res and res["sum"] == 1600.2,
          f"the POST-delivery sum (1600.2) must be charged, not the pre-delivery 1630: {res}")

    # The protocol stream must stay clean through an entire checkout, not just in _log.
    check(out == "", f"checkout wrote to stdout and would corrupt MCP JSON-RPC: {out!r}")

    # And the whole request sequence must have been issued, in order.
    check(page.log.count("payment-gate/payments") == 1,
          f"payment must be attempted exactly once: {page.log}")
    check(page.log.index("deliveries") < page.log.index("order/create")
          < page.log.index("payment-gate/payments"),
          f"deliveries → order/create → payment order violated: {page.log}")
    print("  checkout: charges the post-delivery sum, stdout stays clean")


def test_lost_payment_answer_is_reconciled_not_declared_unknown():
    """The fix that matters most: a timed-out payment whose order really was paid
    used to be reported UNKNOWN, blocking retries and sending the user to reconcile
    by hand. A lost response is not an unpaid order."""
    timed_out = {"status": 0, "body": {}, "timedOut": True}
    lookup_paid = {"status": 200, "body": {"payload": {"order": {"status": "PAID"}}}}
    res, exc, page, _ = run_checkout(routes(timed_out, order_lookup=lookup_paid))
    check(exc is None, f"a payment confirmed by lookup must not raise: {exc!r}")
    check(res and res["order_id"] == "ORD-1",
          f"the paid order must be returned: {res} / {exc!r}")
    check(res and "note" in res,
          "the result must say it was confirmed by lookup, not by the payment response")
    check("grocery/order?" in page.log, f"the order was never read back: {page.log}")

    # Genuinely unpaid → still UNKNOWN, and the message must carry what was observed.
    from src.checkout import CheckoutUnknown
    lookup_new = {"status": 200, "body": {"payload": {"order": {"status": "NEW"}}}}
    res2, exc2, _, _ = run_checkout(routes(timed_out, order_lookup=lookup_new))
    check(isinstance(exc2, CheckoutUnknown),
          f"an unconfirmed payment must stay UNKNOWN, got {res2} / {exc2!r}")
    check("TIMED OUT" in str(exc2),
          f"the message must say the request timed out: {exc2}")
    check("ORD-1" in str(exc2), f"the message must name the order to reconcile: {exc2}")

    # A declined payment (server answered) must also stay unknown, without claiming a timeout.
    declined = {"status": 200, "body": {"stage": {"status": "DECLINED"}}}
    _, exc3, _, _ = run_checkout(routes(declined, order_lookup=lookup_new))
    check(isinstance(exc3, CheckoutUnknown), f"a declined payment must be UNKNOWN: {exc3!r}")
    check("TIMED OUT" not in str(exc3),
          f"a server answer must not be reported as a timeout: {exc3}")
    print("  checkout: lost answer reconciled by lookup, real failures still UNKNOWN")


def test_the_sum_the_user_approved_is_the_sum_that_gets_paid():
    """The user confirms a cart total, and the backend then revises it TWICE before
    anything is charged: the web cart replaces the mobile sum, and deliveries'
    cartPrice replaces that (weight items recompute — the fixtures here are the real
    1630.00 → 1600.20). Both revisions were applied silently, logged to stderr only,
    so a checkout could charge a number the user never saw.

    ticket_pay has had this guard all along (server.py, 0.01 ₽ tolerance). This is
    the same guard on the other money path, and it must refuse in the window where
    refusing is still free — before order/create, so the attempt stays a retryable
    CheckoutError rather than the UNKNOWN that blocks the cart afterwards."""
    from src.checkout import CheckoutError
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}

    # Approving the pre-delivery number does NOT authorise the post-delivery one.
    res, exc, page, _ = run_checkout(routes(paid), expected_sum=1630.0)
    check(isinstance(exc, CheckoutError),
          f"a 29.80 ₽ divergence must refuse, got {res} / {exc!r}")
    check("1600.2" in str(exc) and "1630" in str(exc),
          f"the refusal must name both numbers so the agent can re-confirm: {exc}")
    check("order/create" not in page.log,
          f"the refusal came too late — an order was created: {page.log}")
    check("payment-gate/payments" not in page.log,
          f"money moved despite the divergence: {page.log}")

    # Approving the real number goes through, and the tolerance is not zero.
    res2, exc2, _, _ = run_checkout(routes(paid), expected_sum=1600.2)
    check(exc2 is None and res2 and res2["sum"] == 1600.2,
          f"the approved sum must pay: {res2} / {exc2!r}")
    res3, exc3, _, _ = run_checkout(routes(paid), expected_sum=1600.21)
    check(exc3 is None, f"a 0.01 ₽ rounding difference must not block a checkout: {exc3!r}")

    # Omitted ⇒ the old behaviour, so an existing caller is never broken by the guard.
    res4, exc4, _, _ = run_checkout(routes(paid))
    check(exc4 is None and res4 and res4["sum"] == 1600.2,
          f"without expected_sum the checkout must behave as before: {res4} / {exc4!r}")
    print("  checkout: pays only the sum the user approved, refuses before the order exists")


def test_the_caller_can_choose_which_account_pays():
    """The tool's docstring promised «первый Current RUB с балансом» and the code did
    something else entirely — it used whatever account the bank remembered from the
    user's last in-app grocery payment. The plumbing to override it existed at every
    layer and was wired at none, and `X or account` meant an explicit choice lost to
    the bank's memory whenever that endpoint answered, which is always."""
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}

    res, exc, page, _ = run_checkout(routes(paid), account="0000000000")
    check(exc is None, f"an explicit account must not break the checkout: {exc!r}")
    body = json.loads(page.bodies["payment-gate/payments"][-1])
    check(body["paymentMethod"]["agreement"] == "0000000000",
          f"the caller's account must win over the bank's last-used one: {body}")
    check(res and res["account"] == "0000000000",
          f"the result must name the debited account so the tool can report it: {res}")

    # Default unchanged: the bank's last-used account, and it is still reported.
    res2, _, _, _ = run_checkout(routes(paid))
    check(res2 and res2["account"] == AGREEMENT_OK["body"]["payload"]["accountId"],
          f"without a choice the bank's account is used and named: {res2}")
    print("  checkout: the caller picks the account, and the answer says which paid")


# The store answering HTTP 200 with an error code of its own — the shape a real
# outage took: the delivery request reached ВкусВилл, and ВкусВилл said no.
DELIV_STORE_DOWN = {"status": 200, "body": {"status": "Error", "payload": {
    "code": "211", "message": "Сервис временно недоступен"}}}
DELIV_BAD_REQUEST = {"status": 400, "body": {"errorMessage": "unknown pointId"}}


def test_a_quote_prices_the_order_without_creating_one():
    """The caller cannot compute the number that will be charged: weight goods
    (бананы 0.8, виноград 0.54) are repriced by the backend during delivery, so the
    cart total and the charge are different numbers — 1630.00 and 1600.20 here.
    Before dry_run the only way to learn the second one was to attempt a real
    checkout and be refused by the expected_sum guard, which cost a whole
    user-visible attempt per guess.

    A quote must therefore return the POST-delivery sum, and must not touch either
    call that commits money."""
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    res, exc, page, _ = run_checkout(routes(paid), dry_run=True)

    check(exc is None, f"a dry run must not raise: {exc!r}")
    check(res and res.get("dry_run") is True and res.get("status") == "QUOTE",
          f"the quote must announce itself as one: {res}")
    check(res and res.get("sum") == 1600.2,
          f"the quote must be the post-delivery charge, not the cart total: {res}")
    check(res and res.get("pre_delivery_sum") == 1630.0,
          f"the quote must carry the pre-delivery sum too, or the user cannot be "
          f"told what moved: {res}")
    check("deliveries" in page.log,
          f"a quote that skips deliveries has not priced anything: {page.log}")
    for committing in ("order/create", "payment-gate/payments"):
        check(committing not in page.log,
              f"a dry run POSTed {committing} — it is not a dry run: {page.log}")


def test_a_store_that_blinks_does_not_cost_the_user_an_attempt():
    """HTTP 200 + an app error code means the request reached the store and the STORE
    refused — nothing has been posted, and the same call seconds later routinely
    works. Three consecutive user-visible checkouts died on one such blip because the
    first refusal ended the attempt."""
    import src.checkout as co
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    r = routes(paid)
    r["deliveries"] = [DELIV_STORE_DOWN, DELIV_OK]

    saved = co.DELIVERY_RETRY_DELAY_MS
    co.DELIVERY_RETRY_DELAY_MS = 1                  # keep the suite fast, keep the loop real
    try:
        res, exc, page, _ = run_checkout(r)
    finally:
        co.DELIVERY_RETRY_DELAY_MS = saved

    check(exc is None and res and res.get("sum") == 1600.2,
          f"a single store-side refusal must be retried, not surfaced: {res} / {exc!r}")
    check(page.log.count("deliveries") == 2,
          f"expected exactly one retry, got {page.log.count('deliveries')} attempts: {page.log}")


def test_a_store_that_stays_down_is_named_as_the_cause():
    """`deliveries failed (http=200, code=211)` said nothing about WHOSE fault it was
    or whether retrying could ever help, so the agent had to reconstruct that from
    diagnostics() afterwards. The message must carry the verdict itself."""
    from src.checkout import CheckoutError
    import src.checkout as co
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    r = routes(paid)
    r["deliveries"] = [DELIV_STORE_DOWN]            # the same answer every time

    saved = co.DELIVERY_RETRY_DELAY_MS
    co.DELIVERY_RETRY_DELAY_MS = 1
    try:
        res, exc, page, _ = run_checkout(r)
    finally:
        co.DELIVERY_RETRY_DELAY_MS = saved

    check(isinstance(exc, CheckoutError),
          f"a store that never accepts the delivery must fail the checkout: {res} / {exc!r}")
    check(page.log.count("deliveries") == co.DELIVERY_TRIES,
          f"the retry budget must be spent before giving up: {page.log}")
    msg = str(exc)
    check("магазин" in msg.lower() or "Магазин" in msg,
          f"the refusal must name the store as the cause: {msg}")
    check("НЕ создан" in msg, f"the refusal must say no order exists: {msg}")
    check("211" in msg, f"the store's own code must survive into the message: {msg}")
    check("order/create" not in page.log,
          f"an order was created despite no delivery: {page.log}")


def test_a_delivery_request_that_is_simply_wrong_is_not_retried():
    """A 4xx is the checkout's own fault (wrong store context, dead session) and will
    answer the same way forever. Retrying it only makes the user wait three times as
    long for the same failure."""
    from src.checkout import CheckoutError
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    r = routes(paid)
    r["deliveries"] = [DELIV_BAD_REQUEST]

    res, exc, page, _ = run_checkout(r)
    check(isinstance(exc, CheckoutError), f"a 400 must fail the checkout: {res} / {exc!r}")
    check(page.log.count("deliveries") == 1,
          f"a 4xx must not be retried, got {page.log.count('deliveries')} attempts")
    check("повтор не поможет" in str(exc),
          f"the message must stop the agent from retrying: {exc}")


def test_a_delivery_call_that_never_landed_is_not_success():
    """`_f` reports an aborted or failed fetch as status 0 with an empty body, and the
    old check — `status >= 400 or an error message` — let that through as a healthy
    delivery. The slots were then never initialised and the checkout went on to create
    an order against a sum nothing had confirmed."""
    from src.checkout import CheckoutError
    import src.checkout as co
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    r = routes(paid)
    r["deliveries"] = [{"status": 0, "body": {}, "timedOut": True, "error": "AbortError"}]

    saved = co.DELIVERY_RETRY_DELAY_MS
    co.DELIVERY_RETRY_DELAY_MS = 1
    try:
        res, exc, page, _ = run_checkout(r)
    finally:
        co.DELIVERY_RETRY_DELAY_MS = saved

    check(isinstance(exc, CheckoutError),
          f"a delivery that never landed must stop the checkout: {res} / {exc!r}")
    check("order/create" not in page.log,
          f"an order was created on top of a delivery call that never happened: {page.log}")
    check(page.log.count("deliveries") == co.DELIVERY_TRIES,
          f"a dropped fetch is exactly what a retry is for: {page.log}")
    check("не дошёл" in str(exc), f"the message must name the network as the cause: {exc}")


def test_a_refused_sum_names_the_number_to_pass_next():
    """The guard already named both sums, and the agent still came back with 3701 and
    then 3700 for a 3700.63 cart — it re-typed the number instead of copying it. The
    refusal now spells out the parameter, so there is nothing left to re-type."""
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    _, exc, _, _ = run_checkout(routes(paid), expected_sum=1630.0)
    check("expected_sum=1600.2" in str(exc),
          f"the refusal must hand over the exact literal to pass: {exc}")


def test_a_quote_is_not_an_attempt():
    """Journal attempts are keyed by cart hash and the NEWEST one decides whether a
    retry is blocked. A preview that recorded an attempt would therefore lift the
    block an unresolved real checkout had put in place — turning a "the order may
    already exist" hold into a silent second order. A quote posts nothing, so it
    records nothing; and a cart that is already blocked does not get previewed
    either, because the answer the agent needs there is reconciliation, not a price."""
    from src import journal
    from src import server as S

    goods = [{"id": "g1"}, {"id": "g2"}]
    quote = {"dry_run": True, "status": "QUOTE", "sum": 1600.2,
             "pre_delivery_sum": 1630.0, "item_count": 2, "pre_item_count": 2,
             "delivery_point_id": "700", "delivery_tries": 1}

    class FakeSession:
        def ensure_fresh(self):
            pass

        def grocery_cart_get(self, app_id="", point_id=""):
            return {"cart": {"goods": goods, "goodsSum": 3700.63}}

        def grocery_checkout(self, **kw):
            check(kw.get("dry_run") is True,
                  f"the tool asked for a quote and the session ran a real checkout: {kw}")
            return quote

    saved_require = S._require
    S._require = lambda: FakeSession()
    try:
        before = len(journal.recent(0))
        out = S._do_grocery_checkout("204", "700", False, dry_run=True)
        after = len(journal.recent(0))

        check(after == before,
              f"a dry run wrote {after - before} journal record(s) — a preview must not "
              f"become the newest attempt for this cart")
        check("expected_sum=1600.2" in out,
              f"the quote must hand the agent the exact call to make next: {out!r}")
        check("1600.2" in out and "НЕ создан" in out,
              f"the quote must state the price and that nothing was created: {out!r}")

        # Same cart, now with an unresolved real attempt on file.
        chash = journal.cart_hash_of(goods)
        aid = journal.new_attempt("204", "700", chash, 3700.63)
        journal.record(aid, "order_create", "unknown")
        blocked_out = S._do_grocery_checkout("204", "700", False, dry_run=True)
        check("BLOCKED" in blocked_out,
              f"a cart held for reconciliation must not be previewed either: {blocked_out!r}")
    finally:
        S._require = saved_require


def test_the_checkout_tool_runs_playwright_off_the_event_loop():
    """sync_playwright() raises "It looks like you are using Playwright Sync API
    inside the asyncio loop" when called in FastMCP's loop, which is where sync
    tools run — so grocery_checkout is an async tool that offloads to a worker
    thread. The fix was protected by a docstring only: every checkout test drives
    src.checkout.checkout() directly and never touches the tool, so making it sync
    again would break every real checkout with a green suite.

    Asserted by running the tool inside a real event loop and recording the thread
    its body executed on."""
    import asyncio
    import threading

    from src import server as S

    loop_thread = None
    body_thread = []

    def fake_body(*a, **kw):
        body_thread.append(threading.current_thread().ident)
        return "OK: ran"

    fn = getattr(S.grocery_checkout, "fn", S.grocery_checkout)
    if not asyncio.iscoroutinefunction(fn):
        # Reported, not awaited: awaiting a sync tool raises TypeError and the
        # reader gets a traceback instead of the reason.
        failures.append("grocery_checkout is no longer a coroutine — a sync tool "
                        "runs in FastMCP's event loop, where sync_playwright() "
                        "raises and every real checkout fails")
        return

    saved = S._do_grocery_checkout
    S._do_grocery_checkout = fake_body
    try:
        async def drive():
            nonlocal loop_thread
            loop_thread = threading.current_thread().ident
            # expected_sum is required for a real headless checkout (no elicitation to
            # confirm the sum); without it the tool refuses and returns a quote instead
            # of reaching the body. Pass it so this test isolates the off-loop property.
            return await S.grocery_checkout("204", "5980", expected_sum=100.0)
        out = asyncio.run(drive())
    finally:
        S._do_grocery_checkout = saved

    check(out == "OK: ran", f"the tool must return the body's answer: {out!r}")
    check(body_thread and body_thread[0] != loop_thread,
          f"the checkout body ran ON the event loop thread ({loop_thread}) — "
          f"sync_playwright() would raise there")
    print("  checkout tool: coroutine, and its body runs off the event loop")


def test_payment_gate_problem_json_reaches_the_user():
    """A 4xx from the payment gate arrives as problem+json, and its title/detail is
    the only actionable cause ("Недостаточно средств"). It used to land in
    attempts.jsonl and nowhere else: the tool said "payment not SUCCESS, http=422"
    and the agent went hunting for session bugs while the account was simply short
    of money. The message must carry title/detail, and the diagnostics event must
    carry the problem `type` as app_code instead of an empty string."""
    from src import observability as obs
    from src.checkout import CheckoutUnknown

    problem = {"status": 422, "body": {
        "type": "payment-gate/balance-otb-is-spent",
        "title": "Недостаточно средств", "status": 422,
        "detail": "Операция не может быть выполнена, недостаточно средств"}}
    lookup_new = {"status": 200, "body": {"payload": {"order": {"status": "CREATED"}}}}

    events = []
    saved_emit = obs.emit
    obs.emit = lambda step, **f: events.append((step, f))
    try:
        _, exc, _, _ = run_checkout(routes(problem, order_lookup=lookup_new))
    finally:
        obs.emit = saved_emit

    check(isinstance(exc, CheckoutUnknown), f"a 422 must stay UNKNOWN: {exc!r}")
    check("Недостаточно средств" in str(exc),
          f"the gate's title must reach the user: {exc}")
    check("недостаточно средств" in str(exc).split("Недостаточно средств", 1)[1],
          f"the gate's detail must reach the user too: {exc}")
    check("balance-otb-is-spent" in str(exc),
          f"the problem type must reach the user: {exc}")

    pay_events = [f for step, f in events if step == "payment"]
    check(bool(pay_events), f"no payment event was emitted: {events}")
    check(pay_events[-1].get("app_code") == "payment-gate/balance-otb-is-spent",
          f"diagnostics must carry the problem type as app_code: {pay_events[-1]}")
    print("  checkout: the gateway's problem+json reaches the user and diagnostics")


def test_cart_readiness_distinguishes_not_up_from_empty():
    """The poll used to wait for a NON-EMPTY cart, so a genuinely empty cart burned
    the full deadline and then reported the wrong cause."""
    from src.checkout import CheckoutError

    # Never comes up: must fail fast-ish and promise it is safe to retry.
    # The deadline is passed in rather than left at the production 20 s, which this
    # case burned in real time — 41 sleeps of 500 ms, 40% of the whole suite — to
    # assert a message. What is under test is the branch, not the constant.
    down = {"status": 502, "body": {}}
    res, exc, page, _ = run_checkout(routes(
        {"status": 200, "body": {}}, cart=[down]), cart_ready_timeout_ms=300)
    check(isinstance(exc, CheckoutError), f"an API that never answers must raise: {exc!r}")
    check("never brought its cart API up" in str(exc), f"wrong cause reported: {exc}")
    check("safe to retry" in str(exc), f"must state no order was created: {exc}")
    check("order/create" not in page.log, f"nothing may be ordered: {page.log}")

    # API healthy but the cart is empty: a DIFFERENT, actionable message.
    empty = {"status": 200, "body": {"payload": {"cart": {"goodsSum": 0, "goods": []}}}}
    res2, exc2, page2, _ = run_checkout(routes({"status": 200, "body": {}}, cart=[empty]))
    check(isinstance(exc2, CheckoutError), f"an empty cart must raise: {exc2!r}")
    check("did not sync" in str(exc2),
          f"an empty cart must be reported as a sync problem, not a dead API: {exc2}")
    check("order/create" not in page2.log, f"nothing may be ordered: {page2.log}")

    # Slow start: 502 then 200 — must recover and go on to order.
    slow = [{"status": 502, "body": {}}, CART_OK, CART_OK, CART_OK]
    res3, exc3, page3, _ = run_checkout(routes(
        {"status": 200, "body": {"paymentId": "P", "stage": {"status": "SUCCESS"}}}, cart=slow))
    check(exc3 is None and res3, f"a slow page must recover, got {exc3!r}")
    print("  checkout: 'API down', 'cart empty' and 'slow start' are told apart")


def test_deliveries_error_envelope_is_not_missed():
    """The lifestyle/Город API can fail a step with HTTP 200 +
    {"status":"Error","payload":{"code","message"}} — the same envelope
    client.py's _unwrap() checks for on the mobile-API path. checkout.py's
    deliveries check only looked at HTTP>=400 and a top-level errorMessage, so
    this envelope shape passed through unnoticed and the checkout went on to
    pay for an order whose delivery was never actually set up."""
    from src.checkout import CheckoutError

    deliv_error = {"status": 200, "body": {
        "status": "Error", "payload": {"code": "NO_SLOTS", "message": "Нет слотов доставки"}}}
    paid = {"status": 200, "body": {"paymentId": "PAY-1", "stage": {"status": "SUCCESS"}}}
    r = routes(paid)
    r["deliveries"] = [deliv_error]
    res, exc, page, _ = run_checkout(r)
    check(isinstance(exc, CheckoutError), f"an error envelope on deliveries must raise: {exc!r}")
    check("NO_SLOTS" in str(exc), f"the envelope's code must reach the message: {exc}")
    check("order/create" not in page.log,
          f"a rejected delivery must stop the flow before ordering: {page.log}")
    print("  checkout: an HTTP-200 error envelope on deliveries is not mistaken for success")


def test_messenger_ids_are_validated_before_they_reach_a_url():
    """conversation_id/message_id are agent-supplied — possibly copied straight out
    of a chat message the agent just read — and used to be spliced unvalidated and
    unencoded into an f-string request path. A value containing "/", "..", "?", "#"
    or whitespace must be rejected before any network call is even attempted, not
    sent to the bank as-is."""
    from src.client import TbankApiError

    s = MobileSession.__new__(MobileSession)
    calls = []
    s._call_read = lambda *a, **kw: (calls.append((a, kw)), {})[1]
    s._messenger_read = lambda *a, **kw: (calls.append((a, kw)), [])[1]

    for bad in ("../../etc", "a/b", "a?b", "a#b", "a b", ""):
        try:
            s.messenger_messages(bad)
            failures.append(f"messenger_messages accepted a bad conversation_id: {bad!r}")
        except TbankApiError:
            pass
        try:
            s.messenger_send(bad, "hi")
            failures.append(f"messenger_send accepted a bad conversation_id: {bad!r}")
        except TbankApiError:
            pass

    check(not calls, f"a bad id must be rejected before any network call, got: {calls}")

    # A well-formed id must still work, unchanged.
    s.messenger_messages("c-1")
    s.messenger_hints("c-1")
    s.messenger_faq("c-1")
    s.messenger_send("c-1", "hi")
    s.messenger_mark_read("c-1", "m-1")
    check(len(calls) == 5,
          f"well-formed ids must still reach the network: {len(calls)} calls, {calls}")
    print("  messenger: conversation_id/message_id are validated before hitting the network")


def test_invest_operations_limit_zero_does_not_ask_the_bank_for_zero():
    """The tool's docstring promises limit=0 means "everything the bank
    returned" — but the bank does not have an "all" mode, and sending it a
    literal limit=0 got back one malformed row instead of the real history
    (confirmed live). limit<=0 must ask the bank for a large upstream count,
    not forward the caller's 0 verbatim."""
    s = MobileSession.__new__(MobileSession)
    sent = {}

    def fake_call_read(key, *, overrides=None, body=None, path_override=None):
        sent.update(overrides or {})
        return {"items": [{"id": "1"}, {"id": "2"}], "hasNext": False}

    s._call_read = fake_call_read
    ops, has_next = s.invest_operations("2000000001", limit=0)
    check(sent.get("limit") not in ("0", 0),
          f"limit=0 must not be forwarded to the bank as a literal 0: sent={sent}")
    check(int(sent.get("limit", 0)) >= 100,
          f"limit=0 must ask for a generously large upstream count, sent={sent}")
    check(len(ops) == 2, f"the (stubbed) full answer must still come through: {ops}")

    # A real, positive limit must still be forwarded unchanged.
    sent.clear()
    s.invest_operations("2000000001", limit=10)
    check(sent.get("limit") == "10", f"a real limit must reach the bank unchanged: {sent}")
    print("  invest_operations: limit=0 asks the bank for everything, not literally 0")


def main():
    print("audit regressions (2026-07-25):")
    cases = [
        test_spending_categories_walks_the_interval_tree,
        test_err_redacts_the_sessionid,
        test_cinema_schedule_emits_objectid,
        test_in_page_fetch_gives_up_instead_of_hanging,
        test_the_checkout_browser_never_receives_the_no_otp_credential,
        test_checkout_uses_the_post_delivery_sum_and_stays_off_stdout,
        test_lost_payment_answer_is_reconciled_not_declared_unknown,
        test_the_sum_the_user_approved_is_the_sum_that_gets_paid,
        test_a_quote_prices_the_order_without_creating_one,
        test_a_store_that_blinks_does_not_cost_the_user_an_attempt,
        test_a_store_that_stays_down_is_named_as_the_cause,
        test_a_delivery_request_that_is_simply_wrong_is_not_retried,
        test_a_delivery_call_that_never_landed_is_not_success,
        test_a_refused_sum_names_the_number_to_pass_next,
        test_a_quote_is_not_an_attempt,
        test_the_caller_can_choose_which_account_pays,
        test_the_checkout_tool_runs_playwright_off_the_event_loop,
        test_payment_gate_problem_json_reaches_the_user,
        test_cart_readiness_distinguishes_not_up_from_empty,
        test_the_cart_readiness_poll_uses_a_real_clock,
        test_deliveries_error_envelope_is_not_missed,
        test_messenger_ids_are_validated_before_they_reach_a_url,
        test_invest_operations_limit_zero_does_not_ask_the_bank_for_zero,
    ]
    # A test defined and never called is worse than no test: the suite reports green
    # and the reader believes the behaviour is pinned. It has happened here —
    # test_payment_gate_problem_json_reaches_the_user shipped unregistered and never
    # ran once. The list above stays explicit (order is readable and deliberate);
    # this makes forgetting it impossible.
    registered = {f.__name__ for f in cases}
    defined = {n for n, v in list(globals().items())
               if n.startswith("test_") and callable(v)}
    for name in sorted(defined - registered):
        failures.append(f"{name} is defined but never called by main() — it has never run")

    for case in cases:
        case()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
