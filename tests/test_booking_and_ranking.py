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
# The one recorded cancellation: captures.xml only ever caught this endpoint
# answering 500, which read as "endpoint broken" until this capture showed the
# real request carries paymentId too.
# TBANK_TICKET_CANCEL_CAPTURE, not TBANK_CAPTURE_CANCEL: that name is the GROCERY
# cancel capture in tests/test_grocery_cancel.py. Two tests reading one variable
# with two different default files meant that setting it to either real capture
# broke the other test, with a misleading "fixture drifted" message.
CANCEL_CAPTURE = os.environ.get("TBANK_TICKET_CANCEL_CAPTURE",
                                os.path.expanduser("~/tbank-app/delete-order.xml"))

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


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "booking.json")


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def check_create(real, label, kind, seats):
    s = ReplaySession()
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


def check_pay(real):
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


def check_fixture_still_matches_capture(fx):
    """Only where the real capture lives: the fixture must not drift from what the
    app sends. Scrubbed values (the payer account, the order id) are exempt; every
    structural and catalogue value is not."""
    items = _items()
    for key, idx, scrubbed in (("create_movie", CREATE_MOVIE, ()),
                               ("create_concert", CREATE_CONCERT, ()),
                               ("pay", PAY, ("agreement", "orderId"))):
        real, mine = request_json(items, idx), fx[key]

        def cmp(a, b, path=""):
            if isinstance(b, dict):
                if sorted(a or {}) != sorted(b):
                    check(False, f"fixture {key}{path}: keys drifted "
                                 f"— fixture={sorted(a or {})} capture={sorted(b)}")
                    return
                for k in b:
                    if k in scrubbed:
                        continue
                    cmp((a or {}).get(k), b[k], f"{path}.{k}")
            elif isinstance(b, list):
                check(len(a or []) == len(b),
                      f"fixture {key}{path}: list length drifted")
                for i, item in enumerate(b):
                    cmp((a or [None] * len(b))[i], item, f"{path}[{i}]")
            else:
                check(a == b, f"fixture {key}{path}: value drifted "
                              f"— fixture={a!r} capture={b!r}")
        cmp(mine, real)
    print("  fixture vs capture: bodies still match the real app")


class PaySession(ReplaySession):
    """Records the marketplace payment instead of making it."""

    def __init__(self, booked_amount, stage="SUCCESS"):
        super().__init__()
        self.booked_amount = booked_amount
        self.stage = stage
        self.paid = None

    def ensure_fresh(self, *a, **kw):
        return None

    def order_details(self, order_id):
        return {"cartInfo": {"amount": self.booked_amount}} if self.booked_amount else {}

    def _source_account(self):
        return "1111111111"

    def pay_marketplace_order(self, order_id, amount, account, token):
        self.paid = {"order_id": order_id, "amount": amount,
                     "account": account, "token": token}
        return {"paymentId": "PAY-1", "stage": {"status": self.stage}}


def check_ticket_pay_amount_guard():
    """The only guard on a path that has never been run live: the amount is
    re-read from the bank and a mismatch must stop the payment."""
    from src import server

    saved = server._require
    try:
        # Mismatch → refuse, and do NOT call the gateway.
        wrong = PaySession(booked_amount=1760)
        server._require = lambda: wrong
        out = server.ticket_pay("ORD-1", 176, "482")
        check(wrong.paid is None, "a mismatched amount still reached the payment gateway")
        check("не сходится" in out, f"the refusal must say why: {out}")
        check("1760" in out and "176" in out,
              f"both amounts must be shown so the user can see the difference: {out}")

        # Matching → pays, with the caller's account and token.
        ok = PaySession(booked_amount=1760)
        server._require = lambda: ok
        out2 = server.ticket_pay("ORD-1", 1760, "482", account_id="9999999999")
        check(ok.paid is not None, f"a matching amount must be paid: {out2}")
        check(ok.paid["account"] == "9999999999",
              f"the chosen account must be used: {ok.paid}")
        check(ok.paid["token"] == "482", f"the nfs token must be forwarded: {ok.paid}")
        check("ОПЛАЧЕНО" in out2 and "PAY-1" in out2, f"the result must carry paymentId: {out2}")

        # A missing token must be refused BEFORE any request.
        notoken = PaySession(booked_amount=1760)
        server._require = lambda: notoken
        out3 = server.ticket_pay("ORD-1", 1760, "")
        check(notoken.paid is None, "a payment without the nfs token was attempted")
        check("cinema_book" in out3, f"the message must say where the token comes from: {out3}")

        # A non-SUCCESS stage must not be reported as paid.
        pending = PaySession(booked_amount=1760, stage="PENDING")
        server._require = lambda: pending
        out4 = server.ticket_pay("ORD-1", 1760, "482")
        check("НЕ подтверждена" in out4, f"a non-SUCCESS stage must not read as paid: {out4}")
        check("Не повторяй вслепую" in out4, f"and must warn against a blind retry: {out4}")

        # An order the bank cannot price must not silently skip the check.
        unknown = PaySession(booked_amount=None)
        server._require = lambda: unknown
        server.ticket_pay("ORD-1", 1760, "482")
        check(unknown.paid is not None,
              "with no amount on the order the tool may proceed, but must not crash")
    finally:
        server._require = saved
    print("  ticket_pay: amount cross-check, token guard, non-SUCCESS all enforced")


