"""Grocery checkout via Playwright (headless browser) — Phase 3 safety rewrite.

The T-Bank grocery checkout runs in a webview (www.tbank.ru) with JS that sets up
CSRF tokens, delivery slots, and the web session before calling order/create. A
headless browser runs that JS, then we call order/create + payment_gate_pay from
the page context.

CRITICAL: web cart sync requires these cookies (normally set by JS, set manually):
  - portalSID = mobile sessionid (links mobile cart → web cart)
  - sessionID = access_token
  - deviceId / stDeIdU / __P__wuid = device_id (lowercase)

Flow:
  1. cart/set via API (lifestyle.t-bank-app.ru) — already done
  2. Launch Playwright → set web cookies → load checkout page
  3. GET web cart → pre-delivery sum + goods
  4. POST deliveries (appId + pointId) → CHECK status → init delivery slots
  5. GET web cart AGAIN → post-delivery sum (weight items may recompute) — USE THIS
  6. POST order/create with the post-delivery sum (omit empty clientEmail)
  7. POST payment_gate_pay IMMEDIATELY (before auto-cancel) with the chosen account

Phase 3 changes vs the old fire-and-forget flow (#7/#8/#9/#10):
  - app_id is injected into the JS (no hardcoded 578); pointId is passed to deliveries.
  - deliveries HTTP status + error envelope are checked; a delivery failure stops the flow.
  - the cart is re-read AFTER deliveries and the POST-DELIVERY sum is used for order/create.
  - an empty clientEmail is omitted (not sent as "").
  - the payment ``agreement`` is a real selected account (was an undefined NameError).
  - every step is recorded in the attempt journal; an order created but not confirmed
    paid raises CheckoutUnknown so the server blocks an automatic (duplicate) retry.

NOTE (contract): the exact order/create body and the payment ``agreement`` identifier
are taken from the prior implementation's shape + these safety fixes — they are NOT
re-verified against a fresh authorized frontend trace. Validate with a small live order.
"""
from __future__ import annotations

import json
import sys
import time

from . import journal
from . import observability as obs
from .endpoints import APP_VERSION

# Shared web-checkout query string (capture-verified) for every grocery web fetch.
# Single source of truth — bump here, not in 5 separate JS template strings.
GROCERY_WEB_QS = f"appName=grocery_evo&appVersion={APP_VERSION}&platform=webview_ios"


def _cut(s, n: int) -> str:
    # Same contract as server._cut (checkout must not import server): a cut that
    # is marked, so a gate error trimmed for the answer never reads as complete.
    s = str(s or "")
    return s if n <= 0 or len(s) <= n else s[:n - 1] + "…"


def _envelope_error(body) -> tuple[str, str]:
    """Detect the lifestyle/Город app-level error envelope: HTTP 200 +
    {"status": "Error", "payload": {"code": ..., "message": ...}} — the same
    shape client.py's _unwrap() checks for on the mobile-API path. The shallow
    top-level errorMessage/errorCode checks in this file do not catch it, so a
    bank-rejected step here used to read as success. Returns (code, message),
    both "" if the body isn't this envelope shape."""
    if not isinstance(body, dict):
        return "", ""
    if str(body.get("status", "")).lower() != "error":
        return "", ""
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    code = str(payload.get("code") or body.get("code") or "")
    message = str(payload.get("message") or body.get("message") or "")
    return code, message

# How long the checkout page gets to bring its cart API up. The old 10 s was a bare
# number; this is the p99 page-ready time observed in the capture session (~3.5 s)
# with generous headroom, and it is a CEILING, not a wait — the poll exits as soon
# as the API answers.
CART_READY_TIMEOUT_MS = 20000
CART_POLL_INTERVAL_MS = 500


