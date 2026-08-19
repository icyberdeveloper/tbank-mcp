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
  * cinema_search walked today's listing one page at a time. Making that cheap by
    stopping at the first page holding a match traded requests for correctness —
    matches on later pages disappeared and the count was reported as complete — so
    the pages are now fetched concurrently instead: same answer, one round trip.

Each test counts the calls the real code makes against a session that records them.

    python3 tests/test_request_economy.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Every log the server writes resolves its path at IMPORT time. run_all.py
# redirects them per process; a STANDALONE run of this file did not, so its
# synthetic calls landed in the user's live ~/.local/share/tbank-mcp — the very
# files debug_report()/diagnostics() read back as real agent behaviour.
_LOGS = tempfile.mkdtemp(prefix="tbank-test-logs-")
os.environ.setdefault("TBANK_TRACE_FILE", os.path.join(_LOGS, "calls.jsonl"))
os.environ.setdefault("TBANK_EVENTS", os.path.join(_LOGS, "events.jsonl"))
os.environ.setdefault("TBANK_ATTEMPTS", os.path.join(_LOGS, "attempts.jsonl"))

from src import server  # noqa: E402
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
        self.writes = []          # (key, body) of every call, for asserting shape
        self._memo = {}
        self._lock = threading.Lock()
        # How many reads overlap at any moment, and the highest that ever got. Wall
        # clock cannot tell «8 in parallel» from «2 at a time on a fast machine»,
        # and the concurrency test used to pass with a pool crippled to 2 workers.
        self._inflight = 0
        self.peak = 0

    def ensure_fresh(self, *a, **kw):
        return None

    def sent(self, key):
        return [b for k, b in self.writes if k == key]

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        with self._lock:
            self.calls.append(key)
            self.writes.append((key, body))
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
        try:
            if self.delay:
                time.sleep(self.delay)
            value = self.responses.get(key)
            if value is None:
                raise TbankApiError("NO_STUB", f"unstubbed read: {key}")
            return value(overrides) if callable(value) else value
        finally:
            with self._lock:
                self._inflight -= 1

    def count(self, key):
        return self.calls.count(key)


ACCOUNTS = [
    {"id": "1111111111", "name": "Black", "accountType": "Current",
     "moneyAmount": {"value": 13449.27, "currency": {"name": "RUB"}},
     "cards": [{"id": "100000002", "ucid": "1000000001", "name": "Black",
                "status": "Активна", "value": "510000******1234",
                "paymentSystem": "MC", "expiration": {"milliseconds": 2029957200000}}]},
    {"id": "2222222222", "name": "Депозит", "accountType": "Deposit"},
    {"id": "3333333333", "name": "Внешняя", "accountType": "ExternalAccount",
     "card": {"id": "100000003", "ucid": "", "name": "Внешний банк *0000",
              "value": "220001******0000"}},
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


def test_a_missed_area_id_lookup_is_not_cached_as_the_answer():
    """The memo may only remember an ANSWER, never a miss.

    A store absent from the catalogue on one call — a transient read, a store the
    client has not opened yet — used to be memoised as areaId="" and the empty value
    served for the rest of the process. ВкусВилл's cart/set answers 200 and saves
    nothing without areaId, so every later cart write would silently do nothing, and
    no retry could ever recover: the miss was cached, not the lookup."""
    class FlakyStores(CountingSession):
        catalogue: list = []

        def grocery_stores(self):
            with self._lock:
                self.calls.append("grocery_stores")
            return self.catalogue

    stubs = {
        "grocery_client_info": {"deliveryInfo": {"address": {
            "value": "ул Примерная", "details": {"street": "Примерная"}}}},
        "grocery_cart_get": {"cart": {"goods": [], "sum": 0}},
        "grocery_cart_set": {"goodsSum": 1.0},
    }
    s = FlakyStores(stubs)
    s.catalogue = []                                   # the catalogue comes back empty
    s.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="204", point_id="5980")
    first = s.sent("grocery_cart_set")[-1]["delivery"]
    check("areaId" not in first, f"an unknown areaId must be omitted, not faked: {first}")

    s.catalogue = [{"appId": "204", "pointId": "5980", "areaId": "17040911"}]
    s.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="204", point_id="5980")
    second = s.sent("grocery_cart_set")[-1]["delivery"]
    check(second.get("areaId") == "17040911",
          f"the retry still sends no areaId — the MISS was memoised: {second}")

    # A store that genuinely has no areaId (Азбука Вкуса) IS an answer, and is cached.
    s2 = FlakyStores(stubs)
    s2.catalogue = [{"appId": "578", "pointId": "2", "areaId": None}]
    for _ in range(3):
        s2.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="578", point_id="2")
    check(s2.count("grocery_stores") == 1,
          f"a store with no areaId must still be memoised, looked up "
          f"{s2.count('grocery_stores')} times")
    print("  areaId: a miss is retried, a genuine empty answer is cached")


