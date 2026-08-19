"""The grocery cart/set body must match what the real app sends.

Twice now the cart silently failed to save: cart/set answered HTTP 200 while the
follow-up GET read empty. Both times the cause was a body that diverged from the
app's — first a missing delivery.address (a store with no cart cannot supply one),
then a missing delivery.areaId (required by ВкусВилл/Лента, absent for Азбука).

This pins the body so a third round cannot happen silently.

The contract lives in tests/fixtures/grocery_cart.json — real structure and real
protocol values (areaId, pointId, cartSetMode, the goods shape), synthetic personal
values — so it is verified on every machine. The Burp capture is the ultimate ground
truth and is gitignored; when it IS present the fixture is additionally checked
against it, so it cannot drift away from what the app really sends.

It used to skip and `return 0` when the capture was missing, which meant a clean
clone reported a green suite having verified nothing at all.

    python3 tests/test_cart_body_matches_capture.py
"""
import base64
import gzip
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# cart/set emits observability events, and the module resolves the log path at
# import time. run_all.py redirects these; running this file DIRECTLY did not, so
# a test run appended to the user's real ~/.local/share/tbank-mcp/events.jsonl.
os.environ.setdefault("TBANK_EVENTS",
                      os.path.join(tempfile.mkdtemp(), "events.jsonl"))
os.environ.setdefault("TBANK_ATTEMPTS",
                      os.path.join(tempfile.gettempdir(), "tbank-test-attempts.jsonl"))
os.environ.setdefault("TBANK_TRACE_FILE",
                      os.path.join(tempfile.gettempdir(), "tbank-test-calls.jsonl"))

from src.client import MobileSession, TbankApiError  # noqa: E402

CAPTURE = os.environ.get("TBANK_CAPTURE", os.path.expanduser("~/tbank-app/captures.xml"))

# capture item indices → the request they hold
AZBUKA_CART_SET = 314    # appId=578 pointId=2   — no areaId
VKUSVILL_CART_SET = 708  # appId=204 pointId=5980 — areaId=17040911
CART_GET = 370           # a populated Azbuka cart
CLIENT_INFO = 275        # payload.deliveryInfo.address — the cold-start address seed
RETAILERS = 276          # the only source of areaId
ERROR_ENVELOPE = 1120    # HTTP 200 + status:"Error"

# The cartSetMode escalation lives in the OTHER capture: the app posts a cart, is
# refused with app code 268, and resends the byte-identical body with the reset mode.
CAPTURE2 = os.environ.get("TBANK_CAPTURE2",
                          os.path.expanduser("~/tbank-app/captures2.xml"))
CART_SET_REFUSED = 1073   # captures2: appId=695, SINGLE_CART → 268
CART_SET_ACCEPTED = 1077  # captures2: appId=695, same goods, reset mode → goodsSum


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


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "grocery_cart.json")


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class ReplaySession(MobileSession):
    """A session whose reads are answered from the fixture instead of the network."""

    def __init__(self, fx, store_has_cart):
        self.fx = fx
        self.store_has_cart = store_has_cart
        self.sent_body = None
        self.sent_overrides = None

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        if key == "grocery_cart_get":
            if not self.store_has_cart:
                return {"cart": {"goods": [], "sum": 0}}
            return self.fx["cart_get"]
        if key == "grocery_client_info":
            return self.fx["client_info"]
        if key == "grocery_cart_set":
            self.sent_body = body
            self.sent_overrides = overrides or {}
            return {"goodsSum": 1.0}
        raise AssertionError("unexpected read: " + key)

    def grocery_stores(self):
        out = []
        for cat in self.fx["retailers"].get("categories", []):
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


def check_store(fx, label, app_id, point_id, expected_key, store_has_cart, failures):
    session = ReplaySession(fx, store_has_cart)
    session.grocery_add_to_cart([{"id": "1087", "count": 1}], app_id=app_id, point_id=point_id)
    got, real = session.sent_body, fx[expected_key]

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


def check_merge(fx, failures):
    """cart/set replaces the whole cart, so an add must resend the existing goods."""
    session = ReplaySession(fx, store_has_cart=True)
    session.grocery_add_to_cart([{"id": "999999", "count": 2}], app_id="578", point_id="2")
    sent = {g["id"]: g["count"] for g in session.sent_body["goods"]}
    for existing in fx["cart_get"]["cart"]["goods"]:
        if str(existing["id"]) not in sent:
            failures.append(f"merge: existing good {existing['id']} was dropped from the cart")
    if sent.get("999999") != 2:
        failures.append(f"merge: the new good is missing or miscounted: {sent.get('999999')!r}")
    print(f"  merge: cart had {len(sent) - 1} goods, resending {len(sent)}")


def check_error_envelope(fx, failures):
    """HTTP 200 + status:"Error" must raise, not return the error body as success."""
    envelope = fx["error_envelope"]

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


def check_fixture_still_matches_capture(fx, failures):
    """Only runs where the real capture lives. Guards the fixture against drifting
    away from what the app actually sends — the scrubbed values may differ, but the
    key structure and the protocol values must not."""
    items = _items()
    for label, key, idx in (("azbuka", "expected_azbuka", AZBUKA_CART_SET),
                            ("vkusvill", "expected_vkusvill", VKUSVILL_CART_SET)):
        real = request_json(items, idx)
        mine = fx[key]
        if sorted(mine) != sorted(real):
            failures.append(f"fixture {label}: top-level keys drifted from the capture "
                            f"— fixture={sorted(mine)} capture={sorted(real)}")
            continue
        if sorted(mine["delivery"]) != sorted(real["delivery"]):
            failures.append(f"fixture {label}: delivery keys drifted "
                            f"— fixture={sorted(mine['delivery'])} capture={sorted(real['delivery'])}")
        for field in ("areaId", "pointId", "deliveryType", "isExpress"):
            if str(mine["delivery"].get(field)) != str(real["delivery"].get(field)):
                failures.append(f"fixture {label}: delivery.{field} drifted "
                                f"— fixture={mine['delivery'].get(field)!r} "
                                f"capture={real['delivery'].get(field)!r}")
        if mine.get("cartSetMode") != real.get("cartSetMode"):
            failures.append(f"fixture {label}: cartSetMode drifted")
    print("  fixture vs capture: structure and protocol values still match")