def _poll_until_ready(probe, ready, *, timeout_ms: int, interval_ms: int):
    """Call `probe` until `ready(result)`, then return (result, elapsed_ms).

    Returns (None, elapsed_ms) if the deadline passes first.

    Two things this exists to get right, both of which the inline loop got wrong:

    * The deadline is WALL CLOCK. The old loop added up its own sleeps, ignoring the
      probe itself — and each probe is a real in-page fetch bounded by
      FETCH_TIMEOUT_MS (30 s). Two hung fetches and a "20 second" wait had already
      run more than a minute, with the caller, who is mid-checkout, told nothing.
    * The successful probe's RESULT comes back. The loop used to discard it and the
      caller reissued the identical request — an extra browser round trip on the
      money path, and a window in which the second answer can differ from the one
      that satisfied the check."""
    started = time.monotonic()
    deadline = started + timeout_ms / 1000.0
    while True:
        try:
            result = probe()
            ok = bool(ready(result))
        except Exception:                                    # noqa: BLE001
            result, ok = None, False
        if ok:
            return result, int((time.monotonic() - started) * 1000)
        if time.monotonic() >= deadline:
            return None, int((time.monotonic() - started) * 1000)
        time.sleep(interval_ms / 1000.0)

# Per-request ceiling for every in-page fetch. Playwright's page.evaluate has NO
# timeout of its own (Page.evaluate passes timeout_calculator=None and _inner_send
# awaits without a deadline), so a fetch that never settles hangs the checkout —
# and the worst place for that is between order/create and payment, where the
# money is committed but the result is unknown. AbortController bounds it inside
# the page and returns a value we can classify instead of hanging.
FETCH_TIMEOUT_MS = 30000

# Prepended to every in-page evaluate. Returns the same {status, body} shape as
# before; on timeout or network failure it returns status 0 with `timedOut`/`error`
# so the caller can tell "the request failed" from "the server said no" — for the
# payment step those two are NOT the same and must not be collapsed.
_JS_FETCH = """
  const _f = async (url, opts, ms) => {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), ms);
    try {
      const r = await fetch(url, Object.assign({}, opts || {}, {signal: c.signal}));
      return {status: r.status, body: await r.json().catch(() => ({}))};
    } catch (e) {
      return {status: 0, body: {}, timedOut: true, error: String(e)};
    } finally {
      clearTimeout(t);
    }
  };
"""


def _js(body: str) -> str:
    """Wrap an in-page snippet as `async (a) => { <_f helper> <body> }`.

    Every fetch in this file goes through `_f`, so none of them can hang. `a` is the
    single argument object passed from Python."""
    return "async (a) => {" + _JS_FETCH + body + "}"


def _log(msg: str) -> None:
    """Progress goes to STDERR. FastMCP speaks JSON-RPC over stdout, so a bare
    print() here injects garbage into the protocol stream — during a payment, which
    is the worst possible moment to desync the client. server.py already writes all
    of its diagnostics to stderr; this file was the one place that did not."""
    print(msg, file=sys.stderr, flush=True)


class CheckoutError(RuntimeError):
    """Checkout failed in a way that is safe to retry (no order was POSTed)."""


class CheckoutUnknown(CheckoutError):
    """Checkout result is unknown — an order MAY have been created. Retry must be
    blocked until the user reconciles (grocery_attempts / checks the app)."""


def _safe_record(attempt_id, step, status, **fields):
    """Record a journal event without letting journal failures break checkout."""
    if not attempt_id:
        return
    try:
        journal.record(attempt_id, step, status, **fields)
    except Exception:
        pass


