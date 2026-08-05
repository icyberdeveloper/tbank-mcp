"""Regenerate tests/fixtures/myt.json from the MyT Burp capture.

Separate from regen.py on purpose: that one rebuilds fixtures from the BANKING
capture and refuses to run without it, while this reads a different file
(`~/tbank-app/captures-myt.xml`, the corporate app) and scrubs a different kind of
data. Banking PII is accounts and documents; here it is colleagues — names, emails,
job titles, meeting subjects, office names, a car number. None of that is protocol,
and all of it came out of somebody's real workday.

What survives is exactly what the tests assert on: paths, query keys and values,
request-body keys, header names, ids that the API itself echoes back, and the SHAPE
of the Outlook description (its `<style>` boilerplate is Word's, not a person's).

    python3 tests/fixtures/regen_myt.py [path/to/captures-myt.xml]
"""
import base64
import gzip
import json
import os
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE = os.path.expanduser("~/tbank-app/captures-myt.xml")

# Capture indices. Written out rather than searched for: a search that silently
# matches a different request is how a fixture starts describing something else.
ITEMS = {
    "schedule": 44,            # GET /api/Appointment/short (one day)
    "detail": 1062,            # GET /api/Appointment/{id} — non-recurring, 2 participants
    "detail_recurring": 1077,  # GET /api/Appointment/{id} — series master, start in 2020
    "answer": 1097,            # PUT /api/Appointment/answer → 200
    "answer_throttled": 1101,  # PUT /api/Appointment/answer → 400, one per 5 seconds
    "cancel": 1078,            # PUT /api/Appointment/{id}/cancel?DateTime=
    "front_settings": 82,      # GET /workplacer/api/booking-front-settings
    "parking_buildings": 84,   # GET /workplacer/api/booking/parking/buildings
    "parking_last": 80,        # GET /workplacer/api/booking/parking/last
    "parking_recommended": 88, # GET .../parking/recommended/{date}?buildingId&floorId&resultCount
    "parking_book": 100,       # POST /workplacer/api/booking/parking/{placeId}
    "bookings": 101,           # GET /workplacer/api/booking/{date}/all-user-bookings
    "auth_refresh": 28,        # POST /v3/auth/token (grantType=refresh_token)
}

# Synthetic replacements. Ids keep their numeric range because the app parses them;
# UUIDs keep UUID form for the same reason.
FAKE_UUID = "00000000-0000-4000-8000-0000000000%02d"
FAKE_NAME = "Сотрудник %d"
FAKE_EMAIL = "user%d@example.com"
FAKE_CAR_CYR = "А000АА000"
FAKE_CAR_LAT = "A000AA000"
FAKE_CAR_MODEL = "Model"

_uuids: dict[str, str] = {}
_people: dict[str, int] = {}


def fake_uuid(real: str) -> str:
    if real not in _uuids:
        _uuids[real] = FAKE_UUID % (len(_uuids) + 1)
    return _uuids[real]


def person_no(key: str) -> int:
    if key not in _people:
        _people[key] = len(_people) + 1
    return _people[key]


UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def items(path):
    data = open(path, "rb").read().decode("utf-8", "replace")
    return re.findall(r"<item>(.*?)</item>", data, re.S)


def field(chunk, name):
    m = re.search(r"<%s(?:\s[^>]*)?>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (name, name),
                  chunk, re.S)
    return m.group(1) if m else ""


def split_http(blob):
    i = blob.find(b"\r\n\r\n")
    return (blob.decode("utf-8", "replace"), b"") if i < 0 else (
        blob[:i].decode("utf-8", "replace"), blob[i + 4:])


def decode_body(head, body):
    h = head.lower()
    if "transfer-encoding: chunked" in h:
        out, rest = b"", body
        while True:
            i = rest.find(b"\r\n")
            if i < 0:
                break
            try:
                n = int(rest[:i].split(b";")[0], 16)
            except ValueError:
                break
            if n == 0:
                break
            out += rest[i + 2:i + 2 + n]
            rest = rest[i + 2 + n + 2:]
        body = out or body
    for enc, fn in (("gzip", gzip.decompress),
                    ("deflate", lambda b: zlib.decompress(b, -15))):
        if f"content-encoding: {enc}" in h:
            try:
                body = fn(body)
            except Exception:
                pass
    if "content-encoding: br" in h:
        for mod in ("brotli", "brotlicffi"):
            try:
                body = __import__(mod).decompress(body)
                break
            except Exception:
                continue
    return body.decode("utf-8", "replace")


def parsed(chunk):
    req_head, req_body = split_http(base64.b64decode(field(chunk, "request")))
    res_head, res_body = split_http(base64.b64decode(field(chunk, "response")))
    lines = req_head.split("\r\n")
    start = lines[0].split(" ")
    headers = [ln.split(":", 1)[0] for ln in lines[1:] if ":" in ln]
    url = start[1] if len(start) > 1 else ""
    path, _, query = url.partition("?")
    from urllib.parse import parse_qsl, unquote
    return {
        "method": start[0],
        "path": unquote(path),
        "query": dict(parse_qsl(query, keep_blank_values=True)),
        "req_headers": headers,
        "req_body": decode_body(req_head, req_body),
        "status": int(re.search(r"\s(\d{3})\s", res_head.split("\r\n")[0]).group(1)),
        "res_body": decode_body(res_head, res_body),
    }


