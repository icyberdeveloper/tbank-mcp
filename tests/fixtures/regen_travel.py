"""Regenerate tests/fixtures/travel.json from the flight+rail Burp capture.

The capture is the user's real traffic and is gitignored; the fixture is what the
test suite actually runs against, so the fixture must carry the real STRUCTURE and
the real protocol values (station codes, carSearchId, offer uuids, place types,
service classes) with every personal value replaced.

Why this file exists separately from regen.py: that one scrubs by KEY NAME, and
these two bodies defeat that completely. A passport number here is `document.number`
— and `number` is also a car number, a place number, a train number and a flight
number, every one of them protocol. A name is `name.first`, and `name` is also a
train name and a hotel name. So the scrubbing here is BY PATH: each personal field
is named explicitly, and anything not named is left alone.

    python3 tests/fixtures/regen_travel.py [path/to/captures-flight-train.xml]
"""
import base64
import gzip
import json
import os
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "travel.json")
DEFAULT_CAPTURE = os.path.expanduser("~/tbank-app/captures-flight-train.xml")

# Capture item indices (0-based, in file order). Each is named by what it holds so
# a re-export that shifts them can be re-found rather than silently mismatched.
ITEMS = {
    "train_search_response": 1447,      # POST /api/search/trains
    "train_cars_response": 1463,        # POST /api/search/train/cars
    "train_order_create_request": 1507,  # POST /api/orders/create  ← the reference body
    "train_order_response": 1734,       # GET  /api/orders/{id}
    "train_refund_calc_request": 1893,  # POST /api/orders/refund/calculate
    "train_refund_request": 1898,       # POST /api/orders/refund
    "train_blank_status_response": 1820,  # POST /api/orders/{id}/blank-status
    "flight_preliminary_response": 1006,  # POST .../booking/v2/preliminary
    "flight_seatmaps_response": 1022,   # GET  .../getSeatMaps  (booked offer)
    "flight_checkin_response": 1025,    # POST /api/travel/checkin/calcPrice
    "flight_pay_request": 1109,         # POST /api/prefill/proxy/travel_pay ← reference
    "flight_pay_result_response": 1225,  # GET .../booking/pay/result (status Ok)
    "tpay_init_request": 1534,          # POST /api/v2/tpayid/session INIT_TPW
    "tpay_status_response": 1537,       # GET  /api/v2/tpayid/status
}

# Synthetic replacements. Round, obviously fake, and SHAPED like the real thing so
# nothing that parses a length or a format starts failing on the fixture.
FAKE = {
    "phone": "+79991234567",
    "email": "user@example.com",
    "first": "Иван", "last": "Петров", "middle": "Сергеевич",
    "firstEn": "IVAN", "lastEn": "PETROV", "middleEn": "SERGEEVICH",
    "birth": "1990-01-31",
    "passport": ["1234567890", "1234567891", "1234567892", "1234567893"],
    "bonus_card": "1000000001",
    "pnr": "AAAAAA",
}

# Personal fields BY PATH. A path is a tuple of keys; a list on the way is walked
# for every element. `_passport_seq` hands out a different number per passenger so
# a test can still tell two passengers apart.
RAIL_PERSON = ("ways", "*", "*", "placeGroups", "*", "passengers", "*",
               "documentWithCustomerIndex", "document")
AVIA_PERSON = ("booking", "persons", "*")
AVIA_SEAT_PERSON = ("seats", "*", "passenger")


def _walk_set(node, path, fn):
    """Apply fn to every dict reached by `path` ('*' descends into a list)."""
    if not path:
        if isinstance(node, dict):
            fn(node)
        return
    head, rest = path[0], path[1:]
    if head == "*":
        if isinstance(node, list):
            for item in node:
                _walk_set(item, rest, fn)
        return
    if isinstance(node, dict) and head in node:
        _walk_set(node[head], rest, fn)


class Passports:
    def __init__(self):
        self.n = 0

    def next(self):
        v = FAKE["passport"][self.n % len(FAKE["passport"])]
        self.n += 1
        return v


