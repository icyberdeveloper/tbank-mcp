"""Elicitation (input-free only): pickers + the money gate on every payment tool.
Each test EXECUTES the real async tool through a fake ctx and a fake session.

LIVE FINDING: the Telegram/Hermes client renders ONLY yes/no buttons — no text
field. So the input forms (OTP/SMS/PIN/amount/comment) and the non-payment gates
(messenger_send, ticket/grocery cancel, card reveal) were rolled back. What remains,
and what these tests pin:
  * transfer     — SBP bank picker BEFORE any journal write; gate; decline is clean.
  * from_account — picker only when >1 ruble account; the pick is what's debited.
  * pay_bill     — gate names the real total+fee; decline posts nothing.
  * ticket_pay   — gate; the duplicate guard blocks a repeat, force overrides.
  * grocery_checkout — quote→confirm locks the sum; headless never charges unquoted.

    python3 tests/test_elicitation_flows.py
"""
import asyncio
import inspect
import os
import sys
import tempfile
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="tbank-eflows-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from mcp.shared.exceptions import McpError                    # noqa: E402
from mcp.types import ErrorData                               # noqa: E402

from src import journal, observability, server, trace         # noqa: E402
from src.client import TbankApiError                          # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def _mcp_error():
    return McpError(ErrorData(code=-32603, message="timed out"))


class FakeCtx:
    """Fake fastmcp Context. `pick` selects options[pick] from a choice schema's
    enum, so picker tests do not depend on the exact label text the tool builds."""

    def __init__(self, action="accept", data=None, capable=True, exc=None, pick=None):
        self.request_context = SimpleNamespace(
            session=SimpleNamespace(check_client_capability=lambda cap: capable))
        self._action, self._data, self._exc, self._pick = action, data or {}, exc, pick
        self.asked = []

    async def elicit(self, message, schema):
        self.asked.append((message, schema))
        if self._exc:
            raise self._exc
        if self._action != "accept":
            return SimpleNamespace(action=self._action)
        data = dict(self._data)
        if self._pick is not None:
            props = schema.model_json_schema().get("properties", {})
            for fname, spec in props.items():
                if "enum" in spec:
                    data[fname] = spec["enum"][self._pick]
        return SimpleNamespace(action="accept", data=schema(**data))


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

    def ensure_fresh(self, *a, **k):
        return None

    def resolve_recipient(self, phone):
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
    fctx = FakeCtx(action="accept", pick=1)   # choose Сбер, then accept the gate
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
                   ctx=FakeCtx(action="decline"))
    check(s.sent is None, "a declined bank pick must not send the transfer")
    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", f"a declined pick must leave no journal attempt: {blob!r}")
    check("не выбран" in out and "на месте" in out, f"honest refusal: {out}")
    print("  transfer: a declined bank pick leaves no journal trace")


def test_transfer_default_bank_skips_the_picker():
    _reset()
    recips = _two_banks(); recips[0]["is_default_bank"] = True
    s = TransferSession(recips)
    fctx = FakeCtx(action="accept")           # only the gate, no picker
    out = run_tool(s, server.transfer, 1000, "+79991234567", ctx=fctx)
    check(len(fctx.asked) == 1, f"a default bank must skip the picker: {len(fctx.asked)}")
    check(s.sent and s.sent["pointer_link_id"] == "LINK-T", f"default used: {s.sent}")
    print("  transfer: a default bank means no picker, just the gate")


# ---- from_account picker ---------------------------------------------------

def test_source_picker_only_fires_with_more_than_one_account():
    _reset()
    one = TransferSession(_two_banks()[:1])   # single recipient, single account
    one.recipients[0]["is_default_bank"] = True
    fctx1 = FakeCtx(action="accept")
    run_tool(one, server.transfer, 1000, "+79991234567", ctx=fctx1)
    check(len(fctx1.asked) == 1, "one account → no source picker (just the gate)")

    _reset()
    multi = TransferSession(
        [dict(_two_banks()[0], is_default_bank=True)],
        accounts=[{"id": "1111111111", "name": "Осн", "balance": 5000},
                  {"id": "2222222222", "name": "Втор", "balance": 300}])
    fctx2 = FakeCtx(action="accept", pick=1)  # gate accept + pick 2nd account
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

    def ensure_fresh(self, *a, **k):
        return None

    def ruble_source_accounts(self):
        return [{"id": "1111111111", "name": "Осн", "balance": 9000}]

    def _source_account(self):
        return "1111111111"

    def find_provider(self, provider_id, group=""):
        return {"name": "Мосэнерго", "id": provider_id}

    def validate_provider_fields(self, prov, vals):
        return []

    def payment_commission(self, body):
        self.commissioned = body
        return {"value": {"value": 30}, "total": {"value": 1030}}

    def pay_bill(self, provider_id, vals, amount, *, account, user_payment_id):
        self.posted = {"provider": provider_id, "amount": amount, "account": account}
        return {"payload": {"paymentId": "BILL-1"}}