def test_the_ticket_payment_names_its_calling_system():
    """Pg-Api-System tells the payment gate WHICH system a payment came from, and
    the app sends it on every call. Both captured flavours carry it and they differ:
    the grocery web checkout says «t-grocery-ib», the app's own marketplace payment
    says «t-entertainment-mb». Same path, same body shape — with the header missing
    the gate is left to guess which caller it is talking to."""
    seen = {}

    class GateSession(MobileSession):
        def __init__(self):
            self.mobile_sessionid = "sid"
            self.access_token = "tok"
            self.device_id = "dev"
            self.old_device_id = "dev"
            self.cookie_str = ""
            self.platform, self.app_name, self.app_version = "ios", "mobile", "7.31.6"
            self._memo = {}

        def ensure_fresh(self, *a, **kw):
            return None

        def _unwrap(self, r):
            return {"paymentId": "1", "stage": {"status": "SUCCESS"}}

    s = GateSession()

    class FakeHTTP:
        def post(self, url, **kw):
            seen.update(url=url, headers=kw.get("headers") or {},
                        json=kw.get("json"), data=kw.get("data"))
            return object()

        def get(self, url, **kw):
            return object()

    s._http = FakeHTTP()
    s.pay_marketplace_order("10000000000", 1936, "0000000000", "482")

    hdr = {k.lower(): v for k, v in seen.get("headers", {}).items()}
    check(hdr.get("pg-api-system") == "t-entertainment-mb",
          f"the ticket payment does not name its system: pg-api-system="
          f"{hdr.get('pg-api-system')!r}")
    check("payment-gate/payments" in seen.get("url", ""),
          f"unexpected endpoint: {seen.get('url')}")
    print("  ticket payment: Pg-Api-System names the entertainment system")


CANCEL_ORDER, CANCEL_PAYMENT = "10000000000", "100000000001"


def check_cancel_carries_both_ids():
    """Both ids go in the QUERY, and the body stays absent.

    The app sends paymentId, so we send it; the captures give no evidence that
    omitting it changes the answer — what decides that is isCancelAvailable."""
    s = ReplaySession()
    s.cancel_ticket_order(CANCEL_ORDER, payment_id=CANCEL_PAYMENT)
    check(s.sent_key == "order_cancel_movie", f"wrong template: {s.sent_key!r}")
    check(s.sent_overrides.get("orderId") == CANCEL_ORDER,
          f"orderId missing from the query: {s.sent_overrides}")
    check(s.sent_overrides.get("paymentId") == CANCEL_PAYMENT,
          f"paymentId missing from the query: {s.sent_overrides}")
    # No body at all. This used to assert `== {}`, which pinned the divergence
    # rather than the capture: body={} makes _call_read post a literal two-byte
    # `{}`, while both captured cancels send Content-Length: 0.
    check(s.sent_body is None, f"the body must be absent, got {s.sent_body!r}")

    concert = ReplaySession()
    concert.cancel_ticket_order(CANCEL_ORDER, kind="concert",
                                payment_id=CANCEL_PAYMENT)
    check(concert.sent_key == "order_cancel",
          f"concerts use the generic path, not {concert.sent_key!r}")

    # No paymentId passed → looked up from the orders() feed.
    class WithFeed(ReplaySession):
        def orders(self):
            return [{"orderId": "99", "paymentId": "999"},
                    {"orderId": CANCEL_ORDER, "paymentId": CANCEL_PAYMENT}]

    looked_up = WithFeed()
    looked_up.cancel_ticket_order(CANCEL_ORDER)
    check(looked_up.sent_overrides.get("paymentId") == CANCEL_PAYMENT,
          f"paymentId was not resolved from orders(): {looked_up.sent_overrides}")

    # An unpaid reservation has none: cancel on orderId alone rather than
    # sending paymentId=None and 400-ing.
    unpaid = ReplaySession()
    unpaid.cancel_ticket_order(CANCEL_ORDER)
    check("paymentId" not in unpaid.sent_overrides,
          f"an unresolved paymentId must be omitted: {unpaid.sent_overrides}")
    print("  cancel: orderId + paymentId in the query, empty body, feed lookup")


