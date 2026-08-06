"""MyT — рабочий календарь и парковка: форма запроса и честность ответа.

Два разных класса дефектов, оба уже случались в этом репозитории на других
вертикалях, и оба здесь особенно дороги, потому что сервис корпоративный:

  * ПРИДУМАННАЯ ФОРМА ЗАПРОСА. `floorId=nil` — это литеральная строка "nil",
    которую iOS-клиент подставляет вместо невыбранного этажа; «правильные» пустая
    строка или отсутствие параметра — уже не то, что видел сервер. Такие детали
    ловятся только сверкой с захватом, поэтому каждый запрос здесь сравнивается с
    фикстурой, снятой с реального трафика.

  * 200, КОТОРЫЙ НИЧЕГО НЕ ЗНАЧИТ. И бронь парковки, и отмена встречи отвечают
    200 с пустым телом. В захвате отмена повторяющейся встречи вернула 200, а
    вхождение осталось в расписании. Тул, который на этом основании скажет
    «отменено», врёт участникам встречи, а не просто ошибается.

Фикстура — tests/fixtures/myt.json (структура настоящая, люди и названия
синтетические). Тест работает и без захвата; когда захват есть, дополнительно
проверяется, что фикстура от него не уехала.

    python3 tests/test_myt_calendar_parking.py
"""
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import myt, server  # noqa: E402
from src.client import TbankApiError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "myt.json"), encoding="utf-8"))
CAPTURE = os.path.expanduser("~/tbank-app/captures-myt.xml")

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ── стенд ───────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text or payload is None else json.dumps(payload)
        self.content = b"" if payload is None and not text else self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class RecordingHTTP:
    """Записывает запрос и отвечает по маршруту. Сеть не трогает.

    Подменяет requests.Session ЦЕЛИКОМ, а не метод сессии, чтобы `_call` собирал
    заголовки и параметры по-настоящему — иначе тест проверял бы сам себя."""

    def __init__(self, routes):
        self.headers = {}
        self.routes = routes          # (method, path-suffix) → FakeResp | callable | list
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "params": dict(params or []), "body": json,
                           "headers": dict(headers or {})})
        for (m, suffix), resp in self.routes.items():
            if m == method and suffix in url:
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp(self.calls[-1]) if callable(resp) else resp
        raise AssertionError(f"нет маршрута для {method} {url}")

    def post(self, url, json=None, headers=None, timeout=None):
        return self.request("POST", url, json=json, headers=headers, timeout=timeout)


def session(routes, tz=myt.MSK):
    s = myt.MytSession(access_token="access-token", refresh_token="refresh-token",
                       username="employee", user_id="00000000-0000-4000-8000-000000000099")
    s.default_headers = dict(s._http.headers)   # то, что requests добавил бы сам
    s._http = RecordingHTTP(routes)
    s._minted_at = 2 ** 31                      # свежий: ensure_fresh не пойдёт обновлять
    # Пояс пиним: его определение — отдельный тест, а тут он не должен требовать
    # маршрутов workplacer от каждого сценария.
    s._tz = (tz, "тест")
    return s


def fx(name):
    return FIXTURE[name]


def resp_of(name):
    e = fx(name)
    return FakeResp(e.get("status", 200), e.get("res_body"), e.get("res_text", ""))


def today(offset=0):
    """Московское сегодня — то же, что считает код.

    Через date.today() этот хелпер брал дату ХОСТА (VM живёт в UTC) и с 21:00 UTC
    расходился с тулами на сутки: тест на окно бронирования падал каждый вечер,
    и падал справедливо — as_date() тогда действительно отдавал вчерашний день.
    Стенд везде пинит Москву, поэтому и хелпер считает по ней."""
    return (myt.today_in(myt.MSK) + timedelta(days=offset)).isoformat()


# ── форма запроса против захвата ────────────────────────────────────────────

def check_schedule_request_matches_capture():
    e = fx("schedule")
    s = session({("GET", "/api/Appointment/short"): resp_of("schedule")})
    rows = s.day_appointments("2026-08-05")

    call = s._http.calls[0]
    check(call["method"] == "GET", f"schedule: метод {call['method']}")
    check(call["url"].endswith(e["path"]), f"schedule: путь {call['url']}")
    check(call["params"] == e["query"],
          f"schedule: параметры {call['params']} != захват {e['query']}")
    check(call["params"]["End"] == "2026-08-06",
          "schedule: End — следующий день, диапазон полуоткрытый")

    # Заголовки: часть ставит _call, часть — сессия. Сервер видит объединение.
    sent = {k.lower() for k in call["headers"]} | {k.lower() for k in s.default_headers}
    for h in ("authorization", "x-userid", "x-auth-provider", "x-requested-with",
              "accept", "accept-language", "user-agent"):
        check(h in sent, f"schedule: заголовок {h} не уходит, а в захвате он есть")
    check(call["headers"]["X-Auth-Provider"] == "twork",
          "schedule: X-Auth-Provider должен быть twork")
    check(call["body"] is None, "schedule: у GET не должно быть тела")
    check(len(rows) == len(e["res_body"]["appointments"]),
          "schedule: разобраны не все встречи")
    print(f"  расписание: GET {e['path']} c End/Start и 7 заголовками захвата")


