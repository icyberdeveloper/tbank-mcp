"""Paying a legal entity by bank requisites — the ninth money path, and the first
one built from a capture that arrived after the refusal was written.

`transfer(provider='transfer-legal')` used to raise NOT_SUPPORTED because no
captured /v1/pay existed for that provider: the field NAMES were known from four
commission previews, but pay is not commission-with-extra-fields, and guessing an
envelope on a payment to an arbitrary company is not a thing to do. captures_payreq
.xml holds the whole flow — QR → resolve → commission → pay, 200 with a paymentId —
so the envelope is now read, not guessed.

What this pins:

1. The QR reading. The bank fills its own field schema out of the same string, so
   our parse can be checked against the bank's rather than against itself.
   `Sum` is in KOPECKS — read as rubles it pays 100× the invoice.
2. The pay body. Same keys as the capture, `paidByPhoto` only when the requisites
   really were scanned, no `paymentType` (that belongs to the commission call).
3. The refusals that happen BEFORE anything is sent: a purpose the bank requires,
   an account that is not 20 digits, a БИК that is not 9, an ИНН that is neither
   10 nor 12 — each checked against the provider's own published regexp.
4. That an explicit argument beats the QR, which is how a bad scan is corrected.

Money moves only after the human pressed the «Перевести/Отмена» button
(MCP elicitation), and a client without that capability is refused before the
payment body even starts. So every call here that expects the body to run — the
commission preview, the requisite regexps, the duplicate guard, the /v1/pay
envelope — hands the tool `ctx=accept_ctx()` from tests/elicit_fake.py: a client
whose human presses Accept. The headless refusal itself is pinned in
tests/test_elicitation.py, not here. Only the amount<=0 refusals stay headless:
the gate lets a non-positive sum through to the body, whose message names what
to pass.

The capture is the user's real traffic and is gitignored, so the contract lives in
fixtures/transfer_legal.json: real key sets, real regexps, real protocol constants,
synthetic company and account. When the capture IS present the fixture is
additionally checked against it, so it cannot drift.

    python3 tests/test_requisites.py
"""
import asyncio
import base64
import gzip
import inspect
import json
import os
import re
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "transfer_legal.json")
CAPTURE = os.environ.get("TBANK_CAPTURE_PAYREQ",
                         os.path.expanduser("~/tbank-app/captures_payreq.xml"))

# capture item indices → the request they hold
QR_RESOLVE = 538      # POST /providers/providers/qr/resolve → transfer-legal
CATALOGUE = 334       # GET /providers/compatible/page?groups=Переводы
COMMISSION = 575      # POST /v1/payment_commission, the one with a comment
PAY = 578             # POST /v1/pay — the signed payment, 200
BANK_INFO = 541       # GET /v1/bank_info?bik= → name + correspondent account

_TMP = tempfile.mkdtemp()
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")
# The trace file too: a standalone run of this file drives real paying tools, and
# without this their rows (payee, amounts) land in the user's live calls.jsonl.
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")

from elicit_fake import accept_ctx                          # noqa: E402
from src import client, server                              # noqa: E402
from src.client import MobileSession, TbankApiError          # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ---- capture access (only used when the capture is present) ---------------

def items():
    with open(CAPTURE, "rb") as fh:
        return re.findall(r"<item>(.*?)</item>", fh.read().decode("utf-8", "replace"), re.S)


def raw(item, tag):
    m = re.search(r"<%s( [^>]*)?>(.*?)</%s>" % (tag, tag), item, re.S)
    body = m.group(2).replace("<![CDATA[", "").replace("]]>", "")
    return base64.b64decode(body) if 'base64="true"' in (m.group(1) or "") else body.encode()


def http_body(blob):
    head, _, body = blob.partition(b"\r\n\r\n")
    if b"content-encoding: gzip" in head.lower():
        body = gzip.decompress(body)
    return body


def request_json(its, n):
    return json.loads(http_body(raw(its[n], "request")))


def response_json(its, n):
    return json.loads(http_body(raw(its[n], "response")))


def request_form(its, n):
    return urllib.parse.parse_qs(http_body(raw(its[n], "request")).decode())


def request_query(its, n):
    line = raw(its[n], "request").partition(b"\r\n")[0].decode()
    return urllib.parse.parse_qs(line.split("?", 1)[1].rsplit(" ", 1)[0])


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