def check_every_vertical_reaches_its_own_path():
    """Each call used to choose its path with «concert if kind == 'concert' else
    movie», so anything that was not the string "concert" booked a cinema seat.
    A theatre kind would have gone to /order/create/movie and been accepted.

    The table has to route all four, and refuse a fifth rather than fall back."""
    from src.client import TbankApiError
    want = {
        "movie": ("scheme_sectors_movie", "order_create_movie", "order_cancel_movie"),
        "concert": ("scheme_sectors_concert", "order_create_concert", "order_cancel"),
        "spectacle": ("scheme_sectors_spectacle", "order_create_spectacle", "order_cancel"),
        "exhibition": ("scheme_sectors_exhibition", "order_create_exhibition", "order_cancel"),
    }
    for kind, (sectors, create, cancel) in want.items():
        s = ReplaySession()
        s.event_seats("e", "s", "o", kind=kind)
        check(s.sent_key == sectors, f"{kind} seats → {s.sent_key!r}, want {sectors!r}")
        s = ReplaySession()
        s.create_ticket_order("e", "s", "o", [{"id": "1"}], kind=kind)
        check(s.sent_key == create, f"{kind} create → {s.sent_key!r}, want {create!r}")
        s = ReplaySession()
        s.cancel_ticket_order(CANCEL_ORDER, kind=kind, payment_id=CANCEL_PAYMENT)
        check(s.sent_key == cancel, f"{kind} cancel → {s.sent_key!r}, want {cancel!r}")

    # The Russian words the tools document, and the API's own spellings, both work.
    for alias, segment in (("кино", "movie"), ("театр", "spectacle"),
                           ("выставка", "exhibition"), ("theatre", "spectacle")):
        s = ReplaySession()
        s.event_seats("e", "s", "o", kind=alias)
        check(s.sent_key == f"scheme_sectors_{segment}",
              f"alias {alias!r} → {s.sent_key!r}, want segment {segment!r}")

    # A vertical nobody wired up must fail loudly, not book a cinema seat.
    for bad in ("opera", "movies", ""):
        try:
            ReplaySession().event_seats("e", "s", "o", kind=bad)
            check(False, f"kind={bad!r} was accepted instead of refused")
        except TbankApiError as e:
            check(e.result_code == "UNKNOWN_KIND" and "movie" in str(e),
                  f"the refusal must name the problem: {e}")
    print("  verticals: all four route to their own path, an unknown kind refuses")


