"""Regenerate tests/fixtures/grocery_cart.json from a Burp capture.

The capture is the user's real banking traffic and is gitignored — it can never be
committed. But a test that only runs when the capture happens to be present is not
coverage: on a clean clone tests/test_cart_body_matches_capture.py used to print
SKIP and exit 0, reporting success having verified nothing.

So the contract lives in a fixture: real STRUCTURE and real protocol values (areaId,
pointId, appId, cartSetMode, the goods list shape), synthetic personal values. The
test runs everywhere against the fixture, and when the real capture IS present it
additionally checks the fixture still matches it — so the fixture cannot silently
drift away from what the app sends.

    python3 tests/fixtures/regen.py [path/to/captures.xml]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import test_cart_body_matches_capture as T  # noqa: E402

# Replaced consistently, so structure and protocol-critical values survive intact.
FAKE_ADDR = {
    "city": "Москва", "country": "Россия", "doorphone": "0000", "doorway": "1",
    "flat": "1", "house": "1", "houseType": "house", "name": "",
    "postalCode": "000000", "region": "Москва", "settlement": "",
    "storey": "1", "street": "Примерная", "streetWithType": "ул Примерная",
}
FAKE_VALUE = "ул Примерная, д 1, кв 1"
PERSONAL_KEYS = ("phone", "email", "name", "fio", "clientname", "firstname", "lastname")


def scrub(o):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            kl = k.lower()
            if k == "details" and isinstance(v, dict):
                out[k] = {dk: FAKE_ADDR.get(dk, "") for dk in v}
            elif kl == "coordinates":
                out[k] = {ck: 0.0 for ck in v} if isinstance(v, dict) else [0.0, 0.0]
            elif kl == "value" and isinstance(v, str) and len(v) > 3 and not v.isdigit():
                out[k] = FAKE_VALUE
            elif kl == "comment":
                out[k] = ""
            elif kl in PERSONAL_KEYS and isinstance(v, str):
                out[k] = ""
            else:
                out[k] = scrub(v)
        return out
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o


def renumber_address_ids(client_info):
    """Address-record UUIDs are the user's, not the protocol's.

    scrub() cannot catch them by key name: `id` also carries goods ids and `pointId`
    carries public store ids, both of which are the contract and must survive. So
    they are replaced positionally, here, where the shape of the document is known.
    The replacements keep UUID form (something downstream may parse it) and are
    obviously synthetic."""
    addrs = ((client_info.get("deliveryInfo") or {}).get("addresses") or [])
    for i, a in enumerate(addrs, 1):
        if isinstance(a, dict) and "id" in a:
            a["id"] = f"00000000-0000-4000-8000-{i:012d}"
    return client_info


def scrub_envelope(env):
    """trackingId is the bank's handle on one real request by this user."""
    if isinstance(env, dict) and "trackingId" in env:
        env["trackingId"] = "00000000-0000-4000-8000-000000000000"
    return env


def slim_retailers(payload):
    """Only the appId → pointId/areaId mapping the cart body needs."""
    cats = []
    for cat in payload.get("categories", []):
        rets = []
        for r in cat.get("retailers", []):
            d = r.get("delivery") or {}
            rets.append({"appId": r.get("appId"),
                         "delivery": {"pointId": d.get("pointId"), "areaId": d.get("areaId")}})
        if rets:
            cats.append({"retailers": rets})
    return {"categories": cats}


def build(items):
    return {
        "_note": ("Scrubbed from a Burp capture: structure and protocol values are real, "
                  "personal values are synthetic. Regenerate with tests/fixtures/regen.py."),
        "client_info": renumber_address_ids(
            scrub(T.response_json(items, T.CLIENT_INFO)["payload"])),
        "cart_get": {"cart": scrub(T.response_json(items, T.CART_GET)["payload"]["cart"])},
        "retailers": slim_retailers(T.response_json(items, T.RETAILERS)["payload"]),
        "expected_azbuka": scrub(T.request_json(items, T.AZBUKA_CART_SET)),
        "expected_vkusvill": scrub(T.request_json(items, T.VKUSVILL_CART_SET)),
        "error_envelope": scrub_envelope(T.response_json(items, T.ERROR_ENVELOPE)),
        "cart_set_escalation": build_cart_set_escalation(),
    }


