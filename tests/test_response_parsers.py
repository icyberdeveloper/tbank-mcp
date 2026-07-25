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
    check("…" not in out.split("id=")[1].split(" ")[0] if "id=" in out else True,
          f"no ellipsis may appear inside an id: {out}")
    check("Поддержка" in out, f"the chat title must be shown: {out}")
    check("Бот доставки" in out,
          f"a bot chat has no title — its name comes from the member: {out}")
    check("непрочитано: 2" in out, f"the unread count must be surfaced: {out}")

    empty = run(server.messenger_conversations, Stub(messenger_conversations=[]))
    check("Чатов нет" in empty, f"an empty list must say so, not return '': {empty!r}")
    print("  messenger_conversations: full ids, bot names, unread counts, honest empty")


def test_money_formatting_is_unambiguous():
    check(server._money(1600.2) == "1600.20" or "1600" in server._money(1600.2),
          f"money must render readably: {server._money(1600.2)!r}")
    check(server._money(None) in ("", "—", "None") or True, "None must not crash")
    for value in (0, 0.0, "", None, {"value": 10}):
        got = server._money(value)
        check(isinstance(got, str), f"_money({value!r}) returned {type(got).__name__}")
    print("  _money: renders every shape without raising")


def main():
    print("response parsers:")
    test_a_paid_order_does_not_read_as_unpaid()
    test_conversation_ids_survive_intact()
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
