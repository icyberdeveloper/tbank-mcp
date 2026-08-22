"""The call trace: it must see everything, change nothing, and keep secrets out.

A tracer is an unusual thing to test, because the ways it fails are quiet. It can
alter the tool schemas every agent reads and nobody notices until an agent starts
calling things wrong. It can copy a chat message, a payee's name or an account number
into a file advertised as safe to share. It can record a payment that was REFUSED
as one that completed, so the report counts money that never moved. It can raise
inside a payment and take the payment with it. None of that shows up as a failing
feature — so each of them is executed here.

    python3 tests/test_trace.py
"""
import asyncio
import inspect
import hashlib
import json
import os
import time
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="tbank-trace-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from elicit_fake import accept_ctx, decline_ctx  # noqa: E402
from src import server, trace  # noqa: E402
from src.client import MobileSession, TbankApiError  # noqa: E402

failures = []

# The two heads that replace an answer naming the payee. They are DIFFERENT on
# purpose: «Отправлено» is money gone, «ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ» is a payment the
# bank is still holding — opposite situations for whoever reads the report.
SUCCESS_HEAD = "<успех, получатель не записывается>"
PENDING_HEAD = "<ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ, получатель не записывается>"

# A single default SBP candidate: with the button live, transfer() resolves the
# recipient BEFORE the payment body (to name them on the button), so the stubs
# that pay need this canned answer or the lookup would run the real client code.
RECIPIENT = [{"bank_member_id": "100000000000", "masked_fio": "И. И.",
              "pointer_link_id": "10000000000", "bank_name": "Банк",
              "is_default_bank": True, "workflow_type": "SBPTransfer",
              "is_tbank": False}]


def check(cond, msg):
    if not cond:
        failures.append(msg)


def listed(mcp):
    tools = mcp._tool_manager.list_tools()
    if inspect.isawaitable(tools):
        tools = asyncio.run(tools)
    return {t.name: t for t in tools}


def fresh_trace():
    """Point the tracer at an empty file and hand back its path."""
    path = os.path.join(_TMP, f"t{len(os.listdir(_TMP))}.jsonl")
    trace.TRACE_FILE = path
    return path


class Stub(MobileSession):
    def __init__(self, **answers):
        self._memo = {}
        # transfer() resolves a payer account and journals the attempt before it
        # sends anything, so a stub that only answers transfer() dies earlier and
        # would "pass" the payment tests for the wrong reason.
        self.mobile_sessionid = "sid"
        self.access_token = "tok"
        for name, value in answers.items():
            def make(v):
                def call(*a, **kw):
                    if isinstance(v, Exception):
                        raise v
                    return v
                return call
            setattr(self, name, make(value))

    def ensure_fresh(self, *a, **kw):
        return None


def run(session, fn, *a, **kw):
    saved = server._require
    server._require = lambda: session
    try:
        out = fn(*a, **kw)
        if inspect.iscoroutine(out):
            # Awaited INSIDE the patch window: the async tools run their sync
            # body via asyncio.to_thread, which calls server._require() there.
            out = asyncio.run(out)
        return out
    finally:
        server._require = saved


def test_the_wrapper_does_not_change_what_agents_see():
    """The decorator was replaced once, globally, so every tool is wrapped. If that
    changed a signature, a default or a description, it would change the contract 57
    tools present to every agent — invisibly, because the tools still work.

    Checked by re-registering the UNWRAPPED functions into a second FastMCP and
    comparing, rather than against a copy of the schemas pasted into this file, which
    would just be a second thing to keep in sync."""
    live = listed(server.mcp)
    check(len(live) >= 57, f"expected the full tool surface, got {len(live)}")

    plain = FastMCP("plain")
    wrapped_count = 0
    for name, tool in live.items():
        fn = server.__dict__.get(name)
        check(fn is not None, f"{name} is registered but not a module attribute")
        if fn is None:
            continue
        raw = getattr(fn, "__wrapped__", None)
        check(raw is not None, f"{name} was never wrapped — the trace has a blind spot")
        if raw is None:
            continue
        wrapped_count += 1
        plain.tool()(raw)

    bare = listed(plain)
    for name, tool in live.items():
        other = bare.get(name)
        if other is None:
            continue
        check(tool.parameters == other.parameters,
              f"{name}: the wrapper changed the argument schema\n"
              f"    traced={tool.parameters}\n    plain ={other.parameters}")
        check((tool.description or "") == (other.description or ""),
              f"{name}: the wrapper changed the description an agent reads")
    print(f"  wrapper: {wrapped_count} tools traced, schemas and descriptions identical")


