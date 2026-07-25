"""Regenerate tests/fixtures/grocery_cart.json from a Burp capture.

The capture is the user's real banking traffic and is gitignored — it can never be
committed. But a test that only runs when the capture happens to be present is not
coverage: on a clean clone tests/test_cart_body_matches_capture.py used to print
SKIP and exit 0, reporting success having verified nothing.

So the contract lives in a fixture: real STRUCTURE and real protocol values (areaId,
pointId, appId, cartSetMode, the goods list shape), synthetic personal values. The
test runs everywhere against the fixture, and when the real capture IS present it
additionally checks the fixture still matches it — so the fixture cannot silently
drift away from what the app sends.

    python3 tests/fixtures/regen.py [path/to/captures.xml]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import test_cart_body_matches_capture as T  # noqa: E402

# Replaced consistently, so structure and protocol-critical values survive intact.
FAKE_ADDR = {
    "city": "Москва", "country": "Россия", "doorphone": "0000", "doorway": "1",
    "flat": "1", "house": "1", "houseType": "house", "name": "",
    "postalCode": "000000", "region": "Москва", "settlement": "",
    "storey": "1", "street": "Примерная", "streetWithType": "ул Примерная",
}
FAKE_VALUE = "ул Примерная, д 1, кв 1"
PERSONAL_KEYS = ("phone", "email", "name", "fio", "clientname", "firstname", "lastname")


def scrub(o):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            kl = k.lower()
            if k == "details" and isinstance(v, dict):
                out[k] = {dk: FAKE_ADDR.get(dk, "") for dk in v}
            elif kl == "coordinates":
                out[k] = {ck: 0.0 for ck in v} if isinstance(v, dict) else [0.0, 0.0]
            elif kl == "value" and isinstance(v, str) and len(v) > 3 and not v.isdigit():
                out[k] = FAKE_VALUE
            elif kl == "comment":
                out[k] = ""
            elif kl in PERSONAL_KEYS and isinstance(v, str):
                out[k] = ""
            else:
                out[k] = scrub(v)
        return out
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o


def renumber_address_ids(client_info):
    """Address-record UUIDs are the user's, not the protocol's.

    scrub() cannot catch them by key name: `id` also carries goods ids and `pointId`
    carries public store ids, both of which are the contract and must survive. So
    they are replaced positionally, here, where the shape of the document is known.
    The replacements keep UUID form (something downstream may parse it) and are
    obviously synthetic."""
    addrs = ((client_info.get("deliveryInfo") or {}).get("addresses") or [])
    for i, a in enumerate(addrs, 1):
        if isinstance(a, dict) and "id" in a:
            a["id"] = f"00000000-0000-4000-8000-{i:012d}"
    return client_info


def scrub_envelope(env):
    """trackingId is the bank's handle on one real request by this user."""
    if isinstance(env, dict) and "trackingId" in env:
        env["trackingId"] = "00000000-0000-4000-8000-000000000000"
    return env


def slim_retailers(payload):
    """Only the appId → pointId/areaId mapping the cart body needs."""
    cats = []
    for cat in payload.get("categories", []):
        rets = []
        for r in cat.get("retailers", []):
            d = r.get("delivery") or {}
            rets.append({"appId": r.get("appId"),
                         "delivery": {"pointId": d.get("pointId"), "areaId": d.get("areaId")}})
        if rets:
            cats.append({"retailers": rets})
    return {"categories": cats}


def build(items):
    return {
        "_note": ("Scrubbed from a Burp capture: structure and protocol values are real, "
                  "personal values are synthetic. Regenerate with tests/fixtures/regen.py."),
        "client_info": renumber_address_ids(
            scrub(T.response_json(items, T.CLIENT_INFO)["payload"])),
        "cart_get": {"cart": scrub(T.response_json(items, T.CART_GET)["payload"]["cart"])},
        "retailers": slim_retailers(T.response_json(items, T.RETAILERS)["payload"]),
        "expected_azbuka": scrub(T.request_json(items, T.AZBUKA_CART_SET)),
        "expected_vkusvill": scrub(T.request_json(items, T.VKUSVILL_CART_SET)),
        "error_envelope": scrub_envelope(T.response_json(items, T.ERROR_ENVELOPE)),
        "cart_set_escalation": build_cart_set_escalation(),
    }


def build_cart_set_escalation():
    """The cartSetMode escalation, from captures2.xml.

    Holds no payload — just the two mode strings, the app code the narrow one is
    refused with, and which keys actually differ between the refused and the
    accepted body. That last part is the whole point: the two requests are
    identical apart from cartSetMode, which is why «268 Сервис временно
    недоступен» means «reset the other cart», not «come back later»."""
    import test_cart_body_matches_capture as T

    if not os.path.exists(T.CAPTURE2):
        raise FileNotFoundError(T.CAPTURE2)
    saved = T.CAPTURE
    T.CAPTURE = T.CAPTURE2
    try:
        items = T._items()
        refused = T.request_json(items, T.CART_SET_REFUSED)
        accepted = T.request_json(items, T.CART_SET_ACCEPTED)
        refused_resp = T.response_json(items, T.CART_SET_REFUSED)
    finally:
        T.CAPTURE = saved
    differing = sorted(k for k in set(refused) | set(accepted)
                       if refused.get(k) != accepted.get(k))
    return {
        "refused_mode": refused["cartSetMode"],
        "accepted_mode": accepted["cartSetMode"],
        "refused_code": str((refused_resp.get("payload") or {}).get("code", "")),
        "differing_keys": differing,
    }


