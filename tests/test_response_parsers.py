"""The server-side parsers that turn a bank payload into what the agent reads.

Two past fixes lived here and neither was pinned:

  45d50fb  grocery_order_status reported EVERY order as unpaid with an unknown sum.
           It read order.paymentInfo.paid and a top-level order.sum; neither exists.
           The real schema is order.{id,status,paymentId,cart{sum,goodsSum}} — and
           CREATED_DYNAMIC is the normal status of a placed order, not a failure.
  8a9f90f  messenger listings cut the conversationId to 24 chars with an ellipsis,
           so the id could not be passed to messenger_messages; bot chats showed as
           "?" because their name lives on the member, not on a title.

A parser that misreads a field does not crash — it produces a confident wrong
answer, which is why these need tests rather than eyeballing.

    python3 tests/test_response_parsers.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import server  # noqa: E402
from src.client import MobileSession  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


class Stub(MobileSession):
    """Answers the named methods with canned payloads.

    Methods are bound explicitly rather than through __getattr__: that catches every
    missing attribute, so a dataclass field the tool happens to touch turns into an
    AttributeError the tool swallows into an error string — the test then "fails" for
    a reason that has nothing to do with the parser."""

    def __init__(self, **answers):
        self.mobile_sessionid = "sid"
        self.access_token = "tok"
        self._memo = {}
        for name, value in answers.items():
            setattr(self, name, (lambda v: (lambda *a, **kw: v))(value))

    def ensure_fresh(self, *a, **kw):
        return None


def run(tool, session, *args, **kw):
    saved = server._require
    server._require = lambda: session
    try:
        return tool(*args, **kw)
    finally:
        server._require = saved


# The shape of a genuinely placed and paid order (capture item 691).
PAID_ORDER = {"payload": {"order": {
    "id": "70123456", "status": "CREATED_DYNAMIC", "paymentId": "100000000001",
    "application": {"id": "204", "name": "ВкусВилл"},
    "cart": {"sum": 1600.2, "goodsSum": 1630.0, "goods": [{"id": "1"}]},
}}}


def test_a_paid_order_does_not_read_as_unpaid():
    out = run(server.grocery_order_status, Stub(grocery_order_get=PAID_ORDER), "70123456", "204")
    check("paid=yes" in out, f"an order with a paymentId must read as paid: {out}")
    check("sum=1600.2" in out, f"the sum must come from cart.sum: {out}")
    check("CREATED_DYNAMIC" in out,
          f"the real status must be shown, not translated into a failure: {out}")
    check("ВкусВилл" in out, f"the store must be named: {out}")
    check("100000000001" in out, f"the paymentId must be shown for reconciliation: {out}")

    # No paymentId → honestly unpaid, and the sum still resolves.
    unpaid = {"payload": {"order": {"id": "70123457", "status": "NEW",
                                    "cart": {"goodsSum": 500.0}}}}
    out2 = run(server.grocery_order_status, Stub(grocery_order_get=unpaid), "70123457", "204")
    check("paid=no" in out2, f"an order without a paymentId is unpaid: {out2}")
    check("sum=500.0" in out2, f"goodsSum must be the fallback: {out2}")

    # An empty/odd payload must degrade, not raise.
    for junk in ({}, {"payload": None}, {"payload": {"order": "nope"}}):
        got = run(server.grocery_order_status, Stub(grocery_order_get=junk), "x", "204")
        check("Traceback" not in got and got.strip(),
              f"a malformed order payload must degrade gracefully: {got!r}")

    # app_id is required in practice (the live API 400s without it) despite its
    # empty-string default in the signature — omitting it must say so plainly
    # instead of making the call and surfacing the bank's generic 400.
    no_app_id = run(server.grocery_order_status, Stub(grocery_order_get=PAID_ORDER), "70123456")
    check("app_id" in no_app_id and "обязателен" in no_app_id,
          f"omitting app_id must explain why, not hit the API blind: {no_app_id!r}")
    print("  grocery_order_status: paid/unpaid, sum and status read from the real schema")


def test_grocery_order_status_does_not_cut_the_goods_names():
    """The cart.goods listing (added alongside the app_id fix) used a bare
    name[:35] slice, no "…" mark — the exact same unmarked cut grocery_cart and
    grocery_set_cart had, introduced fresh while fixing something else, by
    copying their already-broken pattern instead of the uncut grocery_search
    convention."""
    order = {"payload": {"order": {
        "id": "1", "status": "CREATED_DYNAMIC", "paymentId": "p1",
        "application": {"name": "ВкусВилл"},
        "cart": {"sum": 500, "goods": [
            {"name": "Сыр Бри с белой плесенью выдержанный 60% Франция",
             "count": 1, "price": {"value": 538.0}}]},
    }}}
    out = run(server.grocery_order_status, Stub(grocery_order_get=order), "1", "204")
    check("60%" in out and "Франция" in out,
          f"the full goods name, not just its first 35 chars, must be shown: {out!r}")
    print("  grocery_order_status: goods names are not cut")


def test_conversation_ids_survive_intact():
    long_id = "c" * 64
    convs = [
        {"conversationId": long_id, "title": "Поддержка",
         "unreadMessagesCount": 2, "updatedAt": "2026-07-25T10:00:00Z",
         "message": {"content": {"text": "Здравствуйте!   Чем помочь?"}}},
        {"conversationId": "bot-1", "members": [{"name": "Бот доставки"}],
         "botInfo": {"login": "delivery_bot"}, "updatedAt": "2026-07-25T09:00:00Z"},
    ]
    out = run(server.messenger_conversations, Stub(messenger_conversations=convs))
    check(long_id in out,
          "the conversationId was truncated — it is the argument to messenger_messages")
    # Written as `… if "id=" in out else True`, this could not fail: rename the label
    # and the assertion evaluates to True having checked nothing — the one shape a
    # test must never have. Assert the ids are THERE, then that they are intact.
    ids = re.findall(r"id=(\S+)", out)
    check(len(ids) == 2, f"every chat must be printed with its id, got {ids}")
    check(all("…" not in i and "..." not in i for i in ids),
          f"an id was elided — it cannot be passed to messenger_messages: {ids}")
    check(ids[:1] == [long_id],
          f"the id must be the conversationId verbatim: {ids[:1]}")
    check("Поддержка" in out, f"the chat title must be shown: {out}")
    check("Бот доставки" in out,
          f"a bot chat has no title — its name comes from the member: {out}")
    check("непрочитано: 2" in out, f"the unread count must be surfaced: {out}")

    empty = run(server.messenger_conversations, Stub(messenger_conversations=[]))
    check("Чатов нет" in empty, f"an empty list must say so, not return '': {empty!r}")
    print("  messenger_conversations: full ids, bot names, unread counts, honest empty")


def test_a_dead_messenger_token_is_renewed_not_displayed_as_a_chat():
    """The messenger answers an expired token with HTTP 200 and an error object in
    the BODY — a LIST, so it flowed through as if it were the conversations and the
    tool printed «- ? | id= |»: one chat with no id, no name and nothing to retry.

    Found by calling every read tool against the live API twice, 25 minutes apart:
    the second run returned that row. _tmsg_expired() only decodes the token's own
    `exp`, so a token the SERVER retired early still looks valid locally and no
    re-mint is attempted."""
    DEAD = [{"errorId": "99c4bb", "errorCode": "AUTH_REQUIRED",
             "errorMessage": "Token inactive"}]
    ALIVE = [{"conversationId": "c-1", "title": "Поддержка",
              "updatedAt": "2026-07-25T10:00:00Z"}]

    class Tmsg(Stub):
        def __init__(self, answers):
            super().__init__()
            self.answers = list(answers)
            self.tmsg_session_id = "jwt.header.payload"
            self.remints = 0

        def _ensure_tmsg(self):
            self.remints += 1
            self.tmsg_session_id = "fresh"

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            return self.answers.pop(0) if self.answers else ALIVE

    s = Tmsg([DEAD, ALIVE])
    out = run(server.messenger_conversations, s)
    check("Поддержка" in out,
          f"the retry after a re-mint must return the real chats: {out!r}")
    check("id= " not in out and "- ? " not in out,
          f"an error envelope was rendered as a conversation: {out!r}")
    check(s.remints == 1, f"exactly one re-mint expected, got {s.remints}")
    check(s.tmsg_session_id == "fresh", "the dead token was not replaced")

    # Still dead after re-minting → say so, actionably. Never a fake chat.
    s2 = Tmsg([DEAD, DEAD])
    out2 = run(server.messenger_conversations, s2)
    check("SESSION EXPIRED" in out2 or "refresh_session" in out2,
          f"a token that stays dead must point at the recovery tool: {out2!r}")
    check("Token inactive" in out2, f"the bank's reason must survive: {out2!r}")
    print("  messenger: a retired token is re-minted once, never shown as a chat")


def test_documents_merge_and_lists():
    """Three losses documents() used to make silently: list-valued entered fields
    (licence categories, OSAGO drivers) were dropped by the dict-only flattener;
    duplicate copies kept only the field-richest one, losing fields unique to the
    poorer copy; and `name` was always hidden although for an unknown code it is
    the only human-readable label."""
    wrap = lambda v: {"isEntered": True, "value": v}          # noqa: E731
    docs = {
        "RusDriversLic": [
            {"value": {"serial": wrap("77"), "number": wrap("123"),
                       "categories": wrap(["B", "B1", "M"]),
                       "person": {"birthDate": wrap("1990-01-01")}}},
            # Same licence from another source: fewer fields, one unique.
            {"value": {"serial": wrap("77"), "number": wrap("123"),
                       "issueDate": wrap("2020-05-01"),
                       "person": {"birthDate": wrap("1990-01-01")}}},
        ],
        "SomeNewCode": [
            {"value": {"number": wrap("42"), "name": wrap("Карта болельщика")}},
        ],
    }

    class DocStub(Stub):
        def ensure_client_session(self, *a, **kw):
            return None

    out = run(server.documents, DocStub(
        identity_documents=docs,
        identity_brief={"birthDate": {"value": "1990-01-01"}}))
    check("B, B1, M" in out,
          f"an entered list field must survive the flattener: {out!r}")
    check("issueDate = 2020-05-01" in out,
          f"a field unique to the poorer duplicate must survive the merge: {out!r}")
    check(out.count("Водительское удостоверение:") == 1,
          f"duplicates must merge into one document, not two: {out!r}")
    check("Карта болельщика" in out,
          f"name must print when the title is a raw code: {out!r}")
    print("  documents: list fields kept, duplicates merged, unknown codes keep their name")


def test_inn_header_shows_the_number_and_duplicate_copies_merge():
    """ИНН has no serial/number/serialAndNumber — its value lives in `inn` — so the
    header used to always print «ИНН: —» even though the real number was right
    there in the body. Two real copies of the same ИНН (one with person fields,
    one without) also failed to merge: the old key required person.lastName/
    birthDate to match, and the person-less copy has neither."""
    wrap = lambda v: {"isEntered": True, "value": v}          # noqa: E731
    docs = {
        "RusINN": [
            {"value": {"inn": wrap("744922535413"),
                       "person": {"birthDate": wrap("1990-01-01"),
                                  "lastName": wrap("Исламов")}}},
            # Same ИНН from another source: no person fields at all.
            {"value": {"inn": wrap("744922535413")}},
        ],
    }

    class DocStub(Stub):
        def ensure_client_session(self, *a, **kw):
            return None

    out = run(server.documents, DocStub(
        identity_documents=docs,
        identity_brief={"birthDate": {"value": "1990-01-01"}}))
    check("ИНН: 744922535413" in out,
          f"the header must show the real inn, not a bare '—': {out!r}")
    check(out.count("ИНН:") == 1,
          f"the person-less copy must merge with the one that has fields, not "
          f"read as a second document: {out!r}")
    check("inn = 744922535413" not in out,
          f"inn is already in the header — it must not also repeat in the body: {out!r}")
    print("  documents: ИНН header shows the number, and person-less duplicates merge")


def test_grocery_search_header_is_honest():
    """The tool header must separate three different numbers: shown, matched, and
    what the store returned at all — «10 товаров» that silently came out of 25
    matches is how the old output read as complete."""
    rows = [{"id": str(i), "name": f"Йогурт {i}", "price": 50 + i, "weight": "",
             "likely_raw": True} for i in range(10)]
    out = run(server.grocery_search, Stub(grocery_search=(rows, 25, 30)),
              "йогурт", "204", "5980")
    head = out.splitlines()[0]
    check("показано 10 из 25" in head and "вернула 30" in head,
          f"the header must say shown/matched/fetched: {head!r}")
    check("limit=0" in head, f"the header must say how to see the rest: {head!r}")
    empty = run(server.grocery_search, Stub(grocery_search=([], 0, 0)),
                "йогурт", "204", "5980")
    check("Не нашёл" in empty, f"an empty search must stay honest: {empty!r}")
    print("  grocery_search: the header separates shown / matched / fetched")


def test_messenger_paging_arguments_reach_the_client():
    """messenger_conversations(offset=, archived=) must reach the wire as the same
    query params the app sends on every call today (offset / use_is_archived), and
    messenger_messages must stay the app's exact request — no invented params."""
    seen = []

    class Wire(MobileSession):
        def __init__(self):
            self.tmsg_session_id = "tok"
            self._memo = {}

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            seen.append((path_override, dict(overrides or {})))
            return []

    w = Wire()
    w.messenger_conversations(archived=True, offset=30)
    path, ov = seen[-1]
    check(path.endswith("/conversations/mobile"),
          f"the chat list must keep its capture path: {path!r}")
    check(ov.get("offset") == "30", f"offset must ride the request: {ov}")
    check(ov.get("use_is_archived") == "true",
          f"archived must ride as use_is_archived: {ov}")

    w.messenger_messages("c-9")
    path2, ov2 = seen[-1]
    check(path2.endswith("/conversations/c-9/messages"),
          f"the history must keep its capture path: {path2!r}")
    check(ov2.get("direction") == "before" and "messageId" not in ov2,
          f"history is paged locally — no unconfirmed params on the wire: {ov2}")
    print("  messenger paging: offset/use_is_archived reach the wire, nothing invented")