def test_a_secret_or_a_private_message_never_reaches_the_file():
    """The trace is meant to be as shareable as events.jsonl, which promises never to
    carry tokens, chat text or account numbers. Asserted against the FILE, not against
    the formatter — the file is what gets shared."""
    path = fresh_trace()
    sid = "AbCdEfGhIjKlMnOpQrStUvWxYz012345.authenticon-0123456789-abcde"
    secret_msg = "Привет, это личное сообщение про здоровье"
    # The payee's own name, handed in as an argument: `masked_fio` on a P2P
    # transfer, `name` on one by requisites. Neither key matches the blocklist —
    # "name" is a word, not a secret shape — so both used to be stored in full,
    # in the same row whose ANSWER is blanked precisely to keep the payee out.
    payee_fio = "И. СИНТЕТИКОВ"
    payee_org = 'ООО "ВЫМЫСЕЛ"'

    run(Stub(messenger_send={"ok": True}), server.messenger_send, "c-1", secret_msg)
    run(Stub(messenger_conversations=[
        {"conversationId": "c-1", "title": "Поддержка", "updatedAt": "2026-07-25",
         "message": {"content": {"text": secret_msg}}}]), server.messenger_conversations)
    run(Stub(list_accounts=[{"id": "40817810100000001234", "accountType": "Current",
                             "moneyAmount": {"value": 100, "currency": {"name": "RUB"}}}]),
        server.list_accounts)
    run(Stub(cards=TbankApiError("X", f"boom at https://x/v1/a?sessionid={sid}")),
        server.list_cards)
    run(Stub(transfer=({"payload": {"paymentId": "1"}}, "И. И."),
             resolve_recipient=RECIPIENT,
             list_accounts=[{"id": "1111111111", "accountType": "Current",
                             "moneyAmount": {"value": 5000,
                                             "currency": {"name": "RUB"}}}]),
        server.transfer, 1000, "+79991234567", "секретная записка к переводу",
        masked_fio=payee_fio, ctx=accept_ctx())
    # No ctx → refused at the gate, nothing sent. The arguments are recorded all
    # the same, so a payee who was never paid must not be stored either.
    run(Stub(), server.transfer_requisites, 1000, name=payee_org)

    raw = open(path, encoding="utf-8").read()
    check(secret_msg not in raw, "a chat message was written into the trace")
    check("секретная записка" not in raw, "a transfer note was written into the trace")
    check(sid not in raw, "the mobile sessionid was written into the trace")
    check("40817810100000001234" not in raw, "an account number reached the trace")
    # The recipient phone rides in as an ARGUMENT. It used to survive: the answer's
    # first line was scrubbed by _RE_LONG_ID, but _short_args unpacked the argument
    # dict before redacting, so the key never reached the blocklist and the value —
    # 11 digits, too short for _RE_CARD and _RE_BLOB — matched no value pattern.
    check("+79991234567" not in raw,
          "the recipient phone reached the trace as a tool argument")
    check(payee_fio not in raw,
          "the payee's name reached the trace as transfer(masked_fio=…)")
    check(payee_org not in raw,
          "the payee's name reached the trace as transfer_requisites(name=…) — on a "
          "call that was REFUSED, so nobody was paid and there is nothing to record")

    rows = trace.load(path)
    sent = next(r for r in rows if r["tool"] == "messenger_send")
    check(sent["args"]["text"] == f"<{len(secret_msg)} chars>",
          f"the message must be measured, not stored: {sent['args']}")
    check("chars" in sent["head"] and secret_msg not in sent["head"],
          f"messenger_send echoes the message in its answer: {sent['head']!r}")
    moved = next(r for r in rows if r["tool"] == "transfer")
    check(moved["args"]["to_account"] == "<redacted>",
          f"a sensitive argument must be redacted by KEY, not left to the value "
          f"patterns: {moved['args']}")
    check(moved["args"]["amount"] == 1000,
          f"redacting by key must not swallow the ordinary arguments that make the "
          f"trace useful: {moved['args']}")
    check(moved["args"]["masked_fio"] == f"<{len(payee_fio)} chars>",
          f"the payee must be measured, not stored — blanking the answer's first "
          f"line is undone by keeping the same person in the args: {moved['args']}")
    refused = next(r for r in rows if r["tool"] == "transfer_requisites")
    check(refused["args"]["name"] == f"<{len(payee_org)} chars>",
          f"the payee must be measured on a refused payment too: {refused['args']}")
    # An error head is still worth keeping — it is already redacted.
    failed = next(r for r in rows if r["tool"] == "list_cards")
    check("sessionid=<redacted>" in failed["head"],
          f"an error must stay readable after redaction: {failed['head']!r}")
    print("  privacy: chat text, transfer notes, sessionid, account numbers, phone "
          "arguments and payee names stay out")