class EscalationSession(ReplaySession):
    """Refuses the narrow cartSetMode the way the backend does, then records what
    the client resends."""

    def __init__(self, fx, refuse_code="268", refuse_modes=("SINGLE_CART",)):
        super().__init__(fx, store_has_cart=False)
        self.refuse_code = refuse_code
        self.refuse_modes = refuse_modes
        self.modes = []

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        if key != "grocery_cart_set":
            return super()._call_read(key, overrides=overrides, body=body,
                                      path_override=path_override)
        mode = (body or {}).get("cartSetMode")
        self.modes.append(mode)
        self.sent_body, self.sent_overrides = body, overrides or {}
        if mode in self.refuse_modes:
            raise TbankApiError(self.refuse_code,
                                "Сервис временно недоступен. Попробуйте позже.")
        return {"goodsSum": 139.0}


def check_cart_set_escalation(fx, failures):
    """«268 Сервис временно недоступен» on cart/set does NOT mean the service is
    down — it means another retailer holds a cart. The app answers by resending
    the same body with the reset mode, and so must we, or every add-to-cart fails
    for good while the app keeps working."""
    esc = fx["cart_set_escalation"]

    # Refused once → retried with the reset mode → reported as a reset.
    s = EscalationSession(fx)
    try:
        res = s.grocery_add_to_cart([{"id": "16072", "count": 1}],
                                    app_id="204", point_id="5980")
    except TbankApiError as e:
        failures.append(f"escalation: {esc['refused_code']} was not retried with "
                        f"{esc['accepted_mode']} — every add-to-cart fails while the "
                        f"app keeps working ({e})")
        return
    if s.modes != [esc["refused_mode"], esc["accepted_mode"]]:
        failures.append(f"escalation: modes sent {s.modes}, expected "
                        f"{[esc['refused_mode'], esc['accepted_mode']]}")
    if not res.get("otherCartsReset"):
        failures.append("escalation: wiping another retailer's cart must be flagged "
                        f"back to the caller, got {res!r}")

    # A DIFFERENT app code is not this situation — it must surface, not be retried.
    other = EscalationSession(fx, refuse_code="211", refuse_modes=("SINGLE_CART",))
    try:
        other.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="204", point_id="5980")
        failures.append("escalation: a non-268 error was swallowed and retried")
    except TbankApiError:
        if len(other.modes) != 1:
            failures.append(f"escalation: a non-268 error must not retry, sent {other.modes}")

    # Clearing a cart posts no goods and is accepted in the narrow mode — escalating
    # there would reset other carts as a side effect of emptying this one.
    clearing = EscalationSession(fx, refuse_modes=("SINGLE_CART", esc["accepted_mode"]))
    try:
        clearing.grocery_set_cart([], app_id="204", point_id="5980", clear=True)
        failures.append("escalation: the stub refused everything, this must raise")
    except TbankApiError:
        if len(clearing.modes) != 1:
            failures.append(f"escalation: an empty cart write must not escalate, "
                            f"sent {clearing.modes}")
    print(f"  cart/set escalation: {esc['refused_mode']} → {esc['refused_code']} → "
          f"{esc['accepted_mode']}, only on 268 and only with goods")


def check_escalation_still_matches_capture(fx, failures):
    """The escalation is only real if the two captured bodies differ in nothing but
    cartSetMode — otherwise 268 meant something else and the retry is a guess."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "fixtures"))
    import regen  # noqa: E402

    try:
        fresh = regen.build_cart_set_escalation()
    except Exception as e:  # noqa: BLE001
        failures.append(f"cart_set_escalation can no longer be rebuilt: "
                        f"{type(e).__name__}: {e}")
        return
    if fresh != fx["cart_set_escalation"]:
        failures.append(f"cart_set_escalation drifted — fixture="
                        f"{fx['cart_set_escalation']!r} capture={fresh!r}")
        return
    if fresh["differing_keys"] != ["cartSetMode"]:
        failures.append(f"the refused and accepted bodies differ in more than "
                        f"cartSetMode: {fresh['differing_keys']} — the retry is "
                        f"no longer justified by the capture")
    print("  escalation vs capture: the two bodies still differ in cartSetMode alone")


def main():
    failures = []
    fx = fixture()
    print("cart/set body vs real app:")
    check_store(fx, "ВкусВилл 204/5980 cold start", "204", "5980", "expected_vkusvill", False, failures)
    check_store(fx, "Азбука 578/2 existing cart", "578", "2", "expected_azbuka", True, failures)
    check_merge(fx, failures)
    check_error_envelope(fx, failures)
    check_cart_set_escalation(fx, failures)
    if os.path.exists(CAPTURE2):
        check_escalation_still_matches_capture(fx, failures)
    else:
        print(f"  (captures2 absent at {CAPTURE2} — escalation drift check skipped;\n"
              f"   the behaviour above was still verified against the fixture)")
    if os.path.exists(CAPTURE):
        check_fixture_still_matches_capture(fx, failures)
    else:
        print(f"  (capture absent at {CAPTURE} — fixture-vs-capture drift check skipped;\n"
              f"   the contract above was still verified against the fixture)")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK — the body matches the capture for both retailer shapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