def test_a_cold_start_add_to_cart_asks_for_the_address_once():
    """One add_to_cart into a store with no cart resolved the delivery address
    twice: _grocery_delivery seeds it from client/info, then grocery_stores() —
    reached while resolving areaId — fetched the same client/info again through its
    own hand-rolled request. Same URL, same answer, back to back, on the path the
    user is waiting on."""
    class Retailers:
        """The retailers catalogue, which grocery_stores() fetches through _http."""

        # _unwrap reads the status before the body: a stub standing in for a
        # Response has to answer like one, or it tests a shape production never has.
        status_code = 200

        @staticmethod
        def json():
            return {"payload": {"categories": [{"retailers": [
                {"appId": "204", "info": {"name": "ВкусВилл"},
                 "delivery": {"pointId": "5980", "areaId": "17040911"}}]}]}}

    class ColdStore(CountingSession):
        # grocery_stores() is NOT stubbed out: the whole point is to count what the
        # real implementation asks for on the way to an areaId.
        def __init__(self, responses):
            super().__init__(responses)
            self.access_token = "tok"
            self.cookie_str = ""
            self.mobile_sessionid = "sid"
            self.device_id = self.old_device_id = "dev"
            self.app_name, self.app_version, self.platform = "mobile", "7.31.6", "ios"
            self.origin, self.ccc, self.cpswc = "mobile", "true", "true"
            self.connection_type, self.inache = "WiFi", "drivetransitt"
            self._http = type("H", (), {"get": lambda *a, **k: Retailers()})()

    s = ColdStore({
        # No cart for this store yet: the address must come from client/info.
        "grocery_cart_get": {"cart": {}},
        "grocery_client_info": {"deliveryInfo": {
            "addresses": [{"id": "a-1", "value": "ул Примерная",
                           "coordinates": {"latitude": 55.75, "longitude": 37.61}}],
            "address": {"value": "ул Примерная", "details": {"street": "Примерная"}}}},
        "grocery_cart_set": {"goodsSum": 1.0},
    })
    s.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="204", point_id="5980")
    n = s.count("grocery_client_info")
    check(n == 1, f"client/info fetched {n} times for one cold-start add_to_cart")

    # The memo is a BURST cache, not a session cache: the delivery address can be
    # edited in the app, so it must expire rather than be believed forever.
    check(s.CLIENT_INFO_TTL <= 300,
          f"client/info is cached for {s.CLIENT_INFO_TTL}s — too long for an "
          f"address the user can change in the app")
    s._memo["client_info"] = (time.time() - s.CLIENT_INFO_TTL - 1,
                              {"deliveryInfo": {"address": {"value": "старый"}}})
    fresh = s.grocery_client_info()
    check(fresh.get("deliveryInfo", {}).get("address", {}).get("value") != "старый",
          "an expired client/info was served from the memo anyway")
    print("  add_to_cart cold start: client/info fetched 1× (was twice)")


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
        def grocery_search(self, query, app_id="", point_id="", limit=10, **kw):
            with self._lock:
                self.calls.append("grocery_search")
            time.sleep(self.delay)
            return goods, len(goods), len(goods)



    s = SearchSession({
        "grocery_good": {"good": {"meta": {"nutritionalValue": {
            "fat": "1", "protein": "2", "carbohydrate": "3", "energy": "100",
            "value": ""}, "weight": {"value": 100.0, "unit": "GRM"}}}},
    }, delay=per_call)
    started = time.monotonic()
    rows, _ = s.grocery_candidates("молоко", app_id="204", point_id="5980",
                                   limit=8, with_nutrition=True)
    elapsed = time.monotonic() - started

    check(len(rows) == 8, f"expected 8 candidates, got {len(rows)}")
    check(all(r.get("kcal") == 100.0 for r in rows),
          "every row must still carry its nutrition after the fan-out")
    check(s.count("grocery_good") == 8,
          f"expected one good request per candidate, got {s.count('grocery_good')}")

    # OBSERVED parallelism, not wall clock. The old assertion was
    # `elapsed < sequential * 0.6`, which a pool crippled from 8 workers to 2 still
    # satisfied — so a 4× latency regression in the fan-out went undetected and only
    # a total loss of concurrency failed. `s.peak` is the highest number of
    # grocery_good calls in flight at once, counted by the fake itself.
    check(s.peak == 8,
          f"the fan-out ran {s.peak} request(s) at a time, not 8 — the pool is "
          f"min(8, len(rows)) and every widening of it is a latency win the user "
          f"waits for")
    # Wall clock is kept as a sanity check, not as the assertion: it catches an
    # implementation that reports parallelism it did not have.
    sequential = per_call * 9          # search + 8 goods, one after another
    check(elapsed < sequential * 0.6,
          f"nutrition still looks sequential: {elapsed:.2f}s vs {sequential:.2f}s serial")
    print(f"  grocery_rank: 8 nutrition lookups, {s.peak} in flight at the peak, "
          f"{elapsed:.2f}s (sequential would be ~{sequential:.2f}s)")


