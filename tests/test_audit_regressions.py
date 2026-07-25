"""Three defects found by the 2026-07-25 audit. Each was invisible in normal use.

1. spending_categories always returned Total 0. `payload["spending"]` is a TREE
   ({summary, intervals:[{aggregated:[…]}]}), not a list of categories, so the old
   `for c in spending:` iterated the two dict KEYS, failed isinstance(dict) on both
   and produced an empty report. Nothing raised: the tool printed "Total: 0.0 RUB"
   over a period whose real spending was 3.87M RUB. An earlier "live validation"
   missed it because it checked for HTTP 200, not for a plausible number.

2. _err() leaked the mobile sessionid — the HMAC key for /v1/pay — into the model's
   context on any network error. Secrets ride in the query string, and requests puts
   the whole URL in the exception text. The existing blob-redactor does NOT catch a
   sessionid: it is 61 chars, but the '.' inside splits it into 32- and 28-char runs,
   both under the 40-char threshold. So this needs a scrub by parameter NAME.

3. cinema_schedule never printed objectId, which cinema_seats/cinema_book require.
   The docs told the agent to take it from there; the tool never emitted one, so the
   whole cinema booking flow dead-ended. concert_schedule had always printed it.

    python3 tests/test_audit_regressions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from src import server  # noqa: E402
from src.client import MobileSession  # noqa: E402
from src.observability import redact_text  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def money(v):
    return {"value": v, "currency": {"code": 643, "name": "RUB", "strCode": "643"}}


# The real shape, trimmed from captures.xml item 52 (GET /v1/operations_histogram,
# 200, groupBy=category, 31 day-intervals). Kept inline so the test runs on a clean
# clone — the captures are gitignored secrets and are not there.
HISTOGRAM = {"payload": {
    "spending": {
        "summary": money(1500.0),
        "intervals": [
            {"summary": money(1000.0), "start": 1782853200000, "end": 1782939599999,
             "aggregated": [
                 {"groupBy": "24", "groupByKey": "24", "amount": money(700.0),
                  "amountPercent": 70.0, "category": {"id": "24", "name": "Переводы"}},
                 {"groupBy": "13", "groupByKey": "13", "amount": money(300.0),
                  "amountPercent": 30.0, "category": {"id": "13", "name": "Супермаркеты"}},
             ]},
            {"summary": money(500.0), "start": 1782939600000, "end": 1783025999999,
             "aggregated": [
                 {"groupBy": "13", "groupByKey": "13", "amount": money(500.0),
                  "amountPercent": 100.0, "category": {"id": "13", "name": "Супермаркеты"}},
             ]},
        ]},
    "earning": {"summary": money(9000.0), "intervals": [
        {"summary": money(9000.0), "aggregated": [
            {"groupBy": "24", "amount": money(9000.0), "category": {"id": "24", "name": "Переводы"}}]}]},
}}


class HistogramSession(MobileSession):
    """Returns the captured histogram shape instead of calling the API."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def _call_read(self, name, **kw):
        self.calls.append((name, kw))
        return self.payload


def test_spending_categories_walks_the_interval_tree():
    s = HistogramSession(HISTOGRAM)
    rep = s.spending_categories("0000000000", 1782853200000, 1785531599999)

    check(rep["total_spent"] == 1500.0,
          f"total must come from spending.summary.value, got {rep['total_spent']}")
    check(rep["total_earned"] == 9000.0,
          f"earnings must be summed too, got {rep['total_earned']}")

    cats = {c["category"]: c["amount"] for c in rep["categories"]}
    check(cats == {"Супермаркеты": 800.0, "Переводы": 700.0},
          f"categories must be summed ACROSS intervals, got {cats}")
    check([c["category"] for c in rep["categories"]][0] == "Супермаркеты",
          "categories must be sorted by amount, biggest first")
    shares = {c["category"]: c["share_pct"] for c in rep["categories"]}
    check(abs(shares["Супермаркеты"] - 53.33) < 0.01,
          f"share must be of the real total, got {shares}")

    # The exact bug: iterating the dict yields its KEYS and silently produces nothing.
    check(rep["categories"], "the tree walk produced no categories at all")

    # A period with no spending must report zero honestly, not crash.
    empty = HistogramSession({"payload": {"spending": {}, "earning": {}}})
    z = empty.spending_categories(None, 0, 1)
    check(z["total_spent"] == 0.0 and z["categories"] == [],
          f"an empty period must be a clean zero, got {z}")

    # A malformed side must not raise either — this is a read tool on a live API.
    junk = HistogramSession({"payload": {"spending": [], "earning": None}})
    j = junk.spending_categories(None, 0, 1)
    check(j["total_spent"] == 0.0, f"malformed payload must degrade, got {j}")
    print("  spending_categories: sums across intervals, honest zero, survives junk")