# ---- the sessions the server tools talk to --------------------------------

class LegalSession(MobileSession):
    """Builds and signs the request exactly as production does, but keeps it.

    Everything it reads — the provider catalogue, the БИК lookup, the commission —
    comes from the fixture, so the whole server-level path runs without a network."""

    def __init__(self, fx, fail=False, payload=None, commission=None):
        self.fx = fx
        self.mobile_sessionid = "sid.authenticon-test"
        self.access_token = "tok"
        self.device_id = "00000000-1111-2222-3333-444444444444"
        self.old_device_id = "0123456789abcdef"
        self.cookie_str = ""
        self.platform = "ios"
        self.app_name = "mobile"
        self.app_version = "7.39.1"
        self.fail = fail
        self.payload = payload
        self.commission_override = commission
        self.url = self.body = self.headers = None
        self.commission_body = None
        self.qr_request = None
        self.bik_lookups = []

    def ensure_fresh(self, *a, **kw):
        return None

    def list_accounts(self):
        return [{"id": "1111111111", "accountType": "Current",
                 "moneyAmount": {"value": 500000, "currency": {"name": "RUB"}}}]

    def find_provider(self, provider_id, group="", max_pages=7):
        # Catalogue paging is not what this file tests; the SCHEMA it returns is.
        return self.fx["provider_catalogue"] if provider_id == "transfer-legal" else {}

    def _call_read(self, key, *, overrides=None, body=None, path_override=None):
        """Answers reads from the fixture — one level BELOW the methods under test,
        so payment_commission(), bank_by_bik() and qr_providers() all run for real
        and what they build is what gets recorded."""
        if key == "payment_commission":
            self.commission_body = (body or {}).get("payParameters")
            if self.commission_override is not None:
                return self.commission_override
            return json.loads(json.dumps(self.fx["commission_response"]))
        if key == "bank_info":
            self.bik_lookups.append((overrides or {}).get("bik"))
            return json.loads(json.dumps(self.fx["bank_info"]))
        if key == "resolve_payment_qr":
            self.qr_request = {"body": body, "query": overrides}
            return {"providersList": {
                "providers": [json.loads(json.dumps(self.fx["provider_resolved"]))]}}
        raise AssertionError("unexpected read: " + key)

    def _call_signed(self, template_key, body_str, extra_query=None):
        self.url, self.headers, self.body = self._signed_parts(
            template_key, body_str, extra_query)
        if self.fail:
            raise ConnectionError("connection reset by peer")
        return self.payload if self.payload is not None else {
            "payload": json.loads(json.dumps(self.fx["pay_response"]))}

    def sent_pay_parameters(self):
        return json.loads(urllib.parse.parse_qs(self.body)["payParameters"][0])

    def sent_form(self):
        return urllib.parse.parse_qs(self.body)

    def sent_query(self):
        return urllib.parse.parse_qs(self.url.split("?", 1)[1])


def run_tool(session, fn, *a, **kw):
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


# ---- the fixture is what the capture says ---------------------------------

def test_the_fixture_still_matches_the_capture():
    """The fixture is a scrub of one real flow. Regenerating is deterministic, so
    the check is: does regen still produce what is committed? A mismatch means the
    app changed its request or the scrub changed, and both want a human."""
    if not os.path.exists(CAPTURE):
        print(f"  (capture absent at {CAPTURE} — drift check skipped; the contract "
              f"below was still verified against the fixture)")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "fixtures"))
    import regen                                            # noqa: E402
    try:
        fresh = regen.build_transfer_legal()
    except Exception as e:                                  # noqa: BLE001
        failures.append(f"transfer_legal.json can no longer be regenerated from "
                        f"the capture ({type(e).__name__}: {e})")
        return
    mine = fixture()
    for key in sorted(set(mine) | set(fresh)):
        if key == "_note":
            continue
        check(mine.get(key) == fresh.get(key),
              f"fixtures/transfer_legal.json[{key}] drifted from the capture — "
              f"rerun tests/fixtures/regen.py and read the diff before committing")
    print("  fixture vs capture: the QR, the pay body and the schema still match")


