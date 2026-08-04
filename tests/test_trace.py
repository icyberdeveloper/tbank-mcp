"""The call trace: it must see everything, change nothing, and keep secrets out.

A tracer is an unusual thing to test, because the ways it fails are quiet. It can
alter the tool schemas every agent reads and nobody notices until an agent starts
calling things wrong. It can copy a chat message or an account number into a file
advertised as safe to share. It can raise inside a payment and take the payment with
it. None of that shows up as a failing feature — so each of them is executed here.

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tbank-trace-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src import server, trace  # noqa: E402
from src.client import MobileSession, TbankApiError  # noqa: E402

failures = []


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
        return fn(*a, **kw)
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
             list_accounts=[{"id": "1111111111", "accountType": "Current",
                             "moneyAmount": {"value": 5000,
                                             "currency": {"name": "RUB"}}}]),
        server.transfer, 1000, "+79991234567", "секретная записка к переводу")

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
    # An error head is still worth keeping — it is already redacted.
    failed = next(r for r in rows if r["tool"] == "list_cards")
    check("sessionid=<redacted>" in failed["head"],
          f"an error must stay readable after redaction: {failed['head']!r}")
    print("  privacy: chat text, transfer notes, sessionid, account numbers and "
          "phone arguments stay out")


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
    run(Stub(), server.transfer_requisites, 100.0, qr, purpose)

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
    redacted, and it is the whole reason to look."""
    path = fresh_trace()
    trace.record("transfer_requisites", {"amount": 23600}, time.time(),
                 'Отправлено 23 600.00 RUB → ООО "РОМАШКА" со счёта 1111111111.', None)
    trace.record("transfer_requisites", {"amount": 23600}, time.time(),
                 "Платёж НЕ выполнен: API error (INVALID_REQUEST_DATA)", "TbankApiError")
    rows = trace.load(path)

    ok, failed = rows[0], rows[1]
    check("РОМАШКА" not in ok["head"],
          f"the payee is recorded on a successful payment: {ok['head']!r}")
    check(ok["err"] is None, "success must still be distinguishable from failure")
    check("НЕ выполнен" in failed["head"],
          f"a failed payment must keep its error line: {failed['head']!r}")
    check("РОМАШКА" not in open(path, encoding="utf-8").read(),
          "the payee reached the file by some other route")
    print("  head: a paid counterparty is not recorded; a failure keeps its message")


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
                       list_accounts=[{"id": "1111111111", "accountType": "Current",
                                       "moneyAmount": {"value": 5000,
                                                       "currency": {"name": "RUB"}}}]),
                  server.transfer, 1000, "+79991234567")
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
    test_pay_bills_fields_argument_is_measured_not_stored()
    test_the_journal_and_the_event_log_redact_too()
    test_the_log_files_are_owner_only_even_if_they_already_existed()
    test_session_file_is_owner_only_even_if_it_already_existed()
    test_a_refusal_is_not_recorded_as_an_error()
    test_the_report_finds_an_agent_that_got_stuck()
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