def test_the_cart_prints_the_ids_it_must_be_edited_by():
    """grocery_set_cart addresses goods BY ID and replaces the whole cart, and
    grocery_cart is the only tool that says what is in it. Printing names alone left
    the agent able to read the cart and unable to change one line of it — the id had
    to be guessed, and a wrong guess drops the real item."""
    goods = [
        {"id": "382032", "name": "Сыр Бри с белой плесенью выдержанный 60% Франция",
         "price": {"value": 538.0}, "count": 1.0},
        {"id": "606", "name": "Помидоры розовые", "price": {"value": 214.0},
         "count": 0.57},
    ]
    out = run(server.grocery_cart, Stub(grocery_cart_get={"cart": {"goods": goods}}),
              "578", "2")

    printed = set(re.findall(r"id=(\S+)", out))
    check(printed == {"382032", "606"},
          f"the cart must print exactly the ids it holds, printed {printed}")
    # The name used to be cut to 35 chars with a bare Python slice (no "…"
    # mark) — "Франция" and "60%" sat past that cut and vanished silently.
    # Full name now, for the same reason as grocery_search: brand/%/origin
    # disambiguate near-identical products and cluster at the END of a name.
    check("Сыр Бри" in out and "60%" in out and "Франция" in out,
          f"the full name, not just its first 35 chars, must be shown: {out}")
    # A weight-priced good keeps its fractional count — rounding it to 1 (or to 0,
    # which means removal) is what a re-send built from this listing would carry.
    check("0.57" in out, f"a fractional count must survive the listing: {out}")

    empty = run(server.grocery_cart, Stub(grocery_cart_get={"cart": {"goods": []}}),
                "578", "2")
    check("Корзина пуста" in empty, f"an empty cart must say so: {empty!r}")
    print("  grocery_cart: every good is printed with the id grocery_set_cart needs")


