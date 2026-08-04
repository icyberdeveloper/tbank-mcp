"""A failure must never be reported as an empty success.

This is the shape of defect the project keeps producing, because it looks like
working code from every angle except the user's: a request fails, something
swallows it, and the tool prints «ничего не найдено». The user believes the answer.
Nothing logs an error, no test goes red, and the only symptom is a person acting on
a fact that was never established — «неоплаченных счетов нет», «непрочитанных нет»,
«полисов нет», and in one case a grocery cart quietly emptied and reported OK.

Six of them, all executed here against the real code:

1. `_unwrap` never looked at the HTTP status. A 4xx/5xx whose body happened to be
   ordinary JSON was returned to the caller AS THE PAYLOAD, and ~40 read tools
   rendered it as nothing found. Both shapes below are real: tm answers 404 with
   {"errorCode":"FAQ_NOT_FOUND",…} and webview 404 with {"message":"Not Found"}.
2. ...but the status check must run AFTER the envelope check, or the OIDC token
   endpoint's HTTP 400 + {"error":"invalid_grant"} stops mapping to SessionExpired
   and silent re-login breaks on the one path that recovers a dead session.
3. An unparseable HTTP 200 on the money path is an UNKNOWN outcome, not a
   confirmed non-charge: something processed the request, and what it did is
   exactly what cannot be read.
4. grocery_add_to_cart treated a failed cart read as an empty cart, then posted a
   full replace — deleting everything already in it, and printing success.
5. messenger_unread bypassed the auth detection and coerced any non-dict answer to
   {}, so a rejected token read as «Непрочитанных сообщений нет.»
6. insurance_policies never saw its host's capital-R `ResultCode`, and turned a
   present-but-empty Policies list into one fabricated policy row.

    python3 tests/test_shadow_errors.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tbank-shadow-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

import requests                                                    # noqa: E402

from src import server                                             # noqa: E402
from src.client import (MobileSession, SessionExpired,              # noqa: E402
                        TbankApiError, UnreadableResponse)

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


class Resp:
    """Stands in for a requests.Response, faithfully enough for _unwrap."""

    def __init__(self, status, body, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body, ensure_ascii=False)

    def json(self):
        if self._body is _UNPARSEABLE:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


_UNPARSEABLE = object()


def session():
    s = MobileSession("sid", "rt")
    return s


# ---- 1 + 2: the HTTP status, and the envelope that outranks it ------------

def test_an_error_status_with_an_innocent_body_is_not_returned_as_data():
    """Both bodies are real, decoded out of the owner's own captures. Neither
    carries resultCode or status:Error, so every envelope check passes them — and
    before this, so did _unwrap."""
    s = session()
    cases = [
        (404, {"errorId": "05ceca", "errorCode": "FAQ_NOT_FOUND",
               "errorMessage": "Faq not found", "requestId": "63D2"}, "FAQ_NOT_FOUND"),
        (404, {"message": "Not Found"}, "Not Found"),
        (500, {"foo": "bar"}, "500"),
    ]
    for status, body, expect in cases:
        try:
            got = s._unwrap(Resp(status, body))
            failures.append(
                f"HTTP {status} {body} came back as data ({got!r}) — a failed "
                f"request would render as «ничего не найдено»")
        except TbankApiError as e:
            check(str(status) in e.result_code,
                  f"the status must be named: {e.result_code}")
            check(expect in str(e),
                  f"the server's own message must survive: {e}")
    print(f"  status: {len(cases)} error responses raise instead of returning a payload")


def test_a_rejected_credential_is_a_session_error_not_a_generic_one():
    """401/403 must reach ensure_fresh's re-login path, not look like a data error."""
    s = session()
    for status in (401, 403):
        try:
            s._unwrap(Resp(status, {"message": "nope"}))
            failures.append(f"HTTP {status} did not raise")
        except SessionExpired:
            pass
        except TbankApiError as e:
            failures.append(f"HTTP {status} raised {type(e).__name__}, not "
                            f"SessionExpired — re-login will not trigger")
    print("  auth: 401 and 403 raise SessionExpired, so re-login still triggers")