def test_the_qr_is_read_the_way_the_bank_reads_it():
    """The bank fills its own field schema out of the same QR string, so the two
    readings can be compared. That is the only check that proves the key mapping
    (Name→addressee, PersonalAcc→bankAcnt, BIC→bankBik, …) rather than restating it."""
    fx = fixture()
    parsed = client.parse_payment_qr(fx["qr"])
    bank = fx["provider_defaults"]
    check(parsed["requisites"] == bank,
          f"our reading of the QR differs from the bank's:\n"
          f"    ours={parsed['requisites']}\n    bank={bank}")
    check(parsed["hash"] == client.payment_qr_hash(fx["qr"]),
          "barcodeHash must be sha1 over the QR's utf-8 bytes")
    check(parsed["format"] == fx["qr_format"],
          f"the QR header is part of the contract: {parsed['format']!r}")
    print(f"  QR: {len(bank)} requisites read exactly as the bank read them")


def test_the_sum_in_a_qr_is_kopecks():
    """`Sum=2360000` is 23 600 ₽, not 2 360 000 ₽. Read as rubles this pays a
    hundred times the invoice, and it is the single most expensive way to be wrong
    in this file."""
    fx = fixture()
    parsed = client.parse_payment_qr(fx["qr"])
    kopecks = int(next(v for k, v in parsed["fields"].items() if k.lower() == "sum"))
    check(parsed["amount"] == round(kopecks / 100.0, 2),
          f"Sum={kopecks} must become {kopecks / 100.0} ₽, got {parsed['amount']}")
    check(parsed["amount"] == fx["qr_amount"],
          f"the fixture's amount is {fx['qr_amount']}, parsed {parsed['amount']}")

    # No Sum at all is an open invoice, not a zero-ruble one.
    open_invoice = client.parse_payment_qr("ST00012|Name=ООО Тест|PersonalAcc=1|BIC=2")
    check(open_invoice["amount"] is None,
          f"a QR with no Sum must give amount=None, got {open_invoice['amount']!r}")

    # Floating point: 123456 kopecks must not come back as 1234.5600000000001.
    cents = client.parse_payment_qr("ST00012|Name=X|Sum=123456")
    check(cents["amount"] == 1234.56, f"kopecks→rubles lost precision: {cents['amount']!r}")
    print("  QR: Sum is kopecks, an absent Sum is None, no float drift")


def test_a_string_that_is_not_a_payment_qr_is_refused():
    """A link, a loyalty card or an SBP QR would otherwise parse into a set of blank
    requisites and reach the payment path as «recipient: nothing»."""
    for junk in ("https://example.com/pay?x=1", "", "   ", "SPD1.0|foo=bar",
                 "1234567890"):
        try:
            client.parse_payment_qr(junk)
            failures.append(f"parse_payment_qr({junk!r}) returned instead of refusing")
        except TbankApiError as e:
            check(e.result_code == "QR_NOT_PAYMENT",
                  f"{junk!r}: wrong error code {e.result_code}")
            check("ST0001" in str(e), f"{junk!r}: the refusal must say what IS accepted")
    print("  QR: five non-payment strings refused, each naming the format expected")


# ---- the body that goes to the bank ---------------------------------------

def test_the_pay_body_matches_the_captured_payment():
    fx = fixture()
    s = LegalSession(fx)
    s.transfer_legal(fx["pay_parameters"]["moneyAmount"],
                     fx["pay_parameters"]["providerFields"],
                     account=fx["pay_parameters"]["account"],
                     user_payment_id=fx["pay_parameters"]["userPaymentId"],
                     from_qr=True)
    got, real = s.sent_pay_parameters(), fx["pay_parameters"]

    check(sorted(got) == sorted(real),
          f"payParameters keys differ\n    ours={sorted(got)}\n    real={sorted(real)}")
    check(sorted(got["providerFields"]) == sorted(real["providerFields"]),
          f"providerFields keys differ\n    ours={sorted(got['providerFields'])}"
          f"\n    real={sorted(real['providerFields'])}")
    for f in ("provider", "currency", "cellularService", "frontCamera",
              "isTransferStatus", "isUrgentTransfer", "paidByPhoto"):
        check(got.get(f) == real.get(f),
              f"payParameters.{f}: ours={got.get(f)!r} real={real.get(f)!r}")
    check("paymentType" not in got,
          "paymentType is sent on /v1/pay — it belongs to payment_commission only")

    form, query = s.sent_form(), s.sent_query()
    extra_form = set(form) - set(fx["form_keys"])
    extra_query = set(query) - set(fx["query_keys"])
    check(not extra_form, f"the pay FORM carries keys the app does not: {sorted(extra_form)}")
    check(not extra_query, f"the pay QUERY carries keys the app does not: {sorted(extra_query)}")
    missing = set(fx["form_keys"]) - set(form)
    check(not missing, f"the pay form is missing {sorted(missing)}")
    print("  body: matches the captured transfer-legal /v1/pay, nothing extra")


