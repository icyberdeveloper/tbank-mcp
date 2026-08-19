"""Elicitation: the «Перевести/Отмена» money gate on transfer_requisites.

The button IS the confirmation — there is no text fallback any more. What these
tests pin (from TBANK_CONFIRM_ABOVE, default 0 = every payment):

  * no ctx / no capability -> NO_ELICITATION_REFUSAL: nothing elicited, ZERO
    journal writes, ZERO HTTP (not even the commission quote), honest wording;
  * below a positive threshold a headless call still pays (threshold semantics
    are the same with and without a button);
  * accept -> the payment POSTs;
  * decline / cancel / client error -> ZERO journal writes, ZERO HTTP;
  * the schema agents see has no `ctx` — in EVERY tool that takes one, discovered
    from the registry rather than listed here;
  * the injected ctx never reaches calls.jsonl.

confirm_payment's OTP path is the plain text flow — covered by
test_payment_confirmation.py.

All values are synthetic.

    python3 tests/test_elicitation.py
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tbank-elicit-")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from src import journal, observability, server, trace         # noqa: E402
from elicit_fake import (FakeCtx, accept_ctx, cancel_ctx,      # noqa: E402
                         decline_ctx, incapable_ctx, mcp_error)
from test_requisites import LegalSession, fixture, run_tool   # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


AMT = 23600
# Same synthetic requisites the rest of the suite uses.
QR = ("ST00012|Name=ООО Тест|PersonalAcc=40702810000000000001"
     "|BIC=044525000|PayeeINN=7700000000|Purpose=Оплата счёта 1|Sum=2360000")

REFUSAL_HEAD = "ПЛАТЁЖ НЕ ВЫПОЛНЕН: этот клиент не поддерживает подтверждение кнопкой"


def _args(fx):
    return dict(fx["tool_args"], from_account="1111111111",
                comment="Счет 1 от 01.01.2026")


def _reset_logs():
    for p in (journal.ATTEMPTS_FILE, observability.EVENTS_FILE, trace.TRACE_FILE):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()


def _no_threshold():
    os.environ.pop("TBANK_CONFIRM_ABOVE", None)


def _journal_blob():
    return open(journal.ATTEMPTS_FILE, encoding="utf-8").read()


class _AttemptCounter:
    """Counts journal.new_attempt calls for the duration of a `with` block — the
    file being empty is necessary, but a counter also catches a write that some
    later code path could redirect elsewhere."""

    def __init__(self):
        self.calls = 0

    def __enter__(self):
        self._saved = journal.new_attempt

        def counting(*a, **kw):
            self.calls += 1
            return self._saved(*a, **kw)
        journal.new_attempt = counting
        return self

    def __exit__(self, *exc):
        journal.new_attempt = self._saved
        return False


def _check_untouched(s, tag):
    """The fake session records every request it would have signed or read."""
    check(s.body is None, f"{tag}: /v1/pay must NOT be built or sent")
    check(s.commission_body is None, f"{tag}: the commission must NOT be quoted")
    check(s.bik_lookups == [], f"{tag}: no BIK lookup either: {s.bik_lookups}")
    check(_journal_blob().strip() == "", f"{tag}: zero journal writes expected")


def _check_honest_refusal(out, tag):
    check(out.startswith(REFUSAL_HEAD), f"{tag}: must be NO_ELICITATION_REFUSAL: {out}")
    check(out == server.NO_ELICITATION_REFUSAL.format(safe="деньги на месте"),
          f"{tag}: the refusal must be the shared constant verbatim: {out}")
    check("ПЛАТЁЖ НЕ ВЫПОЛНЕН" in out and "деньги на месте" in out,
          f"{tag}: must say what did not happen and that money stayed: {out}")
    check("повтори" not in out.lower(),
          f"{tag}: repeating in the same client cannot help — no retry hint: {out}")


# ---- 1. no ctx is refused before anything happens --------------------------

def test_no_ctx_is_refused_before_journal_and_http():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    with _AttemptCounter() as attempts:
        out = run_tool(s, server.transfer_requisites, amount=AMT, **_args(fx))
    _check_honest_refusal(out, "no ctx")
    check(attempts.calls == 0, f"no ctx: journal.new_attempt called {attempts.calls}x")
    _check_untouched(s, "no ctx")
    print("  no ctx: refused, nothing journalled, nothing sent")


# ---- 2. no capability is refused, nothing elicited -------------------------

def test_no_capability_is_refused_and_never_elicits():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    fctx = incapable_ctx()
    with _AttemptCounter() as attempts:
        out = run_tool(s, server.transfer_requisites, amount=AMT, ctx=fctx, **_args(fx))
    check(fctx.asked == [], f"no capability -> nothing elicited: {fctx.asked}")
    _check_honest_refusal(out, "no capability")
    check(attempts.calls == 0, f"no capability: new_attempt called {attempts.calls}x")
    _check_untouched(s, "no capability")
    print("  no capability: refused, nothing elicited, nothing sent")


# ---- 3. below a positive threshold a headless call still pays -------------

def test_headless_below_threshold_still_pays():
    fx = fixture()
    try:
        os.environ["TBANK_CONFIRM_ABOVE"] = "1000"
        for tag, ctx in (("no ctx", None), ("no capability", incapable_ctx())):
            _reset_logs()
            s = LegalSession(fx)
            out = run_tool(s, server.transfer_requisites, amount=500, ctx=ctx, **_args(fx))
            check("Отправлено" in out and "paymentId=" in out,
                  f"{tag}: 500 < 1000 needs no button, must pay: {out}")
            check(s.body is not None, f"{tag}: below the threshold /v1/pay must be sent")
            if ctx is not None:
                check(ctx.asked == [], f"{tag}: nothing to elicit below the threshold")

            # At the threshold the button is required again → refusal, not a pass.
            _reset_logs()
            s = LegalSession(fx)
            out = run_tool(s, server.transfer_requisites, amount=1000, ctx=ctx, **_args(fx))
            _check_honest_refusal(out, f"{tag} at threshold")
            _check_untouched(s, f"{tag} at threshold")
    finally:
        _no_threshold()
    print("  threshold: headless pays below it, is refused from it")


# ---- 4. the threshold gates only from the configured amount ----------------

def test_threshold_gates_only_above():
    fx = fixture()
    try:
        os.environ["TBANK_CONFIRM_ABOVE"] = "1000"
        _reset_logs()
        fctx = accept_ctx()
        run_tool(LegalSession(fx), server.transfer_requisites, amount=500,
                 ctx=fctx, **_args(fx))
        check(fctx.asked == [], f"500 < 1000 must not elicit: {fctx.asked}")
        _reset_logs()
        fctx = accept_ctx()
        run_tool(LegalSession(fx), server.transfer_requisites, amount=1500,
                 ctx=fctx, **_args(fx))
        check(len(fctx.asked) == 1, f"1500 >= 1000 must elicit once: {fctx.asked}")
        if fctx.asked:
            msg = fctx.asked[0][0]
            check("1 500.00" in msg and "ПРИМЕР" in msg,
                  f"the question must name the amount and the payee: {msg}")

        _no_threshold()                          # default: gate every transfer
        _reset_logs()
        fctx = accept_ctx()
        run_tool(LegalSession(fx), server.transfer_requisites, amount=100,
                 ctx=fctx, **_args(fx))
        check(len(fctx.asked) == 1, "default (unset) must gate any amount")

        os.environ["TBANK_CONFIRM_ABOVE"] = "мусор"   # garbage -> like 0
        _reset_logs()
        fctx = accept_ctx()
        run_tool(LegalSession(fx), server.transfer_requisites, amount=100,
                 ctx=fctx, **_args(fx))
        check(len(fctx.asked) == 1, "a garbage threshold must fail safe (gate)")
        _reset_logs()
        s = LegalSession(fx)
        out = run_tool(s, server.transfer_requisites, amount=100, **_args(fx))
        _check_honest_refusal(out, "garbage threshold, no ctx")
        check(s.body is None, "a garbage threshold must fail safe headless too (refuse)")
    finally:
        _no_threshold()
    print("  threshold: gates from the configured amount, default gates all")


# ---- 5. accept sends the payment -------------------------------------------

def test_accept_sends_the_payment():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    fctx = accept_ctx()
    out = run_tool(s, server.transfer_requisites, amount=AMT, ctx=fctx, **_args(fx))
    check(len(fctx.asked) == 1, "the button must have been shown")
    if fctx.asked:
        msg, schema = fctx.asked[0]
        check(msg.startswith("Перевести ") and "23 600.00" in msg,
              f"the button must read «Перевести <sum> → payee»: {msg}")
        check(schema.model_json_schema().get("properties", {}) == {},
              "the money gate must be a zero-field form (Accept IS «Перевести»)")
    check(s.body is not None, "accept must POST the payment")
    check("Отправлено" in out and "paymentId=" in out, f"success text expected: {out}")
    print("  accept: button shown, payment posted")


# ---- 6. decline moves nothing ----------------------------------------------

def test_decline_moves_nothing():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=AMT,
                   ctx=decline_ctx(), **_args(fx))
    _check_untouched(s, "decline")
    check("отменён" in out and "деньги на месте" in out, f"honest refusal text: {out}")
    check("НЕ выполнен" not in out, f"a decline is not a failure: {out}")
    check(REFUSAL_HEAD not in out, f"a decline is not the no-button refusal: {out}")
    print("  decline: no HTTP, no journal, honest text")


# ---- 7. cancel and a client error move nothing either ----------------------

def test_cancel_and_error_move_nothing():
    fx = fixture()
    for fctx in (cancel_ctx(), FakeCtx(exc=mcp_error())):
        _reset_logs()
        s = LegalSession(fx)
        out = run_tool(s, server.transfer_requisites, amount=AMT, ctx=fctx, **_args(fx))
        _check_untouched(s, "cancel/error")
        check(len(fctx.asked) == 1, "cancel/error: the user WAS asked exactly once")
        check("не получено" in out and "НЕ отправлен" in out,
              f"must say the confirmation was not received: {out}")
        check(REFUSAL_HEAD not in out,
              f"asked-but-unanswered is not the no-button refusal: {out}")
    print("  cancel/timeout: no HTTP, no journal, says not confirmed")


# ---- 8. a QR-only call still gates on the QR's own amount ------------------

def test_qr_only_amount_still_gates():
    _reset_logs(); _no_threshold()
    fx = fixture()
    s = LegalSession(fx)
    fctx = decline_ctx()
    out = run_tool(s, server.transfer_requisites, amount=0, qr=QR, ctx=fctx)
    check(len(fctx.asked) == 1, "the QR amount must reach the gate")
    if fctx.asked:
        check("23 600.00" in fctx.asked[0][0],
              f"the question must carry the QR sum: {fctx.asked[0][0]}")
    check(s.body is None and "отменён" in out, f"declined QR payment must stop: {out}")

    # Headless, the QR sum is what decides too: it needs the button → refusal.
    _reset_logs()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=0, qr=QR)
    _check_honest_refusal(out, "qr-only headless")
    _check_untouched(s, "qr-only headless")
    print("  qr-only: gate reads Sum from the QR locally")


# ---- 9. the schema agents see: no ctx, otp still optional -------------------

# Every tool that confirms money with the button. Named here only to assert that
# the DISCOVERY below found them — if a paying tool ever loses its `ctx`, it loses
# the button with it, and a test that merely iterates whatever it discovers would
# go quietly green on the shrunken list.
PAYING = ("transfer", "transfer_requisites", "pay_bill", "ticket_pay",
          "grocery_checkout")


def _tools_taking_ctx():
    """{name: registered tool} for every tool whose function declares `ctx`.

    Discovered from the registry, never listed: `ctx` is FastMCP plumbing — the
    framework injects it and must keep it out of the schema the agent reads, or an
    agent will try to invent one (and a required `ctx` would make the tool
    uncallable). Hard-coding two names pinned that for the two tools somebody
    remembered; the next tool to grow a money gate would ship an agent-visible
    `ctx` and nothing here would notice.

    The registered callable is trace.wrap's wrapper, so unwrap to the function
    FastMCP actually read the signature of."""
    out = {}
    for name, tool in server.mcp._tool_manager._tools.items():
        fn = getattr(tool, "fn", None) or getattr(server, name, None)
        if fn is None:
            continue
        try:
            params = inspect.signature(inspect.unwrap(fn)).parameters
        except (TypeError, ValueError):
            continue
        if "ctx" in params:
            out[name] = tool
    return out


def test_schema_has_no_ctx():
    listed = server.mcp._tool_manager.list_tools()
    if inspect.isawaitable(listed):
        listed = asyncio.run(listed)
    tools = {t.name: t for t in listed}

    ctx_tools = _tools_taking_ctx()
    check(ctx_tools, "no ctx-taking tool was discovered at all — the registry moved "
                     "and this test would now pass against any schema whatsoever")
    missing = [n for n in PAYING if n not in ctx_tools]
    check(not missing,
          f"a paying tool no longer takes ctx — without it there is no button and "
          f"no confirmation: {missing}")

    # confirm_payment takes no ctx (its OTP path is plain text), and is checked
    # anyway: it is the tool that releases a payment the bank is holding, so a
    # `ctx` turning up in ITS schema would be an agent-supplied confirmation.
    for name in sorted(set(ctx_tools) | {"confirm_payment"}):
        tool = tools.get(name)
        check(tool is not None, f"{name}: registered, but missing from the list of "
                                f"tools agents are shown")
        if tool is None:
            continue
        schema = tool.parameters
        props = schema.get("properties", {})
        check("ctx" not in props,
              f"{name}: ctx leaked into the agent schema: {list(props)}")
        check("ctx" not in schema.get("required", []),
              f"{name}: ctx is demanded of the agent: {schema.get('required')}")

    tr = tools["transfer_requisites"].parameters.get("properties", {})
    check(set(tr) == {"amount", "qr", "comment", "account_number", "bik", "inn",
                      "name", "kpp", "corr_account", "bank_name", "nds",
                      "personal_account", "from_account", "force"},
          f"transfer_requisites property set changed: {sorted(tr)}")
    cp = tools["confirm_payment"].parameters
    check("otp" not in cp.get("required", []),
          f"otp must be optional: {cp.get('required')}")
    check("attempt_id" in cp.get("required", []), "attempt_id must stay required")
    print(f"  schema: no ctx in any of the {len(ctx_tools)} tools that take one "
          f"({', '.join(sorted(ctx_tools))}), otp optional, property set pinned")


# ---- 10. the injected ctx never reaches the trace --------------------------

def test_ctx_never_reaches_the_trace():
    _reset_logs()
    fx = fixture()
    run_tool(LegalSession(fx), server.transfer_requisites, amount=AMT,
             ctx=decline_ctx(), **_args(fx))
    raw = open(trace.TRACE_FILE, encoding="utf-8").read()
    check("FakeCtx" not in raw and "SimpleNamespace" not in raw,
          "a ctx repr leaked into calls.jsonl")
    for line in raw.splitlines():
        row = json.loads(line)
        check("ctx" not in (row.get("args") or {}),
              f"a ctx key leaked into the trace args: {row}")
    print("  trace: ctx dropped from calls.jsonl")


def test_the_gate_helpers_map_every_outcome():
    """`_money_gate` is the shared button, and its «no capability» branch is the
    one outcome no tool can reach today: every caller is pre-refused by
    `_button_required` (or, in grocery_checkout, by its own capability check)
    under exactly the same condition. That makes it untestable THROUGH a tool and
    easy to rot — the next tool to use the helper without the pre-check would
    inherit the old silent pass-through. So the helper is pinned directly.

    `_payable_amount`/`_needs_button` are pinned here too: they decide whether the
    button is owed, and their NaN behaviour is what keeps a junk amount from
    walking past every float comparison downstream (ticket_pay shipped that hole).
    """
    _no_threshold()
    gate = lambda ctx: asyncio.run(server._money_gate(ctx, "Заплатить 1 ₽?"))

    check(gate(accept_ctx()) is None, "accept must let the caller proceed")
    for ctx, tag in ((None, "ctx=None"), (incapable_ctx(), "no capability")):
        out = gate(ctx)
        check(out == server.NO_ELICITATION_REFUSAL.format(safe="деньги на месте"),
              f"{tag}: _money_gate must refuse, not wave through: {out!r}")
    check("Отменено пользователем" in (gate(decline_ctx()) or ""),
          "decline must read as the user's own «Отмена»")
    for ctx, tag in ((cancel_ctx(), "cancel"), (FakeCtx(exc=mcp_error()), "client error")):
        out = gate(ctx) or ""
        check("Подтверждение не получено" in out and "повтори тот же вызов" in out,
              f"{tag}: must read as «not confirmed», with a retry hint: {out!r}")

    # `did` is what a tool that already did visible work says instead of the
    # default «Ничего не сделано» — grocery quotes a delivery slot before asking.
    out = asyncio.run(server._money_gate(decline_ctx(), "x?", safe="деньги на месте",
                                         did="Заказ не создан"))
    check("Заказ не создан, деньги на месте" in out and "Ничего не сделано" not in out,
          f"a custom `did` must replace the default claim: {out!r}")

    for junk in (float("nan"), float("inf"), float("-inf"), 0, -5, "abc", None):
        check(server._payable_amount(junk) is None,
              f"{junk!r} is not a payable amount")
    check(server._payable_amount("1760") == 1760.0, "a numeric string is payable")
    check(server._needs_button(float("nan")) is True,
          "an amount nobody can compare must be ASKED about, never skipped")
    os.environ["TBANK_CONFIRM_ABOVE"] = "1000"
    try:
        check(server._needs_button(1000) is True, "«от порога» is inclusive")
        check(server._needs_button(999.99) is False, "below the threshold: no button")
        for bad in ("nan", "inf", "1e999", "-5"):
            os.environ["TBANK_CONFIRM_ABOVE"] = bad
            check(server._confirm_threshold() == 0.0,
                  f"TBANK_CONFIRM_ABOVE={bad!r} must read as 0 (ask about everything), "
                  f"not silently disable every button: {server._confirm_threshold()}")
            check(server._needs_button(1) is True, f"{bad!r}: still asks")
    finally:
        _no_threshold()
    print("  helpers: every gate outcome mapped; junk amounts and a junk threshold "
          "fail towards asking")


def main():
    for t in (test_no_ctx_is_refused_before_journal_and_http,
              test_the_gate_helpers_map_every_outcome,
              test_no_capability_is_refused_and_never_elicits,
              test_headless_below_threshold_still_pays,
              test_threshold_gates_only_above,
              test_accept_sends_the_payment,
              test_decline_moves_nothing,
              test_cancel_and_error_move_nothing,
              test_qr_only_amount_still_gates,
              test_schema_has_no_ctx,
              test_ctx_never_reaches_the_trace):
        t()
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nall elicitation tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
