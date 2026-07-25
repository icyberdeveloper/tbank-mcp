"""The grocery cart/set body must match what the real app sends.

Twice now the cart silently failed to save: cart/set answered HTTP 200 while the
follow-up GET read empty. Both times the cause was a body that diverged from the
app's — first a missing delivery.address (a store with no cart cannot supply one),
then a missing delivery.areaId (required by ВкусВилл/Лента, absent for Азбука).

This pins the body against the real captured requests so a third round cannot
happen silently. Ground truth is the Burp capture; the test skips if it is absent.

    python3 tests/test_cart_body_matches_capture.py
"""
import base64
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.client import MobileSession, TbankApiError  # noqa: E402

CAPTURE = os.environ.get("TBANK_CAPTURE", os.path.expanduser("~/tbank-app/captures.xml"))

# capture item indices → the request they hold
AZBUKA_CART_SET = 314    # appId=578 pointId=2   — no areaId
VKUSVILL_CART_SET = 708  # appId=204 pointId=5980 — areaId=17040911
CART_GET = 370           # a populated Azbuka cart
CLIENT_INFO = 275        # payload.deliveryInfo.address — the cold-start address seed
RETAILERS = 276          # the only source of areaId
ERROR_ENVELOPE = 1120    # HTTP 200 + status:"Error"


def _items():
    with open(CAPTURE, "rb") as fh:
        return re.findall(r"<item>(.*?)</item>", fh.read().decode("utf-8", "replace"), re.S)


def _raw(item, tag):
    m = re.search(r"<%s( [^>]*)?>(.*?)</%s>" % (tag, tag), item, re.S)
    body = m.group(2).replace("<![CDATA[", "").replace("]]>", "")
    return base64.b64decode(body) if 'base64="true"' in (m.group(1) or "") else body.encode()


def _body(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    # Content-Encoding, not Accept-Encoding — requests advertise gzip without using it.
    if b"content-encoding: gzip" in head.lower():
        body = gzip.decompress(body)
    return body


def request_json(items, n):
    return json.loads(_body(_raw(items[n], "request")))


def response_json(items, n):
    return json.loads(_body(_raw(items[n], "response")))


class ReplaySession(MobileSession):
    """A session whose reads are answered from the capture instead of the network."""

    def __init__(self, items, store_has_cart):
        self.items = items
        self.store_has_cart = store_has_cart
        self.sent_body = None
        self.sent_overrides = None

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        if key == "grocery_cart_get":
            if not self.store_has_cart:
                return {"cart": {"goods": [], "sum": 0}}
            return response_json(self.items, CART_GET)["payload"]
        if key == "grocery_client_info":
            return response_json(self.items, CLIENT_INFO)["payload"]
        if key == "grocery_cart_set":
            self.sent_body = body
            self.sent_overrides = overrides or {}
            return {"goodsSum": 1.0}
        raise AssertionError("unexpected read: " + key)

    def grocery_stores(self):
        payload = response_json(self.items, RETAILERS)["payload"]
        out = []
        for cat in payload.get("categories", []):
            for ret in cat.get("retailers", []):
                delivery = ret.get("delivery") or {}
                out.append({
                    "appId": str(ret.get("appId", "")),
                    "pointId": str(delivery.get("pointId", "")),
                    "areaId": str(delivery.get("areaId", "") or ""),
                })
        return out


def keys_at(obj, *path):
    for step in path:
        obj = (obj or {}).get(step) or {}
    return sorted(obj.keys())


def check_store(items, label, app_id, point_id, capture_item, store_has_cart, failures):
    session = ReplaySession(items, store_has_cart)
    session.grocery_add_to_cart([{"id": "1087", "count": 1}], app_id=app_id, point_id=point_id)
    got, real = session.sent_body, request_json(items, capture_item)

    def expect(what, ours, theirs):
        if ours != theirs:
            failures.append(f"{label}: {what}\n    ours={ours!r}\n    real={theirs!r}")

    expect("top-level keys", sorted(got), sorted(real))
    expect("delivery keys", sorted(got["delivery"]), sorted(real["delivery"]))
    expect("address keys", keys_at(got, "delivery", "address"), keys_at(real, "delivery", "address"))
    expect("address.details keys",
           keys_at(got, "delivery", "address", "details"),
           keys_at(real, "delivery", "address", "details"))
    expect("delivery.areaId", got["delivery"].get("areaId"), real["delivery"].get("areaId"))
    expect("delivery.pointId", str(got["delivery"]["pointId"]), str(real["delivery"]["pointId"]))
    expect("cartSetMode", got["cartSetMode"], real["cartSetMode"])
    # pointId belongs in the body, never the query — only appId scopes the cart
    if "pointId" in session.sent_overrides:
        failures.append(f"{label}: pointId leaked into the query string")
    print(f"  {label}: delivery={sorted(got['delivery'])} areaId={got['delivery'].get('areaId')!r}")


def check_merge(items, failures):
    """cart/set replaces the whole cart, so an add must resend the existing goods."""
    session = ReplaySession(items, store_has_cart=True)
    session.grocery_add_to_cart([{"id": "999999", "count": 2}], app_id="578", point_id="2")
    sent = {g["id"]: g["count"] for g in session.sent_body["goods"]}
    for existing in response_json(items, CART_GET)["payload"]["cart"]["goods"]:
        if str(existing["id"]) not in sent:
            failures.append(f"merge: existing good {existing['id']} was dropped from the cart")
    if sent.get("999999") != 2:
        failures.append(f"merge: the new good is missing or miscounted: {sent.get('999999')!r}")
    print(f"  merge: cart had {len(sent) - 1} goods, resending {len(sent)}")


def check_error_envelope(items, failures):
    """HTTP 200 + status:"Error" must raise, not return the error body as success."""
    envelope = response_json(items, ERROR_ENVELOPE)

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return envelope

        def raise_for_status(self):
            pass

    session = MobileSession.__new__(MobileSession)
    try:
        got = session._unwrap(Resp())
    except TbankApiError as exc:
        print(f"  error envelope: raised {exc}")
        return
    failures.append(f"error envelope: swallowed, returned {got!r} instead of raising")


def main():
    if not os.path.exists(CAPTURE):
        print(f"SKIP: capture not found at {CAPTURE} (set TBANK_CAPTURE to override)")
        return 0
    items = _items()
    failures = []
    print("cart/set body vs real app:")
    check_store(items, "ВкусВилл 204/5980 cold start", "204", "5980", VKUSVILL_CART_SET, False, failures)
    check_store(items, "Азбука 578/2 existing cart", "578", "2", AZBUKA_CART_SET, True, failures)
    check_merge(items, failures)
    check_error_envelope(items, failures)
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK — the body matches the capture for both retailer shapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