def test_grocery_search_sees_the_whole_page_before_ranking():
    """grocery_search used to break at the 10th match and sort AFTER the break: a
    cheaper match at position 15 of the server page was unreachable, and «cheapest»
    meant cheapest of an arbitrary first ten."""
    def good(i, price):
        return {"objectType": "grocery_goods",
                "objectSource": {"goodForeignId": str(i), "name": f"Молоко №{i}",
                                 "price": {"value": price},
                                 "weight": {"value": 900.0, "unit": "GRM"}}}
    hits = [good(i, 100 + i) for i in range(30)]
    hits[15] = good(15, 1)             # the cheapest match, beyond the old break

    class Answer:
        status_code = 200

        @staticmethod
        def json():
            return {"payload": {"sortedByScoreObjects": hits}}

    class SearchStore(CountingSession):
        def __init__(self):
            super().__init__({})
            self.access_token = "tok"
            self.mobile_sessionid = "sid"
            self.device_id = self.old_device_id = "dev"
            self.app_name, self.app_version, self.platform = "mobile", "7.31.6", "ios"
            self.origin, self.ccc, self.cpswc = "mobile", "true", "true"
            self.connection_type, self.inache = "WiFi", "drivetransitt"
            self._http = type("H", (), {"post": lambda *a, **k: Answer()})()

    s = SearchStore()
    rows, matched, fetched = s.grocery_search("молоко", app_id="204", point_id="5980")
    check(matched == 30 and fetched == 30,
          f"the counters must see the whole page: matched={matched}, fetched={fetched}")
    check(len(rows) == 10, f"the default cut is still 10 rows, got {len(rows)}")
    check(rows and rows[0]["id"] == "15",
          f"sorting must run over ALL matches before the cut — the cheap match at "
          f"position 15 used to be lost to the break: {rows[0] if rows else None}")

    every, m2, _ = s.grocery_search("молоко", app_id="204", point_id="5980", limit=0)
    check(len(every) == m2 == 30, f"limit=0 must return every match, got {len(every)}")
    print("  grocery_search: 30 hits ranked before the cut, limit=0 returns them all")


def listing(named: dict, amount: int = 120, per_page: int = 30):
    """A four-page afisha where `named` maps (page, index) → film name."""
    def page(overrides):
        n = int((overrides or {}).get("page", 1))
        return {"collection": {"amount": amount, "events": [
            {"name": named.get((n, i), f"Фильм {n}-{i}"), "eventId": f"{n}{i}"}
            for i in range(per_page)]}}
    return page


# The shelf list the client now reads the real collectionCode from, instead of
# transliterating the city name into a guess.
SHELVES = {"collections": [{"code": "Skoro-v_kino_Moskva"},
                           {"code": "Segodnya-v_kino_Moskva"}]}