def test_a_redacted_secret_cannot_be_read_back_out_of_its_own_hash():
    """`args_hash` used to be an unkeyed sha256 of the RAW arguments, stored on the
    same line as the redacted copy. A PIN has 10 000 possible values and an SMS code
    a million, so the digest handed straight back what the redaction had removed —
    measured at 0.05 s and 2.4 s to enumerate.

    This searches the whole keyspace, exactly as an attacker holding the file would,
    and requires the search to come up empty."""
    args = {"pin": "8317"}
    safe, digest = trace._short_args(args)
    check(safe["pin"] == "<4 chars>", f"the pin must not be stored: {safe}")

    def unkeyed(pin):
        canon = json.dumps({"pin": pin}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]

    check(unkeyed("8317") != digest,
          "args_hash is an unkeyed hash of the raw argument again — "
          "the redaction next to it is decorative")
    recovered = next((p for p in (f"{i:04d}" for i in range(10000))
                      if unkeyed(p) == digest), None)
    check(recovered is None,
          f"the PIN was recovered from args_hash by exhaustive search: {recovered}")

    # ...and the digest must still do its job: report() compares it between adjacent
    # rows to spot an agent repeating itself. Hashing the SANITISED dict would also
    # have closed the leak, and would have made two different PINs collide.
    same_a = trace._short_args({"query": "молоко"})[1]
    same_b = trace._short_args({"query": "молоко"})[1]
    other = trace._short_args({"query": "хлеб"})[1]
    check(same_a == same_b, "identical arguments must still produce one digest")
    check(same_a != other, "different arguments must still be distinguishable")
    check(trace._short_args({"pin": "0000"})[1] != trace._short_args({"pin": "9999"})[1],
          "two different PINs collide — a repeat would be reported that never happened")
    print("  args_hash: keyed, the whole PIN keyspace comes up empty, repeats still detected")


def test_a_scanned_qr_does_not_smuggle_the_payee_account_into_the_file():
    """The ГОСТ payment QR packs the payee's name, their 20-digit settlement account,
    the corr account, ИНН and КПП into ONE argument. It matched no key rule and no
    value pattern, and the 64-character cut lands just past the account — so the same
    number that is redacted when passed as `account_number` was stored in full when
    it arrived inside `qr`. Protection must not depend on which argument was used."""
    path = fresh_trace()
    acct = "40702810000000000001"
    qr = (f'ST00012|Name=ООО "ПРИМЕР"|PersonalAcc={acct}'
          f'|BIC=044525000|CorrespAcc=30101810000000000000|PayeeINN=7700000000')
    purpose = "Оплата по счету № 5982 от 03.08.2026 за профиль алюминиевый"

    run(Stub(), server.payment_qr, qr)
    # The button is pressed, so the call gets past the gate and into the payment
    # body — the arguments must be measured on the way in, not only on a refusal.
    run(Stub(), server.transfer_requisites, 100.0, qr, purpose, ctx=accept_ctx())

    raw = open(path, encoding="utf-8").read()
    check(acct not in raw, "the payee account reached the trace inside the QR argument")
    check("ПРИМЕР" not in raw, "the payee name reached the trace inside the QR argument")
    check(purpose not in raw, "назначение платежа was stored verbatim")

    rows = trace.load(path)
    for r in rows:
        for key in ("qr", "comment"):
            if key in r["args"]:
                check(r["args"][key].endswith("chars>"),
                      f"{r['tool']}.{key} must be measured, not stored: {r['args'][key]!r}")
    print("  qr/comment: measured, not stored — the payee account stays out of the file")


def test_a_successful_payment_does_not_record_who_was_paid():
    """«Отправлено 23 600 RUB → ООО «Ромашка» со счёта #» — _RE_LONG_ID scrubs the
    digits and leaves the counterparty, so the trace recorded who was paid and when.
    A FAILED call must keep its head: there the first line is the error, already
    redacted, and it is the whole reason to look.

    Two answers from these tools name a payee, and each gets its OWN marker. Both
    would be safe under one shared marker — and useless: «Отправлено» means the
    money left, «ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ» means the bank is holding the payment
    until a second factor, and a report that groups them cannot tell an operator
    which of the two just happened."""
    path = fresh_trace()
    trace.record("transfer_requisites", {"amount": 23600}, time.time(),
                 'Отправлено 23 600.00 RUB → ООО "РОМАШКА" со счёта 1111111111.', None)
    trace.record("transfer", {"amount": 23600}, time.time(),
                 'ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ. Банк принял платёж 23 600.00 RUB → '
                 'ООО "РОМАШКА" со счёта 1111111111, но держит его до второго '
                 'фактора (код из SMS).\nstatus=WAITING_CONFIRMATION '
                 'attemptId=a-1 nextTool=confirm_payment safeToRetry=false', None)
    trace.record("transfer_requisites", {"amount": 23600}, time.time(),
                 "Платёж НЕ выполнен: API error (INVALID_REQUEST_DATA)", "TbankApiError")
    rows = trace.load(path)

    ok, pending, failed = rows[0], rows[1], rows[2]
    check("РОМАШКА" not in ok["head"],
          f"the payee is recorded on a successful payment: {ok['head']!r}")
    check(ok["head"] == SUCCESS_HEAD, f"the success marker changed: {ok['head']!r}")
    check(ok["err"] is None, "success must still be distinguishable from failure")
    check("РОМАШКА" not in pending["head"],
          f"the payee is recorded on a payment held for a second factor: "
          f"{pending['head']!r}")
    check(pending["head"] == PENDING_HEAD,
          f"a held payment must say so, not borrow the success marker: "
          f"{pending['head']!r}")
    check("НЕ выполнен" in failed["head"],
          f"a failed payment must keep its error line: {failed['head']!r}")
    check("РОМАШКА" not in open(path, encoding="utf-8").read(),
          "the payee reached the file by some other route")
    print("  head: neither a paid nor a held counterparty is recorded, and the two "
          "are told apart; a failure keeps its message")


