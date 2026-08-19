"""Elicitation (input-free only): pickers + the money gate on every payment tool.
Each test EXECUTES the real async tool through a fake ctx and a fake session.

The button IS the confirmation. A client that cannot show one (no ctx, or no
elicitation capability) is REFUSED with NO_ELICITATION_REFUSAL before any journal
write and before any HTTP — never waved through to the payment body. What these
tests pin:
  * transfer     — SBP bank picker BEFORE any journal write; gate; decline is clean.
  * from_account — picker only when >1 ruble account; the pick is what's debited.
  * pay_bill     — gate names the real total+fee; decline posts nothing.
  * ticket_pay   — gate; the duplicate guard blocks a repeat, force overrides; its
                   OWN amount validation refuses NaN/inf/0/negative/junk before the
                   button, before order_details and before any journal line.
  * headless     — transfer / pay_bill / ticket_pay refuse BEFORE the prepare step
                   (no recipient resolve, no commission quote, no order_details).
  * grocery_checkout — quotes the final sum itself, the button names it, accept
                   charges exactly the quote (a stale expected_sum is shown on the
                   button as «было … — банк пересчитал» and then overridden);
                   headless non-dry-run refuses before even quoting; dry_run stays a
                   headless-capable preview; below a positive threshold no button,
                   but expected_sum is still pinned to the quote; a quote that is a
                   refusal STRING (empty cart) is returned as-is, and one that is not
                   a finite positive number never becomes the confirmed sum.
  * the gate obeyed — with the pickers out of the way (one default recipient, one
                   account) the ONLY dialog is the money gate, so its answer is the
                   tool's answer: decline / cancel / an McpError from the client all
                   leave the money where it is, and a client with no elicitation
                   capability is refused before the network. Pinned for all four
                   _money_gate tools, with the exact refusal texts.
  * button text  — transfer and ticket_pay name the sum (via _money) and the payee /
                   order id, and elicit a schema with NO fields at all.
  * threshold    — «для сумм ОТ порога» is inclusive: at exactly TBANK_CONFIRM_ABOVE
                   the button IS shown, one kopeck below it is not (all four tools,
                   in a client that CAN show one).

    python3 tests/test_elicitation_flows.py
"""
import asyncio
import inspect
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tbank-eflows-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from src import journal, observability, server, trace         # noqa: E402
from src.client import TbankApiError                          # noqa: E402
from elicit_fake import (FakeCtx, accept_ctx, cancel_ctx,    # noqa: E402
                         decline_ctx, incapable_ctx, mcp_error)

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def run_tool(session, fn, *a, **kw):
    saved = server._require
    server._require = lambda: session
    try:
        out = fn(*a, **kw)
        if inspect.iscoroutine(out):
            out = asyncio.run(out)
        return out
    finally:
        server._require = saved


def _reset():
    open(journal.ATTEMPTS_FILE, "w").close()


# ---- transfer: SBP bank picker + gate --------------------------------------

class TransferSession:
    def __init__(self, recipients, accounts=None):
        self.recipients = recipients
        self.accounts = (accounts if accounts is not None
                         else [{"id": "1111111111", "name": "Основной", "balance": 5000}])
        self.sent = None
        self.resolved = []               # phones resolve_recipient was asked about

    def ensure_fresh(self, *a, **k):
        return None

    def resolve_recipient(self, phone):
        self.resolved.append(phone)
        return list(self.recipients)

    def ruble_source_accounts(self):
        return list(self.accounts)

    def _source_account(self):
        if self.accounts:
            return self.accounts[0]["id"]
        raise TbankApiError("NO_SOURCE_ACCOUNT", "нет счёта")

    def transfer(self, amount, to_account, description, *, provider, bank_member_id,
                 masked_fio, pointer_link_id, account, user_payment_id):
        self.sent = {"amount": amount, "pointer_link_id": pointer_link_id,
                     "bank_member_id": bank_member_id, "account": account}
        return {"payload": {"paymentId": "PAY-1"}}, (masked_fio or "И. И.")


def _two_banks():
    return [
        {"is_default_bank": False, "is_tbank": True, "masked_fio": "И. И.",
         "bank_name": "Т-Банк", "bank_member_id": "", "pointer_link_id": "LINK-T"},
        {"is_default_bank": False, "is_tbank": False, "masked_fio": "И. И.",
         "bank_name": "Сбер", "bank_member_id": "MEMBER-S", "pointer_link_id": "LINK-S"},
    ]


def test_transfer_bank_picker_then_gate_pays_the_chosen_bank():
    _reset()
    s = TransferSession(_two_banks())
    fctx = accept_ctx(pick=1)   # choose Сбер, then accept the gate
    out = run_tool(s, server.transfer, 1000, "+79991234567", ctx=fctx)
    check(len(fctx.asked) == 2, f"a picker then a gate were expected: {len(fctx.asked)}")
    check(s.sent and s.sent["pointer_link_id"] == "LINK-S",
          f"the chosen bank's link id must be used: {s.sent}")
    check(s.sent and s.sent["bank_member_id"] == "MEMBER-S", f"member id: {s.sent}")
    check("PAY-1" in out, f"the payment must complete: {out}")
    print("  transfer: bank picker → gate → pays the chosen bank")