def build_cart_set_escalation():
    """The cartSetMode escalation, from captures2.xml.

    Holds no payload — just the two mode strings, the app code the narrow one is
    refused with, and which keys actually differ between the refused and the
    accepted body. That last part is the whole point: the two requests are
    identical apart from cartSetMode, which is why «268 Сервис временно
    недоступен» means «reset the other cart», not «come back later»."""
    import test_cart_body_matches_capture as T

    if not os.path.exists(T.CAPTURE2):
        raise FileNotFoundError(T.CAPTURE2)
    saved = T.CAPTURE
    T.CAPTURE = T.CAPTURE2
    try:
        items = T._items()
        refused = T.request_json(items, T.CART_SET_REFUSED)
        accepted = T.request_json(items, T.CART_SET_ACCEPTED)
        refused_resp = T.response_json(items, T.CART_SET_REFUSED)
    finally:
        T.CAPTURE = saved
    differing = sorted(k for k in set(refused) | set(accepted)
                       if refused.get(k) != accepted.get(k))
    return {
        "refused_mode": refused["cartSetMode"],
        "accepted_mode": accepted["cartSetMode"],
        "refused_code": str((refused_resp.get("payload") or {}).get("code", "")),
        "differing_keys": differing,
    }


def build_cancel():
    """The cancellation, from delete-order.xml (the only capture that has one).

    Nothing but SHAPE survives: cancel puts everything in the query and sends an
    empty body, and every one of those query values — orderId, paymentId,
    sessionid, deviceId — is the user's. So the fixture keeps key NAMES only."""
    import urllib.parse
    import test_booking_and_ranking as B

    with open(B.CANCEL_CAPTURE, "rb") as fh:
        blob = fh.read().decode("utf-8", "replace")
    for item in re.findall(r"<item>(.*?)</item>", blob, re.S):
        url = re.search(r"<url>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</url>", item, re.S)
        if not url or "/order/cancel" not in url.group(1):
            continue
        parts = urllib.parse.urlsplit(url.group(1).strip())
        method = re.search(r"<method>(?:<!\[CDATA\[)?(\w+)", item)
        return {
            "method": method.group(1) if method else "POST",
            "host": f"{parts.scheme}://{parts.netloc}",
            "path": parts.path,
            "query_keys": sorted(urllib.parse.parse_qs(parts.query)),
            "body": "",
        }
    raise SystemExit(f"no /order/cancel request in {B.CANCEL_CAPTURE}")


def build_booking():
    """The three money-moving ticket bodies from captures2.xml, plus the
    cancellation shape from delete-order.xml.

    eventId/slotId/objectId/seat ids are public catalogue identifiers and stay real —
    they ARE the contract. The payer's account (`agreement`) and the real orderId are
    the user's, and are replaced."""
    import test_booking_and_ranking as B

    items = B._items()
    movie = B.request_json(items, B.CREATE_MOVIE)
    concert = B.request_json(items, B.CREATE_CONCERT)
    pay = B.request_json(items, B.PAY)
    pay["paymentMethod"]["agreement"] = "0000000000"
    pay["flow"]["orderId"] = "10000000000"
    return {
        "_note": ("Scrubbed from a Burp capture. Catalogue ids are real (they are the "
                  "contract); the payer account and order id are synthetic. "
                  "`cancel` records a QUERY-string endpoint, so it holds key NAMES "
                  "and no values at all. Regenerate with tests/fixtures/regen.py."),
        "create_movie": movie,
        "create_concert": concert,
        "cancel": build_cancel(),
        "pay": pay,
    }