def test_a_refused_payment_is_not_filed_as_a_completed_one():
    """The blanking above used to key on the TOOL NAME alone, so every answer
    transfer/transfer_requisites ever returned was stored as «успех, получатель не
    записывается». Most of their answers are refusals — this client has no button,
    the user pressed «Отмена», the duplicate guard fired, the amount was junk — and
    each of them was filed as a completed payment. debug_report() then counted
    payments that never happened, grouped under a head that says the opposite of
    what occurred, and threw away the one line worth reading: WHY it did not happen.

    Refusals are ordinary return values (`err` stays None), so nothing else in the
    row distinguishes them — the head is the only place the outcome exists.

    Driven through the tools themselves wherever the refusal comes from the gate or
    from validation, so the strings are the server's own and not a copy that can
    drift. The duplicate guard needs journal state to fire, so its answer goes in
    through record() directly."""
    path = fresh_trace()
    account = [{"id": "1111111111", "accountType": "Current",
                "moneyAmount": {"value": 5000, "currency": {"name": "RUB"}}}]

    # No ctx at all → NO_ELICITATION_REFUSAL, before any HTTP, from both tools.
    run(Stub(), server.transfer, 1000, "+79991234567")
    run(Stub(), server.transfer_requisites, 1000, name="ООО ТЕСТ")
    # The button was shown and the human pressed «Отмена». Each tool words this
    # its own way, and both wordings must reach the file.
    run(Stub(resolve_recipient=RECIPIENT, list_accounts=account),
        server.transfer, 1000, "+79991234567", ctx=decline_ctx())
    run(Stub(list_accounts=account), server.transfer_requisites, 1000,
        name="ООО ТЕСТ", ctx=decline_ctx())
    # A refusal that never even reaches the gate.
    run(Stub(), server.transfer, 0, "+79991234567")
    # A payee that is a NAME, not digits: _RE_LONG_ID cannot help here, and the
    # addressee can arrive from a QR without ever being an argument, so the args
    # blocklist cannot either. Keeping the refusal's own first line (the point of
    # this test) must not smuggle the company back into a file the module's own
    # header calls shareable.
    trace.record("transfer_requisites", {"amount": 1000}, time.time(),
                 'ПОВТОР ЗАБЛОКИРОВАН: такой же платёж (1000₽ → ООО "ВЫМЫСЕЛ") '
                 "уже отправлялся и его исход НЕ подтверждён.\n"
                 "Проверь list_operations('1111111111', days=1).", None)

    expect = [
        ("transfer",            "ПЛАТЁЖ НЕ ВЫПОЛНЕН"),
        ("transfer_requisites", "ПЛАТЁЖ НЕ ВЫПОЛНЕН"),
        ("transfer",            "Отменено пользователем"),
        ("transfer_requisites", "Перевод отменён пользователем"),
        ("transfer",            "Сумма должна быть положительным числом"),
        ("transfer_requisites", "ПОВТОР ЗАБЛОКИРОВАН"),
    ]
    rows = sorted(trace.load(path), key=lambda r: r["seq"])
    check(len(rows) == len(expect),
          f"expected one row per refusal, got {[(r['tool'], r['head']) for r in rows]}")
    for row, (tool, opening) in zip(rows, expect):
        check(row["tool"] == tool, f"row order: expected {tool}, got {row['tool']}")
        check(row["head"] not in (SUCCESS_HEAD, PENDING_HEAD),
              f"{tool} «{opening}…» was filed as a payment that happened: "
              f"{row['head']!r}")
        check(row["head"].startswith(opening),
              f"{tool}: the refusal the agent read must survive as the head — "
              f"expected it to start with {opening!r}, got {row['head']!r}")
        check(row["err"] is None,
              f"{tool}: a refusal is a return value, not an exception: {row['err']!r}")

    # …and surviving must not mean smuggling: the head is still scrubbed.
    raw = open(path, encoding="utf-8").read()
    check("79991234567" not in raw,
          "a recipient phone rode into the file inside a refusal")
    check("ВЫМЫСЕЛ" not in raw,
          "the duplicate guard names the payee in cleartext, and that head is kept "
          "now that refusals are no longer blanked — the name must be cut, the "
          "reason kept")
    # The one answer of these two tools that echoes the bank verbatim: json.dumps
    # writes no newlines, so the payload IS the first line, and keeping first
    # lines (the point of this test) would put the bank's own response — payee
    # and all — into the file. The cut is marked: a head that just stops reads
    # as the whole answer.
    trace.record("transfer_requisites", {"amount": 1000}, time.time(),
                 "Ответ банка без paymentId — исход неясен. "
                 "Проверь list_operations('1111111111', days=1). "
                 'Ответ: {"payload": {"addressee": "ООО \\"ВЫМЫСЕЛ\\""}}', None)
    echoed = [r for r in trace.load(path) if r["head"].startswith("Ответ банка")]
    check(len(echoed) == 1, f"expected the echoed-payload row: {echoed}")
    if echoed:
        h = echoed[0]["head"]
        check("ВЫМЫСЕЛ" not in h and "payload" not in h,
              f"the bank's own response must not reach the file: {h!r}")
        check(h.endswith("<ответ банка не записывается>"),
              f"and the cut must be marked, not silent: {h!r}")
        check("исход неясен" in h, f"the reason must survive it: {h!r}")

    guard = next(r for r in rows if r["head"].startswith("ПОВТОР ЗАБЛОКИРОВАН"))
    check("→ #" in guard["head"],
          f"the payee must be replaced, not the whole line dropped: {guard['head']!r}")
    check("уже отправлялся" in guard["head"],
          f"and the REASON must survive the cut: {guard['head']!r}")
    print("  head: every refusal keeps its own first line — a payment that did not "
          "happen is no longer counted as one")