def check_schedule_walks_days_one_by_one():
    s = session({("GET", "/api/Appointment/short"): resp_of("schedule")})
    rows = s.schedule("2026-08-10", "2026-08-12")
    days = [c["params"]["Start"] for c in s._http.calls]
    check(days == ["2026-08-10", "2026-08-11", "2026-08-12"],
          f"диапазон должен идти по дням, как приложение: {days}")
    check({r["day"] for r in rows} == set(days),
          "каждая встреча должна нести день, за который её вернули")
    print("  диапазон: по запросу на день, у каждой строки проставлен день")


def check_answer_body_matches_capture():
    e = fx("answer")
    aid = e["req_body"]["internalAppointmentId"]
    s = session({("PUT", "/api/Appointment/answer"): FakeResp(200),
                 ("GET", "/api/Appointment/"): FakeResp(200, {"currentUserMeetingResponseType": "Tentative"})})
    s.answer(aid, "может быть")
    body = s._http.calls[0]["body"]
    check(body == e["req_body"], f"answer: тело {body} != захват {e['req_body']}")
    check(set(body) == {"withAnswer", "internalAppointmentId", "answer", "responseType"},
          f"answer: состав полей разошёлся с захватом: {sorted(body)}")
    print("  ответ на встречу: тело поле-в-поле как в захвате")


def check_cancel_query_matches_capture():
    e = fx("cancel")
    aid = e["path"].split("/")[3]
    s = session({("PUT", "/cancel"): FakeResp(200)})
    s.cancel(aid, e["query"]["DateTime"])
    call = s._http.calls[0]
    check(call["url"].endswith(f"/api/Appointment/{aid}/cancel"),
          f"cancel: путь {call['url']}")
    check(call["params"] == e["query"],
          f"cancel: DateTime уходит как {call['params']}, в захвате {e['query']}")
    print("  отмена: PUT {id}/cancel?DateTime= как в захвате")


def check_parking_recommended_sends_literal_nil():
    e = fx("parking_recommended")
    s = session({("GET", "/parking/recommended/"): resp_of("parking_recommended")})
    s.parking_recommended("2026-08-06", 66)
    p = s._http.calls[0]["params"]
    check(p == e["query"], f"parking recommended: {p} != захват {e['query']}")
    check(p["floorId"] == "nil",
          "невыбранный этаж уходит литеральной строкой 'nil' — так шлёт iOS-клиент")
    s2 = session({("GET", "/parking/recommended/"): resp_of("parking_recommended")})
    s2.parking_recommended("2026-08-06", 66, floor_id=252)
    check(s2._http.calls[0]["params"]["floorId"] == "252",
          "заданный этаж должен уходить числом, а не 'nil'")
    print("  парковка: floorId=nil литералом, заданный этаж — числом")


def check_parking_book_body_matches_capture():
    e = fx("parking_book")
    place = e["path"].rsplit("/", 1)[-1]
    s = session({("POST", "/booking/parking/"): FakeResp(200)})
    s.parking_book(place, e["req_body"]["date"], e["req_body"]["carNumber"],
                   e["req_body"]["carModel"], e["req_body"]["buildingId"])
    call = s._http.calls[0]
    check(call["url"].endswith(e["path"]), f"бронь: путь {call['url']} != {e['path']}")
    check(call["body"] == e["req_body"], f"бронь: тело {call['body']} != {e['req_body']}")
    check(call["headers"].get("Content-Type") == "application/json",
          "бронь: тело JSON — нужен Content-Type, в захвате он есть")
    print("  бронь парковки: POST .../parking/{place_id} с телом из захвата")


# ── поведение ───────────────────────────────────────────────────────────────

def check_answer_aliases_are_exact():
    check(myt.answer_type("пойду") == "Accept", "«пойду» → Accept")
    check(myt.answer_type("Не пойду") == "Decline", "«не пойду» → Decline")
    check(myt.answer_type("может быть") == "Tentative", "«может быть» → Tentative")
    check(myt.answer_type("  ДА!  ") == "Accept", "регистр и пунктуация не должны мешать")
    check(myt.answer_type("Tentative") == "Tentative", "само значение API тоже принимается")
    for bad in ("наверное как получится", "ok", ""):
        try:
            myt.answer_type(bad)
            failures.append(f"{bad!r} не должен разбираться в ответ — это угадывание")
        except TbankApiError:
            pass
    print("  ответы: словарь точный, непонятная формулировка — ошибка, а не Accept")