def build_cancel():
    """The cancellation, from delete-order.xml (the only capture that has one).

    Nothing but SHAPE survives: cancel puts everything in the query and sends an
    empty body, and every one of those query values — orderId, paymentId,
    sessionid, deviceId — is the user's. So the fixture keeps key NAMES only."""
    import urllib.parse
    import test_booking_and_ranking as B

    with open(B.CANCEL_CAPTURE, "rb") as fh:
        blob = fh.read().decode("utf-8", "replace")
    for item in re.findall(r"<item>(.*?)</item>", blob, re.S):
        url = re.search(r"<url>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</url>", item, re.S)
        if not url or "/order/cancel" not in url.group(1):
            continue
        parts = urllib.parse.urlsplit(url.group(1).strip())
        method = re.search(r"<method>(?:<!\[CDATA\[)?(\w+)", item)
        return {
            "method": method.group(1) if method else "POST",
            "host": f"{parts.scheme}://{parts.netloc}",
            "path": parts.path,
            "query_keys": sorted(urllib.parse.parse_qs(parts.query)),
            "body": "",
        }
    raise SystemExit(f"no /order/cancel request in {B.CANCEL_CAPTURE}")


def build_booking():
    """The three money-moving ticket bodies from captures2.xml, plus the
    cancellation shape from delete-order.xml.

    eventId/slotId/objectId/seat ids are public catalogue identifiers and stay real —
    they ARE the contract. The payer's account (`agreement`) and the real orderId are
    the user's, and are replaced."""
    import test_booking_and_ranking as B

    items = B._items()
    movie = B.request_json(items, B.CREATE_MOVIE)
    concert = B.request_json(items, B.CREATE_CONCERT)
    pay = B.request_json(items, B.PAY)
    pay["paymentMethod"]["agreement"] = "0000000000"
    pay["flow"]["orderId"] = "10000000000"
    return {
        "_note": ("Scrubbed from a Burp capture. Catalogue ids are real (they are the "
                  "contract); the payer account and order id are synthetic. "
                  "`cancel` records a QUERY-string endpoint, so it holds key NAMES "
                  "and no values at all. Regenerate with tests/fixtures/regen.py."),
        "create_movie": movie,
        "create_concert": concert,
        "cancel": build_cancel(),
        "pay": pay,
    }


def build_transfer():
    """The real signed /v1/pay, from captures.xml #1477 (p2p-anybank via SBP).

    Kept: the query keys, the form keys, and the payParameters KEY SET plus the
    protocol constants. Replaced: the payer account, the recipient's phone, name and
    SBP ids, the device id and the session id."""
    import urllib.parse

    import test_cart_body_matches_capture as T

    items = T._items()
    raw = T._raw(items[1477], "request")
    head, _, body = raw.partition(b"\r\n\r\n")
    line = head.split(b"\r\n")[0].decode()
    query = urllib.parse.parse_qs(line.split("?", 1)[1].split(" ")[0])
    form = urllib.parse.parse_qs(body.decode())
    pp = json.loads(form["payParameters"][0])

    pp["account"] = "0000000000"
    # A millisecond timestamp is a handle on one real payment — when it happened, and
    # what the bank deduplicates against. Only its SHAPE is the contract (13 digits),
    # and that is what the test asserts, so the value goes.
    pp["userPaymentId"] = "1700000000000"
    pf = pp.get("providerFields", {})
    for k, v in (("pointer", "+79991234567"), ("maskedFIO", "И. И."),
                 ("bankMemberId", "100000000000"), ("pointerLinkId", "10000000000")):
        if k in pf:
            pf[k] = v
    secret = {"sessionid", "deviceId", "oldDeviceId"}

    # The RESPONSE, from the same exchange. Its shape is the contract for what the
    # tool reports back: commissionInfo carries three money objects and picking the
    # wrong one turns the transfer itself into its own «commission». A stub written
    # from memory (`commissionInfo: {"value": 0}`) is what let that ship.
    resp = json.loads(T._raw(items[1477], "response").partition(b"\r\n\r\n")[2])
    payload = resp.get("payload", resp)
    payload["paymentId"] = "100000000001"

    return {
        "_note": ("Scrubbed from captures.xml #1477 (POST /v1/pay, p2p-anybank, 200). "
                  "Key sets and protocol constants are real; account, recipient, "
                  "userPaymentId, paymentId and device/session ids are synthetic."),
        "query_keys": sorted(query),
        "query_static": {k: v[0] for k, v in sorted(query.items()) if k not in secret},
        "form_keys": sorted(form),
        "pay_parameters": pp,
        "pay_response": payload,
    }


def write(name, data):
    out = os.path.join(HERE, name)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"wrote {out} ({os.path.getsize(out) // 1024} KB)")


def main():
    if len(sys.argv) > 1:
        T.CAPTURE = sys.argv[1]
    if not os.path.exists(T.CAPTURE):
        print(f"capture not found: {T.CAPTURE}")
        return 1
    write("grocery_cart.json", build(T._items()))
    try:
        write("booking.json", build_booking())
    except FileNotFoundError as e:
        print(f"booking fixture skipped: {e}")
    write("transfer.json", build_transfer())
    return 0


if __name__ == "__main__":
    sys.exit(main())
