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
        "client_info": scrub(T.response_json(items, T.CLIENT_INFO)["payload"]),
        "cart_get": {"cart": scrub(T.response_json(items, T.CART_GET)["payload"]["cart"])},
        "retailers": slim_retailers(T.response_json(items, T.RETAILERS)["payload"]),
        "expected_azbuka": scrub(T.request_json(items, T.AZBUKA_CART_SET)),
        "expected_vkusvill": scrub(T.request_json(items, T.VKUSVILL_CART_SET)),
        "error_envelope": T.response_json(items, T.ERROR_ENVELOPE),
    }


def build_booking():
    """The three money-moving ticket bodies, from captures2.xml.

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
                  "Regenerate with tests/fixtures/regen.py."),
        "create_movie": movie,
        "create_concert": concert,
        "pay": pay,
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
