"""What actually goes on the wire.

Every other test stubs `_call_read`, so the layer that builds the request — the one
that has caused the most production bugs in this repo — was pinned by nothing:

  b67cf9a  X-App-* headers injected on every host broke the lifestyle grocery cart
  33da29e  isSuspicious baked into the operations template made list_operations
           return nothing (283 operations without it, 0 with it)
  a8c6519  the full mobile client context was missing, causing a class of 400s
  cc54a62  payment_commission posted JSON to a form-urlencoded endpoint → 400
  8a9f90f  prefill/profile spells the session key `sessionId`, not `sessionid`
  18f60fe  id.t-bank-app.ru OIDC needs client_id and rejects the mobile-BFF params

These drive the real transport against a fake HTTP session and assert on the URL,
query and headers it produced.

    python3 tests/test_transport.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.client import MobileSession, _STRICT_XAPP_HOSTS  # noqa: E402
from src.endpoints import BUILTIN_ENDPOINTS  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.content = b"%PDF-1.4 fake"
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeHTTP:
    """Records the request instead of sending it."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"resultCode": "OK", "payload": []}
        self.sent = []
        self.headers = {}

    def _record(self, method, url, **kw):
        self.sent.append({"method": method, "url": url, "params": kw.get("params") or {},
                          "headers": kw.get("headers") or {}, "json": kw.get("json"),
                          "data": kw.get("data")})
        return FakeResponse(self.payload)

    def get(self, url, **kw):
        return self._record("GET", url, **kw)

    def post(self, url, **kw):
        return self._record("POST", url, **kw)

    def put(self, url, **kw):
        return self._record("PUT", url, **kw)


def session(payload=None):
    s = MobileSession.__new__(MobileSession)
    s.mobile_sessionid = "sid.authenticon-test"
    s.access_token = "tok"
    s.device_id = "00000000-1111-2222-3333-444444444444"
    s.old_device_id = "0123456789abcdef"
    s.cookie_str = "SSO_SESSION=x"
    s.tmsg_session_id = "jwt"
    s.app_name, s.app_version = "mobile", "7.31.6"
    s.origin = "mobile,ib5,loyalty,platform"
    s.platform, s.ccc, s.cpswc = "ios", "true", "true"
    s.connection_type, s.vendor = "WiFi", "t_ios"
    s.client_version, s.inache = "112.0.0", "drivetransitt"
    s.base_url = "https://api.t-bank-app.ru"
    s._memo = {}
    s._http = FakeHTTP(payload)
    return s


def last(s):
    return s._http.sent[-1]


def test_operations_never_asks_for_suspicious_only():
    """33da29e: isSuspicious is a per-operation FIELD, not a client flag. Sent as a
    query param it narrows the result to fraud-flagged operations — 283 → 0."""
    s = session({"resultCode": "OK", "payload": [
        {"id": 1, "account": "1111111111"},
        {"id": 2, "account": "9999999999"},
    ]})
    ops = s.list_operations("1111111111", 0, 1)
    q = last(s)["params"]
    check("isSuspicious" not in q,
          f"isSuspicious is back in the operations query: {q.get('isSuspicious')!r}")
    # The app does NOT scope /v1/operations server-side — it fetches everything and
    # filters on the operation's own `account`. Asserting a server-side filter here
    # would be asserting a divergence from the app.
    check("account" not in q,
          f"/v1/operations must not be scoped server-side (the app does not): {sorted(q)}")
    check([o["id"] for o in ops] == [1],
          f"the client-side account filter must still work, got {[o.get('id') for o in ops]}")

    # And no template may reintroduce it.
    offenders = [k for k, t in BUILTIN_ENDPOINTS.items()
                 if "isSuspicious" in (t.get("params") or {})]
    check(not offenders, f"templates carrying isSuspicious again: {offenders}")
    print("  operations: no isSuspicious in the query or in any template")


def test_x_app_headers_only_where_the_app_sends_them():
    """b67cf9a: injecting X-App-* everywhere broke the grocery cart on lifestyle."""
    s = session({"resultCode": "OK", "payload": {"cart": {"goods": []}}})
    s.grocery_cart_get(app_id="204", point_id="5980")
    h = last(s)["headers"]
    check("X-App-Name" not in h,
          f"X-App-* sent to lifestyle, which is what broke the cart: {sorted(h)}")
    check(h.get("X-Lang") == "ru", f"the always-on mobile headers must still be there: {h}")
    check(h.get("Authorization") == "Bearer tok", "the Bearer must be set")

    # A host that DOES want them must still get them.
    strict = sorted(_STRICT_XAPP_HOSTS)[0]
    key = next((k for k, t in BUILTIN_ENDPOINTS.items()
                if strict in (t.get("host") or "")), None)
    check(key is not None, f"no template uses the strict host {strict}")
    if key:
        s2 = session()
        s2._call_read(key)
        h2 = last(s2)["headers"]
        check("X-App-Name" in h2,
              f"{strict} expects X-App-* and did not get them: {sorted(h2)}")
    print(f"  X-App-*: absent on lifestyle, present on {strict}")


def test_the_session_key_spelling_is_per_endpoint():
    """8a9f90f: prefill/profile rejects the lowercase `sessionid`."""
    s = session({"contacts": [{"id": "c-1"}]})
    s.prefill_contact_id()
    q = last(s)["params"]
    check(q.get("sessionId") == "sid.authenticon-test",
          f"prefill/profile needs sessionId (capital I): {sorted(q)}")
    check("sessionid" not in q,
          "sending both spellings is not what the app does")

    s2 = session()
    s2.list_accounts()
    q2 = last(s2)["params"]
    check(q2.get("sessionid") == "sid.authenticon-test",
          f"the mobile BFF needs the lowercase sessionid: {sorted(q2)}")
    print("  session key: sessionId for prefill, sessionid for the mobile BFF")