def build_transfer():
    """The real signed /v1/pay, from captures.xml #1477 (p2p-anybank via SBP).

    Kept: the query keys, the form keys, and the payParameters KEY SET plus the
    protocol constants. Replaced: the payer account, the recipient's phone, name and
    SBP ids, the device id and the session id."""
    import urllib.parse

    import test_cart_body_matches_capture as T

    items = T._items()
    raw = T._raw(items[1477], "request")
    head, _, body = raw.partition(b"\r\n\r\n")
    line = head.split(b"\r\n")[0].decode()
    query = urllib.parse.parse_qs(line.split("?", 1)[1].split(" ")[0])
    form = urllib.parse.parse_qs(body.decode())
    pp = json.loads(form["payParameters"][0])

    pp["account"] = "0000000000"
    # A millisecond timestamp is a handle on one real payment — when it happened, and
    # what the bank deduplicates against. Only its SHAPE is the contract (13 digits),
    # and that is what the test asserts, so the value goes.
    pp["userPaymentId"] = "1700000000000"
    pf = pp.get("providerFields", {})
    for k, v in (("pointer", "+79991234567"), ("maskedFIO", "И. И."),
                 ("bankMemberId", "100000000000"), ("pointerLinkId", "10000000000")):
        if k in pf:
            pf[k] = v
    secret = {"sessionid", "deviceId", "oldDeviceId"}

    # The RESPONSE, from the same exchange. Its shape is the contract for what the
    # tool reports back: commissionInfo carries three money objects and picking the
    # wrong one turns the transfer itself into its own «commission». A stub written
    # from memory (`commissionInfo: {"value": 0}`) is what let that ship.
    resp = json.loads(T._raw(items[1477], "response").partition(b"\r\n\r\n")[2])
    payload = resp.get("payload", resp)
    payload["paymentId"] = "100000000001"

    return {
        "_note": ("Scrubbed from captures.xml #1477 (POST /v1/pay, p2p-anybank, 200). "
                  "Key sets and protocol constants are real; account, recipient, "
                  "userPaymentId, paymentId and device/session ids are synthetic."),
        "query_keys": sorted(query),
        "query_static": {k: v[0] for k, v in sorted(query.items()) if k not in secret},
        "form_keys": sorted(form),
        "pay_parameters": pp,
        "pay_response": payload,
    }


# The real QR names a real company, its real bank account and the sum the repo owner
# really paid. Every one of those values is replaced by a fixed synthetic one, applied
# to the QR string AND to the bank's own reading of it — so the fixture still proves
# our parse agrees with the bank's, without carrying the transaction.
#
# The replacements keep the FORM the provider's regexps demand: a 20-digit account,
# a БИК starting 0/1/2, a 10-digit ИНН, a 9-digit КПП.
LEGAL_FAKE = {
    "Name": 'ООО "ПРИМЕР"',
    "PersonalAcc": "40702810000000000001",
    "BankName": 'Филиал "Пример" Банка Пример (ПАО)',
    "BIC": "044525000",
    "CorrespAcc": "30101810000000000001",
    "PayeeINN": "7700000000",
    "KPP": "770000001",
    "Sum": "123456",                # 1 234.56 ₽ — still proves kopecks and rounding
}
LEGAL_COMMENT = "Счет 1 от 01.01.2026"