def check_cancel_reads_the_order_before_asking():
    """isCancelAvailable is the only field that predicted the outcome live: the one
    order flagged true cancelled, seven flagged false were refused and stayed put.

    So a flagged-false order must not be asked at all — sending it is a request we
    already know the answer to — and a refusal must never read like a cancellation.
    The previous version of this test asserted the opposite lie (that a missing
    paymentId gets a silent "Success"), which is why it is gone."""
    from src import server

    class CancelSession(ReplaySession):
        """Answers the order card and the cancel separately, and records only the
        cancel — the tool re-reads the card after a success, and that read must not
        be mistaken for the request under test."""

        def __init__(self, *, available=None, payment_id="", status="CREATED",
                     verdict=None, feed=()):
            super().__init__()
            self.available, self.card_payment_id, self.status = available, payment_id, status
            self.verdict = verdict if verdict is not None else {"status": "Success"}
            self.feed, self.cancel_key, self.cancel_query = list(feed), None, None

        def ensure_fresh(self, *a, **kw):
            return None

        def orders(self):
            return self.feed

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            if key == "order_get":
                info = {"status": self.status}
                if self.available is not None:
                    info["isCancelAvailable"] = self.available
                return {"orderInfo": info,
                        "cartInfo": {"paymentId": self.card_payment_id}}
            self.cancel_key, self.cancel_query = key, dict(overrides or {})
            return self.verdict

    saved = server._require
    try:
        # 1. The bank says no → nothing is sent, and the answer says why.
        locked = CancelSession(available=False, status="CREATED")
        server._require = lambda: locked
        out = server.ticket_cancel(CANCEL_ORDER)
        check(locked.cancel_key is None,
              f"a flagged-false order must not be asked, sent {locked.cancel_key!r}")
        check("isCancelAvailable" in out and "force" in out,
              f"the refusal must name the field and the escape hatch: {out}")

        # 2. force=True overrides that — the request goes out.
        forced = CancelSession(available=False, payment_id=CANCEL_PAYMENT)
        server._require = lambda: forced
        server.ticket_cancel(CANCEL_ORDER, force=True)
        check(forced.cancel_key == "order_cancel_movie",
              f"force must send the request, sent {forced.cancel_key!r}")

        # 3. A business refusal is not a cancellation.
        refused = CancelSession(available=True,
                                verdict={"status": "Failed", "code": "1002"})
        server._require = lambda: refused
        no = server.ticket_cancel(CANCEL_ORDER)
        check("ОТКЛОНЕНА" in no and "1002" in no,
              f"a Failed verdict must read as a refusal with its code: {no}")
        check("принята" not in no, f"a refusal must not read as accepted: {no}")

        # 4. A success is reported and re-read.
        ok = CancelSession(available=True, payment_id=CANCEL_PAYMENT,
                           status="PARTIALLY_CANCELED")
        server._require = lambda: ok
        yes = server.ticket_cancel(CANCEL_ORDER)
        check("принята" in yes, f"a Success must read as accepted: {yes}")
        check("PARTIALLY_CANCELED" in yes,
              f"the re-read status must be printed: {yes}")
        check(ok.cancel_query.get("paymentId") == CANCEL_PAYMENT,
              f"the card's paymentId never reached the query: {ok.cancel_query}")

        # 5. An explicit payment_id wins over the one on the card.
        explicit = CancelSession(available=True, payment_id="999")
        server._require = lambda: explicit
        server.ticket_cancel(CANCEL_ORDER, payment_id=CANCEL_PAYMENT)
        check(explicit.cancel_query.get("paymentId") == CANCEL_PAYMENT,
              f"an explicit payment_id was dropped: {explicit.cancel_query}")
    finally:
        server._require = saved
    print("  ticket_cancel: refuses before asking, tells a refusal from a cancellation")


class WireSession(MobileSession):
    """Builds the request for real — through _call_read's own param assembly —
    and records it at the HTTP boundary instead of sending it.

    ReplaySession stops one layer too early to see a QUERY-string endpoint:
    cancel carries everything in the query and nothing in the body, so the only
    way to check what actually goes on the wire is to let the params be built."""

    def __init__(self):
        super().__init__(mobile_sessionid="sid", refresh_token="rt")
        self.access_token = "tok"
        self.device_id = self.old_device_id = "dev"
        self.platform, self.app_name, self.app_version = "ios", "mobile", "7.31.6"
        self.origin = "mobile,ib5,loyalty,platform"
        self.sent = {}

        outer = self

        class FakeHTTP:
            def post(self, url, **kw):
                outer.sent = {"url": url, "params": kw.get("params") or {},
                              "json": kw.get("json"), "data": kw.get("data"),
                              "headers": kw.get("headers") or {}}
                return object()

            def get(self, url, **kw):
                return self.post(url, **kw)

        self._http = FakeHTTP()

    def _unwrap(self, r):
        return {"status": "Success"}