def test_the_payee_name_goes_out_as_utf8_not_as_escapes():
    """The captured body carries «ООО …» as percent-encoded utf-8 (%D0%9E%D0%9E…).
    json.dumps' default ensure_ascii=True would send \\u041e\\u041e\\u041e instead —
    a different byte string on the one request the bank signs and fraud-scores."""
    fx = fixture()
    s = LegalSession(fx)
    s.transfer_legal(100, dict(fx["pay_parameters"]["providerFields"]),
                     account="1111111111")
    check("%5Cu04" not in s.body and "\\u04" not in s.body,
          "the payee name is \\u-escaped; the app sends utf-8")
    check("%D0%" in s.body, "the payee name is not percent-encoded utf-8 at all")
    print("  body: Cyrillic goes out as percent-encoded utf-8, as captured")


def test_paid_by_photo_marks_a_scan_and_only_a_scan():
    """`paidByPhoto: "QR"` tells the bank the requisites were scanned. Sending it on
    a hand-typed payment states something false to the fraud engine; omitting it on
    a scan loses a signal the app always sends."""
    fx = fixture()
    scanned = LegalSession(fx)
    scanned.transfer_legal(100, dict(fx["provider_defaults"], comment="c"),
                           account="1111111111", from_qr=True)
    check(scanned.sent_pay_parameters().get("paidByPhoto") == "QR",
          "a scanned payment must carry paidByPhoto=QR, as the app sends")

    typed = LegalSession(fx)
    typed.transfer_legal(100, dict(fx["provider_defaults"], comment="c"),
                         account="1111111111", from_qr=False)
    check("paidByPhoto" not in typed.sent_pay_parameters(),
          "a hand-typed payment must NOT claim to have been scanned")
    print("  body: paidByPhoto is set for a scan and absent otherwise")


def test_the_commission_asks_as_a_transfer_not_as_a_payment():
    """Every captured commission call for this provider sends
    paymentType='Transfer'. pay_bill's sibling sends 'Payment', and copying that
    here would price a different product."""
    fx = fixture()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=100,
                   comment="Счет 1", ctx=accept_ctx(), **fx["tool_args"])
    check(s.commission_body is not None, f"no commission preview was run: {out}")
    body = s.commission_body or {}
    check(body.get("paymentType") == fx["commission_request"]["paymentType"],
          f"commission paymentType: ours={body.get('paymentType')!r} "
          f"real={fx['commission_request']['paymentType']!r}")
    check(body.get("provider") == "transfer-legal",
          f"commission provider: {body.get('provider')!r}")
    check(sorted(body) == sorted(fx["commission_request"]),
          f"commission keys differ\n    ours={sorted(body)}"
          f"\n    real={sorted(fx['commission_request'])}")
    print("  commission: previewed as a Transfer, with the captured key set")


# ---- what must be refused before a rouble moves ---------------------------

def test_a_payment_without_a_purpose_is_refused_before_it_is_sent():
    """`comment` (назначение платежа) is required for a Pay by the provider's own
    schema, and a QR does not always carry one. The refusal has to happen here —
    the commission preview accepts a body with no comment at all. The human has
    pressed the button — the refusal is the body's own, after the confirmation."""
    fx = fixture()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=100, ctx=accept_ctx(),
                   **fx["tool_args"])
    check(s.body is None, "a payment with no purpose reached the bank")
    check("comment" in out and "НЕ отправлен" in out,
          f"the refusal must name the missing field: {out!r}")
    print("  refusal: no назначение платежа → nothing is sent")