def build_transfer_legal():
    """The whole pay-by-requisites flow, from captures_payreq.xml.

    Kept real: every key set, the provider's published field schema and regexps, the
    protocol constants (paymentType, nds values, paidByPhoto) and the response shape.
    Replaced: the company, its account and bank, the sum, the payer account, the
    userPaymentId and the paymentId."""
    import test_requisites as R

    if not os.path.exists(R.CAPTURE):
        raise FileNotFoundError(R.CAPTURE)
    items = R.items()

    qr_real = R.request_json(items, R.QR_RESOLVE)["qr"]
    head, _, rest = qr_real.partition("|")
    # Value-for-value substitution, so the same real string never survives anywhere.
    subs, pairs = {}, []
    for chunk in rest.split("|"):
        key, sep, value = chunk.partition("=")
        fake = LEGAL_FAKE.get(key.strip(), value)
        if value and fake != value:
            subs[value] = fake
        pairs.append(f"{key}{sep}{fake}")
    qr = head + "|" + "|".join(pairs)

    def scrub(value):
        if isinstance(value, str):
            out = value
            for real, fake in subs.items():
                out = out.replace(real, fake)
            return out
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    resolved = R.response_json(items, R.QR_RESOLVE)["payload"]["providersList"]["providers"][0]
    # The bank's own reading of the QR: field id → defaultValue, scrubbed the same
    # way. `nds` is the provider's constant, not something read out of the QR.
    defaults = {f["id"]: scrub(f["defaultValue"])
                for f in resolved["fields"]
                if f.get("defaultValue") and f["id"] != "nds"}

    catalogue = next(p for p in R.response_json(items, R.CATALOGUE)
                     ["payload"]["providersPage"]["providers"]
                     if p["id"] == "transfer-legal")

    pay_form = R.request_form(items, R.PAY)
    pp = scrub(json.loads(pay_form["payParameters"][0]))
    pp["account"] = "0000000000"
    pp["userPaymentId"] = "1700000000000"
    pp["moneyAmount"] = int(LEGAL_FAKE["Sum"]) / 100.0
    pp["providerFields"]["comment"] = LEGAL_COMMENT

    commission = scrub(json.loads(R.request_form(items, R.COMMISSION)["payParameters"][0]))
    commission["moneyAmount"] = pp["moneyAmount"]
    commission["account"] = pp["account"]
    commission["providerFields"]["comment"] = LEGAL_COMMENT

    commission_resp = scrub(R.response_json(items, R.COMMISSION)["payload"])
    for key in ("value", "total"):
        money = commission_resp.get(key)
        if isinstance(money, dict) and "value" in money:
            money["value"] = 0 if key == "value" else pp["moneyAmount"]

    pay_resp = R.response_json(items, R.PAY)["payload"]
    pay_resp["paymentId"] = "100000000001"
    info = pay_resp.get("commissionInfo") or {}
    for key in ("amount", "amountWithCommission"):
        if isinstance(info.get(key), dict):
            info[key]["value"] = pp["moneyAmount"]
    if isinstance(info.get("commission"), dict):
        info["commission"]["value"] = 0.0

    bank = R.response_json(items, R.BANK_INFO)["payload"]
    corr = (bank.get("correspondentAccount") or {}).get("value")

    return {
        "_note": ("Scrubbed from captures_payreq.xml (QR → resolve → commission → "
                  "signed /v1/pay, transfer-legal, 200). Key sets, the provider's "
                  "field schema and regexps, and the protocol constants are real; "
                  "the company, its account and bank, the sum, the payer account, "
                  "userPaymentId and paymentId are synthetic. "
                  "Regenerate with tests/fixtures/regen.py."),
        "qr": qr,
        "qr_format": head,
        "qr_amount": int(LEGAL_FAKE["Sum"]) / 100.0,
        "provider_defaults": defaults,
        "provider_resolved": {"id": resolved["id"], "name": resolved["name"],
                              "fields": [dict(f, defaultValue=scrub(f["defaultValue"]))
                                         if f.get("defaultValue") else f
                                         for f in resolved["fields"]]},
        "provider_catalogue": catalogue,
        "query_keys": sorted(R.request_query(items, R.PAY)),
        "form_keys": sorted(pay_form),
        "pay_parameters": pp,
        "pay_response": pay_resp,
        "commission_request": commission,
        "commission_response": commission_resp,
        "bank_info": scrub(bank),
        "bank_info_bik": LEGAL_FAKE["BIC"],
        "bank_info_name": LEGAL_FAKE["BankName"],
        "bank_info_corr": LEGAL_FAKE["CorrespAcc"] if corr else "",
        "tool_args": {"name": LEGAL_FAKE["Name"],
                      "account_number": LEGAL_FAKE["PersonalAcc"],
                      "bik": LEGAL_FAKE["BIC"], "inn": LEGAL_FAKE["PayeeINN"],
                      "kpp": LEGAL_FAKE["KPP"]},
    }