def test_pay_bills_fields_argument_is_measured_not_stored():
    """pay_bill's `fields` argument is a provider-defined JSON blob — the key names
    vary per provider (ФНС, ФССП, ГИБДД, ...) and can carry real PII: a taxpayer's
    full name, a passport series+number, an FSSP enforcement-case number, a traffic-
    fine decree number. None of those key names match _REDACT_KEY, so the value used
    to reach calls.jsonl almost verbatim (just truncated to 64 chars — long enough to
    keep a real name and document number intact)."""
    path = fresh_trace()

    @trace.wrap
    def fake_pay_bill(provider_id, fields, amount):
        return "OK"

    pii = '{"fio":"Иванов Иван Иванович","docNumber":"1234567890"}'
    fake_pay_bill("fssp-rf", pii, 500.0)

    raw = open(path, encoding="utf-8").read()
    check("Иванов" not in raw, "a taxpayer/enforcement name reached the trace")
    check("1234567890" not in raw, "a document number reached the trace")

    rec = trace.load(path)[-1]
    check(rec["args"]["fields"] == f"<{len(pii)} chars>",
          f"fields must be measured, not stored: {rec['args']}")
    print("  privacy: pay_bill's provider-defined `fields` blob is measured, not stored")


def test_the_passengers_argument_is_measured_not_stored():
    """train_book/flight_book take a traveller list, and it is the same open-ended
    blob as pay_bill's `fields`: a passport number, a full name, a date of birth
    and an airline bonus card, in JSON the agent composed. None of those key names
    match _REDACT_KEY.

    What made this worth a test of its own: whether the passport survived depended
    on the ORDER the agent serialised the dict in. The 64-character argument cut
    happened to land before `number` when the name came first — so the first probe
    showed «nothing leaked» — and putting `number` first put a real document number
    on disk. Both orderings are asserted here, because only one of them used to
    fail."""
    for label, pii in (
        ("name first",
         '{"first":"Пётр","last":"Петров","birthDate":"1990-01-31","number":"1234567890"}'),
        ("passport first",
         '{"number":"1234567890","birthDate":"1990-01-31","last":"Петров","first":"Пётр"}'),
    ):
        path = fresh_trace()

        @trace.wrap
        def fake_train_book(train_id, seats, passengers):
            return "Забронировано"

        blob = "[" + pii + "]"
        fake_train_book("train_x", "03/10", blob)

        raw = open(path, encoding="utf-8").read()
        check("Петров" not in raw, f"{label}: a passenger's surname reached the trace")
        check("1234567890" not in raw, f"{label}: a passport number reached the trace")
        check("1990-01-31" not in raw, f"{label}: a date of birth reached the trace")

        rec = trace.load(path)[-1]
        check(rec["args"]["passengers"] == f"<{len(blob)} chars>",
              f"{label}: passengers must be measured, not stored: {rec['args']}")
    print("  privacy: the passengers blob is measured, not stored, in either field order")


