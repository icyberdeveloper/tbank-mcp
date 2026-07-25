"""How many HTTP round-trips a scenario costs.

Latency an agent sees is round-trips, and several tools were paying for requests
they did not need:

  * cards() issued one /v1/account_cards per account — 12 extra calls on this user,
    most of them 400s for deposits and invest accounts — although accounts_light
    already embeds the cards. The per-account response is also THINNER: it lacks the
    name, status, paymentSystem, masked number and expiry that list_cards prints.
  * grocery_add_to_cart downloaded the entire retailers catalogue on every call to
    read one areaId, which is a property of the store and never changes.
  * documents() asked for the prefill contact id twice in a single invocation.
  * grocery_rank fetched nutrition for up to 8 candidates strictly in sequence.
  * cinema_search always walked 4 pages of today's listing even for a named film.

Each test counts the calls the real code makes against a session that records them.

    python3 tests/test_request_economy.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.client import MobileSession, TbankApiError  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


class CountingSession(MobileSession):
    """Answers reads from a canned map and counts every one."""

    def __init__(self, responses, delay=0.0):
        self.responses = responses
        self.delay = delay
        self.calls = []
        self._memo = {}
        self._lock = threading.Lock()

    def ensure_fresh(self, *a, **kw):
        return None

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        with self._lock:
            self.calls.append(key)
        if self.delay:
            time.sleep(self.delay)
        value = self.responses.get(key)
        if value is None:
            raise TbankApiError("NO_STUB", f"unstubbed read: {key}")
        return value(overrides) if callable(value) else value

    def count(self, key):
        return self.calls.count(key)


ACCOUNTS = [
    {"id": "1111111111", "name": "Black", "accountType": "Current",
     "moneyAmount": {"value": 13449.27, "currency": {"name": "RUB"}},
     "cards": [{"id": "291395142", "ucid": "1293548933", "name": "Black",
                "status": "Активна", "value": "553691******1234",
                "paymentSystem": "MC", "expiration": {"milliseconds": 2029957200000}}]},
    {"id": "2222222222", "name": "Депозит", "accountType": "Deposit"},
    {"id": "3333333333", "name": "Внешняя", "accountType": "ExternalAccount",
     "card": {"id": "57534194", "ucid": "", "name": "Газпромбанк *7710",
              "value": "220001******7710"}},
]


def test_cards_costs_one_request_not_one_per_account():
    s = CountingSession({"accounts_light": ACCOUNTS})
    cards = s.cards()
    check(s.count("account_cards") == 0,
          f"cards() still fans out: {s.count('account_cards')} per-account requests")
    check(len(s.calls) == 1, f"cards() must be a single request, made {len(s.calls)}: {s.calls}")

    # And the data must not get worse — these are the fields list_cards prints.
    check(len(cards) == 2,
          f"expected the 2 cards embedded in accounts_light, got {len(cards)} "
          f"(reading them from /v1/account_cards instead of the account payload?)")
    if len(cards) == 2:
        first = cards[0]
        for field in ("id", "ucid", "name", "value", "account", "accountName"):
            check(field in first, f"card lost the {field!r} field: {sorted(first)}")
        check(first["account"] == "1111111111", "the card must carry its account id")
        check(cards[1]["account"] == "3333333333",
              "an ExternalAccount's singular `card` must be picked up too")
        # availableBalance lived only on the per-account response. Dropping the
        # fan-out must not drop the balance with it — the account's own balance is
        # what the card spends from.
        check(first.get("availableBalance") == 13449.27,
              f"the card lost its balance: {first.get('availableBalance')!r}")
        check(first.get("currency") == "RUB",
              f"the balance must carry its currency: {first.get('currency')!r}")
        check(cards[1].get("accountType") == "ExternalAccount",
              "an externally linked card must be identifiable as such")
    print(f"  cards(): 1 request for {len(cards)} cards (was 1 + one per account)")


def test_area_id_is_looked_up_once_per_store():
    class StoreSession(CountingSession):
        # grocery_stores() goes through _http directly (it merges the retailers
        # catalogue with client/info), so count it at the method boundary.
        def grocery_stores(self):
            with self._lock:
                self.calls.append("grocery_stores")
            return [{"appId": "204", "pointId": "5980", "areaId": "17040911",
                     "name": "ВкусВилл"}]

    s = StoreSession({
        "grocery_client_info": {"deliveryInfo": {"address": {
            "value": "ул Примерная", "details": {"street": "Примерная"}}}},
        "grocery_cart_get": {"cart": {"goods": [], "sum": 0}},
        "grocery_cart_set": {"goodsSum": 1.0},
    })
    for _ in range(3):
        s.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="204", point_id="5980")
    n = s.count("grocery_stores")
    check(n == 1, f"the retailers catalogue was downloaded {n} times for 3 add_to_cart calls")

    # A DIFFERENT store must not reuse the first one's areaId.
    s.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="246", point_id="7")
    check(s.count("grocery_stores") == 2,
          "a different store must trigger its own lookup, not reuse the memo")
    print(f"  add_to_cart ×3: retailers fetched {n}× (was once per call)")


def test_documents_resolves_the_contact_id_once():
    s = CountingSession({
        "prefill_contact": {"contacts": [{"id": "c-1"}]},
        "prefill_documents": {"documents": {"RusNationalID": []}},
        "prefill_userinfo_brief": {"brief": {"birthDate": "1990-01-01"}},
    })
    s.identity_documents()
    s.identity_brief()
    n = s.count("prefill_contact")
    check(n == 1, f"the contact id was fetched {n} times in one documents() call")
    print(f"  documents(): contact id resolved {n}× (was twice)")


def test_nutrition_is_fetched_concurrently():
    goods = [{"id": str(i), "name": f"Товар {i}", "price": 10 + i,
              "weight": "100.0 GRM"} for i in range(8)]
    per_call = 0.05

    class SearchSession(CountingSession):
        # grocery_search posts to search.t-bank-app.ru through _http, not _call_read.
        def grocery_search(self, query, app_id="", point_id="", **kw):
            with self._lock:
                self.calls.append("grocery_search")
            time.sleep(self.delay)
            return goods

    s = SearchSession({
        "grocery_good": {"good": {"meta": {"nutritionalValue": {
            "fat": "1", "protein": "2", "carbohydrate": "3", "energy": "100",
            "value": ""}, "weight": {"value": 100.0, "unit": "GRM"}}}},
    }, delay=per_call)
    started = time.monotonic()
    rows = s.grocery_candidates("молоко", app_id="204", point_id="5980",
                                limit=8, with_nutrition=True)
    elapsed = time.monotonic() - started

    check(len(rows) == 8, f"expected 8 candidates, got {len(rows)}")
    check(all(r.get("kcal") == 100.0 for r in rows),
          "every row must still carry its nutrition after the fan-out")
    check(s.count("grocery_good") == 8,
          f"expected one good request per candidate, got {s.count('grocery_good')}")

    sequential = per_call * 9          # search + 8 goods, one after another
    check(elapsed < sequential * 0.6,
          f"nutrition still looks sequential: {elapsed:.2f}s vs {sequential:.2f}s serial")
    print(f"  grocery_rank: 8 nutrition lookups in {elapsed:.2f}s "
          f"(sequential would be ~{sequential:.2f}s)")


def test_named_film_search_stops_at_the_first_page_that_matches():
    def page(overrides):
        n = int((overrides or {}).get("page", 1))
        return {"collection": {"amount": 120, "events": [
            {"name": "Майкл" if (n == 1 and i == 0) else f"Фильм {n}-{i}",
             "eventId": f"{n}{i}"} for i in range(30)]}}

    s = CountingSession({"events_collection": page})
    hits = s.cinema_movies(query="майкл")
    check(len(hits) == 1, f"expected the one match, got {len(hits)}")
    check(s.count("events_collection") == 1,
          f"a named search walked {s.count('events_collection')} pages after matching")

    # No query → still paginate, because the caller asked for the whole listing.
    s2 = CountingSession({"events_collection": page})
    everything = s2.cinema_movies()
    check(s2.count("events_collection") == 4,
          f"an unfiltered listing must still page: {s2.count('events_collection')}")
    check(len(everything) == 120, f"expected the full listing, got {len(everything)}")

    # A film that is only on page 3 must still be found.
    def late(overrides):
        n = int((overrides or {}).get("page", 1))
        return {"collection": {"amount": 120, "events": [
            {"name": "Поздний" if (n == 3 and i == 5) else f"Фильм {n}-{i}",
             "eventId": f"{n}{i}"} for i in range(30)]}}

    s3 = CountingSession({"events_collection": late})
    late_hit = s3.cinema_movies(query="поздний")
    check(len(late_hit) == 1,
          f"early exit must not hide a match on a later page: {len(late_hit)}")
    check(s3.count("events_collection") == 3,
          f"expected to stop at page 3, walked {s3.count('events_collection')}")
    print("  cinema_search: stops once the name is found, still pages when it must")


def main():
    print("request economy:")
    test_cards_costs_one_request_not_one_per_account()
    test_area_id_is_looked_up_once_per_store()
    test_documents_resolves_the_contact_id_once()
    test_nutrition_is_fetched_concurrently()
    test_named_film_search_stops_at_the_first_page_that_matches()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
