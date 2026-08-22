"""Pre-push audit: does anything from a real capture appear in a TRACKED file?

`tests/test_no_personal_data.py` is the always-on guard, and it works by SHAPE — a
value that looks like a passport or an e-mail must be declared. That catches the
classes it knows. It cannot catch a real value with no recognisable shape: a
station name, a hotel, a fare description, a bank's own internal id in a format
nobody wrote a regex for.

This closes the other half. It harvests candidate personal values OUT OF the
captures themselves and greps every file git tracks. The capture is the ground
truth for «real», so a hit here came from the owner's traffic by definition.

It lives beside regen.py rather than in the suite because it needs the gitignored
captures: on a clean clone it would verify nothing, and a test that skips is the
thing this repo has been burned by before. Run it before pushing.

    python3 tests/fixtures/audit_captures.py [capture.xml ...]

Exit code 1 means something real is in a tracked file. Read every hit — some are
coincidences (a six-character price that looks like a booking code, a brand colour
that looks like one) and the tool says which, but the decision is a person's.
"""
import base64
import glob
import gzip
import json
import os
import re
import subprocess
import sys
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CAPTURES = os.path.expanduser("~/tbank-app/captures*.xml")

# Keys whose value identifies a PERSON. `name` is deliberately absent: in this API
# it is a station, a city, a hotel, a fare and a bank far more often than a human,
# and including it buries the real hits under place names.
PERSONAL_KEYS = re.compile(
    r"^(first|last|middle)name(en)?$|^fio$|^surname$"
    r"|^email$|^phone$|^birth(date)?$|^inn$|^snils$"
    r"|^bookingnumber$|^booking_number$|^documentnumber$|^accountnumber$"
    r"|^externalblankid$|^externalreservationnumber$|^humanreadable$", re.I)

SHAPES = [
    # A hyphen on either side excludes the tail of a UUID: «…-000000000002» is a
    # digit group inside an identifier, not a taxpayer number, and matching it
    # reported this repo's own synthetic uuid convention as a leak.
    ("паспорт (10 цифр)", re.compile(r"(?<![\d-])\d{10}(?![\d-])")),
    ("ИНН (12 цифр)", re.compile(r"(?<![\d-])\d{12}(?![\d-])")),
    ("счёт (20 цифр)", re.compile(r"(?<![\d-])\d{20}(?![\d-])")),
    # Six uppercase alphanumerics with at least one of each. The bare {6} form
    # matched every six-digit price in the capture and buried everything else.
    ("код брони", re.compile(
        r"\b(?=[A-Z0-9]{6}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6}\b")),
    ("e-mail", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("телефон", re.compile(r"\+7\d{10}\b")),
]

# Protocol values, not identity: currency and airport codes, status enums, and the
# handful of tokens that are the same word in both worlds.
#
# The last two are settled false positives, kept here with their reasons so nobody
# re-investigates them every run: 15E148 is the iOS Safari BUILD number inside a
# User-Agent, and 21A038 is Sberbank's brand green (#21A038) from brand_by_bin.
# A tool that reports the same two non-issues every time stops being read.
STOP = {"RUB", "USD", "EUR", "SVO", "CEK", "LED", "MOW", "CLIENT", "RETAIL",
        "RUBLES", "STRING", "COMMON", "NORMAL", "ACTIVE", "SINGLE", "RETURN",
        "CANCEL", "FAILED", "STATUS", "MOSCOW", "15E148", "21A038",
        # The vendor's own name, and the ГОСТ Р 56042-2014 payment-QR header.
        "TINKOFF", "ST0001",
        # This repo's synthetic placeholder people, declared here for the same
        # reason ALLOWED exists in tests/test_no_personal_data.py: they are the
        # Russian equivalent of John Doe, so they collide with REAL people of the
        # same common name in the captures — a courier, a support agent — every
        # run. None was copied from traffic. The cost of the exemption is that a
        # real leak of exactly these surnames would be missed, which is the same
        # trade ALLOWED already makes.
        "ИВАНОВ", "ИВАНОВА", "ИВАНОВНА", "ИВАНОВИЧ", "ПЕТРОВ", "ПЕТРОВА",
        "МИХАИЛ", "МАКСИМ", "ВЛАДИМИР", "0123456789"}
MIN_LEN = 6
# A run of one repeated digit identifies nobody by construction, and it is also the
# repo's own synthetic convention (account 0000000000, order 400000000001) — so it
# matches on BOTH sides and would be reported forever.
_UNIFORM = re.compile(r"^(\d)\1*$")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_WORDS_ONLY = re.compile(r"^\w+$", re.UNICODE)
# This file quotes «Максим» and «Владимир» in the comments explaining why they
# are false positives, and would otherwise report itself, forever.
SKIP_PREFIXES = ("ca/roots/", "tests/fixtures/audit_captures.py")
SKIP_SUFFIXES = (".png", ".jpg", ".pdf", ".pem", ".crt", ".cer", ".ico")


def _mostly_zeros(s: str) -> bool:
    """A zero-padded counter, not an identifier of a person.

    «000000000002» is eleven zeros and a 2 — it is what both a bank pads a field
    with and what this repo uses for synthetic ids, so it matches on both sides
    forever. Real documents are not shaped like that: the owner's passport has one
    zero in ten digits and their ИНН none in twelve, while a settlement account —
    which IS worth reporting — is only 40% zeros."""
    return s.isdigit() and s.count("0") > len(s) * 0.6


def _keep(s: str) -> bool:
    """Is this value worth comparing at all?"""
    return (len(s) >= MIN_LEN and s.upper() not in STOP
            and not _UNIFORM.match(s) and not _mostly_zeros(s))


def _body(raw: bytes) -> str:
    head, _, body = raw.partition(b"\r\n\r\n")
    low = head.lower()
    if b"transfer-encoding: chunked" in low:
        out, i = bytearray(), 0
        while i < len(body):
            j = body.find(b"\r\n", i)
            if j < 0:
                break
            try:
                n = int(body[i:j].split(b";")[0], 16)
            except ValueError:
                break
            if n == 0:
                break
            out += body[j + 2:j + 2 + n]
            i = j + 2 + n + 2
        body = bytes(out)
    if b"content-encoding: gzip" in low:
        try:
            body = gzip.decompress(body)
        except Exception:                                    # noqa: BLE001
            try:
                body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body)
            except Exception:                                # noqa: BLE001
                pass
    elif b"content-encoding: br" in low:
        try:
            import brotli
            body = brotli.decompress(body)
        except Exception:                                    # noqa: BLE001
            pass
    return body.decode("utf-8", "replace")