def test_the_catalogue_spans_days_and_paginates_only_where_it_can():
    """A range is the point of this endpoint — eight days in Moscow answer with 205
    films against 93 for one — and an event repeats on every day it runs, so the
    same title must not come back once per day.

    The two shapes behind the one call also have to be respected: cinema ignores
    count/page and returns the vertical whole, so paging it is pure waste, while
    concerts and plays paginate for real."""
    from src.client import TbankApiError

    class Catalog(CountingSession):
        """Pages that overlap, the way the days of a range do."""

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            self.calls.append(key)
            self.writes.append((key, body))
            page = (body or {}).get("page", 1)
            # Every page repeats event 1; the rest are its own.
            evs = [{"eventId": "1", "eventName": "Повтор"}] + [
                {"eventId": f"{page}-{i}", "eventName": f"Событие {page}-{i}"}
                for i in range(19)]
            return {"amount": 60, "list": evs}

    concert = Catalog({})
    events, _scanned, amount = concert.afisha_catalog(kind="концерт", city="Москва",
                                            date_from="2026-07-30",
                                            date_to="2026-08-06")
    check(concert.count("events_concert") == 3,
          f"60 at 20 a page is three pages: {concert.count('events_concert')}")
    ids = [e["eventId"] for e in events]
    check(ids.count("1") == 1,
          f"an event running every day must appear once, got {ids.count('1')}")
    check(len(events) == 58, f"expected 1 shared + 57 own, got {len(events)}")
    check(amount == 60, f"the server's own total must survive: {amount}")

    # The window is whole days in Moscow time, both bounds inclusive.
    sent = concert.sent("events_concert")[0]
    check(sent["date"] == {"from": "2026-07-30T00:00:00+03:00",
                           "to": "2026-08-06T23:59:59+03:00"},
          f"the range was not sent as whole days: {sent['date']}")
    check(sent["cityId"] == "1", f"the city must ride as its id: {sent}")

    # Cinema ignores paging, so asking for page 2 is a request that buys nothing.
    film = Catalog({})
    film.afisha_catalog(kind="кино", city="Москва", date_from="2026-07-30")
    check(film.count("events_movie") == 1,
          f"cinema must not be paged: {film.count('events_movie')} requests")

    # Exhibitions have no catalogue at all — no path is invented for them.
    try:
        Catalog({}).afisha_catalog(kind="выставка", city="Москва",
                                   date_from="2026-07-30")
        check(False, "an exhibition catalogue was invented")
    except TbankApiError as e:
        check(e.result_code == "NO_CATALOG", f"wrong refusal: {e}")
    print("  catalogue: a range dedupes by event, cinema is fetched once")


def test_a_named_film_search_sees_every_page():
    """The listing has no server-side search, so a name is matched by us — which
    means every page has to be looked at.

    Stopping at the first page that matched made a named search one request instead
    of four and made it wrong: a film showing only later in the afisha came back as
    "ничего не найдено", and when a match did land on page 1 the tool reported its
    own partial count as the total. Speed comes from fetching the pages at once."""
    # Two prints of the same film, pages 1 and 4. Early exit returned exactly one.
    s = CountingSession({"events_collection": listing({(1, 0): "Майкл",
                                                       (4, 7): "Майкл. Финал"}),
                         "events_by_service": SHELVES})
    hits, scanned, total = s.cinema_movies(query="майкл", city="Москва")
    check(len(hits) == 2,
          f"a match on a later page was dropped: got {len(hits)} of 2 "
          f"({[h['name'] for h in hits]})")
    check((scanned, total) == (120, 120),
          f"the whole afisha must be reported as scanned, got {scanned} of {total}")
    check(s.count("events_collection") == 4,
          f"four pages of 30 in a listing of 120: {s.count('events_collection')}")

    # A film only on the LAST page — the case the early exit could never reach.
    s2 = CountingSession({"events_collection": listing({(4, 5): "Поздний"}),
                          "events_by_service": SHELVES})
    late, _, _ = s2.cinema_movies(query="поздний", city="Москва")
    check(len(late) == 1, f"a match on the last page was not found: {len(late)}")

    # No query → the whole listing, unchanged.
    s3 = CountingSession({"events_collection": listing({}),
                          "events_by_service": SHELVES})
    everything, scanned3, total3 = s3.cinema_movies(city="Москва")
    check(len(everything) == 120, f"expected the full listing, got {len(everything)}")
    check(scanned3 == total3 == 120, f"scanned {scanned3} of {total3}")

    # Pages are read concurrently, so four of them cost about one round trip.
    slow = CountingSession({"events_collection": listing({}),
                            "events_by_service": SHELVES}, delay=0.05)
    started = time.monotonic()
    slow.cinema_movies(city="Москва")
    elapsed = time.monotonic() - started
    # shelf + page 1 + pages 2..4 as one concurrent batch = three waits, not six.
    check(elapsed < 0.05 * 4,
          f"pages 2..4 still look sequential: {elapsed:.2f}s for 4 × 0.05s")

    # The shelf list is per (service, city) and does not change between searches,
    # so a second search on the same session must not pay for it again.
    slow.cinema_movies(city="Москва", query="что-то")
    check(slow.count("events_by_service") == 1,
          f"the shelf list was re-fetched: {slow.count('events_by_service')} times")

    # And when the page cap binds, the caller is told the afisha was NOT fully seen —
    # otherwise "нет такого фильма" and "не смотрели" become the same answer.
    capped = CountingSession({"events_collection": listing({}, amount=300),
                              "events_by_service": SHELVES})
    _, scanned4, total4 = capped.cinema_movies(max_pages=2, city="Москва")
    check(scanned4 == 60 and total4 == 300,
          f"a capped scan must report what it actually saw: {scanned4} of {total4}")
    # The shelf list is a live thing, not a contract: Moscow's came back EMPTY
    # while Petersburg's was full, and Kazan's held only the «coming soon» shelf.
    # Falling back to the convention the server uses for this family is what keeps
    # «what is on in Moscow» from answering «no cinema here».
    thin = CountingSession({"events_collection": listing({}),
                            "events_by_service": {"collections": []}})
    code = thin.afisha_collection_code("Segodnya-v_kino_", "movie", city="Казань")
    check(code == "Segodnya-v_kino_Kazan",
          f"an empty shelf list must fall back to the convention, got {code!r}")
    _, _, total5 = thin.cinema_movies(city="Казань", max_pages=1)
    check(total5 == 120, f"the listing must still be reachable: {total5}")
    print("  cinema_search: every page is seen, concurrently, and a capped scan says so")


