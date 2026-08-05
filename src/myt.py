"""MyT — корпоративное приложение Т-Банка: рабочий календарь и парковка в офисе.

ЭТО ДРУГОЙ ПРОДУКТ, НЕ БАНК. Общего с `MobileSession` — только владелец телефона.
Разные хосты, разные креды, разный формат токена, и поэтому отдельная сессия в
отдельном файле: смешивать их в одном session.json значило бы, что протухший
корпоративный токен роняет банковские тулы (и наоборот).

Три хоста, все из захвата `captures-myt.xml`:

    magentbep.tcsbank.ru   /v3/auth/token          — выдаёт токен (grantType password / refresh_token)
    kairos.tbank.ru        /api/Appointment/*      — календарь (Exchange за фасадом)
    workplacer.tbank.ru    /workplacer/api/*       — бронь мест и парковки

Токен — обычный JWT, выпущенный twork.tbank.ru/auth, живёт `expiresIn` (в захвате
3600 с). Веб-вьюхи внутри приложения добывают его через OIDC-редиректы с куками;
НАТИВНОЕ приложение так не делает — оно ходит в /v3/auth/token и получает пару
accessToken+refreshToken. Мы повторяем нативный путь: без браузера, без кук.

Заголовки на API-хостах не декоративны — это часть контракта, снятая с захвата:
`X-Userid` (UUID сотрудника), `X-Auth-Provider: twork` и `X-Requested-With`
приходят на КАЖДОМ запросе, включая GET.
"""
from __future__ import annotations

import base64
import html as _html
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone

import requests

from .client import SessionExpired, TbankApiError

AUTH_BASE = "https://magentbep.tcsbank.ru"
KAIROS_BASE = "https://kairos.tbank.ru"
WORKPLACER_BASE = "https://workplacer.tbank.ru"

# Версия/платформа/UA — вербатим из захвата. X-App-Code и билд в User-Agent
# совпадают («36») не случайно: это один и тот же номер сборки.
APP_VERSION = "1.51.0"
APP_CODE = "36"
PLATFORM = "ios"
USER_AGENT = "Beta/36 CFNetwork/3860.600.12 Darwin/25.5.0"

# Ответ на приглашение. Значения — ровно те, что уходят в теле /api/Appointment/answer;
# все три сняты с успешных (200) запросов в захвате.
ANSWER_TYPES = ("Accept", "Decline", "Tentative")

# Как пользователь скажет это по-русски. Ключи — в нижнем регистре без пунктуации.
ANSWER_ALIASES = {
    "accept": "Accept", "yes": "Accept", "да": "Accept", "пойду": "Accept",
    "приду": "Accept", "буду": "Accept", "принять": "Accept", "согласен": "Accept",
    "decline": "Decline", "no": "Decline", "нет": "Decline", "не пойду": "Decline",
    "не приду": "Decline", "не буду": "Decline", "отклонить": "Decline",
    "откажусь": "Decline",
    "tentative": "Tentative", "maybe": "Tentative", "может быть": "Tentative",
    "возможно": "Tentative", "под вопросом": "Tentative", "не уверен": "Tentative",
    "постараюсь": "Tentative",
}

# Kairos отдаёт 400 с этим текстом, если ответить на встречу чаще раза в 5 секунд.
# Не ошибка данных — троттлинг; см. answer().
_ANSWER_THROTTLE_S = 5.0
_ANSWER_THROTTLE_MARK = "раз в 5 секунд"


class MytSessionExpired(SessionExpired):
    """Корпоративный токен мёртв — нужен «login_cli.py --myt», банковская сессия ни при чём.

    Отдельный тип, потому что `SessionExpired` в server._err печатает «вызови
    refresh_session()», а refresh_session() обновляет БАНКОВСКУЮ сессию и здесь не
    поможет ничем."""