def test_delivery_speed_is_read_from_both_slot_shapes():
    """`nearestTime` comes in two shapes and they are not interchangeable — in the
    capture 55 of 80 retailers use one and 25 the other:

      Relative — from/to are MINUTES as strings ("Самокат: to=15").
      Absolute — from/to are ISO-8601 TIMESTAMPS ("METRO: tomorrow 08:00–11:00").

    The old code formatted both as f"{from}-{to} min", which turned the majority
    shape into "2026-07-22T08:00:00+03:00-2026-07-22T11:00:00+03:00 min". Nothing
    caught it because grocery_stores() never printed the field at all."""
    import datetime as dt
    from src.client import delivery_eta

    tz = dt.timezone(dt.timedelta(hours=3))
    now = dt.datetime(2026, 7, 22, 7, 0, tzinfo=tz)

    eta, label = delivery_eta({"type": "Relative", "from": "", "to": "15"}, now)
    check(eta == 15.0, f"a relative slot must give its minutes, got {eta!r}")
    check(label == "до 15 мин", f"unexpected label: {label!r}")

    eta, label = delivery_eta({"type": "Relative", "from": "20", "to": "35"}, now)
    check(eta == 35.0, f"a range must be measured to its END, got {eta!r}")
    check(label == "20–35 мин", f"unexpected label: {label!r}")

    # The shape that used to be printed as an ISO range labelled "min".
    eta, label = delivery_eta({"type": "Absolute",
                               "from": "2026-07-22T08:00:00+03:00",
                               "to": "2026-07-22T11:00:00+03:00"}, now)
    check(eta == 240.0, f"an absolute slot must convert to minutes, got {eta!r}")
    check(label == "сегодня 08:00–11:00", f"unexpected label: {label!r}")
    check("T" not in label and "+03:00" not in label,
          f"a raw timestamp leaked into the label: {label!r}")
    check("мин" not in label,
          f"an absolute slot must NOT be labelled in minutes: {label!r}")

    eta, label = delivery_eta({"type": "Absolute",
                               "from": "2026-07-23T13:00:00+03:00",
                               "to": "2026-07-23T16:00:00+03:00"}, now)
    check(eta == 1980.0 and label == "завтра 13:00–16:00",
          f"next-day slot: {eta!r} {label!r}")

    # Unknown, malformed and already-passed windows are all "no idea", never 0.
    for junk, why in ((None, "no delivery block"), ({}, "empty block"),
                      ({"type": "Relative", "from": "", "to": ""}, "no upper bound"),
                      ({"type": "Absolute", "from": "", "to": "не дата"}, "garbage date"),
                      ({"type": "Absolute", "from": "2026-07-21T09:00:00+03:00",
                        "to": "2026-07-21T09:30:00+03:00"}, "window already passed")):
        eta, _ = delivery_eta(junk, now)
        check(eta is None, f"{why}: expected None, got {eta!r} — it would win «fastest»")
    print("  delivery_eta: relative minutes and absolute slots, unknown stays unknown")