def build_recipient():
    """Both halves of a phone lookup, from captures.xml: the app asks
    /v1/get_requisites TWICE per number — pointerSource=internal for the recipient's
    own T-Bank account, pointerSource=external for their SBP banks — and only the
    two together are the answer to "where can this phone be paid".

    The number picked is the one where the difference is visible: external returns
    Sber and VTB, internal returns a T-Bank account, and nothing in the external
    response says a second list exists. `withTinkoff=true` on the external call does
    not fold it in, which is the whole reason this fixture exists.

    Kept real: the query keys of both calls (the internal one carries neither
    withTinkoff nor gapBanks), workflowType, the brand block, and the providerFields
    key set of the commission the app then sends for each candidate — the internal
    one has NO bankMemberId key at all. Replaced: the phone, the name and the ids.

    The commissions are the app's opening probe, moneyAmount=0, so every one of them
    comes back unfinishedFlag=true; the flag is kept because a preview that carries
    it is not a quote, whatever number sits next to it."""
    import urllib.parse

    import test_cart_body_matches_capture as T

    FAKE_PHONE, FAKE_FIO = "+79991234567", "И. И."

    def parsed(item):
        """(path, query, request bytes) for one capture item, or None."""
        raw = T._raw(item, "request")
        line = raw.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        path = line.split(" ")[1] if line.count(" ") >= 2 else ""
        if "?" not in path:
            return path, {}, raw
        return path, urllib.parse.parse_qs(path.split("?", 1)[1]), raw

    # The sample phone is chosen by PROPERTY, never written down: the one number in
    # the capture whose internal AND external lookups both answer with candidates.
    # A literal here would put a real person's phone in a committed file.
    items = T._items()
    answered = {}
    for item in items:
        path, query, _ = parsed(item)
        if "/v1/get_requisites" not in path:
            continue
        pointer = (query.get("pointer") or [""])[0]
        source = (query.get("pointerSource") or [""])[0]
        body = T._raw(item, "response").partition(b"\r\n\r\n")[2].strip()
        if not pointer or source not in ("internal", "external") or not body:
            continue
        if json.loads(body).get("payload"):
            answered.setdefault(pointer, set()).add(source)
    phones = [p for p, s in answered.items() if s == {"internal", "external"}]
    if not phones:
        raise SystemExit("no phone in the capture has BOTH an internal and an "
                         "external get_requisites answer — the fixture would not "
                         "show the difference it exists to show")
    phone = phones[0]

    links = {}         # real pointerLinkId → synthetic, in encounter order
    members = {}       # real bankMemberId  → synthetic, in encounter order

    def fake_link(real):
        return links.setdefault(str(real), f"1000000000{len(links)}")

    def fake_member(real):
        """SBP member ids are public — they name a bank, not a person — but they are
        twelve digits copied out of a capture, which is the shape and the provenance
        tests/test_no_personal_data.py exists to stop at the door. Which bank a
        candidate is survives in brand.name, so nothing the fixture proves needs the
        real number."""
        return members.setdefault(str(real), f"40000000000{len(members) + 2}")

    def scrub_candidate(c):
        c = json.loads(json.dumps(c))
        c["pointerLinkId"] = fake_link(c.get("pointerLinkId"))
        for f in c.get("displayFields") or []:
            if f.get("name") == "maskedFIO":
                f["value"] = FAKE_FIO
            elif f.get("name") == "bankMemberId":
                f["value"] = fake_member(f.get("value"))
        return c

    resolves, commissions = {}, []
    for item in items:
        path, query, raw = parsed(item)
        if "/v1/get_requisites" in path and phone in (query.get("pointer") or []):
            source = (query.get("pointerSource") or [""])[0]
            body = T._raw(item, "response").partition(b"\r\n\r\n")[2].strip()
            if source not in ("internal", "external") or not body:
                continue
            resolves[source] = {
                "query_keys": sorted(k for k in query if k not in
                                     ("sessionid", "deviceId", "oldDeviceId")),
                "payload": [scrub_candidate(c)
                            for c in (json.loads(body).get("payload") or [])],
            }
        elif "/v1/payment_commission" in path:
            form = urllib.parse.parse_qs(raw.partition(b"\r\n\r\n")[2].decode())
            pp = json.loads(form["payParameters"][0])
            pf = pp.get("providerFields") or {}
            if pf.get("pointer") != phone:
                continue
            pf["pointer"] = FAKE_PHONE
            pf["maskedFIO"] = FAKE_FIO
            pf["pointerLinkId"] = fake_link(pf.get("pointerLinkId"))
            if "bankMemberId" in pf:
                pf["bankMemberId"] = fake_member(pf["bankMemberId"])
            pp["account"] = "0000000000"
            resp = json.loads(T._raw(item, "response").partition(b"\r\n\r\n")[2])
            commissions.append({
                "pay_parameters": pp,
                "unfinished_flag": (resp.get("payload") or {}).get("unfinishedFlag"),
            })

    return {
        "_note": ("Scrubbed from captures.xml: /v1/get_requisites for ONE phone, both "
                  "pointerSources, plus the /v1/payment_commission bodies the app "
                  "then sends for each candidate. `internal_pay` is the signed "
                  "/v1/pay for the T-Bank-internal candidate, from captures-pay.xml. "
                  "Query keys, workflowType, brands and the providerFields key sets "
                  "are real; the phone, the name, the ids and the amount are "
                  "synthetic. Regenerate with tests/fixtures/regen.py."),
        "phone": FAKE_PHONE,
        "masked_fio": FAKE_FIO,
        "internal": resolves["internal"],
        "external": resolves["external"],
        "commissions": commissions,
        "internal_pay": build_internal_pay(FAKE_PHONE, FAKE_FIO),
    }