def check_answer_retries_once_on_throttle():
    e = fx("answer_throttled")
    slept = []
    real_time = myt.time
    myt.time = type("T", (), {"sleep": staticmethod(lambda s: slept.append(s)),
                              "time": staticmethod(real_time.time)})
    try:
        s = session({("PUT", "/api/Appointment/answer"): [
            FakeResp(400, text=e["res_text"]), FakeResp(200)]})
        applied = s.answer("00000000-0000-4000-8000-000000000013", "пойду")
    finally:
        myt.time = real_time
    check(applied == "Accept", "после успешного повтора должен вернуться применённый ответ")
    check(len(s._http.calls) == 2, f"троттлинг: должен быть ровно один повтор, было {len(s._http.calls)}")
    check(slept and slept[0] >= 5.0, f"повтор должен ждать не меньше 5 секунд, ждал {slept}")
    print("  троттлинг 5 секунд: ждём и повторяем один раз")


def check_answer_does_not_swallow_other_400():
    s = session({("PUT", "/api/Appointment/answer"): FakeResp(400, text="Встреча не найдена")})
    try:
        s.answer("00000000-0000-4000-8000-000000000013", "пойду")
        failures.append("400 не про троттлинг обязан долетать до вызывающего")
    except TbankApiError as ex:
        check("не найдена" in ex.message, f"текст ошибки сервера должен сохраниться: {ex.message}")
    check(len(s._http.calls) == 1, "повторять не-троттлинг нельзя")
    print("  прочие 400 отдаются как есть, без повтора")


def check_401_says_which_session_died():
    s = session({("GET", "/api/Appointment/short"): FakeResp(401, text="")})
    try:
        s.day_appointments("2026-08-05")
        failures.append("401 обязан подниматься как MytSessionExpired")
        return
    except myt.MytSessionExpired as ex:
        out = server._err(ex)
    check("MYT SESSION EXPIRED" in out, f"ответ должен называть корпоративную сессию: {out}")
    check("refresh_session()" not in out,
          "нельзя отправлять в refresh_session(): он чинит банковскую сессию, не эту")
    check("login_cli.py --myt" in out, f"должен быть назван способ починки: {out}")
    print("  401: MYT SESSION EXPIRED и путь починки, без совета про банковский refresh")


def check_html_agenda_becomes_text():
    html = fx("detail")["res_body"]["description"]
    text = myt.text_from_html(html)
    check("mso-" not in text and "font-family" not in text,
          "Word-овский CSS не должен доезжать до агента")
    check("<" not in text, f"тегов остаться не должно: {text[:120]}")
    check("example.com" in text, "ссылка на созвон из повестки обязана уцелеть")
    check(len(text) < len(html) / 3, f"повестка почти не сжалась: {len(html)} → {len(text)}")
    print(f"  повестка: {len(html)} символов Outlook-HTML → {len(text)} символов текста")


def check_today_means_moscow_not_the_host():
    """«Сегодня» обязано быть московским, даже когда машина живёт в UTC.

    Замораживаем 21:30 UTC — в Москве это уже следующие сутки. Если код вернётся
    к date.today(), он отдаст дату хоста, которая с замороженным временем не
    совпадёт ни с чем, и тест упадёт. Ровно так это и всплыло: тест на окно
    бронирования падал по вечерам, потому что parking_book считал окно от Москвы,
    а as_date — от UTC."""
    import datetime as _dt
    real = myt.datetime

    class Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            base = _dt.datetime(2026, 3, 11, 21, 30, tzinfo=_dt.timezone.utc)
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    myt.datetime = Frozen
    try:
        msk = myt.MSK
        check(myt.as_date("", tz=msk) == "2026-03-12",
              f"пустая дата в 21:30 UTC — это 12 марта по Москве, получили {myt.as_date('', tz=msk)}")
        check(myt.as_date("сегодня", tz=msk) == "2026-03-12", "«сегодня» — по поясу пользователя")
        check(myt.as_date("завтра", tz=msk) == "2026-03-13", "«завтра» — по поясу пользователя")
        check(myt.as_date("", default_days=1, tz=msk) == "2026-03-13", "сдвиг тоже по нему")
        # А у сотрудника из Владивостока то же мгновение — уже другие сутки.
        vvo = myt.parse_tz("+10:00")
        check(myt.as_date("сегодня", tz=vvo) == "2026-03-12",
              "в +10 в 21:30 UTC уже 12 марта, 07:30 утра")
        utc = myt.parse_tz("+00:00")
        check(myt.as_date("сегодня", tz=utc) == "2026-03-11",
              "а в UTC это ещё 11 марта — пояс обязан менять ответ")
    finally:
        myt.datetime = real
    print("  сегодня/завтра: сутки пользователя, а не хоста и не всегда Москвы")