def test_err_redacts_the_sessionid():
    # Synthetic, but the SHAPE is what matters and it mirrors a real one: 32 chars,
    # a '.', then the pod suffix. That '.' is exactly why the blob redactor misses it.
    sid = "AbCdEfGhIjKlMnOpQrStUvWxYz012345.authenticon-0123456789-abcde"
    url = ("https://api.t-bank-app.ru/v1/accounts_light?appName=mobile"
           f"&sessionid={sid}&deviceId=00000000-1111-2222-3333-444444444444")

    # Exactly what requests raises when the bank is unreachable.
    out = server._err(requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='api.t-bank-app.ru', port=443): "
        f"Max retries exceeded with url: {url} (Caused by NewConnectionError())"))
    check(sid not in out, f"sessionid leaked verbatim into the tool result: {out}")
    check("<redacted>" in out, f"redaction marker missing: {out}")
    check("ConnectionError" in out, f"the error TYPE must survive redaction: {out}")

    # The blob pattern alone cannot catch it — proving the by-name scrub is required.
    from src.observability import _RE_BLOB
    check(_RE_BLOB.sub("<blob>", sid) == sid,
          "sessionid is now blob-matchable; this test's premise needs revisiting")

    # An API error carrying a URL must be scrubbed on its branch too.
    from src.client import TbankApiError
    api = server._err(TbankApiError("INTERNAL_ERROR", f"failed calling {url}"))
    check(sid not in api, f"sessionid leaked through the TbankApiError branch: {api}")

    from src.client import SessionExpired
    exp = server._err(SessionExpired("SESSION_IS_ABSENT", f"at {url}"))
    check(sid not in exp, f"sessionid leaked through the SessionExpired branch: {exp}")
    check("refresh_session" in exp, "the expiry branch must still name the recovery tool")

    # The user's phone must not survive either — it rides as ?pointer= on SBP lookups.
    check("+79991234567" not in redact_text("https://x/v1/get_requisites?pointer=+79991234567"),
          "the SBP pointer (a real phone number) must be redacted")
    print("  _err: sessionid, phone and URL secrets scrubbed on all three branches")


class ScheduleSession(MobileSession):
    def __init__(self, venues):
        self.venues = venues

    def ensure_fresh(self, *a, **kw):
        return None

    def cinema_schedule(self, event_id, date, city="Москва"):
        return self.venues


def test_cinema_schedule_emits_objectid():
    """Field names verified against captures2.xml item 733: every one of the 139
    venues carries info.objectId, and the slot carries slotId."""
    venues = [{
        "info": {"objectId": "10031", "objectName": "Синема Парк Метрополис",
                 "geo": {"address": "Ленинградское ш., 16А", "distance": 4200}},
        "events": [{"slots": [
            {"slotId": "132988597", "startTime": "17:30", "hallName": "ЗАЛ №7",
             "prices": {"fix": 880}}]}],
    }]
    original = server._require
    server._require = lambda: ScheduleSession(venues)
    try:
        out = server.cinema_schedule("103693", "2026-07-26")
    finally:
        server._require = original

    check("objectId=10031" in out,
          f"cinema_schedule must print the venue objectId — cinema_seats needs it:\n{out}")
    check("slotId=132988597" in out, f"slotId must still be printed:\n{out}")

    # Both ids in one output is the whole point: either alone is useless downstream.
    check("objectId=10031" in out and "slotId=132988597" in out,
          "slotId without objectId is a dead end — cinema_seats requires both")

    # The docstring must say so, since a skill may not be loaded.
    doc = server.cinema_schedule.__doc__ or ""
    check("objectId" in doc, f"cinema_schedule's docstring never mentions objectId: {doc!r}")
    print("  cinema_schedule: objectId + slotId both emitted, and documented")


def main():
    print("audit regressions (2026-07-25):")
    test_spending_categories_walks_the_interval_tree()
    test_err_redacts_the_sessionid()
    test_cinema_schedule_emits_objectid()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