def test_get_data_arg_and_search_query_are_measured_not_stored():
    """get_data's second positional `arg` is a phone (sbp_me2me), an account number
    (statements/full_debt_amount/statement_exist) or a provider list — PII or an
    account id that matches no sensitive KEY name, so it reached calls.jsonl whole.
    A search `query` (grocery/shop/afisha) is open-ended user text that can carry a
    name or address. Both must be measured, not stored."""
    path = fresh_trace()

    @trace.wrap
    def fake_get_data(section, arg="", days=30, max_chars=5000):
        return "OK"

    @trace.wrap
    def fake_grocery_search(query, limit=20):
        return "OK"

    fake_get_data("statements", "40817810000000000000")
    fake_grocery_search("перевод Иванову Ивану на Ленина 5")

    raw = open(path, encoding="utf-8").read()
    check("40817810000000000000" not in raw, "an account number reached the trace via arg")
    check("Иванову" not in raw, "a name in a search query reached the trace")
    check("Ленина" not in raw, "an address in a search query reached the trace")

    rows = trace.load(path)
    gd = next(r for r in rows if r["tool"] == "fake_get_data")
    check(gd["args"]["arg"] == "<20 chars>",
          f"get_data's arg must be measured, not stored: {gd['args']}")
    gs = next(r for r in rows if r["tool"] == "fake_grocery_search")
    check(str(gs["args"]["query"]).startswith("<") and "chars>" in str(gs["args"]["query"]),
          f"a search query must be measured, not stored: {gs['args']}")
    print("  privacy: get_data `arg` and a search `query` are measured, not stored")


def test_a_refusal_is_not_recorded_as_an_error():
    """Most failures here are ordinary return values — «NO_STORE_CONTEXT»,
    «Неизвестное поле сортировки». If the tracer guessed at the answer string it
    would either miss those or mislabel successes; instead _err() reports the fact.
    Both must be visible, and they must be told apart."""
    path = fresh_trace()
    stores = [{"appId": "204", "name": "ВкусВилл", "pointId": "5980",
               "minOrderSum": 500.0, "etaMin": 60.0, "deliveryWindow": "до 60 мин",
               "deliveryPrice": 0.0, "cashback": 10, "areaId": ""}]

    run(Stub(grocery_stores=stores), server.grocery_stores)
    run(Stub(grocery_stores=stores), server.grocery_stores, "быстрее")   # refusal
    run(Stub(cards=TbankApiError("X", "down")), server.list_cards)       # error

    rows = {r["tool"] + str(r["seq"]): r for r in trace.load(path)}
    by_seq = sorted(trace.load(path), key=lambda r: r["seq"])
    ok, refusal, error = by_seq
    check(ok["err"] is None, f"a successful call must not be flagged: {ok['err']!r}")
    check(refusal["err"] is None,
          f"a refusal is a return value, not an exception: {refusal['err']!r}")
    check("Неизвестное поле" in refusal["head"],
          f"the refusal the agent read must be recorded: {refusal['head']!r}")
    check(error["err"] == "TbankApiError",
          f"a real failure must name its class: {error['err']!r}")
    print("  outcome: refusals stay visible as answers, errors name their exception")


def test_the_report_finds_an_agent_that_got_stuck():
    """The point of the whole thing: the same tool, the same arguments, over and over
    is an agent that did not understand the answer — and that is a docstring problem,
    not a bank problem."""
    rows = [
        {"run": "r1", "seq": 1, "tool": "grocery_stores", "args_hash": "a", "ms": 10,
         "chars": 80, "err": None, "head": "- ВкусВилл appId=#"},
        {"run": "r1", "seq": 2, "tool": "grocery_search", "args_hash": "b", "ms": 5,
         "chars": 40, "err": "TbankApiError", "head": "API error (NO_STORE_CONTEXT): …"},
        {"run": "r1", "seq": 3, "tool": "grocery_search", "args_hash": "b", "ms": 5,
         "chars": 40, "err": "TbankApiError", "head": "API error (NO_STORE_CONTEXT): …"},
        {"run": "r1", "seq": 4, "tool": "grocery_search", "args_hash": "b", "ms": 5,
         "chars": 40, "err": "TbankApiError", "head": "API error (NO_STORE_CONTEXT): …"},
        {"run": "r2", "seq": 1, "tool": "list_accounts", "args_hash": "c", "ms": 900,
         "chars": 200, "err": None, "head": "- # | Current"},
    ]
    rep = trace.report(rows)
    check(rep["runs"] == 2 and rep["calls"] == 5, f"counts: {rep['runs']}/{rep['calls']}")

    stuck = [r for r in rep["repeats"] if r["tool"] == "grocery_search"]
    check(stuck and stuck[0]["times"] == 3,
          f"three identical calls in a row must surface as one repeat: {rep['repeats']}")

    search = next(t for t in rep["tools"] if t["tool"] == "grocery_search")
    check(search["n"] == 3 and search["err"] == 3, f"per-tool counts: {search}")
    check(search["answers"][0] == ("API error (NO_STORE_CONTEXT): …", 3),
          f"the answer the agent kept reading must be grouped: {search['answers']}")

    check(("grocery_stores", "grocery_search") in dict(rep["transitions"]),
          f"transitions must be recorded: {rep['transitions']}")
    # A repeat inside one run must not be joined across runs.
    check(all(r["run"] in ("r1", "r2") for r in rep["repeats"]), "repeat leaked a run")
    check(dict(rep["starts"]).get("grocery_stores") == 1
          and dict(rep["starts"]).get("list_accounts") == 1,
          f"each run's first call must be counted: {rep['starts']}")

    slow = next(t for t in rep["tools"] if t["tool"] == "list_accounts")
    check(slow["p95_ms"] == 900, f"latency must survive: {slow}")
    print("  report: repeats, per-tool errors, grouped answers, transitions, starts")