def test_the_store_list_shows_and_sorts_by_delivery_speed():
    """«Самая быстрая доставка» needs the tool to return the time at all — it did
    not: the client parsed nearestTime and grocery_stores() printed only name, ids,
    minSum and cashback."""
    stores = [
        {"appId": "578", "name": "Азбука Вкуса", "pointId": "2", "minOrderSum": 500.0,
         "etaMin": 110.0, "deliveryWindow": "до 110 мин", "deliveryPrice": 0.0,
         "cashback": 5},
        {"appId": "590", "name": "Самокат", "pointId": "b7", "minOrderSum": 0.0,
         "etaMin": 15.0, "deliveryWindow": "до 15 мин", "deliveryPrice": 0.0,
         "cashback": 3},
        {"appId": "246", "name": "METRO", "pointId": "0503", "minOrderSum": 2000.0,
         "etaMin": 1980.0, "deliveryWindow": "завтра 08:00–11:00",
         "deliveryPrice": 170.0, "cashback": 7},
        {"appId": "11", "name": "Без слота", "pointId": "x", "minOrderSum": 0.0,
         "etaMin": None, "deliveryWindow": "", "deliveryPrice": 0.0, "cashback": 0},
    ]
    names = lambda out: [ln.split()[1] for ln in out.splitlines() if ln.startswith("- ")]

    plain = run(server.grocery_stores, Stub(grocery_stores=stores))
    check("до 15 мин" in plain and "завтра 08:00–11:00" in plain,
          f"the delivery window must be printed at all: {plain}")
    check("170.00 ₽" in plain, f"the delivery price must be printed: {plain}")
    check("срок не указан" in plain,
          f"a store with no slot must say so, not show a blank: {plain}")
    check(names(plain) == ["Азбука", "Самокат", "METRO", "Без"],
          f"without sort_by the bank's order must survive: {names(plain)}")

    fast = run(server.grocery_stores, Stub(grocery_stores=stores), "speed")
    check(names(fast) == ["Самокат", "Азбука", "METRO", "Без"],
          f"«fastest» must sort by the end of the window: {names(fast)}")

    # Unknown stays last in BOTH directions — «no slot» is not «instant».
    slow = run(server.grocery_stores, Stub(grocery_stores=stores), "speed", "desc")
    check(names(slow)[-1] == "Без",
          f"a store with no slot must stay last under desc too: {names(slow)}")

    cheap = run(server.grocery_stores, Stub(grocery_stores=stores), "price")
    check(names(cheap)[-1] == "METRO", f"price sort: {names(cheap)}")
    small = run(server.grocery_stores, Stub(grocery_stores=stores), "min_sum")
    check(names(small)[-1] == "METRO", f"min_sum sort: {names(small)}")

    bad = run(server.grocery_stores, Stub(grocery_stores=stores), "быстро")
    check("speed" in bad and "min_sum" in bad,
          f"an unknown sort key must list the real ones: {bad!r}")
    print("  grocery_stores: window and price shown, speed/price/min_sum sortable")


def test_the_invest_envelopes_are_unwrapped():
    """One bug in four places, found by calling every read tool against the live API.

    _as_list understands `list` and `payload`. These three endpoints answer with
    their own key — {"accounts": …}, {"items": …}, {"portfolios": …} — so it returned
    the ENVELOPE as a single element and every tool rendered one useless row:
    «- ? | », «- [] ? | ». Nothing raised and nothing was empty, so it looked like an
    account with no data rather than a parser that missed.

    The one that mattered is invest_accounts: brokerAccountId is the only argument
    the other three take, so its bad row made the whole investment side unreachable —
    while get_data("invest_accounts") had been returning the same payload all along."""
    accounts = {"accounts": [
        {"brokerAccountId": "2000000001", "brokerAccountType": "InvestBox",
         "brokerAccountStatus": "NORM",
         "totalBalance": {"currency": "RUB", "value": 4459.28},
         "authBalance": {"currency": "RUB", "value": 1178.4},
         "totalYield": {"currency": "RUB", "value": 1.48}},
        {"brokerAccountId": "2000000002", "brokerAccountType": "Fdr",
         "brokerAccountStatus": "NORM", "isBlocked": True,
         "totalBalance": {"currency": "RUB", "value": 7000000.00}}]}

    class InvestStub(Stub):
        def ensure_client_session(self, *a, **kw):
            return None

        def _call_read(self, key, *, overrides=None, body=None, path_override=None):
            return {"investbox_accounts": accounts,
                    "ca_operations": {"hasNext": True, "nextCursor": "1", "items": [
                        {"date": "2026-07-21T17:34:51+03:00", "type": "payOut",
                         "description": "Вывод со счета", "status": "executed",
                         "payment": {"currency": "RUB", "value": -150000}}]},
                    "purchased_securities": {"totals": {}, "portfolios": [
                        {"brokerAccount": {"brokerAccountId": "2000000003",
                                           "name": "Рублевый"},
                         "positions": [{"ticker": "AMD", "securityType": "stock",
                                        "currentBalance": 28, "portfolioPercent": 7.65,
                                        "prices": {"currentPrice": {"value": 521.51,
                                                                    "currency": "USD"}},
                                        "yields": {"yield": {"absolute": {
                                            "value": 1200.0, "currency": "RUB"}}}}]}]},
                    }[key]

    out = run(server.invest_accounts, InvestStub())
    check(out.count("\n") == 1, f"both accounts must be listed: {out!r}")
    check("2000000001" in out and "2000000002" in out,
          f"the brokerAccountId is the only key to the rest of the vertical: {out!r}")
    check("?" not in out.split("|")[0], f"the id must resolve, not print «?»: {out!r}")
    check("4 459.28 RUB" in out, f"the balance must be shown: {out!r}")
    check("ЗАБЛОКИРОВАН" in out, f"a blocked account must say so: {out!r}")

    ops = run(server.invest_operations, InvestStub(), "2000000001", "", 10)
    check("-150 000.00 RUB" in ops,
          f"the amount lives under `payment`, not `amount`: {ops!r}")
    check("Вывод со счета" in ops and "payOut" in ops, f"operation detail: {ops!r}")
    check("[]" not in ops, f"the envelope leaked into the row: {ops!r}")

    secs = run(server.invest_securities, InvestStub())
    check("AMD" in secs and "28 шт" in secs, f"positions must be listed: {secs!r}")
    check("521.51 USD" in secs, f"the price must keep its currency: {secs!r}")
    check("2000000003" in secs,
          f"the PORTFOLIO id differs from the account id and must be shown: {secs!r}")

    # Filtering by an account id that names no portfolio must say why, not answer
    # "no securities" — the ids are from different namespaces.
    none = run(server.invest_securities, InvestStub(), "2000000001")
    check("без аргумента" in none,
          f"an empty filter result must explain the id mismatch: {none!r}")
    print("  invest: accounts, operations and positions unwrapped from their envelopes")