def test_pay_bill_gate_names_total_and_fee():
    _reset()
    s = BillSession()
    fctx = FakeCtx(action="accept")
    out = run_tool(s, server.pay_bill, "mosenergo", '{"account": "123"}', 1000, ctx=fctx)
    check(len(fctx.asked) == 1, "one gate expected")
    if fctx.asked:
        msg = fctx.asked[0][0]
        check("1 030" in msg and "30" in msg and "Мосэнерго" in msg,
              f"the gate must name total, fee and provider: {msg}")
    check(s.posted and "BILL-1" in out, f"accept must pay: {out}")
    print("  pay_bill: the gate names total + fee, accept pays")


def test_pay_bill_decline_posts_nothing():
    _reset()
    s = BillSession()
    out = run_tool(s, server.pay_bill, "mosenergo", '{"account": "123"}', 1000,
                   ctx=FakeCtx(action="decline"))
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

    def ensure_fresh(self, *a, **k):
        return None

    def ruble_source_accounts(self):
        return [{"id": "1111111111", "name": "Осн", "balance": 5000}]

    def _source_account(self):
        return "1111111111"

    def order_details(self, order_id):
        return {"cartInfo": {"amount": self.booked}} if self.booked else {}

    def pay_marketplace_order(self, order_id, amount, account, token):
        if self.fail == "transport":
            raise ConnectionError("reset")   # → outcome recorded as "unknown"
        self.paid = {"order": order_id, "amount": amount, "account": account}
        return {"paymentId": "TPAY-1", "stage": {"status": "SUCCESS"}}


def test_ticket_pay_gate_pays_on_accept():
    _reset()
    s = TicketSession()
    out = run_tool(s, server.ticket_pay, "ORD-OK", 1760, "tok", ctx=FakeCtx(action="accept"))
    check(s.paid is not None and "ОПЛАЧЕНО" in out, f"accept must pay: {out}")
    print("  ticket_pay: gate pays on accept")


def test_ticket_pay_duplicate_guard_blocks_unconfirmed_then_force():
    _reset()
    # First attempt's outcome is UNKNOWN (transport failure) — a blocking status,
    # exactly like transfers. A confirmed 'paid' would NOT block (a deliberate
    # second identical payment is allowed); an unconfirmed one must.
    bad = TicketSession(fail="transport")
    out1 = run_tool(bad, server.ticket_pay, "ORD-DUP", 1760, "tok",
                    ctx=FakeCtx(action="accept"))
    check("НЕИЗВЕСТЕН" in out1, f"a transport failure is an unknown outcome: {out1}")

    s2 = TicketSession()
    out2 = run_tool(s2, server.ticket_pay, "ORD-DUP", 1760, "tok",
                    ctx=FakeCtx(action="accept"))
    check(s2.paid is None and "ЗАБЛОКИРОВАН" in out2,
          f"a repeat of an unconfirmed payment must be blocked: {out2}")

    s3 = TicketSession()
    out3 = run_tool(s3, server.ticket_pay, "ORD-DUP", 1760, "tok", force=True,
                    ctx=FakeCtx(action="accept"))
    check(s3.paid is not None and "ОПЛАЧЕНО" in out3, f"force overrides the guard: {out3}")
    print("  ticket_pay: unconfirmed repeat blocked, force overrides")


def test_ticket_pay_decline_pays_nothing():
    _reset()
    s = TicketSession()
    out = run_tool(s, server.ticket_pay, "ORD-DECL", 1760, "tok",
                   ctx=FakeCtx(action="decline"))
    check(s.paid is None, "a declined ticket payment must not reach the gateway")
    blob = open(journal.ATTEMPTS_FILE, encoding="utf-8").read()
    check(blob.strip() == "", "a declined ticket payment must leave no attempt")
    print("  ticket_pay: decline pays nothing, no journal")