def checkout(session, app_id: str = "", point_id: str = "",
             client_email: str = "", sum_val: float = 0,
             account: str = "", attempt_id: str | None = None,
             expected_sum: float = 0,
             cart_ready_timeout_ms: int = CART_READY_TIMEOUT_MS) -> dict:
    """Run the grocery checkout via headless browser. `session` is a MobileSession
    with a valid access_token + cookies (from login/silent_relogin).

    `expected_sum` is the amount the USER approved. The sum actually charged is
    decided by the backend twice after that approval (the web cart, then
    deliveries' cartPrice), so without this the user confirms one number and a
    different one is paid. Non-zero ⇒ a divergence over 0.01 ₽ refuses BEFORE the
    order exists. Same guard, same tolerance as ticket_pay.

    Contract verified against captures.xml (2026-07-24):
      - agreement   = `account` when the caller names one, else accountId from GET
                      /api/supreme/lifestyle/api/user/payment/account/last
      - clientEmail = email from GET /mybank/api/shopping/mobile/v1/checkout/get-customer-information
                      (the `client_email` arg is only a fallback)
      - order/create body = {appId, clientEmail, sum}
      - payment-gate body = {paymentMethod:{type:"agreement",agreement}, flow:{type:"marketplace",
                      orderId, holdUsingMapi:false, applicationId}, amount:{type:"simple",
                      amount, currencyCode:"643"}}
      - post-delivery sum = deliveries response payload.cartPrice (weight items recompute)
    `attempt_id` = journal attempt id (created by the caller); steps are recorded.

    Returns {order_id, payment_id, status, sum} or raises CheckoutError (safe retry)
    / CheckoutUnknown (retry blocked)."""
    from playwright.sync_api import sync_playwright

    if not app_id:
        # money-path footgun: never default to a store. The MCP layer (_store)
        # always passes app_id; this guards direct/internal callers.
        raise CheckoutError("app_id is required (from grocery_stores())")

    web_ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    checkout_url = f"https://www.tbank.ru/mybank/gorod/grocery/{app_id}/cart/checkout-with-evo/"
    _ts = str(int(time.time() * 1000))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=web_ua,
                viewport={"width": 390, "height": 844},
                is_mobile=True,
            )

            # WEB CART SYNC COOKIES — set manually to link mobile cart → web checkout.
            cookies = [
                {"name": "portalSID", "value": session.mobile_sessionid, "domain": ".tbank.ru", "path": "/"},
                {"name": "sessionID", "value": session.access_token, "domain": ".tbank.ru", "path": "/"},
                {"name": "sso_api_session", "value": session.access_token, "domain": ".tbank.ru", "path": "/"},
                {"name": "old_session_id", "value": session.mobile_sessionid, "domain": ".tbank.ru", "path": "/"},
                {"name": "psid", "value": session.mobile_sessionid, "domain": ".tbank.ru", "path": "/"},
                {"name": "deviceId", "value": session.device_id, "domain": ".tbank.ru", "path": "/"},
                {"name": "stDeIdU", "value": session.device_id.lower(), "domain": ".tbank.ru", "path": "/"},
                {"name": "__P__wuid", "value": session.device_id.lower(), "domain": ".tbank.ru", "path": "/"},
                {"name": "__P__wuid_visit_id", "value": f"v1:0000001:{_ts}:{session.device_id.lower()}", "domain": ".tbank.ru", "path": "/"},
                {"name": "__P__wuid_visit_persistence", "value": _ts, "domain": ".tbank.ru", "path": "/"},
                {"name": "stLaEvTi", "value": _ts, "domain": ".tbank.ru", "path": "/"},
                {"name": "stSeStTi", "value": str(int(_ts) - 1000), "domain": ".tbank.ru", "path": "/"},
                {"name": "userType", "value": "Client-Heavy", "domain": ".tbank.ru", "path": "/"},
                {"name": "isHeavyClient", "value": "true", "domain": ".tbank.ru", "path": "/"},
                {"name": "token_auth_version", "value": "2.0", "domain": ".tbank.ru", "path": "/"},
                {"name": "isSubscribedToPush", "value": "false", "domain": ".tbank.ru", "path": "/"},
            ]
            all_cookies_str = session.cookie_str or ""
            if session.sso_login_cookie:
                all_cookies_str = session.sso_login_cookie + "; " + all_cookies_str
            for part in all_cookies_str.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies.append({"name": k, "value": v, "domain": ".tbank.ru", "path": "/"})
            context.add_cookies(cookies)

            page = context.new_page()

            def web_cart(app):
                """GET web cart, return (sum, goods, raw)."""
                wc = page.evaluate(_js("""
                    return await _f('/api/supreme/lifestyle/api/grocery/cart?' + a.qs
                        + '&appId=' + a.appId + '&origin=web,ib5,platform',
                        {headers: {'Accept': 'application/json'}}, a.ms);
                """), {"appId": app, "qs": GROCERY_WEB_QS, "ms": FETCH_TIMEOUT_MS})
                body = wc.get("body", {}) or {}
                cart = (body.get("payload") or {}).get("cart", {}) if isinstance(body, dict) else {}
                s = cart.get("goodsSum", 0) or cart.get("sum", 0)
                return s, cart.get("goods", []), wc

            # 1. load the checkout page, then wait for the cart API to be ready
            # (replaces a blind sleep(8) — poll the cart endpoint until it answers
            # with goods, or ~10s timeout). #15
            page.goto(checkout_url, wait_until="domcontentloaded", timeout=30000)
            # Wait for the cart API to come UP, which is the actual readiness signal.
            # The old loop waited for a NON-EMPTY cart, which conflates two different
            # facts: "the page is not ready yet" and "the cart really is empty". A
            # genuinely empty cart therefore burned the whole deadline and then
            # reported a misleading error. Track both so the message can say which.
            _probe, _waited = _poll_until_ready(
                lambda: web_cart(app_id),
                ready=lambda r: r[2].get("status") == 200,
                timeout_ms=cart_ready_timeout_ms, interval_ms=CART_POLL_INTERVAL_MS)
            if _probe is None:
                raise CheckoutError(
                    f"checkout page never brought its cart API up within "
                    f"{cart_ready_timeout_ms} ms (waited {_waited} ms) — no order was "
                    f"created, safe to retry")
            _log(f"[checkout] cart API up after ~{_waited}ms")

            # 2. The probe that succeeded IS the cart. It used to be thrown away and
            # the identical request issued again — one extra round trip inside a
            # browser, on the money path.
            pre_sum, goods, _ = _probe
            pre_count = len(goods)
            actual_sum = pre_sum or sum_val
            _log(f"[checkout] web cart (pre-delivery): sum={actual_sum} {pre_count} items")
            if not goods:
                raise CheckoutError(
                    "web cart is empty while its API is healthy — the mobile cart did "
                    "not sync to web. Check grocery_cart(app_id, point_id) shows items "
                    "for the SAME store, then retry; no order was created.")

            # 3. POST deliveries (appId + pointId) → CHECK status. Fire-and-forget
            # was the old bug: a delivery failure silently led to a stale sum + bad order.
            _t0 = time.time()
            deliv = page.evaluate(_js("""
                return await _f('/api/supreme/lifestyle/api/grocery/deliveries?' + a.qs
                    + '&appId=' + a.appId + '&pointId=' + a.pointId, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({serviceKeys: ["FEE", "DYNAMIC_CASHBACK", "PICKING_CART_SUM"]})
                }, a.ms);
            """), {"appId": app_id, "pointId": point_id, "qs": GROCERY_WEB_QS,
                   "ms": FETCH_TIMEOUT_MS})
            _dur = int((time.time() - _t0) * 1000)
            dbody = deliv.get("body", {}) if isinstance(deliv, dict) else {}
            d_err = ""
            d_code = ""
            if isinstance(dbody, dict):
                d_err = str(dbody.get("errorMessage") or "")
                d_code = str(dbody.get("errorCode") or dbody.get("resultCode") or dbody.get("code") or "")
            # The top-level checks above miss the {"status":"Error","payload":{...}}
            # envelope the lifestyle API also uses — check it too, or a rejected
            # delivery reads as success and checkout proceeds to pay for it.
            env_code, env_msg = _envelope_error(dbody)
            d_err = d_err or env_msg
            d_code = d_code or env_code
            obs.emit("delivery", attempt_id=attempt_id, app_id=app_id, point_id=point_id,
                     item_count=pre_count, http_status=deliv.get("status"), app_code=d_code,
                     duration_ms=_dur, blame=obs.blame_of(deliv.get("status"), d_code))
            if deliv.get("status", 0) >= 400 or d_err:
                raise CheckoutError(
                    f"deliveries failed (http={deliv.get('status')}, "
                    f"code={d_code}, err={_cut(d_err, 120)})")
            _log(f"[checkout] deliveries ok (http={deliv.get('status')})")

            # 4. post-delivery: the deliveries RESPONSE carries payload.cartPrice (weight
            # items recompute here — e.g. 1630.00 → 1600.20). Use it as the authoritative
            # sum; re-read the cart for an item-count comparison. Validate the selected
            # slot's pointId matches the requested store. (#7)
            dpayload = dbody.get("payload", {}) if isinstance(dbody, dict) else {}
            d_delivery = dpayload.get("delivery", {}) if isinstance(dpayload, dict) else {}
            selected = (d_delivery.get("selected") or {}) if isinstance(d_delivery, dict) else {}
            sel_point = str(selected.get("pointId", "") or "")
            if sel_point and sel_point != str(point_id):
                raise CheckoutError(
                    f"delivery selected pointId={sel_point} ≠ requested {point_id}")
            cart_price = dpayload.get("cartPrice")
            post_sum, post_goods, _ = web_cart(app_id)
            if cart_price:
                if cart_price != actual_sum:
                    _log(f"[checkout] sum adjusted after delivery: {actual_sum} → {cart_price}")
                actual_sum = cart_price
            elif post_sum:
                actual_sum = post_sum
            if len(post_goods) < pre_count:
                # items dropped after recalculation (out of stock?) — surface it
                _log(f"[checkout] WARN: item count changed {pre_count} → {len(post_goods)} after delivery")

            # actual_sum is final here, and it was decided by the backend — twice —
            # AFTER the user approved a number. Refuse now, while refusing is still
            # free: nothing has been posted, so this stays a CheckoutError (safe to
            # retry) rather than the UNKNOWN that anything past order/create becomes.
            if expected_sum and abs(float(actual_sum) - float(expected_sum)) > 0.01:
                raise CheckoutError(
                    f"сумма изменилась после подтверждения: подтверждено "
                    f"{expected_sum} ₽, к оплате {actual_sum} ₽. Заказ НЕ создан. "
                    f"Покажи корзину заново (grocery_cart) и подтверди новую сумму.")
            _safe_record(attempt_id, "delivery", "delivery_ready", amount=actual_sum)

            # 5. resolve the payment agreement + customer email from the capture-
            # verified endpoints (NOT a guess from list_accounts). #8/#9
            agr_res = page.evaluate(_js("""
                return await _f('/api/supreme/lifestyle/api/user/payment/account/last?'
                    + a.qs + '&serviceName=GROCERY',
                    {headers: {'Accept': 'application/json'}}, a.ms);
            """), {"qs": GROCERY_WEB_QS, "ms": FETCH_TIMEOUT_MS})
            agr_body = agr_res.get("body", {}) if isinstance(agr_res, dict) else {}
            agr_payload = agr_body.get("payload", {}) if isinstance(agr_body, dict) else {}
            # The caller's choice wins. The other way round (bank first, `account` as
            # fallback) meant an explicitly requested account was silently ignored
            # whenever this endpoint answered — which is always, in practice.
            agreement = account or (agr_payload.get("accountId")
                                    if isinstance(agr_payload, dict) else "")
            obs.emit("payment_account", attempt_id=attempt_id, http_status=agr_res.get("status"),
                     agreement_present=bool(agreement), blame=obs.blame_of(agr_res.get("status")))
            if not agreement:
                env_code, env_msg = _envelope_error(agr_body)
                detail = f" ({env_code}: {env_msg})" if env_msg else ""
                raise CheckoutError(
                    f"no payment account: user/payment/account/last returned no "
                    f"accountId{detail}")

            ci_res = page.evaluate(_js("""
                return await _f('/mybank/api/shopping/mobile/v1/checkout/get-customer-information?'
                    + a.qs, {headers: {'Accept': 'application/json'}}, a.ms);
            """), {"qs": GROCERY_WEB_QS, "ms": FETCH_TIMEOUT_MS})
            ci_body = ci_res.get("body", {}) if isinstance(ci_res, dict) else {}
            ci_email = ci_body.get("email") if isinstance(ci_body, dict) else ""
            if not ci_email and isinstance(ci_body, dict):
                ci_email = (ci_body.get("payload", {}) or {}).get("email", "")
                env_code, env_msg = _envelope_error(ci_body)
                if env_msg:
                    _log(f"[checkout] customer-information errored ({env_code}: "
                         f"{env_msg}) — falling back to client_email arg")
            email = client_email or ci_email

            # 6. POST order/create with the POST-DELIVERY sum + the customer email
            # (omit clientEmail only if both caller and customer-info lack one).
            order_obj = {"appId": app_id, "sum": actual_sum}
            if email:
                order_obj["clientEmail"] = email
            order_body = json.dumps(order_obj)
            # Point of no return: a crash / network drop during or after this POST
            # means the backend MAY have created the order without us seeing the
            # response → record the blocking ``order_posting`` state NOW so the
            # server's generic-exception handler treats it as UNKNOWN (no blind retry).
            _safe_record(attempt_id, "order_create", "order_posting", amount=actual_sum)
            _t0 = time.time()
            order_res = page.evaluate(_js("""
                const o = JSON.parse(a.body);
                return await _f('/api/supreme/lifestyle/api/grocery/order/create?appId='
                    + o.appId + '&' + a.qs + '&sum=' + o.sum, {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: a.body
                }, a.ms);
            """), {"body": order_body, "qs": GROCERY_WEB_QS, "ms": FETCH_TIMEOUT_MS})
            _dur = int((time.time() - _t0) * 1000)
            obody = order_res.get("body", {}) if isinstance(order_res, dict) else {}
            order = obody.get("payload", {}).get("order", {}) if isinstance(obody, dict) else {}
            order_id = order.get("id", "")
            o_code = str(obody.get("resultCode") or obody.get("errorCode") or obody.get("code") or "") if isinstance(obody, dict) else ""
            obs.emit("order_create", attempt_id=attempt_id, app_id=app_id,
                     item_count=len(post_goods), amount=actual_sum,
                     http_status=order_res.get("status"), app_code=o_code,
                     order_id_present=bool(order_id), duration_ms=_dur,
                     blame=obs.blame_of(order_res.get("status"), o_code))
            if not order_id:
                # We POSTed order/create but got no orderId. Statically we CANNOT prove
                # the backend created nothing → treat as UNKNOWN (block retry). (#10)
                _safe_record(attempt_id, "order_create", "unknown",
                             http=order_res.get("status"),
                             err=str(obody.get("errorMessage") or obody.get("resultCode") or "")[:120])
                raise CheckoutUnknown(
                    f"order/create returned no orderId (http={order_res.get('status')}, "
                    f"code={o_code}) — order may exist, do NOT retry blindly. "
                    f"See grocery_attempts()/diagnostics() for details.")
            _log(f"[checkout] order created: id={order_id}")
            _safe_record(attempt_id, "order_create", "order_posted", order_id=order_id)

            # 7. POST payment_gate_pay IMMEDIATELY (before auto-cancel). agreement =
            # accountId from user/payment/account/last (capture-verified). #9
            pay_body = json.dumps({
                "paymentMethod": {"type": "agreement", "agreement": agreement},
                "flow": {"type": "marketplace", "orderId": order_id,
                         "holdUsingMapi": False, "applicationId": app_id},
                "amount": {"type": "simple", "amount": actual_sum, "currencyCode": "643"},
            })
            _t0 = time.time()
            pay_res = page.evaluate(_js("""
                return await _f('/api/common/pg-api/v1/payment-gate/payments?origin=web,ib5,platform', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: a.body
                }, a.ms);
            """), {"body": pay_body, "ms": FETCH_TIMEOUT_MS})
            _dur = int((time.time() - _t0) * 1000)
            pbody = pay_res.get("body", {}) if isinstance(pay_res, dict) else {}
            stage = pbody.get("stage", {}) if isinstance(pbody, dict) else {}
            payment_id = pbody.get("paymentId", "") if isinstance(pbody, dict) else ""
            status = stage.get("status", "")
            # On 4xx the gate answers problem+json — {"type": "payment-gate/…",
            # "title": "Недостаточно средств", "detail": "…"} — not the
            # resultCode envelope, so `type` is the code and title/detail are
            # the only human-readable cause the user will ever get.
            p_code = str(pbody.get("resultCode") or pbody.get("errorCode") or pbody.get("type") or status or "") if isinstance(pbody, dict) else ""
            p_err = ""
            if isinstance(pbody, dict):
                p_err = _cut(": ".join(s for s in (str(pbody.get("title") or "").strip(),
                                                   str(pbody.get("detail") or "").strip()) if s), 200)
            obs.emit("payment", attempt_id=attempt_id, app_id=app_id, amount=actual_sum,
                     http_status=pay_res.get("status"), app_code=p_code,
                     order_id_present=bool(order_id), payment_id_present=bool(payment_id),
                     payment_status=status, duration_ms=_dur,
                     blame=obs.blame_of(pay_res.get("status"), p_code))
            if status == "SUCCESS":
                _safe_record(attempt_id, "payment", "paid",
                             order_id=order_id, payment_id=payment_id)
                return {"order_id": order_id, "payment_id": payment_id,
                        "status": status, "sum": actual_sum, "account": agreement}

            # Payment did not report SUCCESS — but that is not the same as "not paid".
            # A timed-out or dropped payment fetch (status 0) is exactly the case where
            # the money may have moved while we lost the answer. Before declaring the
            # result UNKNOWN and blocking every retry, make the ONE read that settles
            # it — the same order lookup the app itself uses for reconciliation.
            order_state, paid = "", False
            try:
                chk = page.evaluate(_js("""
                    return await _f('/api/supreme/lifestyle/api/grocery/order?' + a.qs
                        + '&appId=' + a.appId + '&orderId=' + a.orderId,
                        {headers: {'Accept': 'application/json'}}, a.ms);
                """), {"appId": app_id, "orderId": order_id, "qs": GROCERY_WEB_QS,
                       "ms": FETCH_TIMEOUT_MS})
                cb = chk.get("body") or {}
                o = (cb.get("payload") or {}).get("order") or cb.get("payload") or {}
                order_state = str(o.get("status") or o.get("state") or "")
                paid = bool(o.get("paid")) or order_state.upper() in ("PAID", "PAYED", "SUCCESS")
            except Exception as e:
                # Keep the (redacted) message, not just the class: «TimeoutError»
                # alone left nothing to distinguish a dead page from a dead API.
                order_state = (f"<lookup failed: {type(e).__name__}: "
                               f"{_cut(obs.redact_text(str(e)), 120)}>")
            obs.emit("payment_reconcile", attempt_id=attempt_id, order_id_present=bool(order_id),
                     payment_status=status, order_state=order_state, paid=paid)

            if paid:
                # The gateway answer was lost, not the payment. Report the truth.
                _safe_record(attempt_id, "payment", "paid",
                             order_id=order_id, payment_id=payment_id)
                _log(f"[checkout] payment answer lost (stage={status!r}) but order "
                     f"{order_id} reads back as {order_state} — treating as paid")
                return {"order_id": order_id, "payment_id": payment_id,
                        "status": order_state or "PAID", "sum": actual_sum,
                        "account": agreement,
                        "note": "confirmed by order lookup, not by the payment response"}

            # Still unresolved. Retrying would create a duplicate order; the unpaid one
            # auto-cancels and the user reconciles. (#9/#10)
            _safe_record(attempt_id, "payment", "unknown",
                         order_id=order_id, payment_id=payment_id,
                         http=pay_res.get("status"),
                         payment_status=status, order_state=order_state,
                         err=json.dumps(pbody, ensure_ascii=False)[:160])
            raise CheckoutUnknown(
                f"payment not SUCCESS (order {order_id} exists, stage={status!r}, "
                f"http={pay_res.get('status')}, order reads {order_state or 'unknown'!r}"
                + (", the payment request TIMED OUT" if pay_res.get("timedOut") else "")
                + ") — order may be unpaid/pending; do NOT retry blindly"
                + (f"\nШлюз ответил: {p_err} [{p_code}]" if p_err else ""))
        finally:
            browser.close()