def test_transfer_bank_picker_declined_writes_no_journal():
    _reset()
    s = TransferSession(_two_banks())
    out = run_tool(s, server.transfer, 1000, "+79991234567",
                   ctx=decline_ctx())
    check(s.sent is None, "a declined bank pick must not send the transfer")
    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", f"a declined pick must leave no journal attempt: {blob!r}")
    # The picker's two refusals must stay distinguishable: «Отмена» is the user's
    # own choice, a closed window is not, and the retry advice differs. Both used
    # to be asserted by the substrings they SHARE, so swapping them was invisible.
    check(out == "Перевод отменён — банк получателя не выбран. Деньги на месте.",
          f"a declined pick must read as the user's own «Отмена»: {out!r}")
    _reset()
    s2 = TransferSession(_two_banks())
    out2 = run_tool(s2, server.transfer, 1000, "+79991234567", ctx=cancel_ctx())
    check(s2.sent is None, "a cancelled bank pick must not send the transfer")
    check(out2 == ("Банк получателя не выбран (окно закрыто или истёк таймаут). "
                   "Перевод не отправлен, деньги на месте. Повтори, когда будешь "
                   "готов."),
          f"a closed window must NOT read as the user declining: {out2!r}")
    print("  transfer: declined and timed-out bank picks are distinct, and neither "
          "leaves a journal trace")


def test_transfer_default_bank_skips_the_picker():
    _reset()
    recips = _two_banks(); recips[0]["is_default_bank"] = True
    s = TransferSession(recips)
    fctx = accept_ctx()           # only the gate, no picker
    out = run_tool(s, server.transfer, 1000, "+79991234567", ctx=fctx)
    check(len(fctx.asked) == 1, f"a default bank must skip the picker: {len(fctx.asked)}")
    check(s.sent and s.sent["pointer_link_id"] == "LINK-T", f"default used: {s.sent}")
    print("  transfer: a default bank means no picker, just the gate")


# ---- from_account picker ---------------------------------------------------

def test_source_picker_only_fires_with_more_than_one_account():
    _reset()
    one = TransferSession(_two_banks()[:1])   # single recipient, single account
    one.recipients[0]["is_default_bank"] = True
    fctx1 = accept_ctx()
    run_tool(one, server.transfer, 1000, "+79991234567", ctx=fctx1)
    check(len(fctx1.asked) == 1, "one account → no source picker (just the gate)")

    _reset()
    multi = TransferSession(
        [dict(_two_banks()[0], is_default_bank=True)],
        accounts=[{"id": "1111111111", "name": "Осн", "balance": 5000},
                  {"id": "2222222222", "name": "Втор", "balance": 300}])
    fctx2 = accept_ctx(pick=1)  # gate accept + pick 2nd account
    run_tool(multi, server.transfer, 1000, "+79991234567", ctx=fctx2)
    check(len(fctx2.asked) == 2, f"two accounts → a source picker appears: {fctx2.asked}")
    check(multi.sent and multi.sent["account"] == "2222222222",
          f"the picked account must be debited: {multi.sent}")
    print("  from_account: picker only with >1 account, and the pick is debited")


# ---- pay_bill gate names the total ----------------------------------------

class BillSession:
    def __init__(self):
        self.posted = None
        self.commissioned = None
        self.looked_up = []              # provider ids find_provider was asked about

    def ensure_fresh(self, *a, **k):
        return None

    def ruble_source_accounts(self):
        return [{"id": "1111111111", "name": "Осн", "balance": 9000}]

    def _source_account(self):
        return "1111111111"

    def find_provider(self, provider_id, group=""):
        self.looked_up.append(provider_id)
        return {"name": "Мосэнерго", "id": provider_id}

    def validate_provider_fields(self, prov, vals):
        return []

    def payment_commission(self, body):
        self.commissioned = body
        # A real commission scales with the bill, and the TOTAL is what leaves the
        # account — a constant 1030 here hid the fact that pay_bill used to decide
        # the button on `amount` while debiting `amount + fee`.
        amount = float((body.get("payParameters") or {}).get("moneyAmount") or 0)
        fee = round(amount * 0.03, 2)
        return {"value": {"value": fee}, "total": {"value": round(amount + fee, 2)}}

    def pay_bill(self, provider_id, vals, amount, *, account, user_payment_id):
        self.posted = {"provider": provider_id, "amount": amount, "account": account}
        return {"payload": {"paymentId": "BILL-1"}}


def test_pay_bill_gate_names_total_and_fee():
    _reset()
    s = BillSession()
    fctx = accept_ctx()
    out = run_tool(s, server.pay_bill, "mosenergo", '{"account": "123"}', 1000, ctx=fctx)
    check(len(fctx.asked) == 1, "one gate expected")
    if fctx.asked:
        msg = fctx.asked[0][0]
        # Pinned whole, not by substrings: «30» is a substring of «1 030», so the
        # fee half of a substring check tests nothing — and this button is the
        # human's only disclosure of what leaves the account.
        check(msg == "Оплатить Мосэнерго: спишется 1 030.00 RUB (комиссия 30.00 RUB)?",
              f"the gate must name provider, total AND fee, verbatim: {msg!r}")
    check(s.posted and "BILL-1" in out, f"accept must pay: {out}")
    print("  pay_bill: the gate names total + fee, accept pays")


def test_pay_bill_decline_posts_nothing():
    _reset()
    s = BillSession()
    out = run_tool(s, server.pay_bill, "mosenergo", '{"account": "123"}', 1000,
                   ctx=decline_ctx())
    check(s.posted is None, "a declined bill must not post")
    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", "a declined bill must leave no journal attempt")
    check("Отменено" in out, f"honest refusal: {out}")
    print("  pay_bill: decline posts nothing, no journal")


# ---- ticket_pay gate + duplicate guard ------------------------------------