def scrub_rail_person(doc, seq):
    name = doc.get("name")
    if isinstance(name, dict):
        name["first"] = FAKE["first"]
        name["last"] = FAKE["last"]
        if name.get("middle"):
            name["middle"] = FAKE["middle"]
    if "number" in doc:
        doc["number"] = seq.next()
    if "birthDate" in doc:
        doc["birthDate"] = FAKE["birth"]


def scrub_avia_person(p, seq):
    p["name"] = FAKE["firstEn"]
    p["surname"] = FAKE["lastEn"]
    if p.get("middle_name"):
        p["middle_name"] = FAKE["middleEn"]
    if p.get("birthdate"):
        p["birthdate"] = FAKE["birth"]
    td = p.get("travel_document")
    if isinstance(td, dict) and "number" in td:
        td["number"] = seq.next()
    bc = p.get("bonus_card")
    if isinstance(bc, dict) and bc.get("number"):
        bc["number"] = FAKE["bonus_card"]


def scrub_seat_person(p, seq):
    p["firstName"] = FAKE["firstEn"]
    p["lastName"] = FAKE["lastEn"]
    if "documentNumber" in p:
        p["documentNumber"] = seq.next()


def scrub_contacts(node):
    """phone/email wherever they appear — these two ARE safe to match by name."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                if k in ("phone", "Phone"):
                    node[k] = FAKE["phone"]
                elif k in ("email", "Email"):
                    node[k] = FAKE["email"]
            else:
                scrub_contacts(v)
    elif isinstance(node, list):
        for v in node:
            scrub_contacts(v)


# Sub-second precision on a booking timestamp says when the user was at their
# phone, to the microsecond. Nothing here needs it, and it also trips the
# coordinate shape in tests/test_no_personal_data.py: two digits, a dot and six
# more reads exactly like a latitude. Trimmed to whole seconds; type and format stay.
_FRACTION_RE = re.compile(r"(\d{2}:\d{2}:\d{2})\.\d+")


def scrub_times(node):
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str):
                node[k] = _FRACTION_RE.sub(r"\1", v)
            else:
                scrub_times(v)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = _FRACTION_RE.sub(r"\1", v)
            else:
                scrub_times(v)


def scrub_pnr(node):
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str):
                if k in ("bookingNumber", "booking_number"):
                    node[k] = FAKE["pnr"]
                elif k == "document_name":
                    node[k] = re.sub(r"[A-Z0-9]{6}\.pdf$", FAKE["pnr"] + ".pdf", v)
            else:
                scrub_pnr(v)
    elif isinstance(node, list):
        for v in node:
            scrub_pnr(v)


# Identifiers that are not «personal» but are still the user's: their account
# number, the order ids, and the railway's own blank/reservation numbers printed on
# a real ticket. The repo's rule is that nothing real from a capture reaches a
# tracked file, so these are renumbered too — consistently, so that a test can
# still check that carSearchId from one record turns up in another.
_ID_KEYS = {"externalId", "externalOrderItemId", "externalReservationNumber",
            "externalBlankId", "externalOrderCustomerId", "paymentId",
            "productRequestId"}


class Ids:
    """Consistent, referential renumbering: same input → same output, always fake."""

    def __init__(self):
        self.uuids, self.nums = {}, {}

    def uuid(self, value):
        # The last group is HEX, so the counter is written with an 'a' in it. A
        # plain 12-digit tail is the shape of an ИНН, and every one of these would
        # then have to be declared in tests/test_no_personal_data.py — dozens of
        # entries that say nothing, burying the few that matter.
        if value not in self.uuids:
            n = len(self.uuids) + 1
            self.uuids[value] = f"00000000-0000-4000-8000-00000000a{n:03x}"
        return self.uuids[value]

    def num(self, value):
        # Nine digits, not twelve: the same reason as above, one shape instead of
        # two. These stand in for the railway's blank and reservation numbers.
        if value not in self.nums:
            self.nums[value] = str(300000001 + len(self.nums))
        return self.nums[value]


# Not anchored: an order id also rides INSIDE the payment gateway's return and
# fail URLs, so matching only whole-string values left the real one in the fixture.
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _sub_uuids(text, ids):
    return _UUID_RE.sub(lambda m: ids.uuid(m.group(0)), text)


def scrub_ids(node, ids):
    """Renumber ids in place. Handles ints as well as strings: the rail order card
    carries externalOrderItemId and externalOrderCustomerId as JSON NUMBERS, and a
    string-only sweep left both real."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str):
                if k == "account" and v.isdigit():
                    node[k] = "0000000000"
                elif k in _ID_KEYS and v.isdigit():
                    node[k] = ids.num(v)
                else:
                    node[k] = _sub_uuids(v, ids)
            elif isinstance(v, bool):
                continue                      # bool is an int — leave flags alone
            elif isinstance(v, int) and k in _ID_KEYS:
                node[k] = int(ids.num(str(v)))
            elif isinstance(v, (dict, list)):
                scrub_ids(v, ids)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = _sub_uuids(v, ids)
            else:
                scrub_ids(v, ids)


