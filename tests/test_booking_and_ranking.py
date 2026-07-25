"""Pin the money-moving request bodies against the real app, and the ranking rules.

The booking flow was written from the capture and — by the user's decision — has
never been run live: no order was created and nothing was paid. That makes these
tests the only thing standing between a typo and a wrong charge, so they compare
what the client builds against the bytes the real app sent (captures2.xml).

Ranking is here too because its one non-obvious rule is easy to regress: a good
whose nutrition the retailer never published must never win a "highest calories"
query just because None sorts low.

    python3 tests/test_booking_and_ranking.py
"""
import base64
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.client import MobileSession  # noqa: E402
from src.server import _rank_rows, _seat_rows  # noqa: E402

CAPTURE = os.environ.get("TBANK_CAPTURE2", os.path.expanduser("~/tbank-app/captures2.xml"))

CREATE_MOVIE = 748     # POST /api/order/create/movie
CREATE_CONCERT = 970   # POST /api/order/create/concert
PAY = 850              # POST /pg-api/v1/payment-gate/payments

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def _items():
    with open(CAPTURE, "rb") as fh:
        return re.findall(r"<item>(.*?)</item>", fh.read().decode("utf-8", "replace"), re.S)


def _raw(item, tag):
    m = re.search(r"<%s( [^>]*)?>(.*?)</%s>" % (tag, tag), item, re.S)
    body = m.group(2).replace("<![CDATA[", "").replace("]]>", "")
    return base64.b64decode(body) if 'base64="true"' in (m.group(1) or "") else body.encode()


def request_json(items, n):
    raw = _raw(items[n], "request")
    head, _, body = raw.partition(b"\r\n\r\n")
    if b"content-encoding: gzip" in head.lower():
        body = gzip.decompress(body)
    return json.loads(body)


class ReplaySession(MobileSession):
    """Captures the body instead of sending it."""

    def __init__(self):
        self.sent_key = None
        self.sent_body = None
        self.sent_overrides = None

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        self.sent_key, self.sent_body = key, body
        self.sent_overrides = overrides or {}
        return {}


def check_create(items, label, capture_item, kind, seats):
    s = ReplaySession()
    real = request_json(items, capture_item)
    s.create_ticket_order(real["eventId"], real["slotId"], real["objectId"],
                          seats, kind=kind)
    got = s.sent_body
    check(sorted(got) == sorted(real),
          f"{label}: top-level keys ours={sorted(got)} real={sorted(real)}")
    for field in ("eventId", "slotId", "objectId"):
        check(str(got[field]) == str(real[field]),
              f"{label}: {field} ours={got[field]!r} real={real[field]!r}")
    check(got["seats"] == real["seats"],
          f"{label}: seats ours={got['seats']!r} real={real['seats']!r}")
    print(f"  {label}: {json.dumps(got, ensure_ascii=False)[:110]}")


def check_pay(items):
    real = request_json(items, PAY)
    s = ReplaySession()
    s.pay_marketplace_order(real["flow"]["orderId"], real["amount"]["amount"],
                            real["paymentMethod"]["agreement"],
                            real["flow"]["nfsPaymentToken"])
    got = s.sent_body
    check(got == real, f"pay body diverges\n    ours={got!r}\n    real={real!r}")
    # The mobile gate, not the www.tbank.ru web gate the grocery checkout uses.
    check(s.sent_key == "payment_gate_pay_mobile",
          f"pay must go through the mobile gate, used {s.sent_key!r}")
    print(f"  payment: {json.dumps(got, ensure_ascii=False)}")


def check_ranking():
    rows = [
        {"name": "a", "price": 100, "kcal": 90},
        {"name": "b", "price": 80, "kcal": None},   # retailer published nothing
        {"name": "c", "price": 120, "kcal": 300},
    ]
    asc = [r["name"] for r in _rank_rows(rows, "kcal", "asc")]
    check(asc == ["a", "c", "b"], f"asc: unknown must sort last, got {asc}")
    desc = [r["name"] for r in _rank_rows(rows, "kcal", "desc")]
    check(desc == ["c", "a", "b"], f"desc: unknown must STILL sort last, got {desc}")
    same = [r["name"] for r in _rank_rows(rows, "", "asc")]
    check(same == ["a", "b", "c"], f"no sort_by must preserve store order, got {same}")
    price_desc = [r["name"] for r in _rank_rows(rows, "price", "desc")]
    check(price_desc == ["c", "a", "b"], f"price desc wrong: {price_desc}")
    print("  ranking: unknown values sort last in BOTH directions")


def check_seat_grouping():
    halls = [{"hallName": "Зал 1", "seats": [
        {"status": "vacant", "price": 500.0, "pos": {"row": "1", "number": "2"}},
        {"status": "vacant", "price": 500.0, "pos": {"row": "1", "number": "1"}},
        {"status": "occupied", "price": 500.0, "pos": {"row": "1", "number": "3"}},
        {"status": "vacant", "price": 900.0, "pos": {"row": "2", "number": "1"}},
    ]}]
    text = "\n".join(_seat_rows(halls))
    check("свободно 3" in text, f"occupied seats must be excluded: {text!r}")
    check("1, 2" in text, f"seat numbers must be sorted: {text!r}")
    cheap = "\n".join(_seat_rows(halls, max_price=600))
    check("ряд   2" not in cheap, f"max_price must drop pricier rows: {cheap!r}")
    one = "\n".join(_seat_rows(halls, row="2"))
    check("ряд   1" not in one and "ряд   2" in one, f"row filter broken: {one!r}")
    print("  seats: occupied excluded, sorted, price/row filters applied")


def main():
    print("booking bodies + ranking:")
    check_ranking()
    check_seat_grouping()
    if not os.path.exists(CAPTURE):
        print(f"SKIP body checks: capture not found at {CAPTURE}")
    else:
        items = _items()
        check_create(items, "order/create/movie", CREATE_MOVIE, "movie",
                     [{"id": "7:10", "type": "basic"}])
        check_create(items, "order/create/concert", CREATE_CONCERT, "concert",
                     [{"id": "Фанзона|5000§~§54093386|default"}])
        check_pay(items)
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
