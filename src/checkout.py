"""Grocery checkout via Playwright (headless browser).

The T-Bank grocery checkout runs in a webview (www.tbank.ru) with JS that sets
up CSRF tokens, delivery slots, and the web session before calling order/create.
A headless browser runs that JS — then we call order/create + payment_gate_pay
from the page context.

CRITICAL: the web cart sync requires these cookies (normally set by JS, we set
them manually):
  - portalSID = mobile sessionid (links mobile cart → web cart)
  - sessionID = access_token
  - deviceId / stDeIdU / __P__wuid = device_id (lowercase)

Flow:
  1. cart/set via API (lifestyle.t-bank-app.ru) — already done
  2. Launch Playwright → set web cookies → load checkout page
  3. GET web cart → actual sum (weight-based items may differ from mobile sum)
  4. POST deliveries → init delivery slots
  5. POST order/create with ACTUAL web cart sum
  6. POST payment_gate_pay IMMEDIATELY (before auto-cancel) with currencyCode=643
"""
from __future__ import annotations

import json
import time


def checkout(session, app_id: str = "578", client_email: str = "", sum_val: float = 0) -> dict:
    """Run the grocery checkout via headless browser. `session` is a MobileSession
    with a valid access_token + cookies (from login/silent_relogin).

    If sum_val=0, reads the ACTUAL sum from the web cart (recommended —
    weight-based items like potatoes may have a different sum than the mobile cart).

    Returns {order_id, payment_id, status, sum} or raises on failure."""
    from playwright.sync_api import sync_playwright

    web_ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    checkout_url = f"https://www.tbank.ru/mybank/gorod/grocery/{app_id}/cart/checkout-with-evo/"
    _ts = str(int(time.time() * 1000))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=web_ua,
            viewport={"width": 390, "height": 844},
            is_mobile=True,
        )

        # WEB CART SYNC COOKIES — the critical fix.
        # These are normally set by the checkout page's JS. We set them manually
        # to link the mobile cart (sessionid) to the web checkout.
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
        # also add SSO + mobile cookies from the session
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

        # 2. GET web cart → actual sum (weight-based items may differ)
        wc = page.evaluate("""async () => {
            const r = await fetch('/api/supreme/lifestyle/api/grocery/cart?appName=grocery_evo&appVersion=7.31.6&platform=webview_ios&appId=578&origin=web,ib5,platform', {headers: {'Accept': 'application/json'}});
            return await r.json();
        }""")
        cart = wc.get("payload", {}).get("cart", {})
        actual_sum = cart.get("goodsSum", 0) or cart.get("sum", 0) or sum_val
        goods = cart.get("goods", [])
        print(f"[checkout] web cart: sum={actual_sum} {len(goods)} items")
        if not goods:
            browser.close()
            raise RuntimeError("web cart is empty — cart sync failed or cart was cleared")

        # 3. POST deliveries (init delivery slots)
        page.evaluate("""async () => {
            await fetch('/api/supreme/lifestyle/api/grocery/deliveries?appName=grocery_evo&appVersion=7.31.6&platform=webview_ios&appId=578', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({serviceKeys: ["FEE", "DYNAMIC_CASHBACK", "PICKING_CART_SUM"]})
            });
        }""")

        # 4. POST order/create with ACTUAL web cart sum
        order_body = json.dumps({"appId": app_id, "clientEmail": client_email, "sum": actual_sum})
        order_result = page.evaluate("""async (body) => {
            const sum = JSON.parse(body).sum;
            const r = await fetch('/api/supreme/lifestyle/api/grocery/order/create?appId=' + JSON.parse(body).appId + '&appName=grocery_evo&appVersion=7.31.6&platform=webview_ios&sum=' + sum, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: body
            });
            return await r.json();
        }""", order_body)
        order = order_result.get("payload", {}).get("order", {})
        order_id = order.get("id", "")
        if not order_id:
            browser.close()
            raise RuntimeError(f"order/create failed: {json.dumps(order_result, ensure_ascii=False)[:300]}")
        print(f"[checkout] order created: id={order_id}")

        # 5. POST payment_gate_pay IMMEDIATELY (before auto-cancel)
        # CRITICAL: amount.currencyCode=643 (RUB) is required.
        # Use the ACTUAL web cart sum (not the order's sum, which may be None).
        pay_body = json.dumps({
            "paymentMethod": {"type": "agreement", "agreement": account or ""},
            "flow": {"type": "marketplace", "orderId": order_id, "holdUsingMapi": False, "applicationId": app_id},
            "amount": {"type": "simple", "amount": actual_sum, "currencyCode": "643"}
        })
        pay_result = page.evaluate("""async (body) => {
            const r = await fetch('/api/common/pg-api/v1/payment-gate/payments?origin=web,ib5,platform', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: body
            });
            return await r.json();
        }""", pay_body)
        browser.close()

        stage = pay_result.get("stage", {})
        payment_id = pay_result.get("paymentId", "")
        status = stage.get("status", "")
        if status == "SUCCESS":
            return {"order_id": order_id, "payment_id": payment_id,
                    "status": status, "sum": actual_sum}
        raise RuntimeError(f"payment failed: {json.dumps(pay_result, ensure_ascii=False)[:300]}")