# The transfer to a T-Bank client by phone, captured end to end after the SBP-only
# lookup was found to hide such a recipient. Its own file: it is a later session on a
# later app build, and folding it into captures.xml would blur which capture proves
# what.
CAPTURE_PAY = os.environ.get("TBANK_CAPTURE_PAY",
                             os.path.expanduser("~/tbank-app/captures-pay.xml"))


def build_internal_pay(fake_phone, fake_fio):
    """The signed /v1/pay for a T-Bank-INTERNAL recipient, from captures-pay.xml.

    This is the request the repo had no capture of: same provider and endpoint as the
    SBP transfer in transfer.json, but providerFields carry pointerLinkId and NO
    bankMemberId — the recipient is an account inside the bank, not a member to route
    to. Until it existed the internal body was an inference from the commission
    preview; now it is pinned.

    Kept: the query keys, the form keys, the payParameters KEY SET and the protocol
    constants, plus the two query values that differ from the older capture
    (appVersion, inache) — recorded, not asserted, because they are the app build the
    repo replays, not this flow's contract. Replaced: the payer account, the
    recipient, the ids and the amount."""
    import urllib.parse

    if not os.path.exists(CAPTURE_PAY):
        raise FileNotFoundError(CAPTURE_PAY)
    with open(CAPTURE_PAY, "rb") as fh:
        blob = fh.read().decode("utf-8", "replace")

    import test_cart_body_matches_capture as T

    for item in re.findall(r"<item>(.*?)</item>", blob, re.S):
        url = re.search(r"<url>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</url>", item, re.S)
        if not url or "/v1/pay?" not in url.group(1):
            continue
        raw = T._raw(item, "request")
        head, _, body = raw.partition(b"\r\n\r\n")
        query = urllib.parse.parse_qs(
            head.split(b"\r\n")[0].decode().split("?", 1)[1].split(" ")[0])
        form = urllib.parse.parse_qs(body.decode())
        pp = json.loads(form["payParameters"][0])
        pf = pp.get("providerFields") or {}
        if pp.get("provider") != "p2p-anybank" or "bankMemberId" in pf:
            continue          # an SBP transfer — transfer.json already pins that one

        pp["account"] = "0000000000"
        pp["userPaymentId"] = "1700000000000"
        pp["moneyAmount"] = 1000
        pf["pointer"] = fake_phone
        pf["maskedFIO"] = fake_fio
        pf["pointerLinkId"] = "10000000000"

        resp = json.loads(T._raw(item, "response").partition(b"\r\n\r\n")[2])
        payload = resp.get("payload", resp)
        payload["paymentId"] = "100000000001"
        info = payload.get("commissionInfo") or {}
        for key in ("amount", "amountWithCommission"):
            if isinstance(info.get(key), dict):
                info[key]["value"] = float(pp["moneyAmount"])

        secret = {"sessionid", "deviceId", "oldDeviceId"}
        return {
            "query_keys": sorted(query),
            "query_static": {k: v[0] for k, v in sorted(query.items())
                             if k not in secret},
            "form_keys": sorted(form),
            "pay_parameters": pp,
            "pay_response": payload,
        }
    raise SystemExit(f"no T-Bank-internal /v1/pay (p2p-anybank, no bankMemberId) "
                     f"in {CAPTURE_PAY}")


def write(name, data):
    out = os.path.join(HERE, name)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"wrote {out} ({os.path.getsize(out) // 1024} KB)")


def main():
    if len(sys.argv) > 1:
        T.CAPTURE = sys.argv[1]
    if not os.path.exists(T.CAPTURE):
        print(f"capture not found: {T.CAPTURE}")
        return 1
    write("grocery_cart.json", build(T._items()))
    try:
        write("booking.json", build_booking())
    except FileNotFoundError as e:
        print(f"booking fixture skipped: {e}")
    write("transfer.json", build_transfer())
    try:
        write("recipient.json", build_recipient())
    except FileNotFoundError as e:
        print(f"recipient fixture skipped: {e}")
    try:
        write("transfer_legal.json", build_transfer_legal())
    except FileNotFoundError as e:
        print(f"transfer_legal fixture skipped: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