def test_grocery_stores_seeds_the_area_id_memo():
    """A grocery_stores() call already has every store's areaId in hand — the
    first add_to_cart for a store it just listed used to re-download the whole
    retailers catalogue anyway, just to read that one field back out."""
    class FakeResp:
        status_code = 200
        def json(self):
            return {"categories": [{"name": "Продукты", "retailers": [
                {"appId": "204", "info": {"name": "ВкусВилл"},
                 "delivery": {"pointId": "5980", "areaId": "17040911"}},
                {"appId": "578", "info": {"name": "Азбука Вкуса"},
                 "delivery": {"pointId": "2"}},  # no areaId at all — a real answer
            ]}]}

    class FakeHTTP:
        def get(self, url, **kw):
            return FakeResp()

    s = server._blank_session()
    s._http = FakeHTTP()
    s.grocery_client_info = lambda: {"deliveryInfo": {"addresses": [
        {"value": "ул Примерная",
         "coordinates": {"latitude": "55.7", "longitude": "37.6"}}]}}

    stores = s.grocery_stores()
    check(len(stores) == 2, f"both retailers must come through: {stores}")
    check(s._memo.get("areaId:204:5980") == "17040911",
          f"grocery_stores() must seed the areaId memo it already has in hand: {s._memo}")
    check(s._memo.get("areaId:578:2") == "",
          f"a store with no areaId is still a real answer and must be memoised: {s._memo}")
    print("  grocery_stores: lists stores and seeds the areaId memo for free")