def test_bad_requisites_are_refused_against_the_banks_own_regexps():
    """The catalogue publishes a regexp per field. These are the four ways to pay a
    stranger: a truncated account, a БИК that is not a БИК, an ИНН of the wrong
    length, a КПП that is not nine digits."""
    fx = fixture()
    args = dict(fx["tool_args"], comment="Счет 1")
    cases = [("account_number", "4070281000000000001", "bankAcnt"),   # 19 digits
             ("account_number", "не-счёт", "bankAcnt"),
             ("bik", "44525411", "bankBik"),                          # 8 digits
             ("inn", "35252163", "inn"),                              # 8 digits
             ("kpp", "35250100", "kpp")]                              # 8 digits
    for arg, bad, field_id in cases:
        s = LegalSession(fx)
        # Accept on the button, so what stops the payment is the regexp, not the gate.
        out = run_tool(s, server.transfer_requisites, amount=100, ctx=accept_ctx(),
                       **dict(args, **{arg: bad}))
        check(s.body is None, f"{arg}={bad!r} was PAID instead of refused")
        check(field_id in out, f"{arg}={bad!r}: the refusal must name {field_id}: {out!r}")
    print(f"  refusal: {len(cases)} malformed requisites stopped before the bank")


def test_the_sum_must_come_from_somewhere():
    """Deliberately headless: a non-positive sum needs no button (there is nothing
    to confirm), so the gate lets it through and the body's own message names what
    to pass — in any client."""
    fx = fixture()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=0,
                   qr="ST00012|Name=ООО Тест|PersonalAcc=40702810000000000001"
                      "|BIC=044525000|PayeeINN=7700000000", comment="Счет 1")
    check(s.body is None, "a payment with no amount was sent")
    check("amount" in out, f"the message must say what to pass: {out!r}")

    plain = LegalSession(fx)
    out2 = run_tool(plain, server.transfer_requisites, amount=-5,
                    comment="c", **fx["tool_args"])
    check(plain.body is None, "a negative amount was sent")
    check("положительным числом" in out2,
          f"a negative amount needs its own message: {out2!r}")
    print("  refusal: no sum in the QR and none passed → nothing is sent")


def test_transfer_points_at_the_tool_that_can_do_this():
    """transfer(provider='transfer-legal') cannot carry nine requisites. It used to
    say the payment was unimplemented; now it is, and a stale refusal would send the
    agent to the app for something the MCP does."""
    s = LegalSession(fixture())
    try:
        s.transfer(100, "40702810000000000001", provider="transfer-legal")
        failures.append("transfer(provider='transfer-legal') silently did something")
    except TbankApiError as e:
        check(e.result_code == "WRONG_METHOD", f"wrong code: {e.result_code}")
        check("transfer_requisites" in str(e),
              f"the refusal must name the tool that works: {e}")
    check(s.body is None, "transfer() sent a legal-entity payment through the p2p path")
    print("  transfer(): routes transfer-legal to transfer_requisites instead of refusing")


# ---- filling in what the payer should not have to know --------------------

def test_the_qr_goes_to_the_bank_the_way_the_app_sends_it():
    """The app puts barcodeHash, qr and frontendFeatureFlag in the query AND in the
    JSON body. Which one the gate reads is not observable, so both are reproduced."""
    fx = fixture()
    s = LegalSession(fx)
    s.qr_providers(fx["qr"])
    sent = s.qr_request or {}
    for where in ("body", "query"):
        block = sent.get(where) or {}
        check(sorted(block) == ["barcodeHash", "frontendFeatureFlag", "qr"],
              f"the qr/resolve {where} carries {sorted(block)}")
        check(block.get("qr") == fx["qr"], f"the {where} lost the QR string")
        check(block.get("barcodeHash") == client.payment_qr_hash(fx["qr"]),
              f"the {where} carries a barcodeHash that is not sha1 of the QR")
    print("  qr/resolve: barcodeHash + qr + flag, in both the query and the body")