class TicketSession:
    def __init__(self, booked=1760, fail=None):
        self.booked = booked
        self.fail = fail                 # None | "transport"
        self.paid = None
        self.detailed = []               # order ids order_details was asked about

    def ensure_fresh(self, *a, **k):
        return None

    def ruble_source_accounts(self):
        return [{"id": "1111111111", "name": "Осн", "balance": 5000}]

    def _source_account(self):
        return "1111111111"

    def order_details(self, order_id):
        self.detailed.append(order_id)
        return {"cartInfo": {"amount": self.booked}} if self.booked else {}

    def pay_marketplace_order(self, order_id, amount, account, token):
        if self.fail == "transport":
            raise ConnectionError("reset")   # → outcome recorded as "unknown"
        self.paid = {"order": order_id, "amount": amount, "account": account}
        return {"paymentId": "TPAY-1", "stage": {"status": "SUCCESS"}}


def test_ticket_pay_gate_pays_on_accept():
    _reset()
    s = TicketSession()
    out = run_tool(s, server.ticket_pay, "ORD-OK", 1760, "tok", ctx=accept_ctx())
    check(s.paid is not None and "ОПЛАЧЕНО" in out, f"accept must pay: {out}")
    print("  ticket_pay: gate pays on accept")


def test_ticket_pay_duplicate_guard_blocks_unconfirmed_then_force():
    _reset()
    # First attempt's outcome is UNKNOWN (transport failure) — a blocking status,
    # exactly like transfers. A confirmed 'paid' would NOT block (a deliberate
    # second identical payment is allowed); an unconfirmed one must.
    bad = TicketSession(fail="transport")
    out1 = run_tool(bad, server.ticket_pay, "ORD-DUP", 1760, "tok",
                    ctx=accept_ctx())
    check("НЕИЗВЕСТЕН" in out1, f"a transport failure is an unknown outcome: {out1}")

    s2 = TicketSession()
    out2 = run_tool(s2, server.ticket_pay, "ORD-DUP", 1760, "tok",
                    ctx=accept_ctx())
    check(s2.paid is None and "ЗАБЛОКИРОВАН" in out2,
          f"a repeat of an unconfirmed payment must be blocked: {out2}")

    s3 = TicketSession()
    out3 = run_tool(s3, server.ticket_pay, "ORD-DUP", 1760, "tok", force=True,
                    ctx=accept_ctx())
    check(s3.paid is not None and "ОПЛАЧЕНО" in out3, f"force overrides the guard: {out3}")
    print("  ticket_pay: unconfirmed repeat blocked, force overrides")


def test_ticket_pay_decline_pays_nothing():
    _reset()
    s = TicketSession()
    out = run_tool(s, server.ticket_pay, "ORD-DECL", 1760, "tok",
                   ctx=decline_ctx())
    check(s.paid is None, "a declined ticket payment must not reach the gateway")
    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", "a declined ticket payment must leave no attempt")
    print("  ticket_pay: decline pays nothing, no journal")


def test_ticket_pay_button_names_the_order_and_the_sum():
    """The button string IS the whole disclosure to the human: the order it pays
    and the sum the bank holds for it, rendered by _money. The schema behind it has
    no fields — Accept/Decline is the answer, nothing an auto-fill could tick."""
    _no_threshold()
    _reset()
    s = TicketSession()                       # the bank's booked sum is 1760
    fctx = accept_ctx()
    out = run_tool(s, server.ticket_pay, "10000000000", 1760, "tok", ctx=fctx)
    check(len(fctx.asked) == 1, f"one button expected: {fctx.asked}")
    if fctx.asked:
        msg, schema = fctx.asked[0]
        check(msg == "Оплатить заказ 10000000000: 1 760.00 RUB?",
              f"the ticket button must name the order and the sum: {msg!r}")
        check(msg == f"Оплатить заказ 10000000000: {server._money(1760, 'RUB')}?",
              f"the sum must be rendered by _money: {msg!r}")
        check(schema.model_json_schema().get("properties", {}) == {},
              f"the ticket gate must elicit no fields: {schema.model_json_schema()}")
    check(s.paid is not None and "ОПЛАЧЕНО" in out, f"accept pays: {out}")
    print("  ticket_pay: the button names order + sum, and asks for no fields")


def test_ticket_pay_refuses_an_unpayable_amount_before_anything():
    """ticket_pay is the one paying tool whose amount comes straight from the agent,
    and every guard downstream (booked cross-check, threshold, gateway body) is a
    float comparison — NaN makes them all False. So the amount is validated FIRST:
    no button, no order_details, no journal, no POST."""
    _no_threshold()
    for bad in (float("nan"), float("inf"), -5, 0, "abc"):
        _reset()
        s = TicketSession()
        fctx = accept_ctx()
        out = run_tool(s, server.ticket_pay, "10000000000", bad, "tok", ctx=fctx)
        check(out == (f"Сумма должна быть положительным числом, получено {bad!r}. "
                      f"Возьми её из ответа cinema_book() для заказа 10000000000."),
              f"{bad!r}: the tool must name the bad sum and where to get a real one: {out!r}")
        check(fctx.asked == [], f"{bad!r}: nothing may be put on a button: {fctx.asked}")
        check(s.detailed == [], f"{bad!r}: order_details must not be called: {s.detailed}")
        check(s.paid is None, f"{bad!r}: the gateway must not be called: {s.paid}")
        blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
        check(blob.strip() == "", f"{bad!r}: no journal attempt: {blob!r}")
    print("  ticket_pay: NaN/inf/0/negative/junk refused before the button and the network")


# ---- headless: refused BEFORE the prepare step ---------------------------

REFUSAL_HEAD = "ПЛАТЁЖ НЕ ВЫПОЛНЕН: этот клиент не поддерживает подтверждение кнопкой"


def _no_threshold():
    os.environ.pop("TBANK_CONFIRM_ABOVE", None)