# One instance for the whole regeneration so an id means the same thing in every
# record — the create request's carSearchId must still match the cars response.
IDS = Ids()


def scrub(name, data):
    seq = Passports()
    if name == "train_order_create_request":
        _walk_set(data, RAIL_PERSON, lambda d: scrub_rail_person(d, seq))
    if name == "train_order_response":
        # Scrub the VALUES, keep the structure. Blanking the whole `document`
        # subtree also erased its shape, and the shape is exactly what the
        # capture-drift check compares — a scrub that changes the contract is
        # indistinguishable from the API changing it.
        _walk_set(data, ("ways", "*", "segments", "*", "orderItems", "*",
                         "tickets", "*", "passengers", "*", "document"),
                  lambda d: scrub_rail_person(d, seq))
    if name == "flight_pay_request":
        _walk_set(data, AVIA_PERSON, lambda p: scrub_avia_person(p, seq))
        seat_seq = Passports()
        _walk_set(data, AVIA_SEAT_PERSON, lambda p: scrub_seat_person(p, seat_seq))
    scrub_contacts(data)
    scrub_pnr(data)
    scrub_times(data)
    scrub_ids(data, IDS)
    return data


# ---- capture reading (same decoding as tests/test_travel_booking_bodies.py) ----

def items(path):
    with open(path, "rb") as fh:
        return re.findall(r"<item>(.*?)</item>",
                          fh.read().decode("utf-8", "replace"), re.S)


def raw(item, tag):
    m = re.search(r"<%s( [^>]*)?>(.*?)</%s>" % (tag, tag), item, re.S)
    body = m.group(2).replace("<![CDATA[", "").replace("]]>", "")
    return base64.b64decode(body) if 'base64="true"' in (m.group(1) or "") else body.encode()


def body_of(blob):
    head, _, body = blob.partition(b"\r\n\r\n")
    low = head.lower()
    if b"transfer-encoding: chunked" in low:
        out, i = bytearray(), 0
        while i < len(body):
            j = body.find(b"\r\n", i)
            if j < 0:
                break
            try:
                n = int(body[i:j].split(b";")[0], 16)
            except ValueError:
                break
            if n == 0:
                break
            out += body[j + 2:j + 2 + n]
            i = j + 2 + n + 2
        body = bytes(out)
    if b"content-encoding: gzip" in low:
        try:
            body = gzip.decompress(body)
        except Exception:
            body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body)
    elif b"content-encoding: br" in low:
        import brotli
        body = brotli.decompress(body)
    return body


def main():
    capture = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CAPTURE
    if not os.path.exists(capture):
        raise SystemExit(f"capture not found: {capture}")
    parsed = items(capture)
    out = {}
    for name, index in ITEMS.items():
        tag = "request" if name.endswith("_request") else "response"
        data = json.loads(body_of(raw(parsed[index], tag)))
        out[name] = scrub(name, data)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes, {len(out)} records)")


if __name__ == "__main__":
    main()