def check_dates_accept_russian_words():
    check(myt.as_date("") == today(), "пустая дата — сегодня")
    check(myt.as_date("", default_days=1) == today(1), "дефолт со сдвигом — завтра")
    check(myt.as_date("завтра") == today(1), "«завтра» должно разбираться")
    check(myt.as_date("2026-08-06") == "2026-08-06", "ISO проходит как есть")
    try:
        myt.as_date("когда-нибудь")
        failures.append("нераспознанная дата должна падать, а не молча становиться сегодня")
    except TbankApiError:
        pass
    print("  даты: сегодня/завтра/ISO, мусор — ошибка")


# ── честность ответа тула ───────────────────────────────────────────────────

def with_session(s):
    server._myt_session = s
    return s


def check_cancel_reports_that_nothing_changed():
    """Главный тест файла: 200 на отмене ничего не доказывает."""
    aid = "00000000-0000-4000-8000-000000000008"
    when = "2026-08-05T12:00:00+00:00"
    still_there = {"appointments": [
        {"id": aid, "start": when, "end": "2026-08-05T15:00:00+00:00",
         "title": "Встреча", "currentUserMeetingResponseType": "Organizer", "isEnded": False}]}
    with_session(session({
        ("GET", f"/api/Appointment/{aid}"): FakeResp(200, {
            "id": aid, "title": "Встреча", "start": when, "isRecurrent": False}),
        ("PUT", "/cancel"): FakeResp(200),
        ("GET", "/api/Appointment/short"): FakeResp(200, still_there),
    }))
    out = server.calendar_cancel(aid, when)
    check("НЕ применилась" in out or "всё ещё" in out,
          f"сервер ответил 200, встреча на месте — тул обязан это сказать: {out}")
    check("Отменено" not in out, f"нельзя рапортовать об отмене: {out}")

    with_session(session({
        ("GET", f"/api/Appointment/{aid}"): FakeResp(200, {
            "id": aid, "title": "Встреча", "start": when, "isRecurrent": False}),
        ("PUT", "/cancel"): FakeResp(200),
        ("GET", "/api/Appointment/short"): FakeResp(200, {"appointments": []}),
    }))
    out = server.calendar_cancel(aid, when)
    check(out.startswith("Отменено"), f"когда встреча пропала — можно сказать «отменено»: {out}")
    print("  отмена: рапорт по перечитанному расписанию, а не по коду 200")