def _check_no_button_refusal(out, tag, *, safe="деньги на месте"):
    check(out == server.NO_ELICITATION_REFUSAL.format(safe=safe),
          f"{tag}: must be NO_ELICITATION_REFUSAL verbatim: {out}")
    check(out.startswith(REFUSAL_HEAD) and "деньги на месте" in out
          and "повтори" not in out.lower(),
          f"{tag}: honest wording (what did not happen, money stayed, no retry hint): {out}")


def test_headless_money_tools_refuse_before_prepare():
    """No ctx = no button possible. transfer / pay_bill / ticket_pay must say so at
    once — before the recipient resolve, the commission quote or order_details —
    and leave zero journal attempts."""
    _no_threshold()

    _reset()
    t = TransferSession(_two_banks())
    out = run_tool(t, server.transfer, 1000, "+79991234567", ctx=None)
    _check_no_button_refusal(out, "transfer headless")
    check(t.resolved == [] and t.sent is None,
          f"transfer headless: no recipient resolve, nothing sent: {t.resolved}, {t.sent}")

    _reset()
    b = BillSession()
    out = run_tool(b, server.pay_bill, "mosenergo", '{"account": "123"}', 1000, ctx=None)
    _check_no_button_refusal(out, "pay_bill headless")
    check(b.looked_up == [] and b.commissioned is None and b.posted is None,
          f"pay_bill headless: no provider lookup, no quote, no POST: "
          f"{b.looked_up}, {b.commissioned}, {b.posted}")

    _reset()
    k = TicketSession()
    out = run_tool(k, server.ticket_pay, "ORD-HEADLESS", 1760, "tok", ctx=None)
    _check_no_button_refusal(out, "ticket_pay headless")
    check(k.detailed == [] and k.paid is None,
          f"ticket_pay headless: no order_details, no payment: {k.detailed}, {k.paid}")

    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", f"headless refusals must leave zero journal attempts: {blob!r}")

    # Below a positive threshold the same headless calls pay — no button needed.
    try:
        os.environ["TBANK_CONFIRM_ABOVE"] = "5000"
        _reset()
        b2 = BillSession()
        out = run_tool(b2, server.pay_bill, "mosenergo", '{"account": "123"}', 1000, ctx=None)
        check(b2.posted is not None and "BILL-1" in out,
              f"pay_bill headless below the threshold must pay: {out}")
        k2 = TicketSession()
        out = run_tool(k2, server.ticket_pay, "ORD-SMALL", 1760, "tok", ctx=None)
        check(k2.paid is not None and "ОПЛАЧЕНО" in out,
              f"ticket_pay headless below the threshold must pay: {out}")
    finally:
        _no_threshold()
    print("  headless: transfer/pay_bill/ticket_pay refuse before prepare; pay below threshold")


# ---- grocery_checkout: the tool quotes, the button names the sum ------------

QUOTE = 3700.63


class _GroceryDoubles:
    """Patches the two halves grocery_checkout drives: the Playwright quote
    (_grocery_quote_sum) and the sync body (_do_grocery_checkout). Records every
    call so a test can assert the quote/body did or did not run, and with what.

    `quote` mirrors what the real _grocery_quote_sum can return: a number (→ the
    (sum, text) pair) or a STRING, which is how it reports an empty cart / a failed
    preview and means «there is no sum to confirm»."""

    def __init__(self, quote=QUOTE):
        self.quote = quote
        self.quotes = 0
        self.bodies = []                 # {"expected_sum": …, "dry_run": …}

    def _quote(self, app_id, point_id, account_id):
        self.quotes += 1
        if isinstance(self.quote, str):
            return self.quote            # empty cart / preview failed
        return self.quote, f"[preview] К ОПЛАТЕ: {self.quote} ₽"

    def _body(self, app_id, point_id, force, account_id, expected_sum, dry_run):
        self.bodies.append({"expected_sum": expected_sum, "dry_run": dry_run})
        return (f"[preview] К ОПЛАТЕ: {self.quote} ₽" if dry_run
                else f"✓ ORDER PAID sum={expected_sum}")

    def __enter__(self):
        self._saved = server._grocery_quote_sum, server._do_grocery_checkout
        server._grocery_quote_sum = self._quote
        server._do_grocery_checkout = self._body
        return self

    def __exit__(self, *exc):
        server._grocery_quote_sum, server._do_grocery_checkout = self._saved
        return False

    @property
    def charges(self):
        return [b for b in self.bodies if not b["dry_run"]]


def _checkout(ctx, **kw):
    return asyncio.run(server.grocery_checkout("204", "5980", ctx=ctx, **kw))


def test_grocery_checkout_quote_then_confirm_locks_the_sum():
    _no_threshold()
    with _GroceryDoubles() as g:
        fctx = accept_ctx()
        out = _checkout(fctx)
        check(g.quotes == 1, f"the tool must quote the sum itself, once: {g.quotes}")
        check(fctx.asked and "3 700.63" in fctx.asked[0][0],
              f"the gate must name the quoted sum: {fctx.asked}")
        check(len(g.charges) == 1 and g.charges[0]["expected_sum"] == QUOTE,
              f"the confirmed sum must be locked as expected_sum: {g.bodies}")
        check("PAID" in out, f"accept must run the real checkout: {out}")

    # Decline → the real body never runs.
    with _GroceryDoubles() as g:
        out2 = _checkout(decline_ctx())
        check(g.quotes == 1 and g.bodies == [],
              f"a declined checkout must quote but never run the body: {g.bodies}")
        # The quote already asked the store for a delivery slot, so the refusal
        # says «Заказ не создан» — NOT «Ничего не сделано», which would be a
        # promise the code cannot keep.
        check("Отменено" in out2 and "Заказ не создан" in out2
              and "Ничего не сделано" not in out2, f"refusal wording: {out2}")
    print("  grocery_checkout: quote→confirm locks the sum, decline skips the body")