def test_grocery_stores_refuses_instead_of_defaulting_to_a_city():
    """grocery_stores()/_grocery_delivery() used to silently fall back to a
    hardcoded Moscow coordinate when the account had no saved delivery
    address — a store list or cart write built for the wrong city with no
    indication anything was wrong. Both must refuse instead (commit 4878688)."""
    class TrackingHTTP:
        def __init__(self):
            self.called = False

        def get(self, url, **kw):
            self.called = True
            class R:
                status_code = 200

                def json(self):
                    return {"categories": []}
            return R()

    # grocery_stores(): no addresses at all.
    http = TrackingHTTP()
    s = server._blank_session()
    s._http = http
    s.grocery_client_info = lambda: {"deliveryInfo": {"addresses": []}}
    try:
        s.grocery_stores()
        failures.append("grocery_stores() must refuse when there is no saved address")
    except TbankApiError as e:
        check(e.result_code == "NO_DELIVERY_ADDRESS",
              f"wrong refusal code for no addresses: {e.result_code}")
    check(not http.called, "no addresses must refuse BEFORE any network call")

    # grocery_stores(): an address with no coordinates.
    http2 = TrackingHTTP()
    s2 = server._blank_session()
    s2._http = http2
    s2.grocery_client_info = lambda: {"deliveryInfo": {"addresses": [
        {"value": "где-то", "coordinates": {}}]}}
    try:
        s2.grocery_stores()
        failures.append("grocery_stores() must refuse an address with no coordinates")
    except TbankApiError as e:
        check(e.result_code == "NO_DELIVERY_ADDRESS",
              f"wrong refusal code for a coordinate-less address: {e.result_code}")
    check(not http2.called, "a coordinate-less address must refuse BEFORE any network call")

    # _grocery_delivery(): neither the cart nor client/info has an address.
    s3 = server._blank_session()
    s3.grocery_client_info = lambda: {"deliveryInfo": {}}
    try:
        s3._grocery_delivery("204", "5980", cart={"cart": {}})
        failures.append("_grocery_delivery() must refuse with no address anywhere")
    except TbankApiError as e:
        check(e.result_code == "NO_DELIVERY_ADDRESS",
              f"wrong refusal code for _grocery_delivery with no address: {e.result_code}")
    print("  grocery_stores/_grocery_delivery: no usable address refuses, never defaults to a city")


def test_afisha_places_pages_are_fetched_concurrently():
    """afisha_places() has the same 'no server-side search, every page must be
    read' shape as cinema_movies()/afisha_catalog() — those two already fetch
    pages after the first concurrently, this one still fetched them one at a
    time."""
    def places(total):
        def page(overrides):
            n = int((overrides or {}).get("page", 1))
            per_page = MobileSession.PLACES_PAGE
            start = (n - 1) * per_page
            count = max(0, min(per_page, total - start))
            return {"objects": [{"name": f"Место {start + i}"} for i in range(count)],
                    "pagination": {"totalItems": total}}
        return page

    slow = CountingSession({"events_places": places(250)}, delay=0.05)
    started = time.monotonic()
    out, total = slow.afisha_places(kind="кино", city="Москва")
    elapsed = time.monotonic() - started
    check(len(out) == 250 and total == 250,
          f"three pages of 100 must all be fetched: got {len(out)} of {total}")
    check(slow.count("events_places") == 3,
          f"250 items at 100/page is 3 pages: {slow.count('events_places')}")
    # page 1 + pages 2..3 as one concurrent batch = two waits, not three.
    check(elapsed < 0.05 * 3,
          f"pages 2..3 still look sequential: {elapsed:.2f}s for 3 × 0.05s")
    print("  afisha_places: every page is seen, concurrently")


def test_cart_can_shrink_not_only_grow():
    """The cart was append-only: re-adding a good to "correct" it added again, and
    nothing could remove one. The bank has no delete endpoint — removal is a full
    rewrite without the good (capture: [369] posts 6 goods, [375] posts 5 after a
    removal) — so the tool must read the cart and resend the whole list."""
    cart = {"cart": {"goods": [{"id": "1", "count": 2}, {"id": "2", "count": 1},
                               {"id": "3", "count": 5}], "sum": 100}}

    class CartSession(CountingSession):
        def grocery_stores(self):
            return [{"appId": "204", "pointId": "5980", "areaId": "1"}]

    def sent(session):
        for call in reversed(session._http_bodies):
            return call
        return None

    def make():
        s = CartSession({
            "grocery_client_info": {"deliveryInfo": {"address": {
                "value": "ул Примерная", "details": {"street": "Примерная"}}}},
            "grocery_cart_get": cart,
            "grocery_cart_set": {"goodsSum": 1.0},
        })
        s._http_bodies = []
        original = s._call_read

        def spy(key, *, overrides=None, body=None, path_override=None):
            if key == "grocery_cart_set":
                s._http_bodies.append(body)
            return original(key, overrides=overrides, body=body, path_override=path_override)
        s._call_read = spy
        return s

    # count=0 removes exactly one good and leaves the rest untouched.
    s = make()
    s.grocery_set_cart([{"id": "2", "count": 0}], app_id="204", point_id="5980")
    goods = {g["id"]: g["count"] for g in sent(s)["goods"]}
    check(goods == {"1": 2, "3": 5}, f"removal must drop only that good, got {goods}")

    # An absolute count REPLACES, it does not add.
    s = make()
    s.grocery_set_cart([{"id": "1", "count": 1}], app_id="204", point_id="5980")
    goods = {g["id"]: g["count"] for g in sent(s)["goods"]}
    check(goods == {"1": 1, "2": 1, "3": 5},
          f"count must be absolute (2 -> 1), got {goods}")

    # A good not in the cart yet is simply added.
    s = make()
    s.grocery_set_cart([{"id": "9", "count": 3}], app_id="204", point_id="5980")
    goods = {g["id"]: g["count"] for g in sent(s)["goods"]}
    check(goods.get("9") == 3, f"a new good must be added, got {goods}")
    check(len(goods) == 4, f"the existing goods must survive, got {goods}")

    # clear empties it, and still sends the delivery block (cart/set needs it).
    s = make()
    s.grocery_set_cart([], app_id="204", point_id="5980", clear=True)
    body = sent(s)
    check(body["goods"] == [], f"clear must post an empty goods list, got {body['goods']}")
    check("delivery" in body and body["delivery"].get("pointId") == "5980",
          "cart/set needs the delivery block even when clearing")
    check(body.get("cartSetMode") == "SINGLE_CART", "cartSetMode must survive")

    # add_to_cart must stay RELATIVE — the two tools mean different things.
    s = make()
    s.grocery_add_to_cart([{"id": "1", "count": 1}], app_id="204", point_id="5980")
    goods = {g["id"]: g["count"] for g in sent(s)["goods"]}
    check(goods["1"] == 3, f"add_to_cart must still add (2+1), got {goods['1']}")
    print("  cart: remove, set-absolute, add-new and clear all rewrite the full list")


