"""What a tool's answer claims about itself.

The defects here share one shape: the tool did its job and then described the result
wrongly. Nothing raised, nothing was empty, and every line looked like an answer —
which is what made them survive two audits.

* A scan capped at N pages, with a header stating the bank's TRUE total and a hint
  naming a limit that could never render it. A `query` then filtered the scanned
  slice, so «ничего не найдено» was said about events the scan never reached.
* A truncation note reading «новые сверху» over a schedule sorted by ASCENDING
  date — so the hidden showings were the LATER ones, and an agent asked about
  October concluded it had already seen them.
* A venue hint promising `limit={total}` that one page cannot hold: obeying it
  re-rendered the same page, unchanged. An invitation to loop.
* `flows(topic)` — the tool whose whole job is naming the next call — keeping the
  top three matches and dropping the rest silently.
* A provider search one page deep into a group of 63 889, reported as «не найдено».
* Ids that no tool printed although the response carried them: the cards inside
  list_accounts, the appId inside orders, the total inside grocery_cart.
* A filter that matched nothing because the id was of the wrong KIND, reported as
  «операций нет» rather than «такого счёта нет».

    python3 tests/test_honest_output.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tbank-honest-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

from src import observability as obs, server                      # noqa: E402
from src.client import MobileSession, TbankApiError               # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def run(session, fn, *a, **kw):
    saved = server._require
    server._require = lambda: session
    try:
        return fn(*a, **kw)
    finally:
        server._require = saved


class Stub(MobileSession):
    def __init__(self, **answers):
        self._memo = {}
        for name, value in answers.items():
            setattr(self, name, (lambda v: (lambda *a, **kw: v))(value))

    def ensure_fresh(self, *a, **kw):
        return None

    def ensure_client_session(self, *a, **kw):
        return None


# ---- a capped scan must say it was capped --------------------------------

def test_a_capped_catalogue_scan_says_what_it_did_not_look_at():
    events = [{"eventId": str(i), "eventName": f"Событие {i}", "fields": {}, "slots": []}
              for i in range(40)]
    # 40 scanned out of 206 the bank reports.
    s = Stub(afisha_catalog=(events, 40, 206))
    out = run(s, server.afisha_catalog, kind="концерт", city="Москва",
              date_from="2026-08-01", limit=5)
    check("просмотрено 40 из 206" in out,
          f"the header must state the coverage, not the bank's total alone: {out[:200]!r}")
    check("pages" in out,
          f"the hint must name the argument that widens the scan: {out[:250]!r}")
    check("limit=206" not in out,
          f"the hint must not promise a limit the scan cannot deliver: {out[:250]!r}")

    # A full scan says nothing about pages — the note is for the capped case only.
    full = Stub(afisha_catalog=(events, 40, 40))
    out2 = run(full, server.afisha_catalog, kind="концерт", city="Москва",
               date_from="2026-08-01", limit=5)
    check("просмотрено" not in out2,
          f"a complete scan must not claim to be partial: {out2[:150]!r}")

    # A query that found nothing inside a capped scan is not «нет такого».
    empty = Stub(afisha_catalog=([], 40, 206))
    out3 = run(empty, server.afisha_catalog, kind="концерт", city="Москва",
               date_from="2026-08-01", query="Пикник")
    check("среди просмотренных" in out3,
          f"a miss inside a capped scan must say how deep it looked: {out3!r}")
    check("166" in out3,
          f"...and how many were never checked: {out3!r}")
    print("  afisha_catalog: the scan's coverage is stated, and a miss inside it "
          "is not a fact")


def test_a_truncation_note_describes_the_order_it_actually_has():
    """The note is the sentence that tells the agent WHAT fell off the end."""
    events = [{"eventId": str(i), "name": f"Показ {i}", "prices": {},
               "slots": [{"startDateTime": f"2026-08-{i+1:02d}T19:00"}]}
              for i in range(30)]
    s = Stub(place_schedule=(events, 137))
    out = run(s, server.place_schedule, "9318", limit=5)
    check("новые сверху" not in out,
          f"a schedule runs by ascending date — «новые сверху» inverts what is "
          f"hidden: {out.splitlines()[0]!r}")
    check("ближайшие сверху" in out,
          f"the real order must be stated: {out.splitlines()[0]!r}")

    # ...and an operations list, which IS newest-first, must keep saying so.
    ops = Stub(list_operations=[{"id": str(i), "account": "111",
                                 "operationTime": {"milliseconds": 1785786616973},
                                 "amount": {"value": 10, "currency": {"name": "RUB"}},
                                 "description": "т"} for i in range(10)])
    out2 = run(ops, server.list_operations, "111", limit=3)
    check("новые сверху" in out2,
          f"a newest-first list must still say so: {out2.splitlines()[0]!r}")
    print("  order note: stated per list, and only where it is true")


def test_a_paging_hint_points_at_the_argument_that_works():
    events = [{"eventId": str(i), "name": f"Показ {i}", "prices": {}, "slots": []}
              for i in range(20)]
    s = Stub(place_schedule=(events, 137))
    out = run(s, server.place_schedule, "9318", limit=5)
    head = out.splitlines()[0]
    check("limit=137" not in head,
          f"one page holds 20 rows; limit=137 renders the same page again: {head!r}")
    check("page=2" in head, f"only page= reaches the rest: {head!r}")
    check("limit=20" in head, f"...and limit= is honest about this page: {head!r}")
    print("  place_schedule: the hint names page= for the rest, limit= for this page")


def test_flows_names_the_sections_it_dropped():
    out = server.flows("счета карты переводы продукты билеты чат")
    shown = out.count("\n## ")
    check(shown <= 3, f"flows still returns everything: {shown} sections")
    check("Ещё подошли" in out,
          f"the sections that matched and were dropped must be named — this is the "
          f"tool that tells an agent what to call next: {out[-300:]!r}")
    print("  flows: the dropped matches are named instead of vanishing")


def test_flows_matches_whole_words():
    """«чат» is a substring of «получатель», which is in the transfer keywords — so
    the tool's own advertised topic returned the MONEY flow first."""
    out = server.flows("чат")
    first = next((l for l in out.splitlines() if l.startswith("## ")), "")
    check("Messenger" in first or "чат" in first.lower(),
          f"flows('чат') must lead with the messenger section, got {first!r}")
    print("  flows: topics match whole words, not substrings")