def test_grocery_checkout_headless_refuses_before_quoting():
    """No button possible → refuse before the (slow, Playwright) quote is even
    started; dry_run is a read-only preview and keeps working in any client."""
    _no_threshold()
    with _GroceryDoubles() as g:
        out = _checkout(None)
        _check_no_button_refusal(out, "grocery headless",
                                 safe="заказ не создан, деньги на месте")
        check("заказ не создан" in out, f"the grocery refusal must say no order: {out}")
        check(g.quotes == 0, f"headless: the quote helper must NOT run: {g.quotes}")
        check(g.bodies == [], f"headless: the body must NOT run: {g.bodies}")

        # An agent-supplied expected_sum changes nothing: still no button, still refused.
        out = _checkout(None, expected_sum=QUOTE)
        _check_no_button_refusal(out, "grocery headless + expected_sum",
                                 safe="заказ не создан, деньги на месте")
        check(g.quotes == 0 and g.bodies == [],
              f"expected_sum is not a substitute for the button: {g.quotes}, {g.bodies}")

        # dry_run stays a pure preview, headless-capable.
        out = _checkout(None, dry_run=True)
        check(len(g.bodies) == 1 and g.bodies[0]["dry_run"] is True,
              f"dry_run must preview once through the body: {g.bodies}")
        check(g.quotes == 0, "dry_run must not additionally run the quote helper")
        check("preview" in out and "3700.63" in out, f"dry_run returns the preview: {out}")
        check(REFUSAL_HEAD not in out, f"dry_run is not refused headless: {out}")
    print("  grocery_checkout headless: refused before quoting; dry_run previews")


def test_grocery_checkout_stale_expected_sum_shows_both_and_charges_the_quote():
    """The agent quoted the user 3 650 from an earlier dry_run; the bank now says
    3 700.63. The button must show BOTH numbers, and accept charges the QUOTE."""
    _no_threshold()
    with _GroceryDoubles() as g:
        fctx = accept_ctx()
        out = _checkout(fctx, expected_sum=3650.00)
        check(len(fctx.asked) == 1, f"one button expected: {fctx.asked}")
        if fctx.asked:
            msg = fctx.asked[0][0]
            check("3 700.63" in msg and "3 650.00" in msg,
                  f"the button must name both the quote and the stale sum: {msg}")
            check("банк пересчитал" in msg and "было" in msg,
                  f"the button must say the bank recounted: {msg}")
        check(len(g.charges) == 1 and g.charges[0]["expected_sum"] == QUOTE,
              f"accept must charge the QUOTED total, not the stale number: {g.bodies}")
        check("PAID" in out, f"accept runs the body: {out}")

    # A drift within a kopeck is not a drift: plain button, quote still charged.
    with _GroceryDoubles() as g:
        fctx = accept_ctx()
        _checkout(fctx, expected_sum=QUOTE + 0.005)
        check(fctx.asked and "пересчитал" not in fctx.asked[0][0],
              f"sub-kopeck drift must not be reported: {fctx.asked}")
        check(g.charges and g.charges[0]["expected_sum"] == QUOTE,
              f"the body still gets the quote: {g.bodies}")
    print("  grocery_checkout: stale expected_sum → both sums on the button, quote charged")


def test_grocery_checkout_matching_expected_sum_plain_button():
    _no_threshold()
    with _GroceryDoubles() as g:
        fctx = accept_ctx()
        _checkout(fctx, expected_sum=QUOTE)
        check(len(fctx.asked) == 1, f"one button expected: {fctx.asked}")
        if fctx.asked:
            msg = fctx.asked[0][0]
            check(msg == f"Оформить заказ на {server._money(QUOTE, 'RUB')}?",
                  f"a matching expected_sum gets the plain button: {msg}")
            check("было" not in msg and "пересчитал" not in msg,
                  f"no recount remark when the sums agree: {msg}")
        check(g.charges and g.charges[0]["expected_sum"] == QUOTE,
              f"the body gets the quote: {g.bodies}")
    print("  grocery_checkout: matching expected_sum → plain «Оформить заказ на …?»")


def test_grocery_checkout_below_threshold_pins_the_quote_without_a_button():
    """With TBANK_CONFIRM_ABOVE above the quote nothing is asked, but the charge is
    still pinned to the quote — the kopeck guard in checkout.py is `if expected_sum
    and …`, so 0 would let any recount through silently.

    A client that cannot show the button is refused BEFORE the quote whatever the
    threshold — unlike the other paying tools, this one would have to run Playwright
    (page load + a /grocery/deliveries POST) just to learn a sum it can never
    confirm. So «refused before any HTTP» holds here too."""
    try:
        os.environ["TBANK_CONFIRM_ABOVE"] = "5000"       # > 3700.63
        with _GroceryDoubles() as g:
            ctx = accept_ctx()
            out = _checkout(ctx)                        # no expected_sum from the agent
            check(ctx.asked == [], f"below the threshold no button: {ctx.asked}")
            check(g.quotes == 1, f"the quote still runs to pin the sum: {g.quotes}")
            check(len(g.charges) == 1 and g.charges[0]["expected_sum"] == QUOTE,
                  f"expected_sum must be pinned to the quote, not 0: {g.bodies}")
            check("PAID" in out, f"the checkout runs: {out}")

        # Headless, whatever the threshold says: refused, and NOTHING ran.
        for threshold in ("5000", "1000"):              # above and below the quote
            os.environ["TBANK_CONFIRM_ABOVE"] = threshold
            with _GroceryDoubles() as g:
                out = _checkout(None)
                _check_no_button_refusal(out, f"grocery headless (порог {threshold})",
                                         safe="заказ не создан, деньги на месте")
                check(g.quotes == 0 and g.bodies == [],
                      f"порог {threshold}: headless must be refused BEFORE the quote "
                      f"(quotes={g.quotes}, bodies={g.bodies})")
    finally:
        _no_threshold()
    print("  grocery_checkout: below threshold no button, quote still pinned; "
          "headless refused before quoting at any threshold")