def test_the_body_gets_to_name_its_own_error_before_the_status_does():
    """The OIDC token endpoint answers HTTP 400 with {"error":"invalid_grant"}, and
    that mapping is what makes ensure_fresh fall back to silent re-login. Checking
    the status first would turn it into a bare HTTP_400 and break recovery on the
    one path that recovers a dead session."""
    s = session()
    try:
        s._unwrap(Resp(400, {"error": "invalid_grant",
                             "error_description": "Token is not active"}))
        failures.append("a 400 invalid_grant did not raise")
    except SessionExpired as e:
        check(e.result_code == "invalid_grant",
              f"the envelope's own code must win over the status: {e.result_code}")
    except TbankApiError as e:
        failures.append(f"invalid_grant became {e.result_code} — silent re-login "
                        f"no longer recognises an expired session")

    # And the lifestyle envelope keeps its own code too.
    try:
        s._unwrap(Resp(400, {"status": "Error",
                             "payload": {"code": "268", "message": "Сервис недоступен"}}))
        failures.append("a lifestyle error envelope did not raise")
    except TbankApiError as e:
        check(e.result_code == "268",
              f"the lifestyle code must win over the status: {e.result_code}")
    print("  precedence: a body that names its error keeps it; the status is the fallback")


def test_a_cut_error_body_says_it_was_cut():
    """These messages are diagnostics, and the missing part is often the
    interesting part — a proxy interstitial cut at 200 characters looks like the
    server said nothing at all. Added because this file introduced two bare
    `[:200]` slices while the audit that prompted it was still listing unmarked
    truncation as a defect class."""
    s = session()
    body = "<html>" + ("E" * 4000) + "</html>"
    try:
        s._unwrap(Resp(500, {"message": body}))
        failures.append("a 500 did not raise")
    except TbankApiError as e:
        check("…" in str(e), f"the cut is not marked: {str(e)[-80:]!r}")
        check("симв." in str(e),
              f"the message must say how much was dropped: {str(e)[-80:]!r}")
        check(len(str(e)) < 400, f"the excerpt did not actually cut: {len(str(e))}")

    # An answer that FITS must come back whole — a marker on an uncut string would
    # be its own kind of lie.
    try:
        s._unwrap(Resp(500, {"message": "коротко и целиком"}))
    except TbankApiError as e:
        check("…" not in str(e) and "коротко и целиком" in str(e),
              f"a short message must not be marked as cut: {e}")
    print("  excerpts: a cut error body names the bytes it dropped, a short one is whole")


def test_a_successful_status_still_returns_its_payload():
    """The point is to stop swallowing failures, not to start refusing successes."""
    s = session()
    check(s._unwrap(Resp(200, {"payload": {"a": 1}})) == {"a": 1},
          "a 200 no longer unwraps its payload")
    check(s._unwrap(Resp(200, {"result": [1, 2]})) == [1, 2],
          "a 200 no longer unwraps the messenger envelope")
    check(s._unwrap(Resp(204, {"ok": True})) == {"ok": True},
          "a 204 must still pass through")
    check(s._unwrap(Resp(200, [1, 2, 3])) == [1, 2, 3],
          "a bare JSON list must still pass through")
    print("  regression: 200/204, payload, result and bare lists all still work")


# ---- 3: an unreadable answer on the money path ---------------------------

