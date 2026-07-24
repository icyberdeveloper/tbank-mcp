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
import time

from . import journal


class CheckoutError(RuntimeError):
    """Checkout failed in a way that is safe to retry (no order was POSTed)."""


class CheckoutUnknown(CheckoutError):
    """Checkout result is unknown — an order MAY have been created. Retry must be
    blocked until the user reconciles (grocery_attempts / checks the app)."""


def select_payment_account(session) -> str:
    """Pick a funding account for grocery payment. Returns the account id to use as
    the payment ``agreement``. Selects the first Current (debit) account with a
    positive RUB balance; raises CheckoutError if none.

    NOTE: which identifier the web payment endpoint expects as ``agreement`` (account
    id vs card/agreement number) needs confirmation from a frontend trace — we use the
    account id as the best available match."""
    accs = session.list_accounts() or []
    for a in accs:
        if not isinstance(a, dict):
            continue
        if (a.get("accountType") or "") != "Current":
            continue
        money = a.get("moneyAmount") or {}
        bal = money.get("value", 0) if isinstance(money, dict) else 0
        try:
            bal = float(bal)
        except (TypeError, ValueError):
            bal = 0.0
        if bal <= 0:
            continue
        cur = a.get("currency")
        cur_name = cur.get("name") if isinstance(cur, dict) else cur
        if cur_name and str(cur_name).upper() not in ("RUB", "RUBLES", "РОССИЙСКИЙ РУБЛЬ", "₽"):
            continue
        aid = a.get("id")
        if aid:
            return str(aid)
    raise CheckoutError(
        "no RUB Current account with a positive balance found — cannot select a payment source")


def _safe_record(attempt_id, step, status, **fields):
    """Record a journal event without letting journal failures break checkout."""
    if not attempt_id:
        return
    try:
        journal.record(attempt_id, step, status, **fields)
    except Exception:
        pass