def test_debug_report_marks_a_head_it_shortens():
    """debug_report printed the answer/repeat heads with a bare [:110]/[:90] slice —
    the one report meant to reveal silent truncation was itself truncating silently.
    Routed through _cut, a shortened head now ends with «…» and never shows its
    dropped tail."""
    long_tail = "УНИКАЛЬНЫЙ_ХВОСТ_КОТОРЫЙ_ДОЛЖЕН_ИСЧЕЗНУТЬ"
    head = "API error (SOMETHING): " + ("очень длинная строка ответа " * 6) + long_tail
    rows = [{"run": "r1", "seq": i, "tool": "grocery_search", "args_hash": "b",
             "ms": 5, "chars": 40, "err": "TbankApiError", "head": head}
            for i in range(1, 4)]
    saved = trace.load
    trace.load = lambda *a, **kw: rows
    try:
        out = server.debug_report()
    finally:
        trace.load = saved
    check("…" in out, f"a shortened head must carry the cut marker: {out!r}")
    check(long_tail not in out,
          f"the dropped tail must not appear — nor be shown whole: {out[:200]!r}")
    # And a head that FITS must not gain a spurious «…» (that is the _cut contract).
    short_rows = [{"run": "r2", "seq": 1, "tool": "list_accounts", "args_hash": "c",
                   "ms": 9, "chars": 20, "err": None, "head": "- # | Current"}]
    trace.load = lambda *a, **kw: short_rows
    try:
        out2 = server.debug_report()
    finally:
        trace.load = saved
    check("- # | Current" in out2 and "…" not in out2,
          f"a head that fits must be untouched: {out2!r}")
    print("  debug_report: shortened heads marked with «…», fitting heads untouched")


def test_tracing_off_writes_nothing():
    path = fresh_trace()
    os.environ["TBANK_TRACE"] = "0"
    try:
        out = run(Stub(list_accounts=[]), server.list_accounts)
    finally:
        os.environ.pop("TBANK_TRACE", None)
    check(not os.path.exists(path), "TBANK_TRACE=0 still wrote a trace file")
    check(out, "the tool must still answer with tracing off")

    run(Stub(list_accounts=[]), server.list_accounts)
    check(os.path.exists(path), "tracing did not resume when re-enabled")
    print("  switch: TBANK_TRACE=0 records nothing, and the tool still works")


def test_a_broken_tracer_cannot_break_a_payment():
    """This wraps /v1/pay. If the tracer can raise, it can take a transfer with it —
    and the caller would see a failure for a payment that actually went through."""
    path = fresh_trace()
    saved = trace._append

    def explode(rec):
        raise OSError("disk full")

    trace._append = explode
    try:
        # Caught rather than allowed to propagate: this must be REPORTED as a
        # failure, not kill the run before the remaining checks say why.
        out = run(Stub(transfer=({"payload": {"paymentId": "100000000001"}}, "И. И."),
                       resolve_recipient=RECIPIENT,
                       list_accounts=[{"id": "1111111111", "accountType": "Current",
                                       "moneyAmount": {"value": 5000,
                                                       "currency": {"name": "RUB"}}}]),
                  server.transfer, 1000, "+79991234567", ctx=accept_ctx())
    except BaseException as e:                               # noqa: BLE001
        out = ""
        failures.append(f"the tracer's write error escaped into the payment: "
                        f"{type(e).__name__}: {e}")
    finally:
        trace._append = saved
    check("100000000001" in out,
          f"a failing tracer swallowed the payment result: {out!r}")
    check(not os.path.exists(path) or "100000000001" not in open(path).read(),
          "the record was written despite the writer failing")
    print("  robustness: a tracer that cannot write does not disturb the tool")