def test_the_bik_supplies_the_corr_account_and_the_bank_name():
    """The app looks the БИК up the moment a QR resolves. That is why paying by hand
    needs only the БИК: the correspondent account and the bank's legal name come
    from the bank, not from the payer's memory."""
    fx = fixture()
    s = LegalSession(fx)
    fields, _, _ = server._legal_requisites(
        s, qr="", explicit={"bankBik": fx["bank_info_bik"], "bankAcnt": "x"})
    check(s.bik_lookups == [fx["bank_info_bik"]],
          f"the БИК was not looked up: {s.bik_lookups}")
    check(fields.get("bankCorrAcnt") == fx["bank_info_corr"],
          f"corr account not filled from the БИК: {fields.get('bankCorrAcnt')!r}")
    check(fields.get("bankName") == fx["bank_info_name"],
          f"bank name not filled from the БИК: {fields.get('bankName')!r}")

    # Already known → no request. A lookup per payment is a request the app does
    # not make when the QR already carried both.
    quiet = LegalSession(fx)
    server._legal_requisites(quiet, qr="", explicit={
        "bankBik": fx["bank_info_bik"], "bankCorrAcnt": "30101810000000000009",
        "bankName": "Свой Банк"})
    check(quiet.bik_lookups == [],
          f"the БИК was looked up although both values were known: {quiet.bik_lookups}")
    print("  БИК: fills the corr account and bank name, and only when they are missing")


def test_an_explicit_argument_beats_the_qr():
    """A QR that scanned badly is corrected by passing the field. Preferring the
    scan silently would pay whatever the camera read."""
    fx = fixture()
    s = LegalSession(fx)
    fields, amount, from_qr = server._legal_requisites(
        s, qr=fx["qr"], explicit={"addressee": "ООО ДРУГОЕ", "comment": "Счет 7"})
    check(fields["addressee"] == "ООО ДРУГОЕ",
          f"the explicit payee was overwritten by the QR: {fields['addressee']!r}")
    check(fields["bankAcnt"] == fx["provider_defaults"]["bankAcnt"],
          "the untouched fields must still come from the QR")
    check(fields["comment"] == "Счет 7", "the purpose must be settable alongside a QR")
    check(amount == fx["qr_amount"] and from_qr is True,
          f"the QR's amount and scan flag are lost: {amount!r} {from_qr!r}")
    print("  merge: explicit arguments win, the rest of the QR survives")


def test_nds_defaults_to_the_providers_own_default():
    """The VAT mark ends up on the payment order the recipient's accountant reads.
    It has exactly two legal values and the provider's own defaultValue is 322."""
    fx = fixture()
    s = LegalSession(fx)
    fields, _, _ = server._legal_requisites(s, qr=fx["qr"], explicit={})
    check(fields["nds"] == client.NDS_EXEMPT,
          f"nds must default to {client.NDS_EXEMPT}, got {fields['nds']!r}")
    schema = {f["id"]: f for f in fx["provider_catalogue"]["fields"]}
    values = {o["value"] for o in schema["nds"].get("optionsList", [])}
    check(values == {client.NDS_INCLUDED, client.NDS_EXEMPT},
          f"the provider's nds options changed: {sorted(values)}")

    chosen = LegalSession(fx)
    fields2, _, _ = server._legal_requisites(chosen, qr=fx["qr"],
                                             explicit={"nds": client.NDS_INCLUDED})
    check(fields2["nds"] == client.NDS_INCLUDED, "an explicit nds must be honoured")
    print("  nds: defaults to «НДС не облагается», both legal values still published")


# ---- the outcome the agent is told ----------------------------------------

def test_a_lost_payment_blocks_the_next_identical_one():
    """A dropped connection means the money MAY have gone. The repeat must be
    refused until the user reconciles, and the forced retry must carry the SAME
    userPaymentId or it is a second payment to the same company."""
    fx = fixture()
    args = dict(fx["tool_args"], comment="Счет 1")
    open(os.environ["TBANK_ATTEMPTS"], "w").close()

    lost = LegalSession(fx, fail=True)
    out = run_tool(lost, server.transfer_requisites, amount=4200, ctx=accept_ctx(), **args)
    check("НЕИЗВЕСТЕН" in out, f"a dropped connection must not read as success: {out}")
    check("list_operations" in out, f"the user must be told how to check: {out}")
    first = lost.sent_pay_parameters()["userPaymentId"]

    # The human confirms again — the duplicate guard, not the button, is what
    # must stop the repeat.
    again = LegalSession(fx)
    out2 = run_tool(again, server.transfer_requisites, amount=4200, ctx=accept_ctx(), **args)
    check("ЗАБЛОКИРОВАН" in out2, f"the identical repeat must be blocked: {out2}")
    check(again.body is None, "the blocked repeat was sent anyway")

    # force=True overrides the guard, not the button: it is still confirmed.
    forced = LegalSession(fx)
    out3 = run_tool(forced, server.transfer_requisites, amount=4200, force=True,
                    ctx=accept_ctx(), **args)
    check(fx["pay_response"]["paymentId"] in out3, f"force must go through: {out3}")
    check(forced.sent_pay_parameters()["userPaymentId"] == first,
          f"the retry must reuse the original userPaymentId ({first} → "
          f"{forced.sent_pay_parameters()['userPaymentId']})")
    print("  idempotency: an unknown outcome blocks the repeat and the retry reuses the id")