def harvest(paths):
    values = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (str, int, float)) and PERSONAL_KEYS.match(str(k)):
                    s = str(v).strip()
                    if _keep(s):
                        values.add(s)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    item_rx = re.compile(r"<item>(.*?)</item>", re.S)
    part_rx = re.compile(r"<(request|response)( [^>]*)?>(.*?)</\1>", re.S)
    for path in paths:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
        for item in item_rx.findall(text):
            for _tag, attrs, payload in part_rx.findall(item):
                payload = payload.replace("<![CDATA[", "").replace("]]>", "")
                raw = (base64.b64decode(payload) if 'base64="true"' in (attrs or "")
                       else payload.encode())
                blob = _body(raw)
                stripped = blob.lstrip()
                if stripped.startswith(("{", "[")):
                    try:
                        walk(json.loads(stripped))
                    except ValueError:
                        pass
                for _label, rx in SHAPES:
                    for m in rx.findall(blob):
                        if _keep(str(m)):
                            values.add(str(m))
    return values


def tracked_files():
    out = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True,
                         text=True, check=True).stdout.split()
    return [f for f in out
            if not f.startswith(SKIP_PREFIXES) and not f.endswith(SKIP_SUFFIXES)]


def main(argv):
    paths = argv[1:] or sorted(glob.glob(DEFAULT_CAPTURES))
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        print(f"captures not found ({DEFAULT_CAPTURES}) — nothing to compare against")
        return 0
    print(f"captures: {', '.join(os.path.basename(p) for p in paths)}")
    values = harvest(paths)
    files = tracked_files()
    print(f"harvested {len(values)} candidate values; scanning {len(files)} tracked files")

    # Values made only of word characters compare as tokens; the rest
    # (e-mails, +7 phones) carry their own boundaries and stay substrings.
    word_values = {v for v in values if _WORDS_ONLY.match(v)}
    other_values = values - word_values

    hits = {}
    for f in files:
        try:
            with open(os.path.join(REPO, f), encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        # WHOLE tokens, not substrings. «Максим» is a real courier in one capture
        # and also the middle of the fare name «Эконом Максимум»; «Владимир» is a
        # person and a city in the region table. Plain `in` reported both and
        # buried the output in coincidences.
        #
        # Done by intersecting token SETS rather than one search per value: a
        # regex per value per file is ~370 000 searches and took this past ten
        # minutes, which is long enough that nobody runs it before pushing.
        tokens = set(_TOKEN.findall(text))
        for v in word_values & tokens:
            hits.setdefault(v, []).append(f)
        for v in other_values:
            if v in text:
                hits.setdefault(v, []).append(f)
    if not hits:
        print("\nCLEAN — no captured value appears in any tracked file")
        return 0
    print(f"\n{len(hits)} captured value(s) present in tracked files — read EACH:")
    for v, fs in sorted(hits.items()):
        print(f"  {v!r:34s} -> {', '.join(fs[:4])}{' …' if len(fs) > 4 else ''}")
    print("\nA hit is not automatically a leak: a six-character price can look like "
          "a booking code and a brand colour like a document number. But every one "
          "has to be looked at by a person before pushing.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