def test_the_journal_and_the_event_log_redact_too():
    """The trace was the only one of the three log files with a privacy test asserted
    against the FILE. journal._append and observability.emit each redact on a single
    line, and both are read back by tools the user is told to share (grocery_attempts,
    diagnostics) — so both get the same treatment here.

    They differ from the trace in a way that matters: they hand the WHOLE dict to
    _redact_value, so the key blocklist already fires for them. This pins that."""
    from src import journal
    from src import observability as obs

    jpath = os.path.join(_TMP, "j-redact.jsonl")
    epath = os.path.join(_TMP, "e-redact.jsonl")
    journal.ATTEMPTS_FILE = jpath
    obs.EVENTS_FILE = epath

    account = "40817810100000001234"
    cookie = "SSO_SESSION=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdef"
    aid = journal.new_attempt("204", "5980", "hash1", 1458.0)
    journal.record(aid, "payment", "unknown", account=account, cookie=cookie,
                   err='{"title": "Недостаточно средств"}')
    obs.emit("payment", app_id="204", account=account, cookie=cookie, http_status=422)

    jraw = open(jpath, encoding="utf-8").read()
    eraw = open(epath, encoding="utf-8").read()
    for label, raw in (("attempts.jsonl", jraw), ("events.jsonl", eraw)):
        check(account not in raw, f"an account number reached {label}")
        check(cookie not in raw, f"an SSO cookie reached {label}")
    # Redaction must not empty the file of the diagnostics it exists for: the
    # non-secret fields are the whole point of both logs.
    check("Недостаточно средств" in jraw,
          f"the journal dropped the gateway error it exists to preserve: {jraw!r}")
    check('"http_status": 422' in eraw,
          f"the event log dropped the status it exists to preserve: {eraw!r}")
    print("  privacy: the journal and the event log redact by key, keep the diagnostics")


def test_the_log_files_are_owner_only_even_if_they_already_existed():
    """All three writers open with 0o600 AND chmod afterwards. The chmod is the part
    that matters and the part nothing tested: os.open's mode applies only when the
    file is CREATED and is masked by umask, so a log that already exists at 0644 —
    from an older version, a restore, a careless editor — would keep leaking to every
    account on the machine while the code looks correct."""
    from src import journal
    from src import observability as obs

    cases = []
    jpath = os.path.join(_TMP, "j-perm.jsonl")
    epath = os.path.join(_TMP, "e-perm.jsonl")
    tpath = os.path.join(_TMP, "t-perm.jsonl")
    for path in (jpath, epath, tpath):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")
        os.chmod(path, 0o644)

    journal.ATTEMPTS_FILE = jpath
    obs.EVENTS_FILE = epath
    trace.TRACE_FILE = tpath
    journal.record("a1", "init", "started")
    obs.emit("payment", http_status=200)
    run(Stub(list_accounts=[]), server.list_accounts)
    cases = [("attempts.jsonl", jpath), ("events.jsonl", epath), ("calls.jsonl", tpath)]

    for label, path in cases:
        mode = oct(os.stat(path).st_mode & 0o777)
        check(mode == "0o600",
              f"{label} stayed {mode} on a pre-existing file — the chmod next to "
              f"os.open is what fixes this, and it is why both are there")
    print("  permissions: all three logs come back to 0600 even when they pre-existed")


def test_session_file_is_owner_only_even_if_it_already_existed():
    """session.json holds the live access/refresh tokens — the highest-value file
    this project writes — but unlike journal.jsonl/events.jsonl/calls.jsonl (all
    covered above), nothing pinned its 0600 permissions."""
    path = os.path.join(_TMP, "session-perm.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}")
    os.chmod(path, 0o644)

    saved = server._SESSION_FILE
    server._SESSION_FILE = path
    try:
        s = server._blank_session()
        server._save_session(s)
        mode = oct(os.stat(path).st_mode & 0o777)
        check(mode == "0o600",
              f"session.json stayed {mode} on a pre-existing file — the chmod "
              f"next to os.open is what fixes this")

        loaded = server._load_session()
        check(loaded is not None, "a freshly-saved session must load back")
    finally:
        server._SESSION_FILE = saved
    print("  permissions: session.json comes back to 0600 even when it pre-existed")


def main():
    print("call trace:")
    test_the_wrapper_does_not_change_what_agents_see()
    test_a_secret_or_a_private_message_never_reaches_the_file()
    test_a_redacted_secret_cannot_be_read_back_out_of_its_own_hash()
    test_a_scanned_qr_does_not_smuggle_the_payee_account_into_the_file()
    test_a_successful_payment_does_not_record_who_was_paid()
    test_a_refused_payment_is_not_filed_as_a_completed_one()
    test_pay_bills_fields_argument_is_measured_not_stored()
    test_the_passengers_argument_is_measured_not_stored()
    test_get_data_arg_and_search_query_are_measured_not_stored()
    test_the_journal_and_the_event_log_redact_too()
    test_the_log_files_are_owner_only_even_if_they_already_existed()
    test_session_file_is_owner_only_even_if_it_already_existed()
    test_a_refusal_is_not_recorded_as_an_error()
    test_the_report_finds_an_agent_that_got_stuck()
    test_debug_report_marks_a_head_it_shortens()
    test_tracing_off_writes_nothing()
    test_a_broken_tracer_cannot_break_a_payment()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