def test_grocery_checkout_quote_refusal_is_returned_unchanged():
    """An empty cart (or a preview that failed) makes _grocery_quote_sum return a
    STRING instead of a sum. That string is the answer, verbatim: no button — there
    is no number to confirm — and the checkout body never runs, so nothing can be
    charged."""
    _no_threshold()
    empty = ("[store appId=204 pointId=5980] Корзина пуста — "
             "не из чего оформлять заказ.")
    with _GroceryDoubles(quote=empty) as g:
        fctx = accept_ctx()
        out = _checkout(fctx)
        check(out == empty, f"the quote's own refusal must come back unchanged: {out!r}")
        check(g.quotes == 1, f"the quote ran once: {g.quotes}")
        check(fctx.asked == [], f"nothing to confirm → no button: {fctx.asked}")
        check(g.bodies == [], f"the checkout body must never run: {g.bodies}")
    print("  grocery_checkout: a quote refusal string is returned as-is, nothing charged")


def test_grocery_checkout_unpriced_quote_refuses_instead_of_confirming_it():
    """A quote that is not a finite positive number must not become the confirmed
    sum. 0 is the dangerous one: it would put «0.00 RUB» on the button AND switch
    off the kopeck-exact guard in checkout.py, which reads `if expected_sum and …`.
    NaN/inf are the same trap from the other side — every comparison with them is
    False. All four must refuse: no button, no charge, and the preview text is
    handed back so the human can see what the store actually said."""
    _no_threshold()
    for bad in (0, 0.0, float("nan"), float("inf")):
        with _GroceryDoubles(quote=bad) as g:
            fctx = accept_ctx()
            out = _checkout(fctx)
            check(out.startswith("ОПЛАТА НЕ ВЫПОЛНЕНА: предпросмотр вернул сумму "
                                 f"{bad!r} —"),
                  f"{bad!r}: the refusal must name the unusable sum: {out!r}")
            check("заказ не создан и деньги на месте" in out,
                  f"{bad!r}: the refusal must say what did NOT happen: {out!r}")
            check(f"[preview] К ОПЛАТЕ: {bad} ₽" in out,
                  f"{bad!r}: the preview text must be attached: {out!r}")
            check(fctx.asked == [], f"{bad!r}: an unusable sum must not reach a button: "
                                    f"{fctx.asked}")
            check(g.quotes == 1 and g.bodies == [],
                  f"{bad!r}: quoted once, charged never: {g.quotes}, {g.bodies}")
    print("  grocery_checkout: an unpriced quote (0/NaN/inf) refuses, never confirms")


# ---- the gate itself: whatever answers it, answers the tool -----------------
#
# The picker tests above answer the BANK dialog: with two banks and no default, a
# decline never reaches the money gate at all. One default recipient + one account
# removes both pickers, so the ONLY dialog left is the gate — which is the point.

DECLINE_TEXT = ("Отменено пользователем (кнопка «Отмена»). "
                "Ничего не сделано, деньги на месте.")
NOT_CONFIRMED_TEXT = ("Подтверждение не получено (окно закрыто или истёк таймаут). "
                      "Ничего не сделано, деньги на месте. Когда пользователь готов — "
                      "повтори тот же вызов.")
# grocery passes did="Заказ не создан" (the quote already asked the store for a
# delivery slot, so «ничего не сделано» would be a lie) and safe="деньги на месте",
# so its tail is «Заказ не создан, деньги на месте» — said once, not twice.
NOT_CONFIRMED_GROCERY = ("Подтверждение не получено (окно закрыто или истёк таймаут). "
                         "Заказ не создан, деньги на месте. Когда пользователь готов — "
                         "повтори тот же вызов.")


def _one_default_bank():
    """A single recipient, already the default → no bank picker."""
    return [dict(_two_banks()[0], is_default_bank=True)]


def test_transfer_gate_decline_stops_the_payment():
    _no_threshold()
    _reset()
    s = TransferSession(_one_default_bank())
    fctx = decline_ctx()
    out = run_tool(s, server.transfer, 1000, "+79991234567", ctx=fctx)
    check(len(fctx.asked) == 1, f"the money gate must be the only dialog: {fctx.asked}")
    check(s.sent is None, f"a declined gate must send nothing: {s.sent}")
    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", f"a declined gate must leave no journal attempt: {blob!r}")
    check(out == DECLINE_TEXT,
          f"the tool must RETURN the gate's refusal, not pay past it: {out!r}")
    print("  transfer: a declined money gate is the whole answer — nothing sent")


def test_transfer_without_the_capability_refuses_before_resolve_recipient():
    """No button possible — because the client never declared elicitation, or
    because there is no ctx at all — is refused identically, and before the SBP
    resolve: a client that can never confirm must not cost the user a round-trip."""
    _no_threshold()
    for tag, ctx in (("incapable", incapable_ctx()), ("headless", None)):
        _reset()
        s = TransferSession(_one_default_bank())
        out = run_tool(s, server.transfer, 1000, "+79991234567", ctx=ctx)
        _check_no_button_refusal(out, f"transfer {tag}")
        check(s.resolved == [], f"transfer {tag}: resolve_recipient must not run: {s.resolved}")
        check(s.sent is None, f"transfer {tag}: nothing may be sent: {s.sent}")
        if ctx is not None:
            check(ctx.asked == [],
                  f"transfer {tag}: no dialog may even be attempted: {ctx.asked}")
        blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
        check(blob.strip() == "", f"transfer {tag}: no journal attempt: {blob!r}")
    print("  transfer: no elicitation capability → refused before resolve_recipient")


