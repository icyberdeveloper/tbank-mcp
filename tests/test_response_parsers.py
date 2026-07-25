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
    "id": "70123456", "status": "CREATED_DYNAMIC", "paymentId": "125301542205",
    "application": {"id": "204", "name": "ВкусВилл"},
    "cart": {"sum": 1600.2, "goodsSum": 1630.0, "goods": [{"id": "1"}]},
}}}


def test_a_paid_order_does_not_read_as_unpaid():
    out = run(server.grocery_order_status, Stub(grocery_order_get=PAID_ORDER), "70123456")
    check("paid=yes" in out, f"an order with a paymentId must read as paid: {out}")
    check("sum=1600.2" in out, f"the sum must come from cart.sum: {out}")
    check("CREATED_DYNAMIC" in out,
          f"the real status must be shown, not translated into a failure: {out}")
    check("ВкусВилл" in out, f"the store must be named: {out}")
    check("125301542205" in out, f"the paymentId must be shown for reconciliation: {out}")

    # No paymentId → honestly unpaid, and the sum still resolves.
    unpaid = {"payload": {"order": {"id": "70123457", "status": "NEW",
                                    "cart": {"goodsSum": 500.0}}}}
    out2 = run(server.grocery_order_status, Stub(grocery_order_get=unpaid), "70123457")
    check("paid=no" in out2, f"an order without a paymentId is unpaid: {out2}")
    check("sum=500.0" in out2, f"goodsSum must be the fallback: {out2}")

    # An empty/odd payload must degrade, not raise.
    for junk in ({}, {"payload": None}, {"payload": {"order": "nope"}}):
        got = run(server.grocery_order_status, Stub(grocery_order_get=junk), "x")
        check("Traceback" not in got and got.strip(),
              f"a malformed order payload must degrade gracefully: {got!r}")
    print("  grocery_order_status: paid/unpaid, sum and status read from the real schema")


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
    # The name is truncated for width; the id must not be caught up in that.
    check("Сыр Бри" in out, f"the name must still be shown: {out}")
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


def main():
    print("response parsers:")
    test_a_paid_order_does_not_read_as_unpaid()
    test_conversation_ids_survive_intact()
    test_the_cart_prints_the_ids_it_must_be_edited_by()
    test_delivery_speed_is_read_from_both_slot_shapes()
    test_the_store_list_shows_and_sorts_by_delivery_speed()
    test_money_formatting_is_unambiguous()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