def test_a_refusal_is_not_reported_as_a_possible_charge():
    """The bank answering «no» is not an unknown outcome: the request completed and
    nothing moved. Saying otherwise blocks the next attempt and tells the user their
    money may be gone."""
    fx = fixture()
    open(os.environ["TBANK_ATTEMPTS"], "w").close()

    class Refusing(LegalSession):
        def _call_signed(self, *a, **kw):
            super()._call_signed(*a, **kw)
            raise TbankApiError("INVALID_REQUEST_DATA", "поле заполнено неверно")

    s = Refusing(fx)
    out = run_tool(s, server.transfer_requisites, amount=99,
                   comment="Счет 1", ctx=accept_ctx(), **fx["tool_args"])
    check("НЕ выполнен" in out, f"a refusal must be reported as one: {out}")
    check("НЕИЗВЕСТЕН" not in out, f"a refusal is not an unknown outcome: {out}")
    check("деньги на месте" in out, f"say plainly that nothing moved: {out}")
    print("  outcome: a bank refusal is not dressed up as a possible charge")


def test_the_result_carries_what_the_agent_needs_next():
    fx = fixture()
    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    s = LegalSession(fx)
    out = run_tool(s, server.transfer_requisites, amount=4321,
                   comment="Счет 1 от 01.01.2026", ctx=accept_ctx(), **fx["tool_args"])
    check(fx["pay_response"]["paymentId"] in out, f"paymentId must be returned: {out}")
    check("payment_receipt" in out, f"the tool that uses it must be named: {out}")
    check("4\u00a0321.00 RUB" in out or "4 321.00 RUB" in out,
          f"the amount must be stated with its currency: {out!r}")
    check(fx["tool_args"]["name"] in out, f"the payee must be stated: {out}")
    check("НДС" in out, f"the VAT mark must be visible in the result: {out}")

    blank = LegalSession(fx, payload={"payload": {}})
    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    out2 = run_tool(blank, server.transfer_requisites, amount=10,
                    comment="Счет 1", ctx=accept_ctx(), **fx["tool_args"])
    check("без paymentId" in out2, f"a missing paymentId must be flagged: {out2}")
    print("  result: paymentId, payee, amount and VAT mark all reported")


def test_the_qr_preview_shows_what_the_user_must_confirm():
    """payment_qr is the read-only half: it is what the agent shows before spending
    real money, so it has to carry the payee, the sum and the fee."""
    fx = fixture()
    s = LegalSession(fx)
    out = run_tool(s, server.payment_qr, fx["qr"])
    check(fx["provider_defaults"]["addressee"] in out, f"no payee in the preview: {out}")
    check(fx["provider_defaults"]["bankAcnt"] in out, f"no account in the preview: {out}")
    check("23\u00a0600.00 RUB" in out or "23 600.00 RUB" in out
          or f"{fx['qr_amount']:,.2f}".replace(",", " ") in out,
          f"the amount from the QR must be shown: {out}")
    check("Комиссия" in out, f"the fee must be previewed: {out}")
    check("transfer_requisites" in out, f"the next call must be named: {out}")
    check(s.body is None, "the read-only preview sent a payment")

    # This QR carries no Purpose, so the tool has to say three things: that the bank
    # requires one, that the invoice is where to look FIRST, and what to pass. The
    # middle one is the point — an agent told only «спроси» interrogates the user for
    # a number that is printed on the счёт it was already shown.
    check("банк его требует" in out,
          f"a QR with no purpose must say the bank requires one: {out}")
    check("счёта" in out and "спрашивай" in out,
          f"the hint must send the agent to the invoice before the user: {out}")
    check("comment=" in out, f"the hint must name the parameter to pass: {out}")

    # And when the QR DOES carry a Purpose, there must be no hint at all — the field
    # is already filled and asking again is noise.
    with_purpose = LegalSession(fx)
    out2 = run_tool(with_purpose, server.payment_qr,
                    fx["qr"] + "|Purpose=Оплата по счету 42 от 01.08.2026")
    check("Назначение платежа: Оплата по счету 42 от 01.08.2026" in out2,
          f"a Purpose in the QR must show up as the назначение: {out2}")
    check("банк его требует" not in out2,
          f"nothing to ask for when the QR carried a purpose: {out2}")
    print("  payment_qr: payee, requisites, sum, fee and the next call")