def test_messenger_unread_name_resolution_is_capped():
    """messenger_unread resolves chat names page by page and stops the moment every
    unread id has a name — one page in the common case, never more than 3."""
    target = "want-this-chat"

    def make(unread_on_page):
        # 3 pages of 10 chats; the unread id sits on page `unread_on_page`
        # (0-based), or on none of them when None.
        def page_for(ov):
            off = int((ov or {}).get("offset") or 0)
            i = off // 10
            if i >= 3:
                return []
            page = [{"conversationId": f"c{i}-{j}", "title": f"Чат {i}-{j}"}
                    for j in range(10)]
            if unread_on_page == i:
                page[5] = {"conversationId": target, "title": "Нужный чат"}
            return page
        return CountingSession({
            "messenger_unread": {"conversationIds": [target]},
            "messenger_base": page_for,
        })

    saved = server._require
    try:
        for where, expected in ((0, 1), (1, 2), (None, 3)):
            s = make(where)
            server._require = lambda cur=s: cur
            out = server.messenger_unread()
            check(s.count("messenger_base") == expected,
                  f"unread id on page {where}: expected {expected} page reads, "
                  f"got {s.count('messenger_base')}")
            if where is None:
                check("(чат без названия)" in out,
                      f"an unresolvable chat must degrade visibly: {out!r}")
            else:
                check("Нужный чат" in out,
                      f"the resolved name must be printed: {out!r}")
    finally:
        server._require = saved
    print("  messenger_unread: name walk stops at the resolving page, hard cap 3")


def test_distance_sort_is_not_anchored_to_moscow_by_accident():
    class SchedSession(CountingSession):
        def __init__(self):
            super().__init__({"schedule_movie": {"list": []}})
            self.body = None

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            self.body = body
            return super()._call_read(key, overrides=overrides, body=body,
                                      path_override=path_override)

    s = SchedSession()
    s.cinema_schedule("1", "2026-07-26", city="Санкт-Петербург")
    loc = (s.body or {}).get("location") or {}
    check(abs(loc.get("latitude", 0) - 59.9386) < 0.01,
          f"a Petersburg listing must not be sorted from Moscow: {loc}")

    s2 = SchedSession()
    s2.cinema_schedule("1", "2026-07-26", city="Урюпинск")
    check("location" not in (s2.body or {}),
          f"an unknown city must drop the distance sort, not invent a point: {s2.body}")
    check("sort" not in (s2.body or {}), "sorting by distance with no point is meaningless")

    s3 = SchedSession()
    s3.cinema_schedule("1", "2026-07-26", city="Москва", latitude=55.0, longitude=37.0)
    check((s3.body or {}).get("location") == {"latitude": 55.0, "longitude": 37.0},
          f"an explicit point must win: {s3.body}")
    print("  cinema_schedule: anchor follows the city, explicit point wins, unknown city unsorted")