def check_cancel_request_matches_fixture(fx):
    """Runs EVERYWHERE, capture or no capture — the fixture is the contract.

    Asserts the built request against the real app's shape: same method, host and
    path, and no query key the app sends is missing from ours."""
    import urllib.parse

    real = fx["cancel"]
    s = WireSession()
    s.cancel_ticket_order(CANCEL_ORDER, payment_id=CANCEL_PAYMENT)

    parts = urllib.parse.urlsplit(s.sent["url"])
    check(f"{parts.scheme}://{parts.netloc}" == real["host"],
          f"host drifted: ours={parts.scheme}://{parts.netloc} app={real['host']}")
    check(parts.path == real["path"],
          f"path drifted: ours={parts.path} app={real['path']}")

    params = s.sent["params"]
    missing = [k for k in real["query_keys"] if k not in params]
    check(not missing, f"the app sends {missing} in the query, we omit them")
    check(str(params.get("orderId")) == CANCEL_ORDER,
          f"orderId not in the query: {sorted(params)}")
    check(str(params.get("paymentId")) == CANCEL_PAYMENT,
          f"paymentId not in the query: {sorted(params)}")
    # Content-Length: 0 in the capture — an empty dict, never a JSON payload.
    check(s.sent["json"] in ({}, None) and not s.sent["data"],
          f"the cancel body must stay empty: json={s.sent['json']!r} "
          f"data={s.sent['data']!r}")
    print(f"  cancel wire shape: POST {parts.path}, "
          f"{len(real['query_keys'])} query keys incl. orderId+paymentId")


def check_cancel_fixture_still_matches_capture(fx):
    """Only where the capture lives: the fixture must not drift from the app.
    Prints key names only — the capture holds live ids."""
    import urllib.parse

    with open(CANCEL_CAPTURE, "rb") as fh:
        blob = fh.read().decode("utf-8", "replace")
    real = None
    for item in re.findall(r"<item>(.*?)</item>", blob, re.S):
        url = re.search(r"<url>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</url>", item, re.S)
        if not url or "/order/cancel" not in url.group(1):
            continue
        parts = urllib.parse.urlsplit(url.group(1).strip())
        method = re.search(r"<method>(?:<!\[CDATA\[)?(\w+)", item)
        real = {"method": method.group(1) if method else "",
                "host": f"{parts.scheme}://{parts.netloc}", "path": parts.path,
                "query_keys": sorted(urllib.parse.parse_qs(parts.query))}
        break
    if real is None:
        check(False, f"no /order/cancel request found in {CANCEL_CAPTURE}")
        return
    mine = fx["cancel"]
    before = len(failures)
    for field in ("method", "host", "path"):
        check(mine[field] == real[field],
              f"fixture cancel.{field} drifted — fixture={mine[field]!r} "
              f"capture={real[field]!r}")
    check(sorted(mine["query_keys"]) == real["query_keys"],
          f"fixture cancel.query_keys drifted — fixture={sorted(mine['query_keys'])} "
          f"capture={real['query_keys']}")
    # Only claim a match if one was found: this line used to print unconditionally,
    # so the per-check log said "still matches" directly above the drift it had
    # just recorded.
    if len(failures) == before:
        print("  cancel fixture vs capture: still matches the real app")
    else:
        print("  cancel fixture vs capture: DRIFTED — see the failures below")


def main():
    print("booking bodies + ranking:")
    check_ranking()
    check_seat_grouping()
    check_ticket_pay_amount_guard()
    test_the_ticket_payment_names_its_calling_system()
    check_cancel_carries_both_ids()
    check_every_vertical_reaches_its_own_path()
    check_cancel_reads_the_order_before_asking()
    fx = fixture()
    check_create(fx["create_movie"], "order/create/movie", "movie",
                 [{"id": "7:10", "type": "basic"}])
    check_create(fx["create_concert"], "order/create/concert", "concert",
                 [{"id": "Фанзона|5000§~§54093386|default"}])
    check_pay(fx["pay"])
    check_cancel_request_matches_fixture(fx)
    if os.path.exists(CANCEL_CAPTURE):
        check_cancel_fixture_still_matches_capture(fx)
    else:
        print(f"  (cancel capture absent at {CANCEL_CAPTURE} — the wire shape above "
              f"was still verified against the fixture; only drift check skipped)")
    if os.path.exists(CAPTURE):
        check_fixture_still_matches_capture(fx)
    else:
        print(f"  (capture absent at {CAPTURE} — drift check skipped; the bodies "
              f"above were still verified against the fixture)")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
