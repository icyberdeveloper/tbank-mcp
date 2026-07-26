"""grocery_order_cancel must send the app's request, not the ticket flavour's.

The two flavours share one path, /api/order/cancel, and differ in everything
else. Tickets: orderId AND paymentId in the query, or the host answers 200
"Success" while cancelling nothing. Grocery (cancel-grossary.xml): ONLY orderId,
an EMPTY body, Content-Type: application/json — and the verdict lives in
payload.{status,code}, not in the outer envelope, which says "Ok" even when
nothing was cancelled (payload {"status":"Failed","code":"605"} = already
cancelled; the "Success" payload shape was observed live on a real cancellation
of a paid ВкусВилл order, 2026-07-26).

This runs the REAL client against a recorded transport and pins both the request
bytes and the envelope parsing; when the capture is present the fixture is also
diffed against it, so it cannot drift from what the app really sends.

    python3 tests/test_grocery_cancel.py
"""
import base64
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TBANK_EVENTS",
                      os.path.join(tempfile.mkdtemp(), "events.jsonl"))
os.environ.setdefault("TBANK_ATTEMPTS",
                      os.path.join(tempfile.gettempdir(), "tbank-test-attempts.jsonl"))

from src.client import MobileSession  # noqa: E402

# The GROCERY cancel capture. The ticket one lives under
# TBANK_TICKET_CANCEL_CAPTURE — see tests/test_booking_and_ranking.py.
CAPTURE = os.environ.get("TBANK_GROCERY_CANCEL_CAPTURE",
                         os.environ.get("TBANK_CAPTURE_CANCEL",
                                        os.path.expanduser("~/tbank-app/cancel-grossary.xml")))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "grocery_cancel.json")


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class Recorder:
    """Stands in for session._http: records the one POST and answers with a
    canned envelope."""

    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = []

    def post(self, url, *, params=None, json=None, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "json": json,
                           "data": data, "headers": headers or {}})
        envelope = self.envelope

        class Resp:
            status_code = 200
            text = ""

            def json(self):
                return envelope

            def raise_for_status(self):
                pass

        return Resp()


def make_session(envelope):
    s = MobileSession(mobile_sessionid="sid", refresh_token="rt", access_token="tok",
                      device_id="DEV-1", app_version="7.31.6", platform="ios",
                      app_name="mobile")
    s._http = Recorder(envelope)
    return s


def check_request_matches_capture_shape(fx, failures):
    s = make_session(fx["response_failed"])
    s.cancel_grocery_order("400000000001")
    call = s._http.calls[-1]

    if not call["url"].endswith(fx["request"]["path"]):
        failures.append(f"path: sent {call['url']}, capture uses {fx['request']['path']}")
    if str(call["params"].get("orderId")) != "400000000001":
        failures.append(f"orderId must ride in the query: {call['params']}")
    for absent in fx["request"]["query_has_not"]:
        if absent in call["params"]:
            failures.append(f"{absent} leaked into the query — the app does not send "
                            f"it for grocery orders: {call['params']}")
    # The app posts NO body at all (Content-Length: 0) — a literal "{}" is the
    # ticket flavour, and diverging from the capture is how carts silently broke.
    if call["json"] is not None or call["data"] is not None:
        failures.append(f"body must be empty, sent json={call['json']!r} data={call['data']!r}")
    for header, want in (("Content-Type", fx["request"]["content_type"]),
                         ("Accept", fx["request"]["accept"])):
        got = call["headers"].get(header)
        if got != want:
            failures.append(f"{header}: sent {got!r}, capture sends {want!r}")
    if not call["headers"].get("Authorization", "").startswith("Bearer "):
        failures.append("Authorization: Bearer is missing")
    print(f"  request: orderId alone in the query, empty body, "
          f"Content-Type={call['headers'].get('Content-Type')}")


def check_envelope_parsing(fx, failures):
    # The refused cancellation: outer "Ok", the verdict inside. _unwrap must hand
    # the payload back, not mistake the envelope for an error or for success.
    s = make_session(fx["response_failed"])
    got = s.cancel_grocery_order("400000000001")
    if got.get("status") != "Failed" or got.get("code") != "605":
        failures.append(f"refused cancel must surface payload.status/code, got {got!r}")

    s2 = make_session(fx["response_success"])
    got2 = s2.cancel_grocery_order("400000000001")
    if got2.get("status") != "Success":
        failures.append(f"accepted cancel must read payload.status=Success, got {got2!r}")
    print(f"  envelope: Failed/605 and Success both parsed from payload")


def check_fixture_still_matches_capture(fx, failures):
    """Only runs where the real capture lives — pins the fixture to the app's bytes."""
    with open(CAPTURE, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    items = re.findall(r"<item>(.*?)</item>", text, re.S)

    def raw(item, tag):
        m = re.search(r"<%s( [^>]*)?>(.*?)</%s>" % (tag, tag), item, re.S)
        body = m.group(2).replace("<![CDATA[", "").replace("]]>", "")
        return base64.b64decode(body) if 'base64="true"' in (m.group(1) or "") else body.encode()

    cancels = [it for it in items
               if b"POST /api/order/cancel?" in raw(it, "request")]
    if not cancels:
        failures.append(f"no POST /api/order/cancel item in {CAPTURE}")
        return
    req = raw(cancels[0], "request")
    head, _, body = req.partition(b"\r\n\r\n")
    request_line = head.split(b"\r\n", 1)[0].decode()
    query = request_line.split("?", 1)[1].split(" ", 1)[0]
    keys = {p.split("=", 1)[0] for p in query.split("&")}

    for present in fx["request"]["query_has"]:
        if present not in keys:
            failures.append(f"capture drift: {present} not in the query {sorted(keys)}")
    for absent in fx["request"]["query_has_not"]:
        if absent in keys:
            failures.append(f"capture drift: the app now sends {absent} — the "
                            f"fixture (and the client) must follow")
    if body.strip():
        failures.append(f"capture drift: the app now posts a body: {body[:100]!r}")
    if b"content-type: application/json" not in head.lower():
        failures.append("capture drift: Content-Type is no longer application/json")

    resp_head, _, resp_body = raw(cancels[0], "response").partition(b"\r\n\r\n")
    envelope = json.loads(resp_body)
    if sorted(envelope.get("payload", {})) != sorted(fx["response_failed"]["payload"]):
        failures.append(f"capture drift: response payload keys "
                        f"{sorted(envelope.get('payload', {}))} vs fixture "
                        f"{sorted(fx['response_failed']['payload'])}")
    print("  fixture vs capture: query shape, empty body and envelope still match")


def main():
    failures = []
    fx = fixture()
    print("grocery order cancel vs real app:")
    check_request_matches_capture_shape(fx, failures)
    check_envelope_parsing(fx, failures)
    if os.path.exists(CAPTURE):
        check_fixture_still_matches_capture(fx, failures)
    else:
        print(f"  (capture absent at {CAPTURE} — drift check skipped; "
              f"the contract above was still verified against the fixture)")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK — the cancel request matches the capture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