def test_money_formatting_is_unambiguous():
    """A bare float used to fall through to str(), so every caller holding a plain
    number printed «1000.0» — no separator, no currency, easy to misread."""
    check(server._money(1600.2, "RUB") == "1 600.20 RUB",
          f"a bare number + currency must render fully: {server._money(1600.2, 'RUB')!r}")
    check(server._money({"value": 1600.2, "currency": {"name": "RUB"}}) == "1 600.20 RUB",
          f"the bank's dict shape must render the same: "
          f"{server._money({'value': 1600.2, 'currency': {'name': 'RUB'}})!r}")
    check(server._money(1234567.5, "RUB").startswith("1 234 567"),
          f"thousands must be grouped: {server._money(1234567.5, 'RUB')!r}")

    # Absent is not zero — an empty balance must not read as «0.00».
    for empty in (None, ""):
        check(server._money(empty) == "—",
              f"_money({empty!r}) must say 'unknown', got {server._money(empty)!r}")
    check(server._money(0, "RUB") == "0.00 RUB",
          f"a real zero must still print as zero: {server._money(0, 'RUB')!r}")

    # Nothing may raise, whatever it is handed.
    for value in (0, 0.0, "", None, "abc", {"value": 10}, {"value": None}, []):
        got = server._money(value)
        check(isinstance(got, str), f"_money({value!r}) returned {type(got).__name__}")
    print("  _money: bare numbers, bank dicts, grouping, and 'absent' vs zero")


def test_train_search_prices_and_seats_are_not_all_empty():
    """ac0a84b: price sits under carGroup.refundablePrice.price and seats under
    carGroup.places.total — nested two levels deep on each car group, not flat
    fields on the segment. The first rendering read the wrong place and printed
    «мест нет» for all 47 trains in the live search regardless of what the bank
    actually returned."""
    ways = [{
        "segments": [{
            "origin": {"stationName": "Москва"},
            "destination": {"stationName": "Санкт-Петербург"},
            "departureDateTime": "2026-08-01T08:00:00",
            "arrivalDateTime": "2026-08-01T12:00:00",
            "displayTrainNumber": "001А",
            "brandName": "Сапсан",
            "carGroups": [
                {"refundablePrice": {"price": "3200.00"}, "places": {"total": 24},
                 "carTypeName": "Сидячий"},
                {"refundablePrice": {"price": "1500.00"}, "places": {"total": 8},
                 "carTypeName": "Плацкарт"},
            ],
        }],
    }]
    out = run(server.train_search, Stub(train_search=(ways, "search-1")),
              "2000000", "2004000", "2026-08-01")
    check("мест нет" not in out,
          f"a train with priced, seated car groups must not read as «мест нет»: {out!r}")
    check("1500" in out,
          f"the cheapest car group's price must be shown: {out!r}")
    check("мест 32" in out,
          f"seats are summed across every priced car group (24+8): {out!r}")
    print("  train_search: nested price/seats are read, not left to print «мест нет»")


def test_flight_price_is_read_as_an_object_not_a_number():
    """299e525: price is {"amount","currency"}, not a flat number, and segments
    live under flightSegments — the first rendering assumed a flat numeric price
    and crashed on float(), printing nothing before that."""
    flights = [{
        "flightSegments": [{
            "departure": {"airport": "SVO", "time": "2026-08-01T10:00:00"},
            "arrival": {"airport": "LED", "time": "2026-08-01T11:30:00"},
            "carriers": {"marketing": "SU"},
        }],
        "duration": 90,
    }]
    offers = [{
        "price": {"amount": "5000.00", "currency": "RUB"},
        "flights": [0], "withBaggage": True, "refundable": False,
        "vendor": "Tinkoff", "offerId": "off-1",
    }]
    res = {"flights": flights, "offers": offers, "info": {},
           "complete": True, "batches": 1, "searchId": "s-1"}
    out = run(server.flight_search, Stub(flight_search=res),
              "SVO", "LED", "2026-08-01")
    check("5000" in out,
          f"the offer price (an {{'amount','currency'}} object) must render: {out!r}")
    check("SVO" in out and "LED" in out, f"the route must be shown: {out!r}")
    print("  flight_search: {amount,currency} price and flightSegments parse without crashing")


def test_shop_search_and_cart_parse_ids_and_kopecks():
    """shop_search/shop_cart had no coverage beyond cookie/transport plumbing
    (test_transport.py) — the id triple (skuId/pointId/dolyameShopId) and the
    cart's kopecks->rubles conversion were never exercised against a payload."""
    products = [{"skuId": "1001", "id": "1001", "name": "Тестовый товар",
                "price": 999, "dolyameShopId": "77", "available": True,
                "rating": 4.5, "totalRatings": 10, "pointId": "5"}]
    partners = [{"id": "77", "name": "Тестовый Продавец"}]
    out = run(server.shop_search, Stub(shop_search=(products, partners, 1)), "тест")
    check("999" in out, f"the product price must render: {out!r}")
    check("Тестовый Продавец" in out,
          f"the seller, keyed by dolyameShopId, must be resolved: {out!r}")
    check("skuId=1001" in out and "pointId=5" in out and "shopId=77" in out,
          f"the id triple must be printed: {out!r}")

    carts = [{"merchantName": "Продавец", "cartId": "c1",
             "items": [{"name": "Товар А", "quantity": 2,
                       "totalPriceInKopecks": 15000}]}]
    cart_out = run(server.shop_cart, Stub(shop_carts=carts))
    check("150 ₽" in cart_out,
          f"kopecks must convert to rubles (15000 -> 150): {cart_out!r}")
    check("Товар А" in cart_out and "× 2" in cart_out,
          f"item name and quantity must show: {cart_out!r}")
    print("  shop_search/shop_cart: id triple and kopecks conversion parse correctly")