def scrub(o, depth=0):
    """Replace people, subjects and office names; keep every protocol value."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            kl = k.lower()
            if kl in ("fullname",):
                out[k] = FAKE_NAME % person_no(str(v))
            elif kl == "email":
                out[k] = FAKE_EMAIL % person_no(str(v).split("@")[0])
            elif kl == "legalposition":
                out[k] = "Должность"
            elif kl == "resourceunit":
                out[k] = "Подразделение"
            elif kl == "title" and isinstance(v, str):
                out[k] = "Встреча"
            elif kl in ("buildingname", "name") and isinstance(v, str) and "оск" in v:
                # "Москва, <офис>" — внутренние названия зданий.
                out[k] = f"Офис {o.get('id', '')}".strip()
            elif kl == "carnumber":
                out[k] = FAKE_CAR_LAT if str(v).isascii() else FAKE_CAR_CYR
            elif kl == "carmodel":
                out[k] = FAKE_CAR_MODEL
            elif kl == "description" and isinstance(v, str):
                out[k] = fake_description(v)
            elif kl == "onlinemeetingurl" and v:
                # Настоящая ссылка на созвон — это входная дверь в переговорную
                # комнату, а не идентификатор. Ходит по ней кто угодно.
                out[k] = "https://example.com/meeting/00000000"
            elif kl == "offlinemeetingplace" and v:
                out[k] = "Переговорная"
            elif kl in ("id", "internalappointmentid", "parentappointmentid") and \
                    isinstance(v, str) and UUID_RE.fullmatch(v):
                out[k] = fake_uuid(v)
            elif kl in ("participantid", "masterid") or (kl == "id" and isinstance(v, int)
                                                         and depth > 0 and v > 10000):
                out[k] = 10000 + person_no(f"{kl}:{v}")
            else:
                out[k] = scrub(v, depth + 1)
        return out
    if isinstance(o, list):
        return [scrub(x, depth + 1) for x in o]
    if isinstance(o, str) and UUID_RE.fullmatch(o):
        return fake_uuid(o)
    return o


def fake_description(html: str) -> str:
    """Keep Word's boilerplate, replace everything a human wrote.

    The stripper test needs REAL Outlook HTML — a hand-written `<p>hello</p>` would
    not prove it survives `<head>`, a `<style>` block and mso conditional comments.
    So the markup stays and only the visible text and links are replaced."""
    out = re.sub(r"https?://[^\s\"'<>]+", "https://example.com/meeting", html)
    out = re.sub(r">([^<>]*[А-Яа-яЁё][^<>]*)<",
                 lambda m: ">Текст повестки<" if m.group(1).strip() else m.group(0), out)
    return out


def build():
    chunks = items(CAPTURE)
    fx = {
        "_note": "Structure and protocol values from captures-myt.xml; people, "
                 "meeting subjects, office names and the car are synthetic. "
                 "Regenerate: python3 tests/fixtures/regen_myt.py",
        "_capture_indices": ITEMS,
    }
    for name, idx in ITEMS.items():
        p = parsed(chunks[idx])
        entry = {
            "method": p["method"],
            "path": p["path"],
            "query": p["query"],
            "req_headers": p["req_headers"],
            "status": p["status"],
        }
        if name == "auth_refresh":
            # НИЧЕГО из тела: там живой refresh_token, а рядом в захвате — пароль и
            # SMS-код. Фиксируем только состав полей, он и есть контракт.
            entry["req_body_keys"] = sorted(json.loads(p["req_body"]))
            entry["res_body_keys"] = sorted(json.loads(p["res_body"]))
        else:
            if p["req_body"].strip():
                entry["req_body"] = scrub(json.loads(p["req_body"]))
            if p["res_body"].strip():
                try:
                    entry["res_body"] = scrub(json.loads(p["res_body"]))
                except json.JSONDecodeError:
                    entry["res_text"] = p["res_body"].strip()
            elif p["status"] >= 400:
                entry["res_text"] = p["res_body"].strip()
        if name == "detail_recurring" and isinstance(entry.get("res_body"), dict):
            # Эта запись нужна ради isRecurrent/recurrencePattern/start мастера серии,
            # а не ради письма. Её description — 36 КБ вордовской разметки, две трети
            # всей фикстуры, и его не читает ни один тест (стриппер проверяется на
            # `detail`). Непрочитанный человеком текст в публичном репозитории —
            # это ровно тот способ, которым сюда уже попадали чужие данные.
            entry["res_body"]["description"] = ""
        # id встречи в пути — тот же, что в теле ответа: подменяем согласованно.
        entry["path"] = UUID_RE.sub(lambda m: fake_uuid(m.group(0)), entry["path"])
        entry["query"] = {k: v for k, v in entry["query"].items()}
        fx[name] = entry
    # Ответ на throttle приходит text/plain — нормализуем в одно поле.
    thr = fx["answer_throttled"]
    thr.setdefault("res_text", "")
    return fx


def main():
    global CAPTURE
    if len(sys.argv) > 1:
        CAPTURE = sys.argv[1]
    if not os.path.exists(CAPTURE):
        print(f"capture not found: {CAPTURE}")
        return 1
    out = os.path.join(HERE, "myt.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(build(), fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"wrote {out} ({os.path.getsize(out) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