def checkout(session, app_id: str = "578", point_id: str = "",
             client_email: str = "", sum_val: float = 0,
             account: str = "", attempt_id: str | None = None) -> dict:
    """Run the grocery checkout via headless browser. `session` is a MobileSession
    with a valid access_token + cookies (from login/silent_relogin).

    `account` = the payment agreement id (from select_payment_account); required.
    `attempt_id` = journal attempt id (created by the caller); steps are recorded.
    `sum_val` = mobile-cart sum fallback (the post-delivery WEB sum is preferred).

    Returns {order_id, payment_id, status, sum} or raises CheckoutError (safe retry)
    / CheckoutUnknown (retry blocked)."""
    from playwright.sync_api import sync_playwright

    if not account:
        # defense-in-depth: the server selects an account, but never pay with empty.
        raise CheckoutError("no payment account selected (account is empty)")

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

            # 1. load the checkout page
            page.goto(checkout_url, wait_until="domcontentloaded", timeout=30000)
            print("[checkout] page loaded, waiting 8s for JS init ...")
            time.sleep(8)

            def web_cart(app):
                """GET web cart, return (sum, goods, raw)."""
                wc = page.evaluate("""async (appId) => {
                    const r = await fetch('/api/supreme/lifestyle/api/grocery/cart?appName=grocery_evo&appVersion=7.31.6&platform=webview_ios&appId=' + appId + '&origin=web,ib5,platform', {headers: {'Accept': 'application/json'}});
                    return {status: r.status, body: await r.json().catch(() => ({}))};
                }""", app)
                body = wc.get("body", {}) or {}
                cart = body.get("payload", {}).get("cart", {}) if isinstance(body, dict) else {}
                s = cart.get("goodsSum", 0) or cart.get("sum", 0)
                return s, cart.get("goods", []), wc

            # 2. GET web cart → pre-delivery sum + goods
            pre_sum, goods, _ = web_cart(app_id)
            pre_count = len(goods)
            actual_sum = pre_sum or sum_val
            print(f"[checkout] web cart (pre-delivery): sum={actual_sum} {pre_count} items")
            if not goods:
                raise CheckoutError("web cart is empty — cart sync failed or cart was cleared")

            # 3. POST deliveries (appId + pointId) → CHECK status. Fire-and-forget
            # was the old bug: a delivery failure silently led to a stale sum + bad order.
            deliv = page.evaluate("""async (args) => {
                const r = await fetch('/api/supreme/lifestyle/api/grocery/deliveries?appName=grocery_evo&appVersion=7.31.6&platform=webview_ios&appId=' + args.appId + '&pointId=' + args.pointId, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({serviceKeys: ["FEE", "DYNAMIC_CASHBACK", "PICKING_CART_SUM"]})
                });
                return {status: r.status, body: await r.json().catch(() => ({}))};
            }""", {"appId": app_id, "pointId": point_id})
            dbody = deliv.get("body", {}) if isinstance(deliv, dict) else {}
            d_err = ""
            if isinstance(dbody, dict):
                d_err = str(dbody.get("errorMessage") or dbody.get("errorCode") or "")
            if deliv.get("status", 0) >= 400 or d_err:
                raise CheckoutError(
                    f"deliveries failed (http={deliv.get('status')}, err={d_err[:120]})")
            print(f"[checkout] deliveries ok (http={deliv.get('status')})")

            # 4. re-read the web cart AFTER deliveries → post-delivery sum (weight
            # items may recompute, e.g. 1630.00 → 1600.20). USE the post-delivery sum.
            post_sum, post_goods, _ = web_cart(app_id)
            if post_sum:
                if post_sum != actual_sum:
                    print(f"[checkout] sum adjusted after delivery: {actual_sum} → {post_sum}")
                actual_sum = post_sum
            if len(post_goods) < pre_count:
                # items dropped after recalculation (out of stock?) — surface it
                print(f"[checkout] WARN: item count changed {pre_count} → {len(post_goods)} after delivery")
            _safe_record(attempt_id, "delivery", "delivery_ready", amount=actual_sum)

            # 5. POST order/create with the POST-DELIVERY sum. Omit clientEmail when
            # empty (the old body sent "clientEmail": "").
            order_obj = {"appId": app_id, "sum": actual_sum}
            if client_email:
                order_obj["clientEmail"] = client_email
            order_body = json.dumps(order_obj)
            order_res = page.evaluate("""async (body) => {
                const o = JSON.parse(body);
                const r = await fetch('/api/supreme/lifestyle/api/grocery/order/create?appId=' + o.appId + '&appName=grocery_evo&appVersion=7.31.6&platform=webview_ios&sum=' + o.sum, {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: body
                });
                return {status: r.status, body: await r.json().catch(() => ({}))};
            }""", order_body)
            obody = order_res.get("body", {}) if isinstance(order_res, dict) else {}
            order = obody.get("payload", {}).get("order", {}) if isinstance(obody, dict) else {}
            order_id = order.get("id", "")
            if not order_id:
                # We POSTed order/create but got no orderId. Statically we CANNOT prove
                # the backend created nothing → treat as UNKNOWN (block retry). (#10)
                _safe_record(attempt_id, "order_create", "unknown",
                             http=order_res.get("status"),
                             err=str(obody.get("errorMessage") or obody.get("resultCode") or "")[:120])
                raise CheckoutUnknown(
                    f"order/create returned no orderId (http={order_res.get('status')}, "
                    f"resp={json.dumps(obody, ensure_ascii=False)[:200]}) — order may exist, do NOT retry blindly")
            print(f"[checkout] order created: id={order_id}")
            _safe_record(attempt_id, "order_create", "order_posted", order_id=order_id)

            # 6. POST payment_gate_pay IMMEDIATELY (before auto-cancel). The agreement
            # is the selected account id (was an undefined `account` NameError). #9
            pay_body = json.dumps({
                "paymentMethod": {"type": "agreement", "agreement": account},
                "flow": {"type": "marketplace", "orderId": order_id,
                         "holdUsingMapi": False, "applicationId": app_id},
                "amount": {"type": "simple", "amount": actual_sum, "currencyCode": "643"},
            })
            pay_res = page.evaluate("""async (body) => {
                const r = await fetch('/api/common/pg-api/v1/payment-gate/payments?origin=web,ib5,platform', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: body
                });
                return {status: r.status, body: await r.json().catch(() => ({}))};
            }""", pay_body)
            pbody = pay_res.get("body", {}) if isinstance(pay_res, dict) else {}
            stage = pbody.get("stage", {}) if isinstance(pbody, dict) else {}
            payment_id = pbody.get("paymentId", "") if isinstance(pbody, dict) else ""
            status = stage.get("status", "")
            if status == "SUCCESS":
                _safe_record(attempt_id, "payment", "paid",
                             order_id=order_id, payment_id=payment_id)
                return {"order_id": order_id, "payment_id": payment_id,
                        "status": status, "sum": actual_sum}
            # Order exists but payment did not return SUCCESS → UNKNOWN. Retrying would
            # create a duplicate order; the unpaid order auto-cancels, user reconciles. (#9/#10)
            _safe_record(attempt_id, "payment", "unknown",
                         order_id=order_id, payment_id=payment_id,
                         http=pay_res.get("status"),
                         payment_status=status,
                         err=json.dumps(pbody, ensure_ascii=False)[:160])
            raise CheckoutUnknown(
                f"payment not SUCCESS (order {order_id} exists, stage={status!r}, "
                f"http={pay_res.get('status')}) — order may be unpaid/pending; do NOT retry blindly")
        finally:
            browser.close()