def _b64url(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def jwt_claims(token: str) -> dict:
    """Полезная нагрузка JWT без проверки подписи.

    Подпись проверяет сервер, который токен и выдал; нам claims нужны для одного —
    достать `user_id` (уходит в X-Userid) и `sub` (уходит в X-User-Id на хосте
    авторизации), чтобы не хранить их отдельно и не разъезжаться с токеном."""
    try:
        return json.loads(_b64url(token.split(".")[1]))
    except Exception:
        return {}


_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_DROP = re.compile(r"<(script|style|head)\b.*?</\1>", re.S | re.I)
_HTML_BREAK = re.compile(r"<br\s*/?>|</p>|</div>|</tr>", re.I)


def text_from_html(html: str, limit: int = 1200) -> str:
    """Описание встречи в читаемый текст.

    Приглашения рассылает Outlook, и `description` — это письмо целиком: <head> с
    Word-овскими стилями на полтора килобайта, а сам текст в конце. Отдать это
    агенту как есть значит потратить контекст на CSS и утопить в нём единственное,
    что там ценно — ссылку на созвон и повестку."""
    s = _HTML_DROP.sub(" ", html or "")
    s = _HTML_BREAK.sub("\n", s)
    s = _HTML_TAG.sub("", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*", "\n", s).strip()
    if limit > 0 and len(s) > limit:
        s = s[:limit] + f"… (обрезано, всего {len(s)} символов)"
    return s


def answer_type(value: str) -> str:
    """Нормализовать «пойду»/«не пойду»/«может быть» в значение responseType.

    Никакого «похоже на Accept» — только точное совпадение по словарю. Промах в
    эту сторону стоит дорого: перепутанные Accept и Decline видит вся встреча."""
    v = " ".join(str(value or "").lower().replace("ё", "е").split()).strip(" .!?,")
    if v in ANSWER_ALIASES:
        return ANSWER_ALIASES[v]
    for t in ANSWER_TYPES:
        if v == t.lower():
            return t
    raise TbankApiError("BAD_ANSWER",
        f"Не понял ответ {value!r}. Допустимо: Accept/пойду, Decline/не пойду, "
        f"Tentative/может быть.")


# Ни «сегодня», ни время встречи нельзя считать по календарю машины, где крутится
# MCP: эта VM живёт в UTC, а человек — нет. Но и Москву зашивать нельзя: в
# workplacer 66 зданий и ВОСЕМЬ разных utcOffset, от +02:00 до +10:00. Поэтому
# пояс определяется, а не предполагается — см. MytSession.tz().
MSK = timezone(timedelta(hours=3))
TZ_ENV = "TBANK_MYT_TZ"


def parse_tz(raw: str):
    """«+05:00», «-03:00», «+5» или имя зоны («Asia/Yekaterinburg»)."""
    raw = str(raw or "").strip()
    if raw[:1] in ("+", "-"):
        sign = -1 if raw[0] == "-" else 1
        body = raw[1:].split(":")
        try:
            h = int(body[0])
            m = int(body[1]) if len(body) > 1 else 0
        except ValueError:
            raise TbankApiError("BAD_TZ", f"{TZ_ENV}={raw!r} — не смещение и не имя зоны.")
        return timezone(sign * timedelta(hours=h, minutes=m))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(raw)
    except Exception:
        raise TbankApiError("BAD_TZ",
            f"{TZ_ENV}={raw!r} не разобран. Нужно смещение «+05:00» или имя зоны "
            f"«Asia/Yekaterinburg».")


def tz_label(tz) -> str:
    key = getattr(tz, "key", None)
    if key:
        return key
    total = int((tz.utcoffset(None) or timedelta()).total_seconds()) // 60
    sign, total = ("+" if total >= 0 else "-"), abs(total)
    return f"UTC{sign}{total // 60}" + (f":{total % 60:02d}" if total % 60 else "")


def offset_from_hhmmss(value: str):
    """«03:00:00» из workplacer → timezone(+3). None, если не разобралось."""
    parts = str(value or "").split(":")
    try:
        return timezone(timedelta(hours=int(parts[0]), minutes=int(parts[1])))
    except (ValueError, IndexError):
        return None


def to_local(iso: str, tz):
    """Момент из kairos → время в поясе пользователя.

    Kairos отдаёт ЧЕСТНЫЙ UTC (проверено на живом календаре: встреча с меткой
    15:00+00:00 показана в приложении как 18:00 по Москве). Печатать её цифры как
    есть — значит показывать человеку не его часы и отправить его на встречу на
    три часа позже."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def today_in(tz) -> _date:
    return datetime.now(tz).date()


def as_date(value, default_days: int = 0, tz=MSK) -> str:
    """Привести дату к YYYY-MM-DD, принимая пустую строку/`сегодня`/`завтра`.

    «Сегодня» — в поясе ПОЛЬЗОВАТЕЛЯ (tz), не хоста и не обязательно Москвы."""
    if isinstance(value, (_date, datetime)):
        return value.strftime("%Y-%m-%d")
    v = str(value or "").strip().lower()
    if not v:
        return (today_in(tz) + timedelta(days=default_days)).isoformat()
    if v in ("сегодня", "today"):
        return today_in(tz).isoformat()
    if v in ("завтра", "tomorrow"):
        return (today_in(tz) + timedelta(days=1)).isoformat()
    if v in ("послезавтра",):
        return (today_in(tz) + timedelta(days=2)).isoformat()
    try:
        return datetime.fromisoformat(v[:19]).strftime("%Y-%m-%d")
    except ValueError:
        raise TbankApiError("BAD_DATE", f"Дата {value!r} не разобрана, нужен YYYY-MM-DD.")


@dataclass
class MytSession:
    """Корпоративная сессия MyT. Сохраняется отдельно от банковской (myt.json)."""

    access_token: str = ""
    refresh_token: str = ""
    expires_in: int = 3600
    username: str = ""          # X-User-Id на хосте авторизации (логин или телефон)
    user_id: str = ""           # X-Userid на kairos/workplacer (UUID сотрудника)
    device_id: str = ""
    app_version: str = APP_VERSION
    app_code: str = APP_CODE
    platform: str = PLATFORM
    proxy: str | None = None
    _http: requests.Session = field(default_factory=requests.Session, repr=False)
    _minted_at: float = 0.0

    def __post_init__(self) -> None:
        # Ставится владельцем (server) — каждый refresh РОТИРУЕТ refresh_token, и
        # не записанная на диск ротация сжигает токен для следующего процесса.
        self._on_persist = None
        # Пояс сотрудника: считается один раз за процесс. Обычный атрибут, не поле
        # датакласса — _save_myt сериализует поля, а пояс это факт о человеке
        # СЕГОДНЯ (он может переехать), не часть сессии.
        self._tz = None
        if not self.device_id:
            self.device_id = str(uuid.uuid4()).upper()
        self._http.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "ru",
            "X-Requested-With": "XMLHttpRequest",
        })
        if self.proxy:
            self._http.proxies = {"http": self.proxy, "https": self.proxy}
        # Тот же перестроенный CA-бандл, что и у банковских хостов: *.tbank.ru
        # отдают цепочку с Russian Trusted Root CA, которой нет в системном сторе.
        try:
            from . import tls as _tls
            _tls.rebuild_bundle()
            self._http.mount("https://", _tls.RobustTLSAdapter())
            if _tls.BUNDLE and os.path.exists(_tls.BUNDLE):
                self._http.verify = _tls.BUNDLE
        except Exception:
            pass

    # ── авторизация ────────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        return bool(self.access_token and self.refresh_token)

    def _persist(self) -> None:
        if self._on_persist:
            try:
                self._on_persist()
            except Exception:
                pass

    def _auth_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "X-User-Id": self.username,
            "X-Auth-Method-Version": "2",
            "X-App-Code": self.app_code,
            "X-Device-Id": self.device_id,
            "X-App-Version": self.app_version,
            "X-Platform": self.platform,
        }

    def _token_call(self, body: dict) -> dict:
        try:
            r = self._http.post(f"{AUTH_BASE}/v3/auth/token", json=body,
                                headers=self._auth_headers(), timeout=30)
        except requests.exceptions.RequestException as e:
            raise TbankApiError("NETWORK", f"{type(e).__name__}: {e}")
        try:
            data = r.json()
        except ValueError:
            raise TbankApiError("BAD_RESPONSE", f"HTTP {r.status_code}: {r.text[:200]}")
        err = data.get("error") or {}
        if err:
            code = str(err.get("code") or f"http_{r.status_code}")
            raise TbankApiError(code, str(err.get("message") or code))
        if r.status_code >= 400:
            raise TbankApiError(f"http_{r.status_code}", str(data)[:200])
        return data

    def _adopt(self, data: dict) -> None:
        self.access_token = data.get("accessToken") or ""
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_in = int(data.get("expiresIn") or 3600)
        self._minted_at = time.time()
        claims = jwt_claims(self.access_token)
        # X-Userid берём из токена, а не из конфига: они обязаны совпадать, а
        # сохранённый отдельно UUID пережил бы смену сотрудника в этом же приложении.
        self.user_id = str(claims.get("user_id") or self.user_id)
        self.username = str(claims.get("sub") or self.username)
        self._persist()

    def login(self, username: str, password: str, sms_code: str = "") -> None:
        """grantType=password. Без sms_code сервер отвечает 400 sms_required —
        это НОРМАЛЬНЫЙ первый шаг, а не сбой: он же и отправляет SMS."""
        body = {"grantType": "password", "username": username, "password": password}
        if sms_code:
            body["smsCode"] = sms_code
        self.username = username
        self._adopt(self._token_call(body))

    def refresh(self) -> None:
        if not self.refresh_token:
            raise MytSessionExpired("NO_SESSION", "Нет refresh_token — нужен полный логин.")
        try:
            data = self._token_call({"grantType": "refresh_token",
                                     "refreshToken": self.refresh_token})
        except TbankApiError as e:
            if e.result_code.startswith("http_4") or "grant" in e.result_code:
                raise MytSessionExpired(e.result_code, e.message)
            raise
        self._adopt(data)

    def ensure_fresh(self) -> None:
        if not self.access_token:
            raise MytSessionExpired("NO_SESSION", "Нет токена MyT.")
        # 120 с запаса: токен, истекающий в полёте, вернул бы 401 в середине
        # брони, а бронь — не идемпотентная операция, чтобы её просто повторить.
        if not self._minted_at or time.time() - self._minted_at > max(60, self.expires_in - 120):
            self.refresh()

    # ── часовой пояс сотрудника ────────────────────────────────────────────

    def tz(self) -> tuple:
        """(пояс, откуда взяли). Считается один раз за процесс.

        Порядок неслучаен. Сначала явная настройка: человек мог уехать, работать
        удалённо или сидеть не в том офисе, что записан в кадрах, и никакой API
        этого не знает. Дальше — ЕГО офис: workplacer отдаёт utcOffset на каждое
        здание, и зданий там 66 в восьми поясах, так что «по умолчанию Москва»
        было бы просто неверно для сотрудника из Екатеринбурга или Владивостока.
        Москва остаётся последним резервом — и о том, что это резерв, тул говорит
        вслух, а не молчит.

        Промах workplacer календарь не роняет: пояс — не повод не показать
        встречи, поэтому любая ошибка здесь означает резерв, а не исключение."""
        if self._tz:
            return self._tz
        raw = (os.environ.get(TZ_ENV) or "").strip()
        if raw:
            self._tz = (parse_tz(raw), f"{TZ_ENV}={raw}")
            return self._tz
        try:
            bid = (self.booking_settings().get("userDefaultFloor") or {}).get("buildingId")
            if bid:
                for b in self.buildings():
                    if b.get("id") == bid:
                        off = offset_from_hhmmss(b.get("utcOffset"))
                        if off:
                            self._tz = (off, f"офис: {b.get('name')}")
                            return self._tz
        except Exception:
            pass
        self._tz = (MSK, f"офис определить не удалось, взята Москва — "
                         f"задай {TZ_ENV}, если ты в другом поясе")
        return self._tz

    def buildings(self) -> list:
        return self._call("GET", f"{WORKPLACER_BASE}/workplacer/api/buildings/v2") or []

    # ── транспорт ──────────────────────────────────────────────────────────

    def _api_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "X-Userid": self.user_id,
            "X-Auth-Provider": "twork",
        }

    def _call(self, method: str, url: str, *, params=None, body=None, timeout: int = 30):
        self.ensure_fresh()
        headers = self._api_headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            r = self._http.request(method, url, params=params, json=body,
                                   headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise TbankApiError("NETWORK", f"{type(e).__name__}: {e}")
        if r.status_code in (401, 403):
            raise MytSessionExpired(f"http_{r.status_code}", "Токен MyT отклонён.")
        if r.status_code >= 400:
            # Kairos отвечает text/plain по-русски («Ответ на встречу можно дать раз
            # в 5 секунд»), workplacer — JSON. Отдаём как есть: текст осмысленный,
            # и подменять его своей формулировкой значит терять причину.
            raise TbankApiError(f"http_{r.status_code}", r.text.strip()[:300] or "(пустой ответ)")
        if not r.content:
            return None            # 200 с пустым телом — штатный ответ на запись
        try:
            return r.json()
        except ValueError:
            raise TbankApiError("BAD_RESPONSE", f"не JSON: {r.text[:200]}")

    # ── календарь (kairos) ─────────────────────────────────────────────────

    def day_appointments(self, day: str) -> list:
        """Встречи за ОДИН день. End — следующий день и не включается.

        Приложение ходит именно так, по дню за запрос; диапазон длиннее суток в
        захвате не встречается, поэтому schedule() повторяет этот же цикл, а не
        экономит запросы на неподтверждённом предположении."""
        nxt = (_date.fromisoformat(day) + timedelta(days=1)).isoformat()
        data = self._call("GET", f"{KAIROS_BASE}/api/Appointment/short",
                          params=[("End", nxt), ("Start", day)])
        return (data or {}).get("appointments") or []

    def schedule(self, day_from: str, day_to: str = "") -> list:
        day_to = day_to or day_from
        start, end = _date.fromisoformat(day_from), _date.fromisoformat(day_to)
        out = []
        for i in range((end - start).days + 1):
            day = (start + timedelta(days=i)).isoformat()
            # Копия, а не пометка на месте: kairos отдаёт по встрече на КАЖДЫЙ день,
            # который она задевает, и правка исходного словаря приписала бы одному
            # объекту последний из дней вместо того, за который его вернули.
            out.extend(dict(a, day=day) for a in self.day_appointments(day))
        return out

    def appointment(self, appointment_id: str) -> dict:
        return self._call("GET", f"{KAIROS_BASE}/api/Appointment/{appointment_id}") or {}

    def answer(self, appointment_id: str, response: str, comment: str = "",
               retry_throttle: bool = True) -> str:
        """Ответить на приглашение. Возвращает применённый responseType.

        Порядок ключей в теле у приложения плавает от запроса к запросу — сервер
        на него не смотрит; фиксируем состав, а не порядок."""
        rtype = answer_type(response)
        body = {
            "withAnswer": bool(comment),
            "internalAppointmentId": appointment_id,
            "answer": comment or "",
            "responseType": rtype,
        }
        url = f"{KAIROS_BASE}/api/Appointment/answer"
        try:
            self._call("PUT", url, body=body)
        except TbankApiError as e:
            # Троттлинг — не отказ: тот же запрос через 5 секунд проходит. В захвате
            # это половина всех 400-х, и все они — следствие того, что пользователь
            # тыкал кнопки подряд. Повторяем ОДИН раз, дальше отдаём ошибку как есть.
            if retry_throttle and _ANSWER_THROTTLE_MARK in str(e.message):
                time.sleep(_ANSWER_THROTTLE_S + 0.3)
                self._call("PUT", url, body=body)
            else:
                raise
        return rtype

    def cancel(self, appointment_id: str, when: str) -> None:
        """Отменить встречу (доступно организатору).

        `when` уходит в query-параметр DateTime и адресует КОНКРЕТНОЕ вхождение.
        Формат — как в ответах kairos: 2026-08-05T12:00:00+00:00."""
        self._call("PUT", f"{KAIROS_BASE}/api/Appointment/{appointment_id}/cancel",
                   params=[("DateTime", when)])

    # ── парковка и брони (workplacer) ──────────────────────────────────────

    def booking_settings(self) -> dict:
        return self._call("GET", f"{WORKPLACER_BASE}/workplacer/api/booking-front-settings") or {}

    def parking_buildings(self) -> list:
        return self._call("GET", f"{WORKPLACER_BASE}/workplacer/api/booking/parking/buildings") or []

    def parking_last(self) -> dict:
        """Прошлая бронь: номер и марка машины, здание, этаж. Источник дефолтов."""
        return self._call("GET", f"{WORKPLACER_BASE}/workplacer/api/booking/parking/last") or {}

    def parking_recommended(self, day: str, building_id, floor_id="", count: int = 10) -> dict:
        # floorId=nil — ЛИТЕРАЛЬНАЯ строка "nil": iOS-клиент подставляет в URL
        # Swift-овский nil, когда этаж не выбран, и сервер это принимает. Пустая
        # строка или отсутствие параметра — уже не то, что снято с захвата.
        return self._call(
            "GET", f"{WORKPLACER_BASE}/workplacer/api/booking/parking/recommended/{day}",
            params=[("buildingId", str(building_id)),
                    ("floorId", str(floor_id) if floor_id else "nil"),
                    ("resultCount", str(count))]) or {}

    def parking_book(self, place_id, day: str, car_number: str, car_model: str,
                     building_id) -> None:
        """Забронировать место. 200 с пустым телом = принято; проверять через bookings()."""
        self._call("POST", f"{WORKPLACER_BASE}/workplacer/api/booking/parking/{place_id}",
                   body={"date": day, "carNumber": car_number,
                         "carModel": car_model, "buildingId": int(building_id)})

    def bookings(self, day: str) -> dict:
        """Все брони сотрудника начиная с `day` — не только за этот день.

        В захвате запрос за 2026-08-05 вернул парковку, забронированную на 08-06,
        поэтому одного вызова хватает, чтобы увидеть всё окно вперёд."""
        return self._call(
            "GET", f"{WORKPLACER_BASE}/workplacer/api/booking/{day}/all-user-bookings") or {}