def test_a_non_legal_qr_hands_over_the_fields_pay_bill_needs():
    """A QR whose provider is NOT transfer-legal routes to pay_bill(provider, fields,
    amount). The tool named that call but never printed `fields`, so the values it
    parsed from the QR were dropped and the agent had nothing to pass. The fields
    must be handed over machine-ready."""
    fx = fixture()

    class NonLegal(LegalSession):
        def qr_providers(self, qr):
            # A utility/tax provider: its own field schema, filled from the QR.
            return [{"id": "fns-rf", "name": "ФНС России", "fields": [
                {"id": "purpose", "defaultValue": "Налог"}]}]

    out = run_tool(NonLegal(fx), server.payment_qr, fx["qr"])
    check("pay_bill" in out and "fns-rf" in out,
          f"the pay_bill call must name the provider: {out!r}")
    # The fields line must be present and be parseable JSON carrying real values.
    field_lines = [l for l in out.splitlines() if "передай как fields" in l]
    check(field_lines, f"the machine-ready fields dict must be printed: {out!r}")
    if not field_lines:
        return                      # nothing to parse — the miss is already recorded
    import json as _json, re as _re
    m = _re.search(r"\{.*\}", field_lines[0])
    check(m is not None, f"the fields line must contain a JSON object: {field_lines[0]!r}")
    if not m:
        return
    fields = _json.loads(m.group(0))
    # The QR's own INN wins over the provider default (gap-fill only), and the
    # provider default fills a field the QR lacked (purpose). Both prove the parsed
    # values are handed over, not dropped.
    check(fields.get("inn") == "7700000000",
          f"the QR-parsed INN must reach the fields dict: {fields}")
    check(fields.get("purpose") == "Налог",
          f"a provider default that filled a gap must reach the fields dict: {fields}")
    print("  payment_qr: a non-legal QR hands pay_bill its fields, not just the provider id")


def main():
    print("payment by requisites:")
    test_the_fixture_still_matches_the_capture()
    test_the_qr_is_read_the_way_the_bank_reads_it()
    test_the_sum_in_a_qr_is_kopecks()
    test_a_string_that_is_not_a_payment_qr_is_refused()
    test_the_pay_body_matches_the_captured_payment()
    test_the_payee_name_goes_out_as_utf8_not_as_escapes()
    test_paid_by_photo_marks_a_scan_and_only_a_scan()
    test_the_commission_asks_as_a_transfer_not_as_a_payment()
    test_a_payment_without_a_purpose_is_refused_before_it_is_sent()
    test_bad_requisites_are_refused_against_the_banks_own_regexps()
    test_the_sum_must_come_from_somewhere()
    test_transfer_points_at_the_tool_that_can_do_this()
    test_the_qr_goes_to_the_bank_the_way_the_app_sends_it()
    test_the_bik_supplies_the_corr_account_and_the_bank_name()
    test_an_explicit_argument_beats_the_qr()
    test_nds_defaults_to_the_providers_own_default()
    test_a_lost_payment_blocks_the_next_identical_one()
    test_a_refusal_is_not_reported_as_a_possible_charge()
    test_the_result_carries_what_the_agent_needs_next()
    test_the_qr_preview_shows_what_the_user_must_confirm()
    test_a_non_legal_qr_hands_over_the_fields_pay_bill_needs()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