# ---- ids that were in the response and never printed ----------------------

def test_the_ids_the_next_call_needs_are_printed():
    accounts = Stub(list_accounts=[{
        "id": "5045038535", "accountType": "Current", "name": "Black",
        "moneyAmount": {"value": 100, "currency": {"name": "RUB"}},
        "cards": [{"id": "233849891", "ucid": "1236003428", "name": "Дебетовая"}]}])
    out = run(accounts, server.list_accounts)
    check("ucid=1236003428" in out,
          f"card_limits/card_requisites take the ucid and it is already in this "
          f"response: {out!r}")
    check("id=233849891" in out,
          f"card_operations takes the card id: {out!r}")

    orders = Stub(orders=[{"orderId": "100000000001", "objectType": "grocery",
                           "status": "DONE", "amount": 100, "created": "2026-08-01",
                           "fields": {"applicationId": "204",
                                      "applicationName": "ВкусВилл"}}])
    out2 = run(orders, server.orders)
    check("appId=204" in out2,
          f"grocery_order_status/cancel demand this appId and it was in the record: "
          f"{out2!r}")

    cart = Stub(grocery_cart_get={"cart": {"goods": [{"id": "1", "name": "Молоко",
                                                     "count": 1,
                                                     "price": {"value": 89.9}}],
                                          "goodsSum": 3144.0,
                                          "minOrderSum": 4000}})
    out3 = run(cart, server.grocery_cart, app_id="204", point_id="5980")
    check("3144" in out3,
          f"grocery_checkout demands expected_sum and the total is right here: {out3!r}")
    check("4000" in out3 and "не хватает" in out3,
          f"the store minimum decides whether checkout is possible at all: {out3!r}")
    print("  ids: card ucid/id, order appId and the cart total all reach the agent")


def test_an_id_of_the_wrong_kind_says_so():
    """The fetch is unscoped and filtered client-side, so a card id where an account
    id belongs matches nothing — and read as «this account had no activity»."""
    class Ops(MobileSession):
        def __init__(self):
            self._memo = {}

        def _call_read(self, key, **kw):
            return [{"id": "1", "account": "5045038535", "amount": {"value": 1}},
                    {"id": "2", "account": "5057770645", "amount": {"value": 2}}]

    try:
        Ops().list_operations("233849891", 0, 1)
        failures.append("a wrong-kind id read as an account with no operations")
    except TbankApiError as e:
        check(e.result_code == "NO_SUCH_ACCOUNT", f"wrong code: {e.result_code}")
        check("5045038535" in str(e),
              f"the accounts that DO have operations must be listed: {e}")
        check("list_accounts" in str(e),
              f"...and where the right id comes from: {e}")

    # A genuinely quiet account must still read as quiet, not as an error.
    class Quiet(Ops):
        def _call_read(self, key, **kw):
            return []

    check(Quiet().list_operations("5045038535", 0, 1) == [],
          "an account with no operations in the period must stay an empty list")
    print("  ids: a wrong-KIND id is named as such; a genuinely quiet account is not")


# ---- diagnostics ----------------------------------------------------------