# ---- grocery_checkout quote → confirm -------------------------------------

def test_grocery_checkout_quote_then_confirm_locks_the_sum():
    seen = {}

    def fake_quote(app_id, point_id, account_id):
        return 3700.63, "[preview] К ОПЛАТЕ: 3700.63 ₽"

    def fake_body(app_id, point_id, force, account_id, expected_sum, dry_run):
        seen["expected_sum"] = expected_sum
        return f"✓ ORDER PAID sum={expected_sum}"

    saved_q, saved_b = server._grocery_quote_sum, server._do_grocery_checkout
    server._grocery_quote_sum = fake_quote
    server._do_grocery_checkout = fake_body
    try:
        fctx = FakeCtx(action="accept")
        out = asyncio.run(server.grocery_checkout("204", "5980", ctx=fctx))
        check(fctx.asked and "3 700.63" in fctx.asked[0][0],
              f"the gate must name the quoted sum: {fctx.asked}")
        check(seen.get("expected_sum") == 3700.63,
              f"the confirmed sum must be locked as expected_sum: {seen}")
        check("PAID" in out, f"accept must run the real checkout: {out}")

        # Decline → the real body never runs.
        seen.clear()
        out2 = asyncio.run(server.grocery_checkout("204", "5980",
                                                   ctx=FakeCtx(action="decline")))
        check("expected_sum" not in seen, "a declined checkout must not run the body")
        check("заказ не создан" in out2, f"refusal wording: {out2}")
    finally:
        server._grocery_quote_sum = saved_q
        server._do_grocery_checkout = saved_b
    print("  grocery_checkout: quote→confirm locks the sum, decline skips the body")


def test_headless_checkout_without_expected_sum_refuses_to_charge():
    """On a client with no elicitation, expected_sum is the ONLY guard on the sum —
    and the kopeck-exact check in checkout.py is `if expected_sum and …`, so
    expected_sum=0 disables it and the bank's recomputed number gets charged silently.
    A headless checkout with no expected_sum must refuse to charge and hand back the
    dry-run quote, never pay."""
    seen = []

    def fake_body(app_id, point_id, force, account_id, expected_sum, dry_run):
        seen.append({"expected_sum": expected_sum, "dry_run": dry_run})
        return "[preview] К ОПЛАТЕ: 3700.63 ₽" if dry_run else "✓ ORDER PAID"

    saved_b = server._do_grocery_checkout
    server._do_grocery_checkout = fake_body
    try:
        # No ctx → no elicitation; no expected_sum → must refuse and quote, not charge.
        out = asyncio.run(server.grocery_checkout("204", "5980", ctx=None))
        check(seen and all(c["dry_run"] for c in seen),
              f"a headless checkout with no expected_sum must never charge: {seen}")
        check("ОПЛАТА НЕ ВЫПОЛНЕНА" in out and "expected_sum" in out,
              f"the refusal must name expected_sum and show the quote: {out!r}")

        # Same client, but the agent DID pass expected_sum → the real charge runs once.
        seen.clear()
        out2 = asyncio.run(server.grocery_checkout("204", "5980",
                                                   expected_sum=3700.63, ctx=None))
        check(len(seen) == 1 and seen[0]["dry_run"] is False
              and seen[0]["expected_sum"] == 3700.63,
              f"with expected_sum the real checkout must run once: {seen}")
        check("PAID" in out2, f"a confirmed headless checkout pays: {out2!r}")

        # dry_run stays a pure preview and must not be diverted into the refusal path.
        seen.clear()
        asyncio.run(server.grocery_checkout("204", "5980", dry_run=True, ctx=None))
        check(len(seen) == 1 and seen[0]["dry_run"] is True,
              f"dry_run must preview once: {seen}")
    finally:
        server._do_grocery_checkout = saved_b
    print("  grocery_checkout headless: no expected_sum refuses+quotes, never charges")


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
              test_grocery_checkout_quote_then_confirm_locks_the_sum,
              test_headless_checkout_without_expected_sum_refuses_to_charge):
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