def test_an_unreadable_answer_leaves_the_outcome_unknown():
    """HTTP 200 and a body that is not JSON — a proxy or WAF interstitial. The
    request reached something. transfer() classifies a TbankApiError as «the bank
    refused, money is safe» and a RequestException as «unknown», so this has to be
    both: it IS an answer, and it proves nothing about the money."""
    s = session()
    try:
        s._unwrap(Resp(200, _UNPARSEABLE, text="<html>Service unavailable</html>"))
        failures.append("an unparseable 200 did not raise")
    except Exception as e:                                       # noqa: BLE001
        # Caught broadly and asserted on, so reverting the fix REPORTS rather than
        # crashing the file — the distinction matters when the whole point is a
        # guard that has to be legible when it fires.
        check(isinstance(e, TbankApiError), f"must be a TbankApiError: {type(e).__name__}")
        check(isinstance(e, requests.exceptions.RequestException),
              f"{type(e).__name__} is not a RequestException, so the money tools "
              f"classify an unreadable answer as a confirmed non-charge")
        check(isinstance(e, UnreadableResponse),
              f"expected UnreadableResponse, got {type(e).__name__}")
        check("HTTP_200" in getattr(e, "result_code", ""),
              f"the status must be named: {getattr(e, 'result_code', None)}")

    # A WAF page is kilobytes long, and this branch cuts it too — so it has to say
    # so, like every other cut here.
    try:
        s._unwrap(Resp(200, _UNPARSEABLE, text="<html>" + "W" * 6000 + "</html>"))
        failures.append("a long unparseable 200 did not raise")
    except TbankApiError as e:
        check("симв." in str(e),
              f"the unreadable-body excerpt does not say it was cut: {str(e)[-70:]!r}")
        check(len(str(e)) < 800, f"the excerpt did not cut: {len(str(e))} chars")

    # And that classification must actually reach the user, through the real tool.
    class Wedged(MobileSession):
        def __init__(self):
            self.mobile_sessionid = "sid"
            self.access_token = "tok"

        def ensure_fresh(self, *a, **kw):
            return None

        def list_accounts(self):
            return [{"id": "1111111111", "accountType": "Current",
                     "moneyAmount": {"value": 5000, "currency": {"name": "RUB"}}}]

        def transfer(self, *a, **kw):
            raise UnreadableResponse("HTTP_200", "<html>…</html>")

    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    saved = server._require
    server._require = lambda: Wedged()
    try:
        out = server.transfer(100, "+79991234567", from_account="1111111111")
    finally:
        server._require = saved
    check("НЕИЗВЕСТЕН" in out,
          f"an unreadable answer to /v1/pay must read as an unknown outcome: {out!r}")
    check("деньги на месте" not in out,
          f"it must NOT promise the money is safe: {out!r}")
    print("  money: an unreadable 200 is an unknown outcome, not a confirmed refusal")


# ---- 4: the cart that emptied itself --------------------------------------

def test_an_unreadable_cart_is_not_an_empty_cart():
    """cart/set is a FULL REPLACE, so the pre-read decides what survives. Treating a
    failed read as {} posted only the new items and deleted the rest, while the tool
    printed an ordinary success line."""
    posted = []

    class Broken(MobileSession):
        def __init__(self):
            self._memo = {}

        def grocery_cart_get(self, **kw):
            # The real lifestyle error envelope, HTTP 200 + status:Error.
            raise TbankApiError("500", "Сервис временно недоступен. Попробуйте позже.")

        def grocery_cart_set(self, *a, **kw):
            posted.append(kw)
            return {"goodsSum": 1.0}

    s = Broken()
    try:
        s.grocery_add_to_cart([{"id": "42", "count": 1}], app_id="204", point_id="5980")
        failures.append("a failed cart read was treated as an empty cart")
    except TbankApiError as e:
        check(e.result_code == "CART_READ_FAILED",
              f"the refusal must name itself: {e.result_code}")
        check("НЕ изменена" in str(e) or "не изменена" in str(e).lower(),
              f"it must say the cart was left alone: {e}")
    check(not posted, f"a write was issued after an unreadable read: {posted}")
    print("  cart: an unreadable cart refuses the write instead of replacing it with "
          "the new items")


# ---- 5: the messenger that had nothing to say -----------------------------