def test_place_schedule_reads_the_nested_event_object():
    """place_schedule had zero test coverage and its render() read eventName/
    name/eventId/prices off the row itself — but the real shape (verified live
    against two venues, object_id 14419 and 23625) nests all of that under
    row["event"]; only row["date"] is top-level, which is why date was the one
    field that ever rendered and everything else printed "?"/"цена не указана"."""
    events = [{
        "event": {"eventId": "627008", "name": "Идиот", "eventType": "spectacle",
                  "prices": {"min": 250.0, "max": 3000.0}},
        "times": [{"id": "4595709", "startTime": "2026-08-21T19:00:00+03:00"}],
        "date": "2026-08-21",
    }, {
        # Real case (object_id 9318, "Маяковский" on 2026-10-10): one row can
        # carry TWO showings on the same date — taking only times[0] would
        # silently drop the second one, not just the clock reading.
        "event": {"eventId": "212538", "name": "Маяковский", "eventType": "spectacle",
                  "prices": {"min": 1500.0, "max": 12000.0}},
        "times": [{"id": "1", "startTime": "2026-10-10T13:00:00+03:00"},
                  {"id": "2", "startTime": "2026-10-10T19:00:00+03:00"}],
        "date": "2026-10-10",
    }]
    out = run(server.place_schedule, Stub(place_schedule=(events, 2)), "14419")
    check("Идиот" in out, f"the nested event name must render, not '?': {out!r}")
    check("eventId=627008" in out, f"the nested eventId must render, not '?': {out!r}")
    check("250" in out, f"the nested price must render, not 'цена не указана': {out!r}")
    check("2026-08-21" in out, f"the top-level date must still render: {out!r}")
    check("19:00" in out, f"the showtime clock must render, not just the date: {out!r}")
    check("13:00" in out and "19:00" in out.split("Маяковский")[1],
          f"a row with two showings must print BOTH times, not just times[0]: {out!r}")
    print("  place_schedule: reads name/eventId/prices from the nested event object")


def test_grocery_good_info_never_prints_a_bare_none():
    """Only kcal was guarded against None in the КБЖУ line — a retailer that
    doesn't publish carbs (a real, common case: nutrition() itself returns
    carb=None rather than 0, see test_nutrition) made the line read literally
    "У None" instead of "У ?"."""
    good = {"id": "1", "name": "Сыр", "count": 5, "price": {"value": 300},
            "meta": {"weight": {"value": 200.0, "unit": "GRM"},
                     "nutritionalValue": {"fat": "", "protein": "", "carbohydrate": "",
                                          "energy": "", "value": "белки 26,8 г; жиры 25,2 г; 334 ккал"}}}
    out = run(server.grocery_good_info, Stub(grocery_good=good), "1", "204", "5980")
    check("None" not in out, f"a missing macro must never leak a bare 'None': {out!r}")
    check("У ?" in out, f"missing carbs must render as '?', not be silently dropped: {out!r}")
    check("334" in out, f"the macros that ARE published must still render: {out!r}")
    print("  grocery_good_info: a retailer's unpublished macro renders as '?', never 'None'")


def test_a_cart_write_with_the_wrong_key_name_is_refused_not_reported_as_ok():
    """The cart loops skipped any entry without an exact `id` key. cart/set then
    replaced the cart with the unchanged goods list, answered 200 with a goodsSum,
    and the tool printed «OK: … N новых позиций» — counting the caller's INPUT. So
    `goodId`, `good_id`, `product_id` (all plausible for an agent reading a search
    result) added nothing and reported success.

    Refusing happens in the client, before any request: nothing has been posted, so
    it is a clean pre-write refusal."""
    from src.client import TbankApiError

    posted = []

    class CartStub(Stub):
        def grocery_cart_get(self, **kw):
            return {"cart": {"goods": [{"id": "111", "count": 1}], "goodsSum": 100.0}}

        def _grocery_delivery(self, *a, **kw):
            return {}

        def _grocery_cart_write(self, goods, app_id, delivery):
            posted.append(goods)
            return {"goodsSum": 100.0}

    out = run(server.grocery_add_to_cart, CartStub(),
              '[{"goodId": "222", "count": 1}]', "204", "5980")
    check(not posted, f"a refused write must not reach the backend at all: {posted}")
    check("id" in out and ("BAD_ITEMS" in out or "без ключа" in out),
          f"the refusal must name the missing key, got: {out!r}")
    check("OK" not in out, f"a write that stored nothing must not say OK: {out!r}")
    check("goodId" in out, f"the refusal should show what key WAS sent: {out!r}")

    # The good path still works, and the count comes from the CART, not the input.
    class GoodStub(CartStub):
        def grocery_cart_goods(self, **kw):
            return [{"id": "111"}, {"id": "222"}]

    ok = run(server.grocery_add_to_cart, GoodStub(),
             '[{"id": "222", "count": 1}]', "204", "5980")
    check("OK" in ok and "2 позиций" in ok,
          f"the reported count must come from the cart, not the request: {ok!r}")

    # set_cart refuses the same way, but clear=True still needs no items.
    cleared = run(server.grocery_set_cart, GoodStub(), "[]", "204", "5980", True)
    check("ОШИБКА" not in cleared and "BAD_ITEMS" not in cleared,
          f"clear=True must not be blocked by the item check: {cleared!r}")
    try:
        CartStub().grocery_set_cart([{"good_id": "1", "count": 2}],
                                    app_id="204", point_id="5980")
        failures.append("grocery_set_cart accepted an entry with no id key")
    except TbankApiError as e:
        check(e.result_code == "BAD_ITEMS", f"unexpected refusal code: {e.result_code}")
    print("  cart: a wrong key name is refused before the write, not counted as added")


def test_concert_seats_print_the_id_the_booking_tool_demands():
    """cinema_book wants the concert seat's composite id back verbatim, and its
    docstring plus the tickets skill both say to take it from cinema_seats — which
    printed only row and number. The required argument was obtainable from no tool.
    Concert seats also often carry no `pos`, so row-grouping put the whole hall in
    one «ряд —» bucket."""
    hall = {"hallName": "Стадион", "seats": [
        {"status": "vacant", "price": 5000,
         "id": "Фанзона|5000§~§54093386|default"},
        {"status": "vacant", "price": 3000, "pos": {"row": 2, "number": 7},
         "id": "Партер|3000§~§54093387|default"},
        {"status": "occupied", "price": 1000, "id": "Партер|1000§~§54093388|default"},
    ]}
    out = run(server.cinema_seats, Stub(event_seats=[hall]),
              "e1", "s1", "o1", "", 0, "concert")
    check("Фанзона|5000§~§54093386|default" in out,
          f"the composite seatId must be printed verbatim: {out!r}")
    check("54093388" not in out, f"an occupied seat must not be offered: {out!r}")
    check("kind=\"concert\"" in out,
          f"the next-step hint must tell the agent to pass kind: {out!r}")
    check("ряд:место" not in out,
          f"the cinema seat format must not be suggested for a concert: {out!r}")
    check("без нумерации" in out,
          f"a seat with no pos must say so rather than land in a «ряд —» bucket: {out!r}")

    # Cinemas are unchanged: rows, numbers, and the row:number booking format.
    movie_hall = {"hallName": "ЗАЛ 1", "seats": [
        {"status": "vacant", "price": 400, "pos": {"row": 5, "number": 3}}]}
    mv = run(server.cinema_seats, Stub(event_seats=[movie_hall]), "e1", "s1", "o1")
    check("ряд" in mv and "ряд:место" in mv,
          f"the cinema rendering must not have changed: {mv!r}")
    print("  seats: concerts print the composite seatId, cinemas keep rows")


