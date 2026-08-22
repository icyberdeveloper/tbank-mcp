"""The opaque travel handles must survive the round trip, and fail loudly.

Rail ordering needs six values threaded across three calls, two of which
(`trainSearchId` and `carSearchId`) look alike and come from different responses.
Rather than print all six and hope the agent keeps them straight, each step hands
out ONE token that carries its own tuple.

That trade buys correctness at the search step and moves the risk here: a token is
opaque, so when one is wrong the agent cannot read it to find out why. Two things
therefore have to hold, and neither is visible by inspection —

  * decode(encode(x)) == x, for values that really occur: Cyrillic train numbers
    like «083Й», leading-zero car numbers, timestamps with an offset;
  * a token of the WRONG KIND or a corrupted one raises an error that NAMES the
    tool which mints a new one. Without that the agent has no next move.

    python3 tests/test_travel_ids.py
"""
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
from src.client import TbankApiError, pack_ref, unpack_ref  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def test_round_trip_real_values():
    """Values that actually occur, not tidy ASCII ones."""
    cases = [
        {"so": "2000000", "sd": "2040000", "date": "2026-09-15",
         "n": "083Й", "dep": "2026-09-15T06:05:00+03:00"},
        {"so": "2000005", "sd": "2004000", "date": "2026-12-31",
         "n": "002А", "dep": "2026-12-31T23:59:00+03:00"},
        {"car": "03", "place": "010", "type": "SideUpperWithHigherLevelOfNoise"},
    ]
    for payload in cases:
        token = pack_ref("train", payload)
        back = unpack_ref("train", token)
        check(back == payload, f"round trip lost data: {payload} -> {back}")
        check(" " not in token and "\n" not in token,
              f"token must survive being pasted as an argument: {token!r}")


def test_wrong_kind_is_refused():
    """A seat handle spent where a train handle is expected must not decode."""
    seat = pack_ref("seat", {"car": "03", "place": "10"})
    try:
        unpack_ref("train", seat)
    except TbankApiError as e:
        check("train" in str(e),
              f"wrong-kind error must name the expected kind, got: {e}")
    else:
        failures.append("wrong-kind: a seat token decoded as a train token")


def test_corrupt_token_names_the_remedy():
    """A token is opaque, so «получи новый» alone leaves the agent nowhere — the
    error must NAME the tool that re-mints a train handle (train_search), on every
    corruption path: wrong prefix, empty, and garbage that fails to decode."""
    for bad in ("", "train_", "train_!!!!", "не токен", "train_" + "A" * 40):
        try:
            unpack_ref("train", bad)
        except TbankApiError as e:
            check(str(e).strip() != "",
                  f"corrupt token {bad!r} produced an empty error")
            check("train_search" in str(e),
                  f"corrupt token {bad!r} must name train_search as the re-issuer: {e}")
        else:
            failures.append(f"corrupt token accepted: {bad!r}")


def test_train_ref_carries_what_resolve_needs():
    """The handle must hold exactly the fields _resolve_train reads back."""
    seg = {"number": "083Й", "departureDateTime": "2026-09-15T06:05:00+03:00",
           "origin": {"stationCode": "2000005"},
           "destination": {"stationCode": "2040000"}}
    token = server._train_ref("2000000", "2040000", "2026-09-15", seg)
    ref = unpack_ref("train", token)
    for key in ("so", "sd", "date", "n", "dep"):
        check(key in ref, f"train handle is missing {key} — _resolve_train needs it")
    check(ref["n"] == seg["number"] and ref["dep"] == seg["departureDateTime"],
          "train handle must match on the train number AND its departure: "
          "one number runs on many days")
    check(ref["so"] == "2000000",
          "train handle keeps the SEARCHED station, not the segment's — the search "
          "has to be repeatable")


def test_handle_is_not_a_snapshot():
    """It must carry no price and no availability: both go stale in minutes."""
    seg = {"number": "083Й", "departureDateTime": "2026-09-15T06:05:00+03:00",
           "origin": {"stationCode": "2000005"},
           "destination": {"stationCode": "2040000"},
           "carGroups": [{"refundablePrice": {"price": 5637.0}}]}
    ref = unpack_ref("train", server._train_ref("2000000", "2040000",
                                                "2026-09-15", seg))
    blob = str(ref)
    check("5637" not in blob and "carGroups" not in blob,
          f"handle carries stale pricing/availability: {ref}")


def test_seat_parsing():
    check(server._parse_seats("03/10, 03/12") == [("03", "10"), ("03", "12")],
          "seat list must keep order and strip spaces")
    check(server._parse_seats("3/10;4/12") == [("3", "10"), ("4", "12")],
          "semicolons separate too — agents produce both")
    for bad in ("", "03", "03-10", "/10", "03/"):
        try:
            server._parse_seats(bad)
        except TbankApiError as e:
            check("вагон/место" in str(e) or "мест" in str(e),
                  f"bad seat spec {bad!r}: unhelpful error {e}")
        else:
            failures.append(f"bad seat spec accepted: {bad!r}")


def test_place_group_type_mapping():
    """The order body names a layout; the car listing names a plural enumeration."""
    check(server._place_group_type("Compartments") == "compartment",
          "Compartments must map to the layout the order body uses")
    check(server._place_group_type("Seats") == "seat", "Seats -> seat")
    # An unknown enumeration must still produce something orderable rather than
    # raising: an unusual car should not be unbookable because of a lookup table.
    check(server._place_group_type("Gondolas") == "gondola",
          "an unmapped enumeration must fall back to the singular, not fail")
    check(server._place_group_type("") == "", "empty stays empty")


def main():
    for fn in (test_round_trip_real_values, test_wrong_kind_is_refused,
               test_corrupt_token_names_the_remedy,
               test_train_ref_carries_what_resolve_needs,
               test_handle_is_not_a_snapshot, test_seat_parsing,
               test_place_group_type_mapping):
        fn()
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("ok — travel handles round-trip and fail with a usable message")
    return 0


if __name__ == "__main__":
    sys.exit(main())
