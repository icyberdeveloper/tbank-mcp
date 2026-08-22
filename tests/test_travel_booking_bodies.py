"""The travel booking bodies must match what the real app sends.

None of these three calls can ever be exercised live: `orders/create` reserves a
real seat, `orders/pay` and `travel_pay` move real money. So the only way they are
verified at all is here — by building the body the code WOULD send from recorded
inputs and comparing it, field by field, against the body the app actually sent.

Every bug in this repo's booking history came from a body that looked right:
`isSuspicious` baked into a template, JSON posted to a form endpoint, a dropped
`X-App-*` header answering 406. These are the same class, and they are silent —
the bank answers with a code, not with «you forgot carSearchId».

Three things here are counter-intuitive enough that they are asserted by name,
because a future tidy-up would «fix» each of them:

  * refund/calculate takes `TicketIds` and refund takes `ticketIds`. Same ids,
    different capitalisation, one API.
  * `segmentId` is invented by the CLIENT — it appears in no response anywhere in
    the capture — so it must be a fresh uuid, not looked up.
  * a flight's charge is fare + seats + check-in, three numbers, and the seat
    price keeps the JSON type the seat map gave it.

The contract lives in tests/fixtures/travel.json (real structure and protocol
values, synthetic personal ones), so this runs on any machine. When the gitignored
capture IS present the fixture is additionally checked against it, so it cannot
drift away from what the app really sends.

    python3 tests/test_travel_booking_bodies.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TBANK_EVENTS", os.path.join(tempfile.mkdtemp(), "events.jsonl"))
os.environ.setdefault("TBANK_ATTEMPTS",
                      os.path.join(tempfile.gettempdir(), "tbank-test-attempts.jsonl"))
os.environ.setdefault("TBANK_TRACE_FILE",
                      os.path.join(tempfile.gettempdir(), "tbank-test-calls.jsonl"))

from src import server  # noqa: E402
from src.client import MobileSession, TbankApiError  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "travel.json")

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


FX = fixture()


def payload(name):
    """What a client method receives. The fixture keeps whole responses as the app
    saw them, and the travel hosts wrap theirs in an envelope that _unwrap strips —
    so a test that fed the raw record to the code under test would be testing a
    shape the code never sees."""
    record = FX[name]
    return record["payload"] if isinstance(record, dict) and "payload" in record else record

PEOPLE = [
    {"first": "Иван", "last": "Петров", "middle": "Сергеевич",
     "firstEn": "IVAN", "lastEn": "PETROV", "middleEn": "SERGEEVICH",
     "birthDate": "1990-01-31", "number": "1234567890", "sex": "male"},
    {"first": "Мария", "last": "Петрова", "middle": "Ивановна",
     "firstEn": "MARIA", "lastEn": "PETROVA", "middleEn": "IVANOVNA",
     "birthDate": "1992-02-02", "number": "1234567891", "sex": "female"},
]


def shape(node, path="", out=None):
    """Every leaf path with its JSON type — the thing that must not drift."""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            shape(v, f"{path}.{k}", out)
    elif isinstance(node, list):
        if node:
            shape(node[0], f"{path}[]", out)
        else:
            out[path + "[]"] = "empty"
    else:
        out[path] = "null" if node is None else type(node).__name__
    return out


def compare_shape(name, ours, theirs, ignore=()):
    a, b = shape(ours), shape(theirs)
    for key in sorted(set(a) | set(b)):
        if any(key.startswith(i) for i in ignore):
            continue
        if key not in a:
            failures.append(f"{name}: MISSING field {key} (app sends it)")
        elif key not in b:
            failures.append(f"{name}: EXTRA field {key} (app does not send it)")
        elif a[key] != b[key] and "null" not in (a[key], b[key]):
            failures.append(f"{name}: {key} type {a[key]}, app sends {b[key]}")


# ---- rail: orders/create ---------------------------------------------------

def test_rail_order_body():
    """The order body we build == the one the app posted, field for field."""
    real = FX["train_order_create_request"]
    search_seg = FX["train_search_response"]["directions"][0]["ways"][0]["segments"][0]
    cars = FX["train_cars_response"]
    real_seg = real["ways"][0][0]
    real_group = real_seg["placeGroups"][0]
    wanted = [(real_group["carNumber"],
               p["places"][0]["placeNumber"]) for p in real_group["passengers"]]

    hits = server._find_places(cars["cars"], wanted)
    ways = server._rail_ways(search_seg, cars["carSearchId"], hits, PEOPLE)
    ours = {"ways": ways, "customer": {"phone": "+79991234567",
                                       "email": "user@example.com"}}
    compare_shape("orders/create", ours, real)

    seg = ours["ways"][0][0]
    check(seg["carSearchId"] == cars["carSearchId"],
          "orders/create: carSearchId must come from the CARS response, not the search")
    check(seg["origin"] == real_seg["origin"],
          f"orders/create: origin {seg['origin']!r} != app's {real_seg['origin']!r} "
          "— it is the SEGMENT's station, not the searched city")
    group = seg["placeGroups"][0]
    for field in ("type", "carType", "gender", "serviceClass",
                  "bookFullCompartmentType"):
        check(group[field] == real_group[field],
              f"orders/create: placeGroups[0].{field} = {group[field]!r}, "
              f"app sends {real_group[field]!r}")
    check(len(seg["segmentId"]) == 36 and seg["segmentId"].count("-") == 4,
          "orders/create: segmentId must be a fresh uuid — it exists in no response")
    other = server._rail_ways(search_seg, cars["carSearchId"], hits, PEOPLE)
    check(other[0][0]["segmentId"] != seg["segmentId"],
          "orders/create: segmentId must be NEW per order, not a constant")
    places = [p["places"][0]["placeNumber"] for p in group["passengers"]]
    check(places == [n for _, n in wanted],
          f"orders/create: places {places} lost the requested order {wanted}")


def test_rail_seat_order_is_passenger_order():
    """Passenger i must get seat i — a reshuffle seats the wrong person."""
    cars = FX["train_cars_response"]
    car = cars["cars"][0]
    numbers = [p["number"] for g in car["places"] for p in g["places"]][:2]
    wanted = [(car["number"], numbers[1]), (car["number"], numbers[0])]
    hits = server._find_places(cars["cars"], wanted)
    seg = FX["train_search_response"]["directions"][0]["ways"][0]["segments"][0]
    ways = server._rail_ways(seg, cars["carSearchId"], hits, PEOPLE)
    passengers = ways[0][0]["placeGroups"][0]["passengers"]
    got = [(p["documentWithCustomerIndex"]["document"]["name"]["first"],
            p["places"][0]["placeNumber"]) for p in passengers]
    check(got == [(PEOPLE[0]["first"], numbers[1]), (PEOPLE[1]["first"], numbers[0])],
          f"seat order: {got} — passenger i must keep seat i")


def test_rail_taken_seat_is_named():
    """A seat that is gone must be reported by name, not as a bank error code."""
    cars = FX["train_cars_response"]
    try:
        server._find_places(cars["cars"], [("99", "999")])
    except Exception as e:  # noqa: BLE001
        check("99/999" in str(e),
              f"taken seat: error must name the seat, got {e}")
    else:
        failures.append("taken seat: a nonexistent place was accepted")


# ---- rail: the refund key-case trap ----------------------------------------

def test_refund_key_case_differs():
    """calculate wants TicketIds, refund wants ticketIds. Both spellings pinned."""
    sent = {}

    class S(MobileSession):
        def __init__(self):
            pass

        def _call_read(self, key, *, overrides=None, body=None, path_override=None,
                       return_response=False):
            sent[key] = body
            return {}

    s = S()
    ids = ["t1", "t2"]
    s.train_refund_calc("order-1", ids)
    s.train_refund("order-1", ids)
    calc, refund = sent["train_refund_calc"], sent["train_refund"]
    check("TicketIds" in calc,
          f"refund/calculate must send TicketIds (capital T), sent {sorted(calc)}")
    check("ticketIds" in refund,
          f"refund must send ticketIds (lowercase t), sent {sorted(refund)}")
    check("TicketIds" in FX["train_refund_calc_request"],
          "fixture drift: the captured calculate no longer uses TicketIds")
    check("ticketIds" in FX["train_refund_request"],
          "fixture drift: the captured refund no longer uses ticketIds")
    compare_shape("refund/calculate", calc, FX["train_refund_calc_request"])
    compare_shape("refund", refund, FX["train_refund_request"])


# ---- flights: travel_pay ---------------------------------------------------

def test_flight_pay_body():
    real = FX["flight_pay_request"]
    prelim = payload("flight_preliminary_response")
    seatmaps = payload("flight_seatmaps_response")
    # NOT coerced: the API sends whole roubles as an int and the body has to carry
    # the same JSON type, so the test holds the raw value the way the code does.
    checkin = payload("flight_checkin_response")["price"]["amount"]

    real_seats = [f"{s['flights'][0]['row']}{s['flights'][0]['letter']}"
                  for s in real["seats"]]
    blocks, seat_sum = server._seat_blocks(real_seats, PEOPLE, seatmaps, prelim)
    fare = float(prelim["offers"][0]["price"]["amount"])
    total = round(fare + seat_sum + float(checkin), 2)

    ours = {
        "pay_request": {"moneyAmount": total, "currency": "RUB",
                        "attachCard": False, "account": "0000000000"},
        "timezone": "+180", "screen_resolution": "420x912",
        "device_platform": "iOS",
        "contact_info": {"email": "user@example.com", "phone": "+79991234567"},
        "booking": {"persons": [server._person_block(p) for p in PEOPLE],
                    "offer_uuid": prelim["offers"][0]["uuid"]},
        "payAdditionalInfo": dict(server._FLIGHT_PAY_INFO),
        "seats": blocks,
        "checkin": {"price": {"amount": checkin, "currency": "RUB"},
                    "seatStrategies": ["skip"], "placements": [],
                    "summaryText": "Регистрация на рейс",
                    "summaryTextWithoutSeats": "Регистрация на рейс"},
    }
    compare_shape("travel_pay", ours, real,
                  ignore=(".booking.persons[].bonus_card",))

    check(total == real["pay_request"]["moneyAmount"],
          f"travel_pay: charge {total} != the app's {real['pay_request']['moneyAmount']} "
          "— it is fare + seats + check-in, three numbers")
    leg = ours["seats"][0]["flights"][0]
    for field in ("operatingCarrier", "marketingCarrier", "number", "date"):
        check(field in leg,
              f"travel_pay: a seat must name its flight — {field} missing")
    real_leg = real["seats"][0]["flights"][0]
    check(type(leg["price"]["amount"]) is type(real_leg["price"]["amount"]),
          "travel_pay: seat price must keep the seat map's JSON type")
    person = ours["booking"]["persons"][0]
    check(person["name"].isupper() and person["surname"].isupper(),
          "travel_pay: names go on a ticket in capitals")
    check(person["name"] == PEOPLE[0]["firstEn"],
          "travel_pay: the Latin spelling must come from the bank, not be guessed")


def test_flight_bonus_card_both_ways():
    """The same booking carries a passenger WITH an airline card and one without.

    The app sends an object for the first and null for the second; always sending
    null would silently stop the miles from being credited, and always sending an
    object would be a field the app never sends."""
    real_people = payload("flight_preliminary_response") and FX["flight_pay_request"]["booking"]["persons"]
    with_card = next((p for p in real_people if p.get("bonus_card")), None)
    without = next((p for p in real_people if not p.get("bonus_card")), None)
    check(with_card is not None and without is not None,
          "fixture drift: the captured booking no longer has both bonus_card variants")
    if not (with_card and without):
        return
    carded = dict(PEOPLE[0], bonus_card={"carrier_code": "su", "number": "1000000001"})
    block = server._person_block(carded)
    check(block["bonus_card"] == {"carrier_code": "SU", "number": "1000000001"},
          f"bonus_card: built {block['bonus_card']!r}, app sends "
          f"{ {'carrier_code': 'SU', 'number': '1000000001'} !r}")
    check(server._person_block(PEOPLE[1])["bonus_card"] is None,
          "bonus_card: a passenger without a card must send null, not an empty object")
    check(shape(block)[".bonus_card.number"] == shape(with_card)[".bonus_card.number"],
          "bonus_card: number must be a string, as the app sends it")


def test_flight_seats_refuse_connections():
    """One seat per passenger cannot describe two legs — say so, do not guess."""
    prelim = json.loads(json.dumps(payload("flight_preliminary_response")))
    seg = prelim["flights"][0]["flightSegments"][0]
    prelim["flights"][0]["flightSegments"] = [seg, dict(seg)]
    try:
        server._seat_blocks(["13A"], PEOPLE[:1], payload("flight_seatmaps_response"), prelim)
    except Exception as e:  # noqa: BLE001
        check("seats" in str(e).lower() or "перелёт" in str(e),
              f"connection: unhelpful error {e}")
    else:
        failures.append("connection: seats were silently applied to one leg only")


# ---- passengers on the money path -----------------------------------------

class DocSession:
    """A session whose document store holds the owner's passport AND a relative's."""

    def __init__(self, owner_bd="1990-01-31", entries=None, brief_bd="1990-01-31"):
        self._entries = entries if entries is not None else [
            {"value": {"serial": {"value": "1234"}, "number": {"value": "567890"},
                       "person": {"firstName": {"value": "Иван"},
                                  "lastName": {"value": "Петров"},
                                  "birthDate": {"value": owner_bd}}}},
            # A relative — different birthDate, LONGER number (so «longest wins»
            # would pick THIS one without the owner filter).
            {"value": {"serial": {"value": "9999"}, "number": {"value": "88888888"},
                       "person": {"firstName": {"value": "Тёща"},
                                  "lastName": {"value": "Петрова"},
                                  "birthDate": {"value": "1955-05-05"}}}},
        ]
        self._brief_bd = brief_bd

    def identity_documents(self):
        return {"RusNationalID": self._entries}

    def identity_brief(self):
        return {"birthDate": {"value": self._brief_bd}} if self._brief_bd else {}


def test_own_passenger_is_the_owner_not_a_relative():
    """documents() filters relatives by birthDate; the money path must too.

    The store holds the owner (1990) and the mother-in-law (1955, longer number).
    `max(len(number))` alone would pick the relative — putting a stranger's
    passport on the ticket. With the owner filter it must pick the owner's.
    """
    who = server._own_passenger(DocSession())
    check(who["number"] == "1234567890",
          f"must pick the OWNER's passport (serial+number), got {who['number']}")
    check(who["first"] == "Иван",
          f"must be the owner, not the relative: {who['first']}")

    # If nothing matches the holder, refuse rather than guess.
    orphan = DocSession(owner_bd="2000-01-01", brief_bd="1990-01-31")
    try:
        server._own_passenger(orphan)
        check(False, "a store with no owner-matching passport must refuse")
    except TbankApiError as e:
        check(e.result_code == "PASSENGER_AMBIGUOUS",
              f"the refusal must name itself: {e.result_code}")
    print("  passengers: «me» is the owner's passport, ambiguity refused")


def test_a_minor_is_refused_on_the_booking_path():
    """The booking hardcodes adult fare + RussianPassport; a child booked there is
    charged an adult fare with the wrong document, and no capture verifies a child
    booking. So a minor must be refused, on both «me» and explicit passengers.

    Ages are computed against today's clock (not a dated literal), so the birth
    dates here are derived from date.today() and stay correct every year."""
    from datetime import date
    today = date.today()
    child_bd = today.replace(year=today.year - 10).isoformat()
    adult_bd = today.replace(year=today.year - 40).isoformat()

    # «me» that is a minor → refuse.
    minor_me = DocSession(owner_bd=child_bd, brief_bd=child_bd)
    try:
        server._passengers(minor_me, "me")
        check(False, "a minor account holder must be refused on the booking path")
    except TbankApiError as e:
        check(e.result_code == "PASSENGER_MINOR", f"wrong refusal: {e.result_code}")

    # Explicit child passenger → refuse, naming which one.
    spec = json.dumps([{"first": "А", "last": "Б", "birthDate": adult_bd, "number": "1"},
                       {"first": "Д", "last": "Е", "birthDate": child_bd, "number": "2"}])
    try:
        server._passengers(DocSession(), spec)
        check(False, "an explicit child passenger must be refused")
    except TbankApiError as e:
        check(e.result_code == "PASSENGER_MINOR", f"wrong refusal: {e.result_code}")

    # All-adult explicit list passes.
    ok = server._passengers(DocSession(), json.dumps(
        [{"first": "А", "last": "Б", "birthDate": adult_bd, "number": "1"}]))
    check(len(ok) == 1 and ok[0]["birthDate"] == adult_bd,
          f"an adult passenger must pass: {ok}")
    print("  passengers: a minor is refused (me and explicit), adults pass")


# ---- tpay ------------------------------------------------------------------

def test_tpay_init_shape():
    real = FX["tpay_init_request"]
    for field in ("type", "productRequestId", "productId", "fingerprint",
                  "scenario", "theme"):
        check(field in real, f"fixture drift: INIT_TPW lost {field}")
    check(real["type"] == "INIT_TPW", "fixture drift: INIT_TPW type changed")
    check(MobileSession.TPAY_THEME == real["theme"],
          f"tpay: theme {MobileSession.TPAY_THEME} != captured {real['theme']}")
    check(MobileSession.TPAY_VIEWPORT ==
          (real["fingerprint"]["screen_width"], real["fingerprint"]["screen_height"]),
          "tpay: viewport differs from the captured fingerprint")
    check(MobileSession.TPAY_SSO_CLIENT == "tinkoff-pay-web",
          "tpay: the SSO client id is what the gateway keys the code on")


def test_tpay_flow_sends_account_step_and_correct_headers():
    """Drive the real tpay_pay(dry_run) through a recording jar.

    Two capture-verified facts the old code got wrong:
      * ACCOUNT_TPW must be sent after TOKEN_TPW, else /account never leaves NEW
        and the poll times out — after the order already holds a payment timer.
      * /status keys off the PRODUCT request id with `T-Request-Id`, while /account
        keys off the SESSION id with `T-Session-Id`; the old code sent
        `T-Session-Id=sessionId` on both.
    Also: every tpay request carries the WEBVIEW UA, not the native one.
    """
    from src.client import MobileSession

    class Resp:
        def __init__(self, body=b"", js=None):
            self.content = body
            self.status_code = 200
            self._js = js
            self.text = ""

        def json(self):
            if self._js is None:
                raise ValueError("no json")
            return self._js

        def raise_for_status(self):
            pass

    class RecJar:
        def __init__(self):
            self.reqs = []

        def _answer(self, method, url, headers, body):
            leaf = url.split("/api/v2/tpayid/")[-1].split("?")[0]
            self.reqs.append({"method": method, "leaf": leaf,
                              "headers": dict(headers), "body": body})
            if leaf == "session":
                t = (body or {}).get("type")
                if t == "INIT_TPW":
                    return Resp(b"{}", {"sessionId": "SID-1", "state": "STATE-1"})
                return Resp(b"{}", {})           # TOKEN/ACCOUNT/PAY
            if leaf == "token":
                return Resp(b"")                 # zero-length body
            if leaf == "account":
                return Resp(b"{}", {"status": "ACCOUNT",
                                    "accounts": [{"id": "a"}], "cards": [{"cardId": "c"}],
                                    "brandInfo": {"brandName": "Поезда"}})
            if leaf == "status":
                return Resp(b"{}", {"amount": 1000})
            return Resp(b"{}", {})

        def post(self, url, json=None, headers=None, timeout=None):
            return self._answer("POST", url, headers or {}, json)

        def get(self, url, headers=None, timeout=None):
            return self._answer("GET", url, headers or {}, None)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    s = MobileSession.__new__(MobileSession)
    s.sso_login_cookie = "SSO_SESSION=x"
    s.platform = "ios"
    jar = RecJar()
    s._fresh_jar = lambda: jar
    s._tpay_sso_code = lambda j, state, url: "CODE-1"

    out = s.tpay_pay("https://tpay.tbank.ru/PRID-1/17/tpay", dry_run=True)
    check(out.get("dry_run") is True and out["accounts"] == [{"id": "a"}],
          f"dry_run must return the payment methods: {out}")

    session_types = [r["body"]["type"] for r in jar.reqs if r["leaf"] == "session"]
    check(session_types == ["INIT_TPW", "TOKEN_TPW", "ACCOUNT_TPW"],
          f"the session sequence must include ACCOUNT_TPW after TOKEN_TPW: {session_types}")

    status_req = next((r for r in jar.reqs if r["leaf"] == "status"), None)
    check(status_req is not None, "no /status request was made")
    if status_req:
        check(status_req["headers"].get("T-Request-Id") == "PRID-1",
              f"/status must key off productRequestId via T-Request-Id: {status_req['headers']}")
        check("T-Session-Id" not in status_req["headers"],
              "/status must NOT carry T-Session-Id (that was the bug)")

    acct_req = next((r for r in jar.reqs if r["leaf"] == "account"), None)
    check(acct_req and acct_req["headers"].get("T-Session-Id") == "SID-1",
          f"/account must key off the session id via T-Session-Id: {acct_req and acct_req['headers']}")

    init = next(r for r in jar.reqs if r["leaf"] == "session")
    check(init["body"]["fingerprint"]["userAgent"] == MobileSession.TPAY_WEBVIEW_UA,
          "the fingerprint must carry the webview UA, not the native one")
    check("Mozilla/5.0" in init["headers"].get("User-Agent", ""),
          "tpay requests must go out with the webview User-Agent")
    print("  tpay: ACCOUNT_TPW sent, /status vs /account headers correct, webview UA")


# ---- the fixture must not drift from the capture ---------------------------

def test_fixture_matches_capture():
    """When the real capture is present, the fixture must still match its SHAPE."""
    capture = os.environ.get("TBANK_CAPTURE_TRAVEL",
                             os.path.expanduser("~/tbank-app/captures-flight-train.xml"))
    if not os.path.exists(capture):
        print(f"  (capture absent — fixture shape not re-verified: {capture})")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
    import regen_travel as R

    parsed = R.items(capture)
    for name, index in R.ITEMS.items():
        tag = "request" if name.endswith("_request") else "response"
        try:
            live = json.loads(R.body_of(R.raw(parsed[index], tag)))
        except Exception as e:  # noqa: BLE001
            failures.append(f"fixture drift: cannot read capture item {index} "
                            f"for {name}: {e}")
            continue
        a, b = shape(FX[name]), shape(live)
        missing = sorted(set(b) - set(a))
        extra = sorted(set(a) - set(b))
        check(not missing,
              f"fixture drift: {name} is missing {missing[:4]} — "
              f"re-run tests/fixtures/regen_travel.py")
        check(not extra,
              f"fixture drift: {name} has stale {extra[:4]} — "
              f"re-run tests/fixtures/regen_travel.py")


def test_the_flight_pay_envelope_reports_the_shape_it_got():
    """_envelope carries the state that lives OUTSIDE `payload` for flight pay/result,
    and must never raise on a non-2xx — for those calls a 400 means «nothing in
    flight», not «broken». A mutation to its branches broke no test, so pin all three:
    a dict body (http filled, not overwritten), an unreadable body, and a non-dict
    JSON body."""
    class Resp:
        def __init__(self, body, status=200, text="", raise_json=False):
            self._body, self.status_code, self.text = body, status, text
            self._raise = raise_json
        def json(self):
            if self._raise:
                raise ValueError("no json")
            return self._body

    # (a) dict body: status_code fills `http` via setdefault…
    d = MobileSession._envelope(Resp({"status": "WaitingPayment"}, status=200))
    check(d["status"] == "WaitingPayment" and d["http"] == 200,
          f"a dict envelope must keep its fields and gain http: {d}")
    # …but an http the payload already carried must NOT be overwritten.
    d2 = MobileSession._envelope(Resp({"status": "X", "http": 418}, status=200))
    check(d2["http"] == 418, f"setdefault must not clobber an existing http: {d2}")

    # (b) unreadable body on a non-2xx: no raise, status=Unreadable, text kept.
    u = MobileSession._envelope(Resp(None, status=400, text="upstream 400 boom",
                                     raise_json=True))
    check(u["status"] == "Unreadable" and u["http"] == 400,
          f"an unreadable body must be reported, not raised: {u}")
    check("boom" in u["text"], f"the body excerpt must survive for diagnosis: {u}")

    # (c) JSON that is not a dict (a bare list) → Unexpected, data preserved.
    x = MobileSession._envelope(Resp([1, 2, 3], status=200))
    check(x["status"] == "Unexpected" and x["data"] == [1, 2, 3],
          f"a non-dict JSON body must be flagged, its data kept: {x}")
    print("  _envelope: dict/unreadable/non-dict shapes all reported, never raised")


def main():
    for fn in (test_rail_order_body, test_rail_seat_order_is_passenger_order,
               test_rail_taken_seat_is_named, test_refund_key_case_differs,
               test_flight_pay_body, test_flight_bonus_card_both_ways,
               test_flight_seats_refuse_connections,
               test_own_passenger_is_the_owner_not_a_relative,
               test_a_minor_is_refused_on_the_booking_path,
               test_tpay_init_shape,
               test_tpay_flow_sends_account_step_and_correct_headers,
               test_the_flight_pay_envelope_reports_the_shape_it_got,
               test_fixture_matches_capture):
        fn()
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("ok — travel booking bodies match the captured app traffic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