def test_weight_priced_goods_survive_a_cart_write():
    """captures2.xml #1005 posts {"id":"606","count":0.57} beside 0.63 and 1.35 —
    goods sold by weight carry a FRACTIONAL count. Coercing with int() turned every
    such line into 0, and 0 is how this API removes a good: rebuilding the cart to
    add one item silently deleted every vegetable, fruit and meat line in it, while
    cart/set answered 200."""
    cart = {"cart": {"goods": [{"id": "606", "count": 0.57},
                               {"id": "605", "count": 0.63},
                               {"id": "64336", "count": 1.35},
                               {"id": "700", "count": 2}], "sum": 100}}

    class CartSession(CountingSession):
        def grocery_stores(self):
            return [{"appId": "204", "pointId": "5980", "areaId": "1"}]

    def make():
        s = CartSession({
            "grocery_client_info": {"deliveryInfo": {"address": {
                "value": "ул Примерная", "details": {"street": "Примерная"}}}},
            "grocery_cart_get": cart,
            "grocery_cart_set": {"goodsSum": 1.0},
        })
        s.bodies = []
        original = s._call_read

        def spy(key, *, overrides=None, body=None, path_override=None):
            if key == "grocery_cart_set":
                s.bodies.append(body)
            return original(key, overrides=overrides, body=body, path_override=path_override)
        s._call_read = spy
        return s

    # Adding an unrelated good must not disturb the weight lines.
    s = make()
    s.grocery_add_to_cart([{"id": "999", "count": 1}], app_id="204", point_id="5980")
    sent = {g["id"]: g["count"] for g in s.bodies[-1]["goods"]}
    check(sent.get("606") == 0.57,
          f"the 0.57 kg line was destroyed by an unrelated add: {sent.get('606')!r}")
    check(sent.get("64336") == 1.35, f"1.35 was rounded away: {sent.get('64336')!r}")
    check(sent.get("700") == 2 and isinstance(sent["700"], int),
          f"a whole count must stay an int, got {sent.get('700')!r}")
    check(len(sent) == 5, f"a good disappeared from the cart: {sent}")

    # Same through the absolute-count path.
    s = make()
    s.grocery_set_cart([{"id": "700", "count": 1}], app_id="204", point_id="5980")
    sent = {g["id"]: g["count"] for g in s.bodies[-1]["goods"]}
    check(sent.get("606") == 0.57 and sent.get("605") == 0.63,
          f"grocery_set_cart dropped the weight goods: {sent}")
    check(sent.get("700") == 1, f"the absolute count did not apply: {sent.get('700')!r}")

    # A fractional count the CALLER asks for must survive too (570 g of something).
    s = make()
    s.grocery_set_cart([{"id": "605", "count": 0.8}], app_id="204", point_id="5980")
    sent = {g["id"]: g["count"] for g in s.bodies[-1]["goods"]}
    check(sent.get("605") == 0.8, f"a fractional target count was lost: {sent.get('605')!r}")

    # And removal must still be exactly 0, not "anything below 1".
    s = make()
    s.grocery_set_cart([{"id": "606", "count": 0}], app_id="204", point_id="5980")
    sent = {g["id"]: g["count"] for g in s.bodies[-1]["goods"]}
    check("606" not in sent, f"count=0 must remove: {sent}")
    check(sent.get("605") == 0.63, f"removal disturbed a neighbour: {sent}")
    print("  cart: fractional (weight-priced) counts survive add, set and removal")


def main():
    print("request economy:")
    test_cards_costs_one_request_not_one_per_account()
    test_area_id_is_looked_up_once_per_store()
    test_a_missed_area_id_lookup_is_not_cached_as_the_answer()
    test_a_cold_start_add_to_cart_asks_for_the_address_once()
    test_documents_resolves_the_contact_id_once()
    test_nutrition_is_fetched_concurrently()
    test_grocery_search_sees_the_whole_page_before_ranking()
    test_the_catalogue_spans_days_and_paginates_only_where_it_can()
    test_a_named_film_search_sees_every_page()
    test_grocery_stores_seeds_the_area_id_memo()
    test_grocery_stores_refuses_instead_of_defaulting_to_a_city()
    test_afisha_places_pages_are_fetched_concurrently()
    test_cart_can_shrink_not_only_grow()
    test_weight_priced_goods_survive_a_cart_write()
    test_messenger_unread_name_resolution_is_capped()
    test_distance_sort_is_not_anchored_to_moscow_by_accident()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