def test_transfer_button_names_the_sum_and_the_payee():
    """The button string IS the disclosure: the sum via _money and who gets it
    (masked name · bank, as resolved), and a schema with no fields."""
    _no_threshold()
    _reset()
    s = TransferSession(_one_default_bank())
    fctx = accept_ctx()
    out = run_tool(s, server.transfer, 1000, "+79991234567", ctx=fctx)
    check(len(fctx.asked) == 1, f"one button expected: {fctx.asked}")
    if fctx.asked:
        msg, schema = fctx.asked[0]
        check(msg == "Перевести 1 000.00 RUB → И. И. · Т-Банк?",
              f"the transfer button must name sum and payee: {msg!r}")
        check(msg == f"Перевести {server._money(1000, 'RUB')} → И. И. · Т-Банк?",
              f"the sum must be rendered by _money: {msg!r}")
        check(schema.model_json_schema().get("properties", {}) == {},
              f"the transfer gate must elicit no fields: {schema.model_json_schema()}")
    check(s.sent is not None and "PAY-1" in out, f"accept pays: {out}")
    print("  transfer: the button names sum + payee, and asks for no fields")


def _four_gated_tools(make_ctx, tag):
    """Drive the four tools that confirm through _money_gate with a fresh ctx from
    make_ctx(), and assert nothing moved: no transfer, no bill POST, no gateway
    call, no grocery charge, no journal. Returns {tool name: the tool's answer}.

    The read-only prepare steps DO run before the button (that is where the real
    total comes from) — what must not happen is a payment."""
    _no_threshold()
    _reset()
    outs = {}

    t = TransferSession(_one_default_bank())
    outs["transfer"] = run_tool(t, server.transfer, 1000, "+79991234567", ctx=make_ctx())
    check(t.sent is None, f"{tag}/transfer: nothing may be sent: {t.sent}")

    b = BillSession()
    outs["pay_bill"] = run_tool(b, server.pay_bill, "mosenergo", '{"account": "123"}',
                                1000, ctx=make_ctx())
    check(b.posted is None, f"{tag}/pay_bill: nothing may be posted: {b.posted}")
    check(b.commissioned is not None,
          f"{tag}/pay_bill: the read-only quote still runs before the button")

    k = TicketSession()
    outs["ticket_pay"] = run_tool(k, server.ticket_pay, "10000000000", 1760, "tok",
                                  ctx=make_ctx())
    check(k.paid is None, f"{tag}/ticket_pay: the gateway must not be called: {k.paid}")

    with _GroceryDoubles() as g:
        outs["grocery_checkout"] = _checkout(make_ctx())
        check(g.quotes == 1, f"{tag}/grocery: quoted once before the button: {g.quotes}")
        check(g.bodies == [], f"{tag}/grocery: the checkout body must not run: {g.bodies}")

    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "",
          f"{tag}: an unconfirmed payment must leave no journal attempt: {blob!r}")
    return outs


def _check_not_confirmed(outs, tag):
    for tool, out in outs.items():
        want = NOT_CONFIRMED_GROCERY if tool == "grocery_checkout" else NOT_CONFIRMED_TEXT
        check(out == want, f"{tag}/{tool}: exact «not confirmed» wording: {out!r}")
        check("Подтверждение не получено" in out and "повтори тот же вызов" in out,
              f"{tag}/{tool}: must read as not-confirmed + retryable: {out!r}")
        check(REFUSAL_HEAD not in out and "Отменено пользователем" not in out,
              f"{tag}/{tool}: a closed window is neither a decline nor «no capability»: "
              f"{out!r}")


def test_money_gate_cancel_confirms_nothing():
    """cancel = the window was closed / dismissed. Not an accept, and not a decline
    either: the user never answered, so the tool says so and invites a retry."""
    _check_not_confirmed(_four_gated_tools(cancel_ctx, "cancel"), "cancel")
    print("  money gate: cancel pays nothing in transfer/pay_bill/ticket_pay/grocery")


def test_money_gate_client_error_confirms_nothing():
    """The client raised McpError instead of answering (its form timed out, or it
    errored out). The user was asked and did not answer — that must read exactly
    like a closed window, never as «proceed»."""
    _check_not_confirmed(_four_gated_tools(lambda: FakeCtx(exc=mcp_error()), "McpError"),
                         "McpError")
    print("  money gate: an McpError from the client pays nothing either")