def test_search_keeps_the_venues_it_used_to_drop():
    """A venue hit names itself in objectName and carries no eventName, so a parser
    reading only the event keys found neither a name nor an id and counted it as
    unrecognised. Every cinema, hall and theatre in the results vanished that way —
    「найди кинотеатр Каро 11」 answered with nothing while the id was right there
    on the hit. Shapes below are the ones in captures-gorod.xml."""
    hits = [
        {"objectType": "cinema", "id": "10587", "objectSource": {
            "objectId": "10587", "objectName": "Каро 11 Октябрь",
            "address": "Н.Арбат, 24", "city": "Москва"}},
        {"objectType": "theatre", "id": "9530", "objectSource": {
            "objectId": "9530", "objectName": "Театр Российской Армии",
            "address": "Суворовская пл., 2", "city": "Москва"}},
        # An event hit must keep rendering exactly as before: objectName is where
        # it plays, not what it is called.
        {"objectType": "movie", "objectSource": {
            "eventId": "103693", "eventName": "Майкл",
            "objectName": "Каро 11 Октябрь", "dateForShow": "29 июля"}},
        # Pure scaffolding stays dropped, and silently — it is not a lost result.
        {"objectType": "masterWidget", "objectSource": {}},
    ]
    rows, skipped = server._search_rows(hits)
    by_name = {r["name"]: r for r in rows}
    check(len(rows) == 3, f"expected 3 rows, got {len(rows)}: {rows}")
    check(skipped == 0, f"nothing here is unrecognisable, skipped={skipped}")

    venue = by_name.get("Каро 11 Октябрь")
    check(venue is not None, f"the cinema was dropped again: {rows}")
    check(venue and venue["id"] == "10587",
          f"the venue id must be the objectId a schedule call takes: {venue}")
    check(venue and "Н.Арбат, 24" in venue["note"],
          f"a venue is located by its address, not by its own name: {venue}")
    check(by_name.get("Театр Российской Армии", {}).get("id") == "9530",
          f"the theatre lost its id: {rows}")

    film = by_name.get("Майкл")
    check(film and film["id"] == "103693", f"the event id changed: {film}")
    check(film and "Каро 11 Октябрь" in film["note"],
          f"an event must still print its venue in the note: {film}")

    # The deeplink forms the app uses for venues, both present in the captures.
    carded = [
        {"objectType": "cinema", "objectSource": {
            "title": {"value": "Каро 11"},
            "link": {"deeplink": "tinkoffbank://Main/CinemaTickets/Cinemas?cinemaId=10587"}}},
        {"objectType": "theatre", "objectSource": {
            "title": {"value": "Ленком"},
            "link": {"deeplink": "tinkoffbank://Playbill/Venue/12915"}}},
    ]
    card_rows, _ = server._search_rows(carded)
    check([r["id"] for r in card_rows] == ["10587", "12915"],
          f"venue ids must be read out of both deeplink shapes: {card_rows}")
    print("  search: venue hits keep their name and objectId; events unchanged")


def test_free_seating_counts_choices_not_tickets():
    """A free-seating sector returns one entry per available TICKET, all sharing a
    seatId. Counting them made «свободно» a ticket count — 40 for a sector with
    one choice — and printing them straight repeated the same line forty times.
    seatsQuantity is the sector's own number and is what the app shows."""
    hall = {"hallName": "Танцпол", "seatsQuantity": 72, "seats": [
        {"status": "vacant", "price": 5500, "id": "Танцпол|5500§~§1|default"}
        for _ in range(40)
    ] + [{"status": "vacant", "price": 3900, "id": "Танцпол|3900§~§2|default"}]}
    out = run(server.cinema_seats, Stub(event_seats=[hall]),
              "e1", "s1", "o1", "", 0, "концерт")
    check("свободно 72" in out,
          f"the sector's own count must be printed, not the ticket rows: {out!r}")
    check(out.count("Танцпол|5500") == 1,
          f"one seatId must print once, not once per ticket: {out!r}")
    check("Танцпол|3900" in out, f"the other choice went missing: {out!r}")

    # With no seatsQuantity the row count is still the best available answer.
    bare = {"hallName": "Партер", "seats": [
        {"status": "vacant", "price": 100, "id": "a"},
        {"status": "vacant", "price": 200, "id": "b"}]}
    out2 = run(server.cinema_seats, Stub(event_seats=[bare]),
               "e1", "s1", "o1", "", 0, "театр")
    check("свободно 2" in out2, f"fallback count is wrong: {out2!r}")
    check('kind="театр"' in out2,
          f"the next-step hint must echo the kind the caller used: {out2!r}")
    print("  free seating: sector count, one line per choice, not per ticket")


def test_a_city_is_resolved_or_refused_never_assumed():
    """cityId used to be Moscow and only Moscow, and the collection code was built
    by transliterating the city name — a guess that is right for Moscow and wrong
    wherever the server spells it differently (its own shelves say Moskva, moscow
    and msk). Both are gone; what replaces them must never quietly answer about
    Moscow when asked about somewhere else."""
    from src.client import city_id_of, CITY_IDS, TbankApiError

    check(city_id_of("Москва") == "1", "Moscow must resolve to 1")
    check(city_id_of("санкт-петербург") == "2", "case must not matter")
    check(city_id_of("СПб") == "2", "the alias people actually type must work")
    check(city_id_of("Ростов-на-Дону") == "12", "hyphenated names must resolve")
    check(len(CITY_IDS) > 60,
          f"the table should carry the whole walked range, has {len(CITY_IDS)}")

    # An explicit id is the escape hatch for a city outside the table, and it wins.
    check(city_id_of("Москва", 77) == "77", "an explicit city_id must win")
    check(city_id_of("", 77) == "77", "an explicit city_id needs no name")

    # The two refusals. Neither may fall back to Moscow.
    for bad, why in ((("Атлантида", 0), "an unknown city"),
                     (("", 0), "no city at all")):
        try:
            city_id_of(*bad)
            check(False, f"{why} was accepted instead of refused")
        except TbankApiError as e:
            check("1" != str(e), "must not be a silent Moscow")
            check(e.result_code in ("UNKNOWN_CITY", "CITY_REQUIRED"),
                  f"{why}: wrong code {e.result_code}")

    # A near miss should hand back something to try rather than just «no».
    try:
        city_id_of("Казан")
        check(False, "a near miss was accepted")
    except TbankApiError as e:
        check("Казань" in str(e), f"the refusal should suggest the real name: {e}")
    print("  cities: names, aliases and explicit ids resolve; unknown refuses")