def check_cancel_refuses_a_series_without_the_occurrence():
    aid = "00000000-0000-4000-8000-000000000008"
    s = with_session(session({
        ("GET", f"/api/Appointment/{aid}"): FakeResp(200, {
            "id": aid, "title": "Встреча", "start": "2020-12-07T12:00:00+00:00",
            "isRecurrent": True, "recurrencePattern": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"}),
        ("PUT", "/cancel"): FakeResp(200),
    }))
    out = server.calendar_cancel(aid)
    check("occurrence_start" in out, f"нужно попросить конкретное вхождение: {out}")
    check("2020" in out, f"надо показать, что start — это начало серии: {out}")
    check(not any(c["method"] == "PUT" for c in s._http.calls),
          "отмена серии по мастеру не должна уходить на сервер вообще")
    print("  отмена серии без occurrence_start: отказ, запрос не уходит")


def check_book_reports_the_row_that_was_saved():
    day = today(1)
    saved = {"parkingBookings": [{"date": day, "position": "194", "floorName": "-2",
                                  "buildingName": "Офис 66", "carNumber": "A000AA000",
                                  "carModel": "Model", "parkingPlaceId": 56239}]}
    routes = {
        ("GET", "/parking/last"): resp_of("parking_last"),
        ("GET", "/booking-front-settings"): resp_of("front_settings"),
        ("POST", "/booking/parking/"): FakeResp(200),
        ("GET", "/all-user-bookings"): FakeResp(200, saved),
    }
    with_session(session(routes))
    out = server.parking_book(day, 56239)
    check("194" in out and day in out, f"должен назвать сохранённое место и дату: {out}")
    check("A000AA000" in out,
          f"печатать надо номер ИЗ ОТВЕТА (транслит), а не тот, что отправили: {out}")

    routes[("GET", "/all-user-bookings")] = FakeResp(200, {"parkingBookings": []})
    with_session(session(routes))
    out = server.parking_book(day, 56239)
    check("НЕ забронировано" in out, f"пустой список броней = места нет: {out}")

    # Полностью заданный вызов не должен спрашивать прошлую бронь: её ответ некуда
    # девать. И раз марка задана, подсказки про пустую марку в отказе быть не должно.
    s = with_session(session(routes))
    out = server.parking_book(day, 56239, car_number="X000XX99", car_model="Model",
                              building_id=66)
    check(not any("/parking/last" in c["url"] for c in s._http.calls),
          "при заданных номере, марке и здании запрос прошлой брони лишний")
    check("Марка машины пустая" not in out, f"марка задана — подсказки быть не должно: {out}")

    # А когда марку взять неоткуда, отказ обязан назвать её как подозреваемую:
    # комбинация с пустой carModel в захвате не встречается ни разу.
    routes[("GET", "/parking/last")] = FakeResp(200, {"carNumber": "X000XX99",
                                                      "carModel": "", "buildingId": 66})
    with_session(session(routes))
    out = server.parking_book(day, 56239)
    check("Марка машины пустая" in out, f"пустая марка должна быть названа причиной: {out}")
    routes[("GET", "/parking/last")] = resp_of("parking_last")
    print("  бронь: рапорт по перечитанной броне, пустой список — честный отказ")
    print("  бронь: лишнего запроса прошлой брони нет, пустая марка названа причиной")


def check_book_refuses_a_date_outside_the_window():
    horizon = fx("front_settings")["res_body"]["availableParkingPeriodDays"]
    far = today(horizon + 5)
    s = with_session(session({
        ("GET", "/parking/last"): resp_of("parking_last"),
        ("GET", "/booking-front-settings"): resp_of("front_settings"),
        ("POST", "/booking/parking/"): FakeResp(200),
    }))
    out = server.parking_book(far, 56239)
    check(today(horizon) in out, f"надо назвать последний доступный день: {out}")
    check(not any(c["method"] == "POST" for c in s._http.calls),
          "запрос за пределами окна не должен уходить на сервер")
    print(f"  бронь: дальше {horizon} дней — отказ с последней доступной датой")


def check_schedule_caps_the_range_and_says_so():
    s = with_session(session({("GET", "/api/Appointment/short"): resp_of("schedule")}))
    out = server.calendar_schedule("2026-08-01", "2026-09-01")
    check("14" in out, f"нужно назвать предел: {out}")
    check("2026-08-14" in out, f"нужно предложить конкретный годный диапазон: {out}")
    check(not s._http.calls, "за пределом диапазона не должно уходить ни одного запроса")
    print("  расписание: диапазон больше 14 дней — отказ до первого запроса")


def check_schedule_prints_whole_ids():
    aid = fx("schedule")["res_body"]["appointments"][0]["id"]
    with_session(session({("GET", "/api/Appointment/short"): resp_of("schedule")}))
    out = server.calendar_schedule("2026-08-05")
    check(f"id={aid}" in out, f"id встречи печатается целиком, обрезанный ничего не найдёт")
    check("всего" in out.splitlines()[0], f"шапка должна называть общее число: {out.splitlines()[0]}")
    print("  расписание: полные id и честная шапка со счётчиком")


def check_places_answers_the_whole_question():
    routes = {
        ("GET", "/booking-front-settings"): resp_of("front_settings"),
        ("GET", "/parking/buildings"): resp_of("parking_buildings"),
        ("GET", "/parking/last"): resp_of("parking_last"),
        ("GET", "/parking/recommended/"): resp_of("parking_recommended"),
    }
    s = with_session(session(routes))
    out = server.parking_places("2026-08-06")
    cfg = fx("front_settings")["res_body"]
    check(str(cfg["availableParkingPeriodDays"]) in out, f"окно бронирования не названо: {out}")
    check(cfg["openParkingAccessTime"] in out, f"час открытия не назван: {out}")
    check(fx("parking_last")["res_body"]["carNumber"] in out, "машина по умолчанию не названа")
    check("place_id=56239" in out, f"place_id должен быть в каждой строке: {out}")
    check(len(s._http.calls) == 4, f"тул обещает 4 запроса, сделал {len(s._http.calls)}")

    # Захват отдаёт ровно resultCount мест — то есть упёршийся потолок, а не итог.
    # «10 всего» здесь было бы закрытым ответом: агент, спрошенный про другой этаж,
    # прочитал бы его как «мест больше нет» и ошибся молча.
    head = out.splitlines()[4]
    check("всего" not in head,
          f"на упёршемся потолке нельзя объявлять итог: {head}")
    check(f"resultCount={server._PARK_COUNT}" in head,
          f"надо назвать предел запроса: {head}")
    check(s._http.calls[3]["params"]["resultCount"] == str(server._PARK_COUNT),
          "потолок из вывода должен быть тем же, что ушёл в запрос")

    # А когда мест меньше потолка — это уже настоящий итог, и его можно называть.
    few = {"recommendedParkingPlaces":
           (fx("parking_recommended")["res_body"]["recommendedParkingPlaces"])[:3],
           "noRecommendedParkingPlacesReason": 0}
    routes[("GET", "/parking/recommended/")] = FakeResp(200, few)
    with_session(session(routes))
    out = server.parking_places("2026-08-06")
    check("3 всего, показано 3" in out,
          f"ниже потолка итог настоящий и должен быть назван: {out}")
    routes[("GET", "/parking/recommended/")] = resp_of("parking_recommended")

    # Пустой список: причина есть, и она не выдумана.
    routes[("GET", "/parking/recommended/")] = FakeResp(
        200, {"recommendedParkingPlaces": [], "noRecommendedParkingPlacesReason": 2})
    with_session(session(routes))
    out = server.parking_places("2026-08-06")
    check("noRecommendedParkingPlacesReason=2" in out,
          f"код причины печатается как есть, без домыслов: {out}")
    check("office_bookings" in out, f"надо подсказать проверку уже сделанной брони: {out}")
    print("  места: окно, час, машина, place_id; пустой список — с кодом причины")


def check_places_stops_when_there_is_no_access():
    s = with_session(session({
        ("GET", "/booking-front-settings"): FakeResp(200, {"hasParkingTag": False}),
    }))
    out = server.parking_places("2026-08-06")
    check("нет доступа" in out.lower(), f"должен сказать про отсутствие доступа: {out}")
    check(len(s._http.calls) == 1,
          "без доступа к парковке остальные три запроса делать незачем")
    print("  без доступа к парковке: один запрос и внятный ответ")


def check_respond_flags_a_disagreeing_server():
    aid = "00000000-0000-4000-8000-000000000013"
    with_session(session({
        ("PUT", "/api/Appointment/answer"): FakeResp(200),
        # сервер показывает НЕ то, что мы отправили
        ("GET", "/api/Appointment/"): FakeResp(200, {"currentUserMeetingResponseType": "Decline"}),
    }))
    out = server.calendar_respond(aid, "пойду")
    check("Decline" in out and "Accept" in out,
          f"расхождение отправленного и сохранённого должно быть видно: {out}")
    check("Ответ записан" not in out, f"нельзя рапортовать об успехе при расхождении: {out}")
    print("  ответ: расхождение с сервером не прячется")


def check_event_keeps_what_matters():
    aid = fx("detail")["path"].rsplit("/", 1)[-1]
    with_session(session({("GET", "/api/Appointment/"): resp_of("detail")}))
    out = json.loads(server.calendar_event(aid))
    body = fx("detail")["res_body"]
    check(out["участников"] == len(body["participants"]), "число участников не сошлось")
    check(out["созвон"] == body["onlineMeetingUrl"], "ссылка на созвон обязана уцелеть")
    check(all(p["ответ"] for p in out["участники"]), "ответ каждого участника нужен")
    check("<" not in out["повестка"], "повестка должна быть текстом, а не HTML")
    print("  детали встречи: участники с ответами, ссылка на созвон, текстовая повестка")


def check_timezone_is_resolved_not_assumed():
    """Пояс берётся из офиса сотрудника, а не «всегда Москва».

    В workplacer 66 зданий в ВОСЬМИ поясах (+02:00…+10:00), поэтому московская
    константа сделала бы время неверным для всех, кто сидит не в Москве."""
    routes = {
        ("GET", "/booking-front-settings"): FakeResp(200, {"userDefaultFloor": {"buildingId": 7}}),
        ("GET", "/buildings/v2"): FakeResp(200, [
            {"id": 3, "name": "Офис 3", "utcOffset": "03:00:00"},
            {"id": 7, "name": "Офис 7", "utcOffset": "05:00:00"}]),
    }
    s = session(routes); s._tz = None
    tz, src = s.tz()
    check(myt.tz_label(tz) == "UTC+5", f"должен взять пояс СВОЕГО офиса: {myt.tz_label(tz)}")
    check("Офис 7" in src, f"источник пояса должен быть назван: {src}")
    check(s.tz() is tz or s.tz()[0] is tz, "пояс считается один раз за процесс")
    check(len(s._http.calls) == 2, f"на определение пояса — 2 запроса, было {len(s._http.calls)}")

    # Явная настройка бьёт всё: человек мог уехать, и API об этом не знает.
    os.environ[myt.TZ_ENV] = "+10:00"
    try:
        s2 = session(routes); s2._tz = None
        tz2, src2 = s2.tz()
        check(myt.tz_label(tz2) == "UTC+10", f"env должен побеждать офис: {myt.tz_label(tz2)}")
        check(myt.TZ_ENV in src2, f"источник — переменная окружения: {src2}")
        check(not s2._http.calls, "с заданным env запросы за поясом не нужны")
    finally:
        del os.environ[myt.TZ_ENV]

    # Workplacer недоступен — календарь всё равно работает, но резерв назван вслух.
    s3 = session({("GET", "/booking-front-settings"): FakeResp(500, text="nope")})
    s3._tz = None
    tz3, src3 = s3.tz()
    check(tz3 == myt.MSK, "резерв — Москва")
    check(myt.TZ_ENV in src3 and "не удалось" in src3,
          f"резерв обязан признаться, что он резерв: {src3}")
    print("  пояс: офис сотрудника → env перебивает → Москва только как названный резерв")


def check_meeting_times_are_converted_to_the_employee_timezone():
    """Kairos отдаёт UTC. Печатать его цифры значит отправить человека на 3 часа позже."""
    utc_noon = {"appointments": [{"id": "00000000-0000-4000-8000-000000000001",
                                  "start": "2026-08-07T12:00:00+00:00",
                                  "end": "2026-08-07T13:00:00+00:00",
                                  "title": "Встреча",
                                  "currentUserMeetingResponseType": "Accept",
                                  "isEnded": False}]}
    with_session(session({("GET", "/api/Appointment/short"): FakeResp(200, utc_noon)}))
    out = server.calendar_schedule("2026-08-07")
    check("15:00–16:00" in out, f"12:00 UTC — это 15:00 в Москве: {out}")
    check("12:00" not in out.split("\n", 1)[1], f"сырое UTC печатать нельзя: {out}")
    check("UTC+3" in out.splitlines()[0], f"пояс должен быть подписан в шапке: {out}")

    with_session(session({("GET", "/api/Appointment/short"): FakeResp(200, utc_noon)},
                         tz=myt.parse_tz("+05:00")))
    out = server.calendar_schedule("2026-08-07")
    check("17:00–18:00" in out, f"для сотрудника в +05:00 это 17:00: {out}")
    check("UTC+5" in out.splitlines()[0], f"подпись пояса должна следовать за поясом: {out}")

    # Ключ вхождения для отмены остаётся исходным UTC, а не местным временем.
    aid = "00000000-0000-4000-8000-000000000001"
    s = with_session(session({
        ("GET", f"/api/Appointment/{aid}"): FakeResp(200, {
            "id": aid, "title": "Встреча", "start": "2026-08-07T12:00:00+00:00",
            "isRecurrent": False}),
        ("PUT", "/cancel"): FakeResp(200),
        ("GET", "/api/Appointment/short"): FakeResp(200, {"appointments": []}),
    }))
    server.calendar_cancel(aid)
    sent = next(c for c in s._http.calls if c["method"] == "PUT")["params"]["DateTime"]
    check(sent == "2026-08-07T12:00:00+00:00",
          f"в DateTime уходит момент из kairos, не местное время: {sent}")
    print("  время: UTC → пояс сотрудника, подпись в шапке, ключ отмены не тронут")


def check_refresh_tool_does_the_capture_exchange():
    """myt_refresh_session — тот самый обмен из захвата, запись #28.

    Тело и заголовки сверяются с фикстурой: это единственный способ убедиться, что
    мы шлём grantType=refresh_token, а не придуманный /refresh или form-urlencoded."""
    e = fx("auth_refresh")
    s = with_session(session({("POST", "/v3/auth/token"): FakeResp(200, {
        "accessToken": "новый", "refreshToken": "тот-же", "expiresIn": 3600,
        "tokenType": "Bearer"})}))
    s.refresh_token = "тот-же"
    out = json.loads(server.myt_refresh_session())

    call = s._http.calls[0]
    check(call["method"] == e["method"] and call["url"].endswith(e["path"]),
          f"обмен идёт не туда: {call['method']} {call['url']}, в захвате {e['method']} {e['path']}")
    check(sorted(call["body"]) == e["req_body_keys"],
          f"состав тела {sorted(call['body'])} != захват {e['req_body_keys']}")
    check(call["body"]["grantType"] == "refresh_token",
          f"grantType должен быть refresh_token: {call['body'].get('grantType')}")
    for h in ("X-User-Id", "X-Device-Id", "X-App-Version", "X-Platform",
              "X-App-Code", "X-Auth-Method-Version"):
        check(h in call["headers"], f"заголовок {h} есть в захвате, а мы его не шлём")

    check(out["access_токен_сменился"] is True, f"access обязан обновиться: {out}")
    check(out["refresh_токен_ротирован"] is False,
          "этот сервер refresh-токен НЕ ротирует — если начал, это надо заметить")
    check(out["живёт_секунд"] == 3600, f"expiresIn берётся из ответа: {out}")

    # Мёртвый refresh — не «обменяно», а честный MYT SESSION EXPIRED.
    s2 = with_session(session({("POST", "/v3/auth/token"): FakeResp(
        400, {"error": {"code": "invalid_grant", "message": "no"}})}))
    out2 = server.myt_refresh_session()
    check("MYT SESSION EXPIRED" in out2, f"мёртвый refresh должен так и называться: {out2}")
    check("login_cli.py --myt" in out2, f"и назвать единственный путь починки: {out2}")
    print("  refresh: обмен из захвата #28, токен не ротируется, мёртвый — не скрыт")


def check_status_verifies_instead_of_doing_arithmetic():
    """Ноль в остатке — не «мертва», и статус обязан это различать запросом.

    Именно на этом споткнулся живой агент: увидел «токен_живёт_ещё_секунд: 0» у
    совершенно рабочей сессии, решил, что её надо обновить, и пошёл искать тул
    рефреша, которого нет и не требуется."""
    s = with_session(session({
        ("POST", "/v3/auth/token"): FakeResp(200, {
            "accessToken": "новый", "refreshToken": "новый-refresh",
            "expiresIn": 3600, "tokenType": "Bearer"}),
        ("GET", "/api/Appointment/short"): resp_of("schedule"),
    }))
    s._minted_at = 1.0                      # протух по арифметике
    out = json.loads(server.myt_status())
    check(out["статус"].startswith("жива"), f"сессия рабочая — статус должен это сказать: {out}")
    check(out["токен_обновлён_этим_вызовом"] is True,
          "просроченный по времени токен должен быть переминчен прямо здесь")
    check(out["токен_живёт_ещё_секунд"] > 3000,
          f"после переминта остаток обязан вырасти: {out['токен_живёт_ещё_секунд']}")
    check("вручную не нужно" in out["продление"],
          "ответ должен снимать сам вопрос про ручное обновление")
    check(any("/api/Appointment/short" in c["url"] for c in s._http.calls),
          "статус без живого запроса — это гадание по локальным часам")

    # А мёртвую сессию нельзя объявлять живой.
    s2 = with_session(session({
        ("POST", "/v3/auth/token"): FakeResp(400, {"error": {"code": "invalid_grant",
                                                             "message": "no"}}),
    }))
    s2._minted_at = 1.0
    out2 = server.myt_status()
    check("MYT SESSION EXPIRED" in out2, f"мёртвая сессия должна называться мёртвой: {out2}")
    check("login_cli.py --myt" in out2, f"и назвать способ починки: {out2}")
    print("  статус: проверяет запросом, сам продлевает, мёртвую не выдаёт за живую")


def check_no_session_does_not_pretend():
    server._myt_session = None
    saved, server._MYT_FILE = server._MYT_FILE, os.path.join(HERE, "no-such-myt.json")
    try:
        out = server.calendar_schedule()
    finally:
        server._MYT_FILE = saved
        server._myt_session = None
    check("login_cli.py --myt" in out, f"без сессии нужно сказать, что делать: {out}")
    check("Встречи" not in out, f"пустого расписания вместо ошибки быть не должно: {out}")
    print("  без сессии: инструкция по логину, а не пустой ответ")


# ── дрейф фикстуры ──────────────────────────────────────────────────────────

def check_fixture_still_matches_capture():
    if not os.path.exists(CAPTURE):
        print(f"  (захвата нет: {CAPTURE} — сверка с ним пропущена, фикстура проверена выше)")
        return
    sys.path.insert(0, os.path.join(HERE, "fixtures"))
    import regen_myt as R
    R.CAPTURE = CAPTURE
    chunks = R.items(CAPTURE)
    for name, idx in FIXTURE["_capture_indices"].items():
        live = R.parsed(chunks[idx])
        e = FIXTURE[name]
        check(live["method"] == e["method"], f"{name}: метод разошёлся с захватом")
        check(live["query"] == e["query"],
              f"{name}: query в захвате {live['query']}, в фикстуре {e['query']}")
        check(live["status"] == e["status"], f"{name}: статус разошёлся с захватом")
        if "req_body" in e:
            check(sorted(json.loads(live["req_body"])) == sorted(e["req_body"]),
                  f"{name}: состав полей тела разошёлся с захватом")
    print(f"  фикстура сверена с захватом: {len(FIXTURE['_capture_indices'])} запросов")


def main():
    for fn in (check_schedule_request_matches_capture,
               check_schedule_walks_days_one_by_one,
               check_answer_body_matches_capture,
               check_cancel_query_matches_capture,
               check_parking_recommended_sends_literal_nil,
               check_parking_book_body_matches_capture,
               check_answer_aliases_are_exact,
               check_answer_retries_once_on_throttle,
               check_answer_does_not_swallow_other_400,
               check_401_says_which_session_died,
               check_html_agenda_becomes_text,
               check_today_means_moscow_not_the_host,
               check_dates_accept_russian_words,
               check_cancel_reports_that_nothing_changed,
               check_cancel_refuses_a_series_without_the_occurrence,
               check_book_reports_the_row_that_was_saved,
               check_book_refuses_a_date_outside_the_window,
               check_schedule_caps_the_range_and_says_so,
               check_schedule_prints_whole_ids,
               check_places_answers_the_whole_question,
               check_places_stops_when_there_is_no_access,
               check_respond_flags_a_disagreeing_server,
               check_event_keeps_what_matters,
               check_timezone_is_resolved_not_assumed,
               check_meeting_times_are_converted_to_the_employee_timezone,
               check_refresh_tool_does_the_capture_exchange,
               check_status_verifies_instead_of_doing_arithmetic,
               check_no_session_does_not_pretend,
               check_fixture_still_matches_capture):
        fn()
    server._myt_session = None
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