def test_confirm_threshold_boundary_is_inclusive_everywhere():
    """«Для сумм ОТ порога» is inclusive: at exactly TBANK_CONFIRM_ABOVE the button
    IS shown, one kopeck below it is not — and below it the payment still goes
    through without one. Pinned in a client that CAN show the button, for every
    tool (the headless tests above prove the refusal, not where the line is)."""
    try:
        os.environ["TBANK_CONFIRM_ABOVE"] = "1000"
        _reset()
        at = TransferSession(_one_default_bank()); ctx_at = accept_ctx()
        run_tool(at, server.transfer, 1000, "+79991234567", ctx=ctx_at)
        check(len(ctx_at.asked) == 1 and at.sent is not None,
              f"transfer at the threshold: button, then pays: {ctx_at.asked}, {at.sent}")
        _reset()
        below = TransferSession(_one_default_bank()); ctx_below = accept_ctx()
        run_tool(below, server.transfer, 999.99, "+79991234567", ctx=ctx_below)
        check(ctx_below.asked == [], f"transfer a kopeck below: no button: {ctx_below.asked}")
        check(below.sent is not None, f"transfer below the threshold still pays: {below.sent}")

        # pay_bill's threshold applies to what LEAVES THE ACCOUNT (amount + fee),
        # not to the bill: with a 3% commission a 999.99 ₽ bill debits 1 029.99 ₽,
        # which is over the «спрашивай от 1000» line and must be asked about. The
        # bill itself has to drop to 970.87 before the total clears the threshold.
        _reset()
        b_at = BillSession(); ctx_at = accept_ctx()
        run_tool(b_at, server.pay_bill, "mosenergo", '{"account": "123"}', 1000, ctx=ctx_at)
        check(len(ctx_at.asked) == 1 and b_at.posted is not None,
              f"pay_bill at the threshold: button, then pays: {ctx_at.asked}, {b_at.posted}")
        _reset()
        b_fee = BillSession(); ctx_fee = accept_ctx()
        run_tool(b_fee, server.pay_bill, "mosenergo", '{"account": "123"}', 999.99,
                 ctx=ctx_fee)
        check(len(ctx_fee.asked) == 1,
              f"a bill below the threshold whose TOTAL is above it must still be "
              f"asked about — 999.99 + 3% = 1 029.99 leaves the account: {ctx_fee.asked}")
        if ctx_fee.asked:
            check("1 029.99" in ctx_fee.asked[0][0],
                  f"and the button names that total: {ctx_fee.asked[0][0]!r}")
        _reset()
        b_below = BillSession(); ctx_below = accept_ctx()
        run_tool(b_below, server.pay_bill, "mosenergo", '{"account": "123"}', 900,
                 ctx=ctx_below)   # 900 + 27 = 927 < 1000
        check(ctx_below.asked == [], f"pay_bill a kopeck below: no button: {ctx_below.asked}")
        check(b_below.posted is not None, f"pay_bill below the threshold pays: {b_below.posted}")
        _reset()
        b_head = BillSession()
        head = run_tool(b_head, server.pay_bill, "mosenergo", '{"account": "123"}',
                        999.99, ctx=None)     # headless: the total needs a button
        check(b_head.posted is None and head.startswith("ПЛАТЁЖ НЕ ВЫПОЛНЕН"),
              f"a client with no button must not pay a total above the threshold "
              f"just because the BILL is below it: posted={b_head.posted}, {head!r}")

        os.environ["TBANK_CONFIRM_ABOVE"] = "1760"
        _reset()
        k_at = TicketSession(booked=1760); ctx_at = accept_ctx()
        run_tool(k_at, server.ticket_pay, "10000000000", 1760, "tok", ctx=ctx_at)
        check(len(ctx_at.asked) == 1 and k_at.paid is not None,
              f"ticket_pay at the threshold: button, then pays: {ctx_at.asked}, {k_at.paid}")
        _reset()
        k_below = TicketSession(booked=1759.99); ctx_below = accept_ctx()
        run_tool(k_below, server.ticket_pay, "10000000001", 1759.99, "tok", ctx=ctx_below)
        check(ctx_below.asked == [], f"ticket_pay a kopeck below: no button: {ctx_below.asked}")
        check(k_below.paid is not None, f"ticket_pay below the threshold pays: {k_below.paid}")

        os.environ["TBANK_CONFIRM_ABOVE"] = "3700.63"
        with _GroceryDoubles(quote=3700.63) as g:
            ctx_at = accept_ctx()
            _checkout(ctx_at)
            check(len(ctx_at.asked) == 1 and len(g.charges) == 1,
                  f"grocery at the threshold: button, then charges: {ctx_at.asked}, {g.bodies}")
        with _GroceryDoubles(quote=3700.62) as g:
            ctx_below = accept_ctx()
            _checkout(ctx_below)
            check(ctx_below.asked == [], f"grocery a kopeck below: no button: {ctx_below.asked}")
            check(len(g.charges) == 1 and g.charges[0]["expected_sum"] == 3700.62,
                  f"grocery below the threshold charges the quote: {g.bodies}")
    finally:
        _no_threshold()
    print("  threshold: exactly at the threshold the button is shown, a kopeck below it "
          "is not (transfer/pay_bill/ticket_pay/grocery)")


def main():
    for t in (test_transfer_bank_picker_then_gate_pays_the_chosen_bank,
              test_transfer_bank_picker_declined_writes_no_journal,
              test_transfer_default_bank_skips_the_picker,
              test_source_picker_only_fires_with_more_than_one_account,
              test_pay_bill_gate_names_total_and_fee,
              test_pay_bill_decline_posts_nothing,
              test_ticket_pay_gate_pays_on_accept,
              test_ticket_pay_duplicate_guard_blocks_unconfirmed_then_force,
              test_ticket_pay_decline_pays_nothing,
              test_ticket_pay_button_names_the_order_and_the_sum,
              test_ticket_pay_refuses_an_unpayable_amount_before_anything,
              test_headless_money_tools_refuse_before_prepare,
              test_grocery_checkout_quote_then_confirm_locks_the_sum,
              test_grocery_checkout_headless_refuses_before_quoting,
              test_grocery_checkout_stale_expected_sum_shows_both_and_charges_the_quote,
              test_grocery_checkout_matching_expected_sum_plain_button,
              test_grocery_checkout_below_threshold_pins_the_quote_without_a_button,
              test_grocery_checkout_quote_refusal_is_returned_unchanged,
              test_grocery_checkout_unpriced_quote_refuses_instead_of_confirming_it,
              test_transfer_gate_decline_stops_the_payment,
              test_transfer_without_the_capability_refuses_before_resolve_recipient,
              test_transfer_button_names_the_sum_and_the_payee,
              test_money_gate_cancel_confirms_nothing,
              test_money_gate_client_error_confirms_nothing,
              test_confirm_threshold_boundary_is_inclusive_everywhere):
        t()
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nall elicitation-flow tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