def test_diagnostics_says_how_much_it_is_showing():
    path = os.environ["TBANK_EVENTS"]
    open(path, "w").close()
    for i in range(120):
        obs.emit("checkout", attempt_id=f"a{i}")
    out = server.diagnostics()
    head = out.splitlines()[0]
    check("120 всего" in head, f"the total must be stated: {head!r}")
    check("показано 40" in head, f"...and how many of it: {head!r}")
    check("limit=0" in head, f"...and how to get the rest: {head!r}")
    check(len(out.splitlines()) == 41, f"40 rows plus the header: {len(out.splitlines())}")
    check(len(server.diagnostics(limit=0).splitlines()) == 121,
          "limit=0 must show everything, like every other list tool here")
    print("  diagnostics: 120 events reported as 120, not silently as 40")


def test_a_venue_id_from_the_wrong_vertical_is_named_as_such():
    """Cinema venues and concert/theatre venues live in two id namespaces that do
    not mix, and the bank says so only by failing opaquely: a cinema id answers
    HTTP 500 from place/info, a concert id answers code 201 from the cinema
    schedule. Neither message names the cause, and the label an agent follows —
    `objectId=` — is the same word in both worlds, so it walks straight in.

    Verified live in both directions across five venues each: 10031/10210/10237
    (cinema) fail in place_info and work in cinema_schedule; 14419/9290/9530
    (concert, theatre) do the opposite."""
    class Vertical(MobileSession):
        def __init__(self, code):
            self._memo = {}
            self.code = code

        def ensure_fresh(self, *a, **kw):
            return None

        def place_info(self, *a, **kw):
            raise TbankApiError(self.code, "Сервис временно недоступен. Попробуйте позже.")

        def place_schedule(self, *a, **kw):
            raise TbankApiError(self.code, "Сервис временно недоступен. Попробуйте позже.")

        def cinema_schedule(self, *a, **kw):
            raise TbankApiError(self.code, "Сервис временно недоступен. Попробуйте позже.")

    for tool, args in ((server.place_info, ("10031",)),
                       (server.place_schedule, ("10031",))):
        out = run(Vertical("500"), tool, *args)
        check("КИНОТЕАТРА" in out,
              f"{tool.__name__}: a cinema id must be named as such: {out!r}")
        check("cinema_schedule" in out,
              f"{tool.__name__}: the tool that DOES serve it must be named: {out!r}")
        check("500" in out,
              f"{tool.__name__}: the bank's own answer must survive — the hint is a "
              f"guess about a cause, not a replacement for what happened: {out!r}")

    out = run(Vertical("201"), server.cinema_schedule, "", "2026-08-04",
              object_id="14419")
    check("afisha_places" in out and "movie" in out,
          f"the mirror case must name where a cinema id comes from: {out!r}")

    # An error with no object_id involved must NOT be blamed on the namespace.
    plain = run(Vertical("500"), server.cinema_schedule, "103693", "2026-08-04")
    check("КИНОТЕАТРА" not in plain and "afisha_places" not in plain,
          f"a failure without an object_id is not a namespace mix-up: {plain!r}")
    print("  venues: both id namespaces are named on failure, and only when relevant")


def test_the_venue_list_names_the_tool_its_ids_work_in():
    """afisha_places prints `objectId=` for every vertical, and that label is the
    argument name of a tool that only accepts half of them."""
    places = [{"id": "10031", "name": "Синема Парк", "address": "Ленинградское ш.",
               "subways": []}]

    class Places(MobileSession):
        def __init__(self):
            self._memo = {}

        def ensure_fresh(self, *a, **kw):
            return None

        def afisha_places(self, kind="movie", **kw):
            return places, len(places)

    out = run(Places(), server.afisha_places, "movie", "Москва")
    check("cinema_schedule(object_id" in out,
          f"a cinema listing must point at the tool its ids work in: {out!r}")
    check("place_info" not in out,
          f"...and must NOT point at the one that answers them with a 500: {out!r}")

    out2 = run(Places(), server.afisha_places, "concert", "Москва")
    check("place_info" in out2 and "place_schedule" in out2,
          f"a concert listing must point at the venue tools: {out2!r}")
    print("  venues: the listing names the next call per vertical")


def main():
    print("honest output:")
    test_a_capped_catalogue_scan_says_what_it_did_not_look_at()
    test_a_truncation_note_describes_the_order_it_actually_has()
    test_a_paging_hint_points_at_the_argument_that_works()
    test_flows_names_the_sections_it_dropped()
    test_flows_matches_whole_words()
    test_the_ids_the_next_call_needs_are_printed()
    test_an_id_of_the_wrong_kind_says_so()
    test_a_venue_id_from_the_wrong_vertical_is_named_as_such()
    test_the_venue_list_names_the_tool_its_ids_work_in()
    test_diagnostics_says_how_much_it_is_showing()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
