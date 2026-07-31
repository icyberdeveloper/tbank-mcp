"""Read tools must not lose records without saying so.

get_data / operations_histogram / invest_portfolio returned json.dumps(...)[:N] and
list_operations rendered ops[:50] with no count and no way to ask for more. Both
shapes lie by omission, and the agent has no way to notice:

  * On the real capture, get_data("merchant_subs") serializes to 5871 chars holding
    8 subscriptions. The 5000-char cut severed one mid-object and dropped another,
    leaving a string that still looks like data — the budget skill reads it, counts
    6, and under-reports the monthly subscription spend.
  * A 30-day list_operations returning 229 operations showed the newest 50 — about
    four days — with nothing in the output saying so, and operations 51+ were
    unreachable through any argument.

    python3 tests/test_truncation.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import server  # noqa: E402
from src.client import MobileSession  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def test_json_payload_keeps_whole_records():
    """A payload that does not fit must lose WHOLE records and say how many."""
    subs = {"subscriptions": [
        {"id": f"sub-{i}", "name": f"Подписка номер {i}", "amount": {"value": 100 + i},
         "merchant": {"title": f"Мерчант {i}", "logo": "https://x/" + "l" * 60}}
        for i in range(8)]}
    full = json.dumps(subs, ensure_ascii=False)
    check(len(full) > 900, f"fixture too small to trigger trimming ({len(full)})")

    out = server._json_out(subs, limit=900)
    check("ПОКАЗАНО" in out, f"truncation must be announced, got: {out[:120]}")

    body = out.split("\n", 1)[1]
    parsed = json.loads(body)          # the old [:N] made this impossible
    kept = parsed["subscriptions"]
    check(0 < len(kept) < 8, f"expected a partial list, got {len(kept)} of 8")
    check(f"{len(kept)} из 8" in out,
          f"the header must state how many of how many: {out.splitlines()[0]}")
    for rec in kept:
        check(set(rec) == {"id", "name", "amount", "merchant"},
              f"a kept record was cut apart: {rec}")
    check("не считай" in out.lower(),
          "the agent must be told not to compute totals from a partial answer")

    # Fits → returned untouched and parseable, with no scary header.
    small = server._json_out({"a": 1}, limit=900)
    check(small == '{"a": 1}', f"a payload that fits must be untouched, got {small!r}")


def test_json_out_zero_means_no_cap():
    """limit=0 is «everything», like every list tool. Before the guard it fell
    through the <= checks and answered «ОТВЕТ ОБРЕЗАН: 0 из N» with an empty body."""
    blob = {"records": [{"id": i, "pad": "x" * 50} for i in range(100)]}
    out = server._json_out(blob, limit=0)
    check(not out.startswith("#"), f"limit=0 must not announce truncation: {out[:80]}")
    check(json.loads(out) == blob, "limit=0 must return the whole payload, parseable")


def test_cut_marks_what_it_removes():
    """_cut is the one column cutter: silent when nothing is lost, «…» when cut,
    n<=0 = never cut."""
    check(server._cut("короткое", 40) == "короткое", "an uncut string must be untouched")
    long = "Перевод от ИП Иванов Иван Иванович за консультационные услуги"
    cut = server._cut(long, 40)
    check(len(cut) == 40 and cut.endswith("…"),
          f"a cut string must fit the column and end with the marker: {cut!r}")
    check(server._cut(long, 0) == long, "n=0 must mean no cut at all")
    check(server._cut(None, 10) == "", "None must render as empty, not 'None'")


def test_untrimmable_payload_is_flagged_loudly():
    """No list to trim: the text is still cut, but it must be impossible to mistake
    for a complete answer."""
    blob = {"description": "x" * 5000}
    out = server._json_out(blob, limit=200)
    check(out.startswith("# ОТВЕТ ОБРЕЗАН"), f"missing the loud marker: {out[:80]}")
    check("НЕ валидный JSON" in out,
          "the agent must be told the remainder does not parse")
    check("5" in out.split("\n")[0], "the header should carry the real size")


def trimmed_body(out):
    """The parsed payload of a «ПОКАЗАНО …» answer, or None if _json_out fell back
    to the character cut. Returning None rather than raising is what lets the test
    report «it fell through» instead of dying inside json.loads."""
    if not out.startswith("# ПОКАЗАНО"):
        return None
    try:
        return json.loads(out.split("\n", 1)[1])
    except (ValueError, IndexError):
        return None


def test_trimming_handles_shapes_the_single_pass_gave_up_on():
    """Two ordinary payloads used to fall through to the character cut even though
    dropping whole records would have fitted — the worst of both: nothing parses AND
    records are lost."""
    # (a) Sibling lists of comparable size. Shrinking only the biggest one never gets
    # under the limit, and a single pass had nothing else to try.
    wide = {f"группа{g}": [{"id": f"{g}-{i}", "name": "Запись " + "я" * 40}
                           for i in range(20)] for g in range(4)}
    out = server._json_out(wide, limit=1500)
    parsed = trimmed_body(out)
    check(parsed is not None,
          f"a payload of several lists fell through to the char cut: {out[:110]}")
    if parsed is not None:
        check(sum(len(v) for v in parsed.values()) < 80, "nothing was actually dropped")
        for name, lst in parsed.items():
            for rec in lst:
                check(set(rec) == {"id", "name"}, f"{name}: record cut apart: {rec}")
        check("из 20" in out,
              f"the header must state the real per-list totals: {out[:200]}")

    # (b) The payload IS the list. Its path is (), which _set_in cannot address, so a
    # bare list — what several get_data sections return — was always character-cut.
    rows = [{"id": i, "name": "Операция " + "я" * 40} for i in range(60)]
    out2 = server._json_out(rows, limit=1200)
    kept = trimmed_body(out2)
    check(kept is not None,
          f"a top-level list fell through to the char cut: {out2[:110]}")
    if kept is not None:
        check(isinstance(kept, list) and 0 < len(kept) < 60,
              f"expected a partial list of records, got {type(kept).__name__}")
        check("из 60" in out2, f"the header must say how many of how many: {out2[:200]}")
        check(all(set(r) == {"id", "name"} for r in kept), "a kept record was cut apart")


def test_list_tools_report_the_total_they_are_hiding():
    rows = [{"n": i} for i in range(229)]
    out = server._rows_out(rows, lambda r: f"- {r['n']}", limit=50, total=len(rows),
                           header="[account X] операции за 30 дн.")
    head = out.splitlines()[0]
    check("229 всего" in head, f"the real total must be in the header: {head}")
    check("показано 50" in head, f"the shown count must be in the header: {head}")
    check("limit=229" in head, f"the header must say how to get the rest: {head}")
    check(len(out.splitlines()) == 51, f"expected header + 50 rows, got {len(out.splitlines())}")

    # limit=0 means everything, and then there is nothing to warn about.
    every = server._rows_out(rows, lambda r: f"- {r['n']}", limit=0, total=len(rows),
                             header="h")
    check(len(every.splitlines()) == 230, "limit=0 must render every row")
    check("limit=" not in every.splitlines()[0],
          f"a complete answer must not nag about limit: {every.splitlines()[0]}")

    # Nothing hidden → no misleading "новые сверху" promise either.
    few = server._rows_out(rows[:3], lambda r: f"- {r['n']}", limit=50, total=3, header="h")
    check("показано 3" in few and "limit=" not in few.splitlines()[0], few.splitlines()[0])


class OpsSession(MobileSession):
    def __init__(self, n):
        self.n = n

    def ensure_fresh(self, *a, **kw):
        return None

    def list_operations(self, account_id, start, end):
        return [{"operationTime": {"milliseconds": 1784658904000 - i * 3600_000},
                 "type": "Debit", "amount": {"value": 100 + i, "currency": {"name": "RUB"}},
                 "description": f"Покупка {i}"} for i in range(self.n)]


def test_list_operations_end_to_end():
    """Through the real tool: the header must expose the truncation, and limit must
    actually widen the window."""
    saved = server._require
    server._require = lambda: OpsSession(229)
    try:
        out = server.list_operations("0000000000", days=30)
        head = out.splitlines()[0]
        check("229 всего" in head and "показано 50" in head,
              f"list_operations hides its truncation: {head}")
        check(len(out.splitlines()) == 51, f"expected 50 rows, got {len(out.splitlines()) - 1}")

        wide = server.list_operations("0000000000", days=30, limit=0)
        check(len(wide.splitlines()) == 230,
              f"limit=0 must return every operation, got {len(wide.splitlines()) - 1}")

        exact = server.list_operations("0000000000", days=30, limit=229)
        check("limit=" not in exact.splitlines()[0],
              "asking for exactly the total must not still nag about limit")
    finally:
        server._require = saved


class RowsSession(MobileSession):
    """A card's operations and the client's orders, in the shapes the tools parse."""

    def __init__(self, n):
        self.n = n

    def ensure_fresh(self, *a, **kw):
        return None

    def list_operations(self, account_id, start, end):
        return [{"operationTime": {"milliseconds": 1784658904000 - i * 3600_000},
                 "type": "Debit", "card": "100000002",
                 "amount": {"value": 100 + i, "currency": {"name": "RUB"}},
                 "description": f"Покупка {i}"} for i in range(self.n)]

    def orders(self):
        return [{"orderId": f"o-{i}", "objectType": "grocery", "status": "DONE",
                 "created": f"2026-07-{(i % 28) + 1:02d}", "amount": 100 + i,
                 "fields": {"applicationName": "ВкусВилл"}} for i in range(self.n)]


def test_limit_zero_means_everything_in_every_list_tool():
    """`limit=0` is «покажи всё» in list_operations, and the docstrings say so — but
    card_operations and orders sliced with a bare rows[:limit], where 0 means the
    opposite. An agent asking for the complete answer got an empty one, under a
    header that still announced the full count."""
    saved = server._require
    server._require = lambda: RowsSession(120)
    try:
        for name, call in (("card_operations",
                            lambda lim: server.card_operations("100000002", 30, lim)),
                           ("orders", lambda lim: server.orders("", lim))):
            every = call(0)
            rows = [ln for ln in every.splitlines() if ln.startswith("- ")]
            check(len(rows) == 120,
                  f"{name}(limit=0) returned {len(rows)} rows, expected all 120 "
                  f"— 0 read as «ничего»?")
            head_all = (every.splitlines() or [""])[0]
            check("limit=" not in head_all,
                  f"{name} must not nag about limit when it showed everything: "
                  f"{head_all!r}")

            few = call(5)
            head = (few.splitlines() or [""])[0]
            shown = [ln for ln in few.splitlines() if ln.startswith("- ")]
            check(len(shown) == 5, f"{name}(limit=5) returned {len(shown)} rows")
            check("120 всего" in head and "показано 5" in head,
                  f"{name} must say what it is hiding: {head!r}")
            check("limit=120" in head, f"{name} must say how to get the rest: {head!r}")
    finally:
        server._require = saved
    print("  limit=0 means «all» in card_operations and orders, as in list_operations")


class AnySession(MobileSession):
    """Answers whatever the tool under test asks for with one canned value."""

    def __init__(self, **answers):
        self._memo = {}
        for name, value in answers.items():
            setattr(self, name, (lambda v: (lambda *a, **kw: v))(value))

    def ensure_fresh(self, *a, **kw):
        return None

    def ensure_client_session(self, *a, **kw):
        return None


# One payload big enough to overflow every limit in use (1000 … 5000 chars).
BIG = {"records": [{"id": f"r-{i}", "name": "Запись " + "я" * 50, "amount": 100 + i}
                   for i in range(200)]}
MANY = [{"date": f"2026-07-{(i % 28) + 1:02d}", "amount": {"value": 100 + i},
         "description": f"Операция номер {i}"} for i in range(200)]

# Every tool that can answer with more than it shows. The point is coverage of the
# CALL SITES: _json_out and _rows_out were pinned by direct unit tests plus a single
# tool, so reverting get_data or invest_operations to a bare slice broke nothing.
JSON_TOOLS = [
    ("session_status", lambda: server.session_status(),
     AnySession(session_status=BIG)),
    ("operations_histogram", lambda: server.operations_histogram("1111111111", 30),
     AnySession(operations_histogram=BIG)),
    ("get_data", lambda: server.get_data("subscriptions"),
     AnySession(get_data=BIG)),
    ("payment_commission",
     lambda: server.payment_commission('{"payParameters": {"moneyAmount": 1}}'),
     AnySession(payment_commission=BIG)),
    ("invest_portfolio", lambda: server.invest_portfolio("2000000001", 30),
     AnySession(invest_portfolio=BIG)),
]
ROW_TOOLS = [
    ("list_operations", lambda lim: server.list_operations("1111111111", 30, lim),
     AnySession(list_operations=MANY)),
    ("invest_operations", lambda lim: server.invest_operations("2000000001", "", lim),
     AnySession(invest_operations=(MANY, False))),
    ("cinema_search", lambda lim: server.cinema_search("", "Москва", lim),
     AnySession(cinema_movies=([{"name": f"Фильм {i}", "eventId": str(i)}
                                for i in range(200)], 200, 200))),
]


def test_provider_page_is_printed_whole_and_notfound_names_its_boundary():
    """The provider listing used to render provs[:60] under a header that counted
    BEFORE the slice — a full page claimed «100 из N» while printing 60. And a
    provider_id miss after 5 pages of 1026 read as an unconditional «не найден»."""
    page = {"providers": [{"id": f"p-{i}", "name": f"Провайдер {i}"} for i in range(100)],
            "page": 1, "totalPages": 1026, "totalProviders": 102571}
    session = AnySession(providers_compatible_page=page)
    saved = server._require
    server._require = lambda: session
    try:
        out = server.payment_providers(group="ЖКХ")
        rows = [ln for ln in out.splitlines() if ln.startswith("- ")]
        head = out.splitlines()[0]
        check(len(rows) == 100,
              f"a 100-provider page must print whole, got {len(rows)} rows")
        check("100 из 102571" in head, f"the header must count what is printed: {head!r}")
        check("page=2" in head, f"the header must chain the next page: {head!r}")

        miss = server.payment_providers(group="ЖКХ", provider_id="нет-такого")
        check("из 1026" in miss and "5 страниц" in miss,
              f"a not-found after an incomplete scan must name the boundary: {miss!r}")
        check("pages=" in miss, f"the miss must name the widening argument: {miss!r}")
    finally:
        server._require = saved
    print("  payment_providers: the page prints whole, a bounded miss says so")


def test_invest_has_next_is_announced_even_at_limit_zero():
    """When the bank says hasNext, the HEADER says so — including at limit=0, where
    every fetched row is shown and more_hint would stay silent. No cursor is sent:
    a bigger limit is the only capture-backed way to ask for more."""
    session = AnySession(invest_operations=(MANY[:50], True))
    saved = server._require
    server._require = lambda: session
    try:
        out = server.invest_operations("2000000001", "", 0)
        head = out.splitlines()[0]
        check("у банка есть ещё" in head and "limit" in head,
              f"hasNext must surface in the header with the recovery arg: {head!r}")
        check(len([ln for ln in out.splitlines() if ln.startswith("- ")]) == 50,
              "limit=0 must still render every fetched row")
    finally:
        server._require = saved
    print("  invest_operations: hasNext reaches the header even when nothing is hidden")


def test_invest_operations_never_calls_a_bank_confirmed_partial_fetch_a_total():
    """limit=5 against a bank that says hasNext=True used to print "5 всего,
    показано 5" (from _rows_out, fed len(ops) as the total) right next to "у
    банка есть ещё" — claiming a limit-bounded fetch was complete and
    incomplete in the same header."""
    session = AnySession(invest_operations=(MANY[:5], True))
    saved = server._require
    server._require = lambda: session
    try:
        head = server.invest_operations("2000000001", "", 5).splitlines()[0]
        check("всего" not in head,
              f"a fetch the bank itself says is partial must never be called a total: {head!r}")
        check("у банка есть ещё" in head, f"hasNext must still be surfaced: {head!r}")
    finally:
        server._require = saved
    print("  invest_operations: a hasNext fetch is never mislabeled as a complete total")


def test_messenger_messages_window_is_honest():
    """The chat history is paged LOCALLY over the one page the bank returns: the
    header states page size and window, offset walks toward older messages, and a
    long message is cut WITH a marker naming max_chars=0 — this is the exact spot
    that used to swallow the tail of long bank messages silently (text[:400])."""
    long_text = "Важное сообщение банка " + "х" * 1000
    msgs = [{"timestamp": f"2026-07-01T10:{i:02d}:00Z",
             "author": {"name": "Банк"},
             "content": {"text": long_text if i == 59 else f"Сообщение {i}"}}
            for i in range(60)]
    calls = []
    session = AnySession()
    session.messenger_messages = lambda *a, **kw: (calls.append(a), msgs)[1]

    saved = server._require
    server._require = lambda: session
    try:
        out = server.messenger_messages("c-1")
        head = out.splitlines()[0]
        rows = [ln for ln in out.splitlines() if ln.startswith("- ")]
        check("60 сообщений" in head and "показано 20" in head,
              f"the header must state page and window: {head!r}")
        check(len(rows) == 20, f"default window must hold 20 rows, got {len(rows)}")
        check("Сообщение 40" in out, "the default window must hold the NEWEST messages")
        check("offset=20" in head, f"the header must say how to go older: {head!r}")
        check(long_text not in out, "a long message must not print whole by default")
        check(f"всего {len(long_text)} симв." in out and "max_chars=0" in out,
              "a cut message must name its full length and the way to get it")

        older = server.messenger_messages("c-1", 20, 20)
        orows = [ln for ln in older.splitlines() if ln.startswith("- ")]
        check(len(orows) == 20 and "Сообщение 39" in older and "Сообщение 20" in older,
              f"offset=20 must show the next-older window: {older.splitlines()[0]!r}")
        check("Сообщение 40" not in older and "Сообщение 19" not in older,
              "the offset window must not leak neighbours")
        check("offset=40" in older.splitlines()[0],
              "the header must chain to the next window")

        every = server.messenger_messages("c-1", 0)
        check(len([ln for ln in every.splitlines() if ln.startswith("- ")]) == 60,
              "limit=0 must render the whole page")

        full = server.messenger_messages("c-1", 20, 0, 0)
        check(long_text in full, "max_chars=0 must print the message whole")
        check("обрезано" not in full, "an uncut message must carry no cut marker")

        check(len(calls) == 4,
              f"each call must cost exactly one page fetch, got {len(calls)}")
    finally:
        server._require = saved
    print("  messenger_messages: honest local window over the bank's page, marked cuts")


def test_every_json_tool_trims_by_records_not_by_characters():
    saved = server._require
    try:
        for name, call, session in JSON_TOOLS:
            server._require = lambda s=session: s
            out = call()
            check(out.startswith("# ПОКАЗАНО"),
                  f"{name} did not go through _json_out — a raw slice? {out[:100]!r}")
            parsed = trimmed_body(out)
            check(parsed is not None, f"{name} returned unparseable JSON: {out[:100]!r}")
            if parsed is not None:
                kept = len(parsed.get("records", []))
                check(0 < kept < 200, f"{name} kept {kept} of 200 records")
                check(f"из 200" in out,
                      f"{name} must state the true total: {out.splitlines()[0]}")
    finally:
        server._require = saved
    print(f"  {len(JSON_TOOLS)} JSON tools trim whole records and report the total")


def test_every_row_tool_reports_what_it_hides():
    saved = server._require
    try:
        for name, call, session in ROW_TOOLS:
            server._require = lambda s=session: s
            head = (call(5).splitlines() or [""])[0]
            check("200 всего" in head and "показано 5" in head,
                  f"{name} hides its truncation: {head!r}")
            # Either wording is fine — cinema_search suggests narrowing the query
            # instead of asking for 200 films — as long as the agent is told of a
            # concrete argument that widens the window.
            check("limit=200" in head or "limit=0" in head,
                  f"{name} does not say how to get the rest: {head!r}")
            rows = [ln for ln in call(0).splitlines() if ln.startswith("- ")]
            check(len(rows) == 200,
                  f"{name}(limit=0) returned {len(rows)} rows, expected all 200")
    finally:
        server._require = saved
    print(f"  {len(ROW_TOOLS)} row tools state their total and honour limit=0")


def _cinema_venue(i):
    return {"info": {"objectName": f"Кино {i}", "objectId": str(i),
                     "geo": {"address": f"ул. {i}", "distance": i * 100}},
           "events": [{"eventName": "Фильм", "slots": [
               {"startTime": "18:00", "prices": {"fix": 300},
                "hallName": "Зал 1", "slotId": f"s{i}"}]}]}


def test_cinema_schedule_caps_a_citywide_dump_honestly():
    """A city-wide call with no cinema/around filter can return hundreds of
    venues in one response (133 cinemas, confirmed live) — cinema_schedule had
    no limit at all, unlike every other listing tool in this file."""
    venues = [_cinema_venue(i) for i in range(30)]
    saved = server._require
    server._require = lambda: AnySession(cinema_schedule=venues)
    try:
        out = server.cinema_schedule("1", "2026-08-01", city="Москва")
        head = out.splitlines()[0]
        check("30 площадок" in head, f"the honest total must be announced: {head}")
        check("показано 20" in head, f"the default cap is 20: {head}")
        check(out.count("objectId=") == 20,
              f"only the capped count may render: {out.count('objectId=')}")

        out_all = server.cinema_schedule("1", "2026-08-01", city="Москва", limit=0)
        check(out_all.count("objectId=") == 30,
              f"limit=0 must show every venue: {out_all.count('objectId=')}")
    finally:
        server._require = saved
    print("  cinema_schedule: a city-wide dump is capped with an honest header, limit=0 shows all")


def _concert_venue(i):
    return {"info": {"objectName": f"Зал {i}", "objectId": str(i),
                     "geo": {"address": f"пр. {i}"}},
           "events": [{"slots": [{"startDateTime": "2026-08-01T19:00",
                                   "prices": {"fix": 500}, "slotId": f"s{i}"}]}]}


def test_concert_schedule_caps_a_touring_events_venues_honestly():
    """concert_schedule has no date filter at all (every future showing comes
    back at once), making it the more exposed of the two schedule tools to an
    unbounded venue list for a touring show."""
    venues = [_concert_venue(i) for i in range(20)]
    saved = server._require
    server._require = lambda: AnySession(event_showings=venues)
    try:
        out = server.concert_schedule("1")
        head = out.splitlines()[0]
        check("20 площадок всего" in head, f"the honest total must be announced: {head}")
        check("показано 15" in head, f"the default cap is 15: {head}")
        check(out.count("objectId=") == 15,
              f"only the capped count may render: {out.count('objectId=')}")

        out_all = server.concert_schedule("1", limit=0)
        check("площадок всего" not in out_all,
              f"limit=0 must not print a truncation header: {out_all.splitlines()[:1]}")
        check(out_all.count("objectId=") == 20,
              f"limit=0 must show every venue: {out_all.count('objectId=')}")
    finally:
        server._require = saved
    print("  concert_schedule: an unbounded touring-event dump is capped, limit=0 shows all")


def test_real_capture_payload_survives():
    """The concrete case from the audit: 8 subscriptions must not become 6."""
    cap = os.environ.get("TBANK_CAPTURE", os.path.expanduser("~/tbank-app/captures.xml"))
    if not os.path.exists(cap):
        print("  real capture: SKIPPED (capture absent — synthetic cases above still ran)")
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import test_cart_body_matches_capture as C

    C.CAPTURE = cap
    items = C._items()
    payload = None
    for i, _ in enumerate(items):
        try:
            body = C.response_json(items, i)
        except Exception:
            continue
        p = body.get("payload") if isinstance(body, dict) else None
        if isinstance(p, dict) and isinstance(p.get("subscriptions"), list) \
                and len(p["subscriptions"]) >= 4:
            payload = p
            break
    if payload is None:
        print("  real capture: no merchant_subs payload found, skipped")
        return
    n = len(payload["subscriptions"])
    out = server._json_out(payload, 5000)
    if out.startswith("#"):
        kept = json.loads(out.split("\n", 1)[1])["subscriptions"]
        check(f"{len(kept)} из {n}" in out,
              "the trimmed real payload must state the true count")
        json.loads(out.split("\n", 1)[1])          # must still parse
        print(f"  real capture: {n} subscriptions → {len(kept)} kept, count reported")
    else:
        check(json.loads(out) == payload, "an untrimmed payload must round-trip")
        print(f"  real capture: {n} subscriptions fit whole, nothing dropped")


def main():
    print("truncation honesty:")
    test_json_payload_keeps_whole_records()
    test_json_out_zero_means_no_cap()
    test_cut_marks_what_it_removes()
    test_untrimmable_payload_is_flagged_loudly()
    test_trimming_handles_shapes_the_single_pass_gave_up_on()
    test_list_tools_report_the_total_they_are_hiding()
    test_list_operations_end_to_end()
    test_limit_zero_means_everything_in_every_list_tool()
    test_provider_page_is_printed_whole_and_notfound_names_its_boundary()
    test_invest_has_next_is_announced_even_at_limit_zero()
    test_invest_operations_never_calls_a_bank_confirmed_partial_fetch_a_total()
    test_messenger_messages_window_is_honest()
    test_every_json_tool_trims_by_records_not_by_characters()
    test_every_row_tool_reports_what_it_hides()
    test_cinema_schedule_caps_a_citywide_dump_honestly()
    test_concert_schedule_caps_a_touring_events_venues_honestly()
    test_real_capture_payload_survives()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