def test_a_rejected_messenger_token_is_not_no_unread_messages():
    """The documented rejection shape is HTTP 200 with a LIST:
    [{"errorCode":"AUTH_REQUIRED",…}]. messenger_unread coerced any non-dict to {},
    so it read as «Непрочитанных сообщений нет.» — nothing to retry, nothing to
    read, and an agent that believes the inbox is empty."""
    calls = []

    class Rejecting(MobileSession):
        def __init__(self):
            self.tmsg_session_id = "t"

        def _call_read(self, key, **kw):
            calls.append(key)
            return [{"errorCode": "AUTH_REQUIRED", "errorMessage": "Token inactive"}]

        def _ensure_tmsg(self):
            return None

    s = Rejecting()
    try:
        s.messenger_unread()
        failures.append("a rejected messenger token read as an ordinary answer")
    except Exception as e:                                       # noqa: BLE001
        check(isinstance(e, SessionExpired),
              f"a rejected token must be a session error, got {type(e).__name__}: {e}")
        check("переоформлен" in str(e) or "refresh_session" in str(e),
              f"the recovery call must be named: {e}")
    check(len(calls) == 2,
          f"the token must be re-minted and the read retried once: {calls}")

    # A non-auth error must surface too, rather than being rendered as a message.
    class NotFound(MobileSession):
        def __init__(self):
            self.tmsg_session_id = "t"

        def _call_read(self, key, **kw):
            return {"errorId": "61b3", "errorCode": "CONVERSATION_NOT_FOUND",
                    "errorMessage": "Conversation not found"}

        def _ensure_tmsg(self):
            return None

    try:
        NotFound()._messenger_read(path_override="/x")
        failures.append("a messenger error object was returned as content")
    except TbankApiError as e:
        check(e.result_code == "CONVERSATION_NOT_FOUND",
              f"any errorCode must surface, not just the auth ones: {e.result_code}")
    print("  messenger: a rejected token raises and re-mints; any errorCode surfaces")


# ---- 6: the policy that was not there -------------------------------------

def test_an_empty_policy_list_is_not_one_policy():
    """api.tinsurance.ru envelopes with a capital ResultCode, and `or [pol]` fell
    through on a present-but-empty Policies list — printing the ENVELOPE as a
    fabricated policy row."""
    s = session()
    try:
        s._unwrap(Resp(200, {"ResultCode": "Error", "Payload": None}))
        failures.append("the capital-R error envelope was returned as data")
    except TbankApiError as e:
        check(e.result_code == "Error", f"unexpected code: {e.result_code}")

    # ...and the SUCCESS value of that same envelope is spelled "Ok", not "OK".
    # Reading the capital key without relaxing the value comparison turned every
    # successful insurance response into «API error (Ok)» — found by a live sweep
    # after the fix, because no fixture carried this host's exact spelling.
    #
    # Only that it does not RAISE. The capital `Payload` is deliberately NOT peeled
    # here — insurance_policies does that itself (server.py:3896), and teaching
    # _unwrap a second envelope spelling would change what that caller receives.
    for spelling in ("Ok", "OK", "ok", "success", "0"):
        try:
            got = s._unwrap(Resp(200, {"ResultCode": spelling, "Payload": {"x": 1}}))
            check(got == {"ResultCode": spelling, "Payload": {"x": 1}},
                  f"ResultCode={spelling!r}: unexpected shape {got!r}")
        except TbankApiError as e:
            failures.append(f"ResultCode={spelling!r} is a SUCCESS value and was "
                            f"raised as an error: {e}")

    class Empty(MobileSession):
        def __init__(self):
            pass

        def ensure_fresh(self, *a, **kw):
            return None

        def insurance_policies(self):
            return {"Payload": {"Policies": []}}

    saved = server._require
    server._require = lambda: Empty()
    try:
        out = server.insurance_policies()
    finally:
        server._require = saved
    check(out.strip() == "Действующих полисов нет.",
          f"an empty list must read as no policies, not as one: {out!r}")
    check("№?" not in out, f"a fabricated policy row was printed: {out!r}")
    print("  insurance: the capital-R envelope is seen, an empty list stays empty")


def main():
    print("shadow errors:")
    test_an_error_status_with_an_innocent_body_is_not_returned_as_data()
    test_a_rejected_credential_is_a_session_error_not_a_generic_one()
    test_the_body_gets_to_name_its_own_error_before_the_status_does()
    test_a_cut_error_body_says_it_was_cut()
    test_a_successful_status_still_returns_its_payload()
    test_an_unreadable_answer_leaves_the_outcome_unknown()
    test_an_unreadable_cart_is_not_an_empty_cart()
    test_a_rejected_messenger_token_is_not_no_unread_messages()
    test_an_empty_policy_list_is_not_one_policy()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