def test_a_cinema_repertoire_reads_as_films_not_as_venues():
    """Two questions share one response. Asked about a FILM, each entry is another
    cinema and the venue is the heading. Asked about a CINEMA, every entry carries
    the SAME venue and what varies is the film — so heading each one printed the
    address twenty-four times and announced «24 площадок» for one building.

    The request differs too: with a venue there is no city, and none is demanded."""
    from src.client import TbankApiError

    venue = {"objectId": "10587", "objectName": "Каро 11 Октябрь",
             "geo": {"address": "Н.Арбат, 24"}}
    answer = [
        {"info": venue, "events": [{"eventName": "Майкл", "slots": [
            {"startTime": "20:20", "prices": {"fix": 660}, "hallName": "ЗАЛ №7",
             "slotId": "1"}]}]},
        {"info": venue, "events": [{"eventName": "Холоп 3", "slots": [
            {"startTime": "18:00", "prices": {"fix": 660}, "hallName": "ЗАЛ №9",
             "slotId": "2"}]}]},
    ]

    class Sched(Stub):
        def __init__(self):
            super().__init__()
            self.body = None

        def cinema_schedule(self, event_id="", date="", city="", object_id="", **kw):
            self.body = {"event_id": event_id, "city": city, "object_id": object_id}
            return answer

    s = Sched()
    out = run(server.cinema_schedule, s, "", "2026-07-29", "", "", 90, "", "10587")
    check("24 площадок" not in out and "2 площадок" not in out,
          f"a venue's day must not be counted in venues: {out!r}")
    check("2 фильмов" in out, f"the heading must count films: {out!r}")
    check(out.count("Н.Арбат, 24") == 1,
          f"the address belongs once, got {out.count('Н.Арбат, 24')}: {out!r}")
    check("Майкл" in out and "Холоп 3" in out, f"film names missing: {out!r}")
    check(s.body["city"] == "" and s.body["object_id"] == "10587",
          f"a venue query must not carry a city: {s.body}")

    # Asked about a film across a city, the venue stays the heading.
    s2 = Sched()
    city_out = run(server.cinema_schedule, s2, "103693", "2026-07-29", "", "", 90,
                   "Москва", "")
    check("площадок" in city_out, f"film mode must still count venues: {city_out!r}")

    # Neither a film nor a venue is not a question anyone can answer.
    try:
        MobileSession.cinema_schedule(Stub(), date="2026-07-29")
        check(False, "a schedule with no target was accepted")
    except TbankApiError as e:
        check(e.result_code == "NO_TARGET", f"wrong refusal: {e}")
    print("  schedule: a venue's day lists films, a film's day lists venues")


def test_the_ticket_says_what_it_has_and_what_it_lacks():
    """What is presented at the door lives in the orders feed, and how much of it
    exists depends on the partner: across 75 real afisha orders every one carried a
    booking code, 53 carried a QR, and Ticketland hands out neither QR nor PDF.

    So «no QR» must read as a fact about that partner, not as a failure — and an
    order missing from the feed means the ticket is not issued yet, which is a
    different thing from the order not existing."""
    feed = [
        {"orderId": "1", "status": "CREATED_DYNAMIC", "fields": {
            "eventName": "Майкл", "hallName": "ЗАЛ №7", "reservationCode": "WS7BZJW",
            "qr": "WS7BZJW", "partnerName": "Рамблер/Касса"}},
        {"orderId": "2", "status": "CREATED_DYNAMIC", "fields": {
            "eventName": "Лекция", "reservationCode": "115382035",
            "pdfUrl": "https://example.invalid/t.pdf", "partnerName": "Рамблер"}},
        {"orderId": "3", "status": "CREATED_DYNAMIC", "fields": {
            "eventName": "Стас", "reservationCode": "85776589",
            "partnerName": "Ticketland"}},
    ]
    s = Stub(orders=feed)

    withqr = run(server.ticket_qr, s, "1")
    check("WS7BZJW" in withqr, f"the QR payload must be printed: {withqr!r}")
    check("не картинка" in withqr,
          f"a 7-character payload must not be mistaken for an image: {withqr!r}")

    pdf = run(server.ticket_qr, s, "2")
    check("example.invalid/t.pdf" in pdf, f"the PDF link went missing: {pdf!r}")

    none = run(server.ticket_qr, s, "3")
    check("Ticketland" in none and "код брони" in none,
          f"a partner that issues no QR must be named, not treated as an error: {none!r}")
    check("85776589" in none, f"the booking code is what is shown instead: {none!r}")

    missing = run(server.ticket_qr, s, "404")
    check("Неоплаченные" in missing or "order_details" in missing,
          f"an absent order must point at where unpaid bookings live: {missing!r}")
    print("  ticket: prints what the partner issues, names what it does not")


def main():
    print("response parsers:")
    test_a_city_is_resolved_or_refused_never_assumed()
    test_the_ticket_says_what_it_has_and_what_it_lacks()
    test_a_cinema_repertoire_reads_as_films_not_as_venues()
    test_search_keeps_the_venues_it_used_to_drop()
    test_free_seating_counts_choices_not_tickets()
    test_a_cart_write_with_the_wrong_key_name_is_refused_not_reported_as_ok()
    test_concert_seats_print_the_id_the_booking_tool_demands()
    test_a_paid_order_does_not_read_as_unpaid()
    test_grocery_order_status_does_not_cut_the_goods_names()
    test_conversation_ids_survive_intact()
    test_a_dead_messenger_token_is_renewed_not_displayed_as_a_chat()
    test_documents_merge_and_lists()
    test_inn_header_shows_the_number_and_duplicate_copies_merge()
    test_grocery_search_header_is_honest()
    test_messenger_paging_arguments_reach_the_client()
    test_the_cart_prints_the_ids_it_must_be_edited_by()
    test_delivery_speed_is_read_from_both_slot_shapes()
    test_the_store_list_shows_and_sorts_by_delivery_speed()
    test_the_invest_envelopes_are_unwrapped()
    test_money_formatting_is_unambiguous()
    test_train_search_prices_and_seats_are_not_all_empty()
    test_flight_price_is_read_as_an_object_not_a_number()
    test_shop_search_and_cart_parse_ids_and_kopecks()
    test_place_schedule_reads_the_nested_event_object()
    test_grocery_good_info_never_prints_a_bare_none()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