def test_every_read_carries_the_mobile_client_context():
    """a8c6519: templates with sparse params must still send the full context."""
    sparse = [k for k, t in BUILTIN_ENDPOINTS.items()
              if t.get("method", "GET").upper() == "GET"
              and len(t.get("params") or {}) <= 2
              and not t.get("session_param")]
    check(sparse, "expected at least one sparse template to exercise")
    for key in sparse[:5]:
        s = session()
        try:
            s._call_read(key)
        except Exception as e:                                   # noqa: BLE001
            failures.append(f"{key}: transport raised {type(e).__name__}: {e}")
            continue
        q = last(s)["params"]
        for required in ("appName", "appVersion", "origin", "platform",
                         "inache", "deviceId", "oldDeviceId"):
            check(required in q, f"{key}: missing {required} from the query: {sorted(q)}")
    print(f"  client context: {len(sparse[:5])} sparse templates all carry it")


def test_form_endpoints_post_a_form_not_json():
    """cc54a62: payment_commission is x-www-form-urlencoded; JSON there → 400."""
    s = session({"resultCode": "OK", "payload": {"commission": 0}})
    s.payment_commission({"payParameters": {"account": "1", "moneyAmount": 10}})
    sent = last(s)
    check(sent["json"] is None, "a form endpoint must not be sent as JSON")
    check(isinstance(sent["data"], dict), f"expected form data, got {sent['data']!r}")
    check("payParameters" in (sent["data"] or {}),
          f"payParameters must be a form field: {sent['data']}")
    value = (sent["data"] or {}).get("payParameters")
    check(isinstance(value, str), "a dict field must be JSON-encoded into the form")
    if isinstance(value, str):
        check(json.loads(value)["account"] == "1", "the encoded field must round-trip")
    print("  form endpoints: posted as a form with JSON-encoded fields")


def test_raw_endpoints_return_bytes_not_parsed_json():
    s = session()
    pdf = s.payment_receipt_pdf("123")
    check(isinstance(pdf, bytes), f"a raw endpoint must return bytes, got {type(pdf).__name__}")
    check(pdf.startswith(b"%PDF"), "the body must come back untouched")
    h = last(s)["headers"]
    check("text/html" in (h.get("Accept") or ""),
          f"the receipt endpoint picks its serializer from Accept: {h.get('Accept')!r}")
    print("  raw endpoints: bytes returned, browser-ish Accept preserved")


def test_messenger_uses_its_own_cookie_and_vendor_accept():
    s = session({"unreadCount": 2})
    s.messenger_unread()
    sent = last(s)
    check("tmsgSessionID=jwt" in (sent["headers"].get("Cookie") or ""),
          f"messenger must use the tmsg cookie: {sent['headers'].get('Cookie')!r}")
    check("SSO_SESSION" not in (sent["headers"].get("Cookie") or ""),
          "the SSO cookie must NOT be sent to the messenger host")
    check("vnd.chats" in (sent["headers"].get("Accept") or ""),
          f"unread needs its vendor Accept or answers 406: {sent['headers'].get('Accept')!r}")
    print("  messenger: tmsg cookie only, vendor Accept for unread")


def test_templates_stay_structurally_sane():
    """Guards the shape of BUILTIN_ENDPOINTS itself, which no test read before."""
    for key, tpl in BUILTIN_ENDPOINTS.items():
        check(isinstance(tpl.get("path"), str) and tpl["path"].startswith("/"),
              f"{key}: path must be an absolute path, got {tpl.get('path')!r}")
        host = tpl.get("host")
        check(host is None or host.startswith("https://"),
              f"{key}: host must be https, got {host!r}")
        check((tpl.get("method") or "GET").upper() in ("GET", "POST", "PUT"),
              f"{key}: unexpected method {tpl.get('method')!r}")
        for live in ("sessionid", "sessionId", "Authorization", "Cookie"):
            check(live not in (tpl.get("params") or {}),
                  f"{key}: live credential {live} baked into the template params")
            check(live.lower() not in {h.lower() for h in (tpl.get("headers") or {})},
                  f"{key}: live credential {live} baked into the template headers")
    print(f"  templates: {len(BUILTIN_ENDPOINTS)} shapes structurally valid, no baked secrets")


def test_bank_documents_asks_for_the_v2_record_shape():
    """Without X-Api-Version: v2 the endpoint answers in the v1 form, whose ids are
    negative ints in tecmId instead of the uuid in tecmUuid — which is why the tool
    used to print ids nothing else accepts (captures2.xml #44)."""
    s = session({"documents": []})
    s.bank_documents()
    h = last(s)["headers"]
    check(h.get("X-Api-Version") == "v2",
          f"the v2 record shape must be requested explicitly: {sorted(h)}")
    check("X-App-Name" in h,
          f"the app sends X-App-* to cx-evolution-api too: {sorted(h)}")
    print("  bank_documents: X-Api-Version v2 + X-App-* as the app sends them")


def main():
    print("transport:")
    test_operations_never_asks_for_suspicious_only()
    test_x_app_headers_only_where_the_app_sends_them()
    test_the_session_key_spelling_is_per_endpoint()
    test_every_read_carries_the_mobile_client_context()
    test_form_endpoints_post_a_form_not_json()
    test_raw_endpoints_return_bytes_not_parsed_json()
    test_messenger_uses_its_own_cookie_and_vendor_accept()
    test_bank_documents_asks_for_the_v2_record_shape()
    test_templates_stay_structurally_sane()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
