"""T-Bank mobile API MCP server (FastMCP).

Low-level API calls are encapsulated in high-level tools; get_data(section)
covers 60+ read endpoints in one tool. The tool docstrings ARE the agent-facing
reference — there is no separate tool list to keep in sync.

Run: python -m src.server
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from . import trace
from .client import MobileSession, TbankApiError, SessionExpired, ms_for_period, vertical
from .endpoints import VERTICALS
from .observability import redact_text, _redact_value

mcp = FastMCP("tbank")

# Every @mcp.tool() below is recorded. Done by replacing the decorator ONCE rather
# than touching 57 functions: a per-tool opt-in is a list somebody has to remember to
# extend, and the tool that gets forgotten is the one whose behaviour is a mystery.
# trace.wrap keeps __wrapped__, so FastMCP still builds its schema and description
# from the real signature — pinned by tests/test_trace.py, because a schema that
# changed here would change what every agent sees.
_untraced_tool = mcp.tool

# ── What each tool DOES, as the host needs to know it ───────────────────────
#
# `readOnlyHint: true` lets a host run a tool WITHOUT asking. That is the whole
# point: before this table all 58 tools prompted alike, so confirming
# list_accounts looked exactly like confirming transfer — which is how a person
# learns to click "allow" without reading, on the one call that moves money.
#
# Three kinds. The line for CONFIRM is drawn at MONEY, by the repo owner's rule:
# a booking that expires by itself, a cart line, a chat message and an SMS are all
# recoverable; a payment is not. Only the three tools that actually move money get
# destructiveHint, which is the flag that forces a confirmation dialog.
#
#   READ   nothing changes at the bank or on disk. Safe to auto-approve, safe to
#          retry. (Refreshing our own session token does not count — every tool
#          here does that implicitly on the way in.)
#   WRITE  changes something, but nothing that costs money: a cart, a booking, a
#          chat message, an OTP, a token, a local file. NOT marked destructive —
#          but not marked read-only either, because they do modify things and
#          `readOnlyHint: true` states the opposite. A host that still prompts on
#          these is applying its own worst-case default; the fix for that belongs
#          in the client ("always allow"), not in a false claim here.
#   MONEY  the three tools that debit an account. destructiveHint + never
#          idempotent: the loudest signal the protocol has.
#
# A tool missing from this table raises at import. That is deliberate: the
# alternative is a new tool defaulting to whatever the host assumes, which looks
# harmless right up until the tool it happened to be was a payment.
READ, WRITE, MONEY = "read", "write", "money"
TOOL_KINDS: dict[str, tuple[str, str]] = {
    # session
    "login": ("Вход по телефону", WRITE),
    "confirm_otp": ("Подтверждение кода из SMS", WRITE),
    "confirm_password": ("Подтверждение пароля", WRITE),
    "confirm_pin": ("Подтверждение PIN", WRITE),
    "refresh_session": ("Обновление сессии", WRITE),
    "session_status": ("Статус сессии", READ),
    "keepalive": ("Продление сессии", READ),
    # accounts and operations
    "list_accounts": ("Счета", READ),
    "list_operations": ("Операции по счёту", READ),
    "spending_categories": ("Траты по категориям", READ),
    "operations_histogram": ("График трат", READ),
    "get_data": ("Банковские данные по разделам", READ),
    # cards and documents
    "list_cards": ("Карты", READ),
    "card_limits": ("Лимиты карты", READ),
    "card_requisites": ("Реквизиты карты", READ),
    "card_operations": ("Операции по карте", READ),
    "account_requisites": ("Реквизиты счёта", READ),
    "documents": ("Документы клиента", READ),
    "bank_documents": ("Справки банка", READ),
    "insurance_policies": ("Страховые полисы", READ),
    "payment_receipt": ("Скачивание чека в файл", WRITE),
    # orders
    "orders": ("Заказы", READ),
    "order_details": ("Детали заказа", READ),
    "travel_order_details": ("Детали поездки", READ),
    # grocery
    "grocery_stores": ("Магазины и доставка", READ),
    "grocery_search": ("Поиск товара", READ),
    "grocery_rank": ("Товары с сортировкой", READ),
    "grocery_good_info": ("Карточка товара и КБЖУ", READ),
    "grocery_plan_order": ("Планирование заказа", READ),
    "grocery_cart": ("Содержимое корзины", READ),
    "grocery_attempts": ("Попытки оформления", READ),
    "grocery_order_status": ("Статус заказа", READ),
    "grocery_add_to_cart": ("Добавление в корзину", WRITE),
    "grocery_set_cart": ("Перезапись корзины", WRITE),
    "grocery_checkout": ("Оформление и оплата заказа", MONEY),
    "grocery_order_cancel": ("Отмена продуктового заказа", WRITE),
    # tickets
    "cinema_search": ("Поиск фильма", READ),
    "cinema_schedule": ("Расписание сеансов", READ),
    "afisha_catalog": ("Афиша за период", READ),
    "ticket_qr": ("Билет: QR и код брони", READ),
    "afisha_places": ("Площадки города", READ),
    "place_schedule": ("Афиша площадки", READ),
    "place_info": ("Карточка площадки", READ),
    "cinema_seats": ("Свободные места", READ),
    "concert_schedule": ("Показы концерта", READ),
    "concert_hall": ("Секторы концертной площадки", READ),
    "cinema_book": ("Бронирование мест", WRITE),
    "ticket_pay": ("Оплата брони", MONEY),
    "ticket_cancel": ("Отмена заказа", WRITE),
    # search
    "search_app": ("Поиск по приложению", READ),
    "train_search": ("Поиск поездов", READ),
    "train_calendar": ("Даты продажи ЖД", READ),
    "flight_search": ("Поиск авиабилетов", READ),
    "flight_history": ("История авиапоисков", READ),
    "shop_search": ("Поиск товаров в маркетплейсе", READ),
    "shop_cart": ("Корзины маркетплейса", READ),
    # messenger
    "messenger_conversations": ("Чаты", READ),
    "messenger_messages": ("История чата", READ),
    "messenger_unread": ("Непрочитанные", READ),
    "messenger_send": ("Отправка сообщения", WRITE),
    # money
    "transfer_sbp_resolve": ("Получатель СБП по телефону", READ),
    "payment_commission": ("Предпросмотр комиссии", READ),
    "payment_providers": ("Каталог платёжных провайдеров", READ),
    "pay_bill": ("Оплата счёта", MONEY),
    "transfer": ("Перевод денег", MONEY),
    # invest
    "invest_accounts": ("Инвест-счета", READ),
    "invest_portfolio": ("Статистика портфеля", READ),
    "invest_operations": ("Брокерские операции", READ),
    "invest_securities": ("Бумаги в портфеле", READ),
    # utility
    "flows": ("Порядок вызовов по теме", READ),
    "diagnostics": ("События последних оплат", READ),
    "debug_report": ("Как использовали этот MCP", READ),
}


def _annotations_for(name: str) -> ToolAnnotations:
    if name not in TOOL_KINDS:
        raise RuntimeError(
            f"tool {name!r} has no entry in TOOL_KINDS. Classify it as READ "
            f"(nothing changes), WRITE (changes something, costs nothing) or "
            f"MONEY (debits an account) — see the note above the table.")
    title, kind = TOOL_KINDS[name]
    # openWorldHint everywhere: every one of these talks to the bank.
    ann = {"title": title, "openWorldHint": True}
    if kind == READ:
        ann.update(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    elif kind == WRITE:
        # Modifies something, destroys nothing. destructiveHint defaults to TRUE
        # when readOnlyHint is false, so saying false here is what actually takes
        # the confirmation dialog off these.
        ann.update(readOnlyHint=False, destructiveHint=False)
    else:
        ann.update(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
    return ToolAnnotations(**ann)


def _traced_tool(*a, **kw):
    def register(fn):
        title, _ = TOOL_KINDS.get(fn.__name__, ("", ""))
        opts = {"title": title, "annotations": _annotations_for(fn.__name__), **kw}
        return _untraced_tool(*a, **opts)(trace.wrap(fn))
    return register


mcp.tool = _traced_tool
_session: MobileSession | None = None
_SESSION_FILE = os.environ.get(
    "TBANK_SESSION",
    os.path.expanduser("~/.local/share/tbank-mcp/session.json"),
)
# Serializes grocery checkouts. FastMCP runs sync tools in the event-loop thread,
# but checkout is async + offloaded to a worker thread (sync_playwright cannot run
# inside the loop). A concurrent checkout would race two browsers on one cart →
# duplicate order. This lock makes the whole attempt body mutually exclusive.
_CHECKOUT_LOCK = threading.Lock()


def _blank_session():
    return _with_persist(MobileSession(mobile_sessionid="", refresh_token="",
        client_id="gorod-app", client_version="112.0.0",
        vendor="t_ios", origin="mobile,ib5,loyalty,platform",
        platform="ios", app_name="mobile", app_version="7.31.6"))


def _with_persist(s):
    """Make the session save itself after every re-mint.

    Not an optimisation: refresh() rotates the refresh_token, and ensure_fresh()
    runs on the first call of nearly every tool. A re-mint that never reaches disk
    leaves the next process holding a spent token, which then falls back to
    silent_relogin and degrades the session to ANONYMOUS — see
    MobileSession._persist for the full chain."""
    if s is not None:
        s._on_persist = lambda: _save_session(s)
    return s


def _save_session(s):
    """Save session to disk with 0600 permissions (owner-only read/write).
    Persists _minted_at for correct expiry tracking across restarts."""
    try:
        d = {k: v for k, v in s.__dict__.items() if not k.startswith("_") or k == "_minted_at"}
        os.makedirs(os.path.dirname(_SESSION_FILE), exist_ok=True)
        fd = os.open(_SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.chmod(_SESSION_FILE, 0o600)
        print(f"[tbank] session saved: {_SESSION_FILE} ({os.path.getsize(_SESSION_FILE)} bytes, 0600)", file=sys.stderr)
    except OSError as e:
        print(f"[tbank] session save failed: {e}", file=sys.stderr)


def _load_session():
    if not os.path.exists(_SESSION_FILE):
        print(f"[tbank] no session file: {_SESSION_FILE}", file=sys.stderr)
        return None
    try:
        d = json.load(open(_SESSION_FILE))
        # keep known non-underscore fields + _minted_at; drop runtime fields (_http,
        # _login_*) and removed fields (e.g. legacy sso_access_token) so an old
        # session.json loads without TypeError.
        fields = MobileSession.__dataclass_fields__
        keep = {k for k in fields if not k.startswith("_")} | {"_minted_at"}
        d = {k: v for k, v in d.items() if k in keep}
        s = MobileSession(**d)
        mode = oct(os.stat(_SESSION_FILE).st_mode & 0o777)
        print(f"[tbank] session loaded: {_SESSION_FILE} ({os.path.getsize(_SESSION_FILE)} bytes, {mode})", file=sys.stderr)
        return s
    except Exception as e:
        print(f"[tbank] session load failed: {e}", file=sys.stderr)
        return None


def _require():
    global _session
    if _session is None:
        _session = _with_persist(_load_session())
    if not _session or not _session.mobile_sessionid:
        raise TbankApiError("NO_SESSION",
            "Call login(phone) first.")
    return _session


def _err(e):
    """The error path for every tool — and therefore the last thing standing between
    a live credential and the model's context.

    The mobile sessionid (the HMAC key for /v1/pay) travels as a QUERY PARAM on every
    request, and requests/urllib3 put the whole URL into the text of ConnectionError,
    MaxRetryError and the HTTPError from raise_for_status(). So a plain network blip —
    no attacker needed — used to publish the session credential into the transcript.
    Redact before returning, on every branch: an API error message can carry a URL too.
    """
    # Tell the tracer this call failed, and with what. Every tool funnels its
    # failures through here, so this one line is what makes the recorded outcome a
    # fact from the error path instead of a guess made by matching the answer string.
    trace.note_error(e)

    def safe(msg):
        # _redact_value (not redact_text): it is JSON-aware — a raw error body
        # dumped whole (e.g. confirm_step's error path) gets its dict structure
        # parsed and redacted by key name, catching a short token or a phone
        # number that redact_text's value-pattern regexes alone would miss.
        return _cut(_redact_value(str(msg)), 300)
    if isinstance(e, SessionExpired):
        return f"SESSION EXPIRED: call refresh_session(). {safe(e.message)}"
    if isinstance(e, TbankApiError):
        return f"API error ({e.result_code}): {safe(e.message)}"
    return f"{type(e).__name__}: {safe(e)}"


def _biggest_list(obj, path=()):
    """The longest list anywhere in a JSON-ish structure, with its path.

    Used to trim the part of a payload that is actually big, instead of slicing the
    serialized text — which cuts inside a token and yields something that looks like
    truncated JSON but parses as nothing."""
    best = (0, None, None)
    if isinstance(obj, list):
        best = (len(obj), obj, path)
    if isinstance(obj, (dict, list)):
        items = obj.items() if isinstance(obj, dict) else enumerate(obj)
        for k, v in items:
            n, lst, p = _biggest_list(v, path + (k,))
            if n > best[0]:
                best = (n, lst, p)
    return best


def _set_in(obj, path, value):
    for step in path[:-1]:
        obj = obj[step]
    obj[path[-1]] = value


def _json_out(data, limit: int = 5000) -> str:
    """Serialize a payload for the agent WITHOUT losing records silently.

    The old code returned json.dumps(...)[:N]. On real data that severs an object
    mid-token: get_data("merchant_subs") serializes to 5871 chars holding 8
    subscriptions, and the 5000-char cut left 6 of them plus half of a seventh. The
    string still looked like data, so the budget skill happily under-reported the
    user's monthly spend with no signal that anything was missing.

    Now: trim lists to whole elements and SAY how many were dropped. If nothing is
    left to trim, cut the text but prefix a marker loud enough that the result cannot
    be mistaken for the whole answer.

    Trimming repeats rather than picking one list once. Two payload shapes made the
    single pass give up and fall through to the character cut, which is the very
    outcome it exists to avoid: several sibling lists of comparable size (shrinking
    the biggest alone never fits), and a payload that IS a list at the top level —
    its path is (), which `_set_in` cannot address. Both are ordinary here: the root
    is trimmed through a holder, and each pass re-picks whatever is biggest now.

    `limit <= 0` means NO cap — the same convention as _rows_out. Without the guard
    a zero fell through every `<= limit` check and returned «ОТВЕТ ОБРЕЗАН: 0 из N»
    with an empty body."""
    full = json.dumps(data, ensure_ascii=False, default=str)
    if limit <= 0 or len(full) <= limit:
        return full

    import copy
    holder = {"_": copy.deepcopy(data)}
    trims: dict[str, list] = {}          # path → [kept, original count]
    body = full
    for _ in range(500):                 # each pass strictly shrinks; a backstop only
        if len(body) <= limit:
            break
        count, lst, path = _biggest_list(holder["_"])
        if not lst or count <= 1:
            break
        keep = count * 3 // 4 if count > 4 else count - 1
        _set_in(holder, ("_",) + path, lst[:keep])
        where = ".".join(str(p) for p in path) or "(корень)"
        trims.setdefault(where, [keep, count])[0] = keep
        body = json.dumps(holder["_"], ensure_ascii=False, default=str)

    if trims and len(body) <= limit:
        what = ", ".join(f"«{w}» {kept} из {total}" for w, (kept, total) in trims.items())
        return (f"# ПОКАЗАНО {what} записей (ответ не помещается целиком). "
                f"Остальные НЕ включены — не считай по этому фрагменту итогов "
                f"и сумм.\n{body}")

    # Nothing addressable left to drop: whole records could not save it.
    text = body if trims else full
    dropped = (" Часть записей уже отброшена целиком, и этого не хватило."
               if trims else "")
    return (f"# ОТВЕТ ОБРЕЗАН: {limit} из {len(text)} символов, и это НЕ валидный "
            f"JSON. Данные неполные — не делай по ним выводов о суммах и "
            f"количестве.{dropped}\n{text[:limit]}")


def _rows_out(rows, render, *, limit: int, total: int, header: str, more_hint: str = "",
              order_note: str = "новые сверху") -> str:
    """Render a list of rows with an honest header.

    list_operations used to print `for o in ops[:50]` with no count and no limit
    argument: a 30-day request returning 229 operations showed the newest 50 — four
    days — presented as a month, with operations 51+ unreachable by any argument.

    `limit <= 0` means EVERYTHING, and every list tool must agree on that: a bare
    `rows[:limit]` reads the same argument as "nothing" and returns an empty answer
    to an agent that asked for the complete one. Going through here is what keeps
    the meaning identical across tools."""
    shown = rows[:limit] if limit > 0 else rows
    head = f"{header}: {total} всего, показано {len(shown)}"
    if len(shown) < total:
        # Not every list is chronological — flights come out cheapest first, and
        # telling the agent otherwise invites it to read the top row as "latest".
        head += (f" ({order_note}). " if order_note else " ")
        head += more_hint or f"Передай limit={total}, чтобы увидеть все."
    return "\n".join([head] + [render(r) for r in shown])


def _cut(s, n: int) -> str:
    """Cut a string for a column, MARKING the cut.

    A bare `s[:40]` is indistinguishable from the full text, so a payment
    description that ends exactly where the merchant name got interesting reads
    as complete. `n <= 0` means no cut at all — same convention as limit in
    _rows_out."""
    s = str(s or "")
    if n <= 0 or len(s) <= n:
        return s
    return s[:n - 1] + "…"


def _store(app_id: str, point_id: str) -> tuple[str, str]:
    """Resolve explicit grocery store context. The agent MUST pass appId/pointId
    taken from grocery_stores() — there is NO silent 578/700 default here.
    Without this, add_to_cart / cart / checkout would silently operate on
    different stores and the cart would look empty ('Корзина пуста')."""
    if not app_id or not point_id:
        raise TbankApiError("NO_STORE_CONTEXT",
            "Передай app_id и point_id из grocery_stores() в КАЖДЫЙ grocery-тул. "
            "Без явного магазина add_to_cart, cart и checkout разъедутся по разным "
            "корзинам — дефолтный магазин вслепую не подставляется.")
    return app_id, point_id


# ── LOGIN ───────────────────────────────────────────────────

@mcp.tool()
def login(phone: str) -> str:
    """Начать логин. Отправляет SMS OTP. Возвращает какой шаг следующий (otp/password/pin)."""
    global _session
    _session = _blank_session()
    try:
        return _session.login(phone)
    except Exception as e:
        return _err(e)

@mcp.tool()
def confirm_otp(otp: str) -> str:
    """Отправить SMS-код."""
    global _session
    if not _session: return "call login(phone) first"
    try:
        _session.confirm_step("otp", otp)
        _save_session(_session)
        return "OK. session active."
    except Exception as e:
        return _err(e)

@mcp.tool()
def confirm_password(password: str) -> str:
    """Отправить пароль аккаунта (первый логин на новом устройстве)."""
    global _session
    if not _session: return "call login(phone) first"
    try:
        _session.confirm_step("password", password)
        _save_session(_session)
        return "OK. session active."
    except Exception as e:
        return _err(e)

@mcp.tool()
def confirm_pin(pin: str) -> str:
    """Отправить PIN (re-auth)."""
    global _session
    if not _session: return "call login(phone) first"
    try:
        _session.confirm_step("pin", pin)
        _save_session(_session)
        return "OK. session active."
    except Exception as e:
        return _err(e)


# ── SESSION ─────────────────────────────────────────────────

@mcp.tool()
def refresh_session() -> str:
    """Обновить сессию. Сначала пробует refresh_token, при invalid_grant —
    silent re-login через SSO_SESSION (без OTP). Если оба пути не работают — REAUTH_REQUIRED."""
    try:
        from . import observability as obs
        s = _require()
        grant = None
        try:
            s.refresh()
            grant = "refresh_token"
        except SessionExpired:
            # refresh_token invalid — try silent re-login via SSO_SESSION
            if s.sso_login_cookie and s.auth_step_fingerprint:
                s.silent_relogin()
                grant = "sso_silent"
            else:
                obs.emit("refresh", grant="none", result="reauth_required", blame="app",
                         error="refresh_token invalid, no SSO_SESSION")
                return "REAUTH_REQUIRED: refresh_token истёк и нет SSO_SESSION. Нужен полный логин (login + OTP + password)."
        _save_session(s)
        obs.emit("refresh", grant=grant, result="ok")
        return "OK. session active."
    except Exception as e:
        try:
            from . import observability as obs
            obs.emit("refresh", result="error", blame="app", error=str(e)[:160])
        except Exception:
            pass
        return _err(e)

@mcp.tool()
def session_status() -> str:
    """Проверить жива ли сессия. Сам поднимает уровень до CLIENT, если окно
    портальной сессии (~11 минут) успело закрыться."""
    try:
        s = _require(); s.ensure_client_session()
        return _json_out(s.session_status(), 1000)
    except Exception as e:
        return _err(e)

@mcp.tool()
def keepalive() -> str:
    """Пинг — продлить сессию."""
    try:
        # _json_out, not str(dict)[:200]: the repr uses single quotes, so an agent
        # that tried to parse the answer got invalid JSON, and the character cut
        # could sever it mid-key.
        return _json_out(_require().keepalive(), 1000)
    except Exception as e:
        return _err(e)


# ── CORE READS ──────────────────────────────────────────────

@mcp.tool()
def list_accounts() -> str:
    """Счета + карты + балансы."""
    try:
        s = _require(); s.ensure_fresh()
        accs = s.list_accounts()
        if not accs:
            return ("Счетов не найдено. Это НЕ ошибка запроса — сессия жива, банк "
                    "вернул пустой список. Проверь session_status().")
        return "\n".join(f"- {a.get('id','?')} | {a.get('accountType','')} | "
            f"{_cut(a.get('name',''), 30)} | {(a.get('moneyAmount') or {}).get('value','?')} "
            f"{((a.get('currency') or {}).get('name','') if isinstance(a.get('currency'),dict) else a.get('currency',''))}"
            for a in accs)
    except Exception as e:
        return _err(e)

@mcp.tool()
def list_operations(account_id: str, days: int = 30, limit: int = 50,
                    desc_len: int = 40) -> str:
    """Операции за период, новые сверху.

    limit — сколько показать (0 = все). В шапке всегда указано, сколько операций
    всего за период, поэтому видно, обрезан ли ответ.
    desc_len — ширина колонки описания (0 = описание целиком). Обрезанное
    описание кончается на «…» — полный текст даст desc_len=0."""
    try:
        s = _require(); s.ensure_fresh()
        start, end = ms_for_period(days)
        ops = s.list_operations(account_id, start, end)
        if not ops:
            return f"[account {account_id}] Операций за {days} дн. нет."
        def when(o):
            # operationTime is {"milliseconds": 1784658904000}, not a string
            t = o.get("operationTime") or o.get("debitingTime") or {}
            ms = t.get("milliseconds") if isinstance(t, dict) else t
            if not ms:
                return "?"
            return datetime.fromtimestamp(ms / 1000).strftime("%d.%m %H:%M")
        def sign(o):
            return "-" if (o.get("type") == "Debit") else "+"
        def render(o):
            return (f"- [{when(o)}] {sign(o)}{(o.get('amount') or {}).get('value','?')} "
                    f"{((o.get('amount') or {}).get('currency') or {}).get('name','')} | "
                    f"{_cut(o.get('description') or '', desc_len)}")
        return _rows_out(ops, render, limit=limit, total=len(ops),
                         header=f"[account {account_id}] операции за {days} дн.")
    except Exception as e:
        return _err(e)

@mcp.tool()
def spending_categories(account_id: str, days: int = 30) -> str:
    """Траты по категориям."""
    try:
        s = _require(); s.ensure_fresh()
        start, end = ms_for_period(days)
        rep = s.spending_categories(account_id, start, end)
        cats = rep["categories"]
        # Digit grouping with spaces, as _money and every other tool here does. A
        # comma group («3,466,386.89») reads as a decimal comma in Russian, which is
        # the worst possible ambiguity to put next to a sum of money.
        def num(v, width=0):
            return f"{v:>{width},.2f}".replace(",", " ")
        lines = [f"Траты за {days} дн.: {num(rep['total_spent'])} {rep['currency']} "
                 f"по {len(cats)} категориям (поступления {num(rep['total_earned'])})"]
        for c in cats:
            lines.append(f"- {_cut(c['category'], 25):25} {num(c['amount'], 12)} {c['share_pct']:5.1f}%")
        if not cats:
            lines.append("(категорий нет — за период не было расходных операций)")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)

@mcp.tool()
def operations_histogram(account_id: str = "", days: int = 30,
                        period: str = "day", group_by: str = "category") -> str:
    """Траты, сгруппированные банком. Возвращает сырой JSON (дерево
    summary + intervals[].aggregated[]); для готовой разбивки по категориям
    бери spending_categories() — он это дерево уже разворачивает.

    Внутренние переводы (между своими счетами) ИСКЛЮЧЕНЫ: эндпоинт всегда
    вызывается с config=allNotInner, как в приложении. Полный список операций,
    включая внутренние, — list_operations().

    В захвате приложения этот эндпоинт вызывался 27 раз и КАЖДЫЙ раз с
    period=«day», group_by=«category» — только эта пара проверена. Любое другое
    значение (в том числе «month») ничем не подтверждено, а на неизвестный enum
    эндпоинт отвечает 400: пробуй осознанно и проверяй ответ."""
    try:
        s = _require(); s.ensure_fresh()
        start, end = ms_for_period(days)
        return _json_out(s.operations_histogram(account_id or None, start, end,
                                                period=period, group_by=group_by), 4000)
    except Exception as e:
        return _err(e)

@mcp.tool()
def get_data(section: str, arg: str = "", days: int = 30, max_chars: int = 5000) -> str:
    """Универсальный getter. section = subscriptions | subscription_bills |
    credit_schedule | credit_rating |
    statements | invoices | templates | contacts | cards | loans | autopayments |
    sbp | offers | gifts | services | bundles | manager | merchant_subs | profile | homes |
    cars | shortcuts | finhealth_total | finhealth_turnover | invest_accounts |
    invest_offers | invest_yield | pension | broker_margin | shared | appointments.

    ⚠️ Счета к оплате лежат в ДВУХ разных местах, и «пусто» в одном не значит, что
    счетов нет:
      invoices           — выставленные счета (e-invoicing). Часто пусто.
      subscription_bills — счета по подпискам на ЖКХ и прочие услуги. Именно здесь
                           обычно и лежит неоплаченная квитанция, вместе с
                           paymentFields, которые нужны pay_bill().
    Проверяй ОБА, прежде чем сказать «неоплаченных счетов нет».

    ТРЁМ секциям НУЖЕН arg — без него тул не вернёт пустоту, а поднимет ошибку:
      providers  — arg = список id через запятую («fns-rf,gibdd-online-rf»).
                   Перечислить все провайдеры этим эндпоинтом нельзя, только найти
                   известные по id.
      requisites — arg = телефон. Обычно вместо этого нужен transfer_sbp_resolve(phone);
                   а реквизиты СВОЕГО счёта — это account_requisites(account_id).
      statements — arg = номер счёта из list_accounts(). days задаёт окно выписки
                   (по умолчанию 30 — раньше это окно было зашито и нигде не
                   упоминалось; другие секции days не принимают).

    max_chars — кап ответа в символах (по умолчанию 5000; 0 = весь JSON без
    обрезки). Обрезка всегда помечена заголовком «ПОКАЗАНО X из Y».

    (invest_portfolio/operations/securities и account_requisites — отдельные тулы.)"""
    try:
        s = _require(); s.ensure_fresh()
        return _json_out(s.get_data(section, arg, days=days), max_chars)
    except Exception as e:
        return _err(e)


# ── GROCERY ─────────────────────────────────────────────────

STORE_SORT_KEYS = {"speed": "etaMin", "price": "deliveryPrice", "min_sum": "minOrderSum"}


def _duration(minutes: float) -> str:
    """A wait in the unit a person would say it in. «через ~1980 мин» is a number
    nobody can read as "tomorrow afternoon"."""
    if minutes < 90:
        return f"через ~{int(minutes)} мин"
    if minutes < 24 * 60:
        return f"через ~{minutes / 60:.0f} ч"
    return f"через ~{minutes / 1440:.0f} дн"


@mcp.tool()
def grocery_stores(sort_by: str = "", order: str = "asc") -> str:
    """Магазины, доступные по адресу пользователя: appId/pointId (нужны всем
    остальным grocery-тулам), окно ближайшей доставки, её цена, минимальная сумма
    заказа и кешбэк.

    Это ИНСТРУМЕНТ, а не политика: без sort_by порядок остаётся тем, что вернул
    банк. Сортируй, только когда пользователь назвал критерий.

    sort_by: speed (быстрее приедет) | price (дешевле доставка) | min_sum (ниже
    минимальная сумма). order: asc | desc.

    «Быстрее» считается по КОНЦУ ближайшего окна — «привезут не позже», — потому
    что банк отдаёт два разных вида слота: «до 15 мин» и «завтра 08:00–11:00», и
    сравнимы они только по этому числу. Магазины, у которых слота нет (или он уже
    прошёл), уходят в КОНЕЦ и при asc, и при desc: «неизвестно» не равно нулю и не
    должно выигрывать запрос «побыстрее»."""
    try:
        s = _require(); s.ensure_fresh()
        key = sort_by.strip().lower()
        if key and key not in STORE_SORT_KEYS:
            return (f"Неизвестное поле сортировки {sort_by!r}. Доступны: "
                    f"{', '.join(STORE_SORT_KEYS)} (или пусто — порядок банка).")
        stores = s.grocery_stores()
        if not stores:
            return ("Магазинов для этого адреса не нашлось — похоже, в этой зоне "
                    "доставки честно нет ни одного ритейлера. Адрес доставки можно "
                    "посмотреть и сменить в приложении Т-Банка (Город).")
        rows = _rank_rows(stores, STORE_SORT_KEYS.get(key, ""), order)

        def render(st):
            eta = st.get("etaMin")
            when = st.get("deliveryWindow") or "срок не указан"
            # A relative window already reads in minutes («до 15 мин») — repeating
            # the number adds nothing. An absolute one reads as a clock time, and
            # «завтра 13:00–16:00» does not say how far away that is.
            speed = (f"{when} ({_duration(eta)})"
                     if eta is not None and not when.endswith("мин") else when)
            return (f"- {st['name']} appId={st['appId']} pointId={st['pointId']} "
                    f"| доставка: {speed}, {_money(st.get('deliveryPrice'), '₽')} "
                    f"| minSum={st.get('minOrderSum', '')} "
                    f"| cashback={st.get('cashback', '')}%")
        body = "\n".join(render(st) for st in rows)
        # Which address this list was built for — with several saved addresses the
        # listing silently meant «the first one» and nothing said so.
        addr = rows[0].get("address") if rows and isinstance(rows[0], dict) else ""
        if addr:
            n_addr = rows[0].get("addressCount") or 1
            head = f"Адрес доставки: {addr}"
            if n_addr > 1:
                head += (f" (в профиле адресов: {n_addr}, используется первый — "
                         f"сменить можно в приложении)")
            return head + "\n" + body
        return body
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_search(query: str, app_id: str = "", point_id: str = "",
                   limit: int = 10) -> str:
    """Поиск товара по названию. app_id/point_id — из grocery_stores() (обязательны).
    Возвращает товары с тегом likely_raw (сырой/готовый).
    limit — сколько показать (0 = все подходящие); в шапке видно, сколько
    нашлось всего и сколько товаров вообще вернула сеть."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        results, matched, fetched = s.grocery_search(query, app_id=app_id,
                                                     point_id=point_id, limit=limit)
        if not results:
            return f"[store appId={app_id} pointId={point_id}]\nНе нашёл '{query}'"
        head = (f"[store appId={app_id} pointId={point_id}] "
                f"«{query}»: показано {len(results)} из {matched} подходящих "
                f"(сеть вернула {fetched})")
        if len(results) < matched:
            head += "; limit=0 — все подходящие"
        body = "\n".join(f"- id={r['id']} | {_cut(r['name'], 40)} | {r['price']}₽ | {r.get('weight','') or '-'} | {'RAW' if r.get('likely_raw') else 'PREP'}"
            for r in results)
        return head + "\n" + body
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_plan_order(ingredients: str, app_id: str = "", point_id: str = "") -> str:
    """Спланировать заказ: для каждого ингредиента ищет (custom_ordered → global).
    ingredients = JSON массив, напр. ["свёкла","говядина","капуста"].
    app_id/point_id — из grocery_stores() (обязательны)."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        plan = s.grocery_plan_order(json.loads(ingredients),
                                    store_app_id=app_id, store_point_id=point_id)
        lines = [f"[store appId={app_id} pointId={point_id}] Total: {plan['total_sum']}₽"]
        for i in plan["items"]:
            # id and weight are what the caller actually needs: without the id every
            # item had to be re-searched by hand before add_to_cart, and without the
            # weight a 30 g single-serving pack is indistinguishable from a real one.
            lines.append(
                f"✓ id={i.get('id','?')} | {i['name'][:38]} | {i['price']}₽"
                f" | {i.get('weight') or '-'}"
                f" | {'RAW' if i.get('likely_raw') else 'PREP'} | {i['source']}")
        if plan["missing"]:
            lines.append(f"MISSING: {', '.join(plan['missing'])}")
        lines.append("Проверь вес/форму позиций перед add_to_cart — id можно "
                     "передавать в grocery_add_to_cart как есть.")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_add_to_cart(items: str, app_id: str = "", point_id: str = "") -> str:
    """Добавить товары в корзину. items = JSON [{id, count}, ...].
    app_id/point_id — из grocery_stores() (обязательны). Запомни их — тот же
    магазин нужен для grocery_cart и grocery_checkout."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        try:
            wanted = json.loads(items)
        except json.JSONDecodeError as e:
            return (f"[store appId={app_id} pointId={point_id}] items — это JSON-строка "
                    f"вида [{{\"id\": \"123\", \"count\": 1}}], разобрать не удалось: {e}")
        r = s.grocery_add_to_cart(wanted, app_id=app_id, point_id=point_id)
        pl = r if isinstance(r, dict) else {}
        # Every successful cart/set in the capture returns payload.goodsSum. Its
        # absence means the backend did not accept the cart, so do NOT report OK —
        # that is what previously produced "OK: goodsSum=?" on a rejected write.
        if "goodsSum" not in pl:
            return (f"[store appId={app_id} pointId={point_id}] ОШИБКА: бэкенд не принял "
                    f"корзину (в ответе нет goodsSum). Товары НЕ добавлены. Ответ: {str(pl)[:300]}")
        # The write had to escalate to SINGLE_CART_WITH_OTHER_CART_RESET, which is
        # what the backend demands once another retailer has a cart — and it wipes
        # that cart. Silently succeeding here would lose someone's basket without
        # a word, so say it.
        reset = ("\n⚠️ Корзины ДРУГИХ магазинов при этом очищены — бэкенд не даёт "
                 "держать две сразу. Если там что-то лежало, оно потеряно."
                 if pl.get("otherCartsReset") else "")
        # Report what the CART holds, not what the caller sent — the two used to be
        # assumed equal, so a write that stored nothing still printed the input count.
        try:
            in_cart = len(s.grocery_cart_goods(app_id=app_id, point_id=point_id))
            held = f"в корзине {in_cart} позиций"
        except Exception:                                   # noqa: BLE001
            held = f"добавлено {len(wanted)} позиций"
        return (f"[store appId={app_id} pointId={point_id}] OK: goodsSum={pl['goodsSum']}"
                f" ({held}){reset}")
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_set_cart(items: str = "[]", app_id: str = "", point_id: str = "",
                     clear: bool = False) -> str:
    """Изменить или убрать товары в корзине. Считает количества АБСОЛЮТНО, в отличие
    от grocery_add_to_cart, который прибавляет.

    items = JSON [{"id": "123", "count": 2}, ...]:
      count > 0 — сделать ровно столько (не прибавить);
      count = 0 — убрать товар из корзины;
      товары, которых нет в списке, остаются как были.
    clear=True — очистить корзину целиком, items тогда не нужен.

    Отдельного эндпоинта удаления у банка нет: корзина всегда перезаписывается
    целиком, поэтому тул сам дочитывает текущий состав и шлёт полный список.
    Возвращает содержимое корзины ПОСЛЕ изменения — сверь его с ожидаемым."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        parsed = json.loads(items) if items else []
        if not clear and not parsed:
            return ("Нечего менять: передай items вида "
                    "[{\"id\": \"123\", \"count\": 0}] или clear=True.")
        r = s.grocery_set_cart(parsed, app_id=app_id, point_id=point_id, clear=clear)
        pl = r if isinstance(r, dict) else {}
        if "goodsSum" not in pl:
            return (f"[store appId={app_id} pointId={point_id}] ОШИБКА: бэкенд не принял "
                    f"корзину (в ответе нет goodsSum). Ничего НЕ изменено. "
                    f"Ответ: {str(pl)[:300]}")
        goods = s.grocery_cart_goods(app_id=app_id, point_id=point_id)
        head = (f"[store appId={app_id} pointId={point_id}] "
                f"{'корзина очищена' if clear else 'корзина обновлена'}: "
                f"{len(goods)} позиций, goodsSum={pl['goodsSum']}")
        if pl.get("otherCartsReset"):
            head += ("\n⚠️ Корзины ДРУГИХ магазинов очищены — бэкенд не даёт держать "
                     "две сразу. Если там что-то лежало, оно потеряно.")
        rows = [f"- {g.get('name','?')[:40]} ×{g.get('count','?')} | id={g.get('id','?')}"
                for g in goods]
        return "\n".join([head] + rows)
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_cart(app_id: str = "", point_id: str = "") -> str:
    """Содержимое корзины. app_id/point_id — из grocery_stores() (обязательны) и
    должны совпадать с теми, что использовались в grocery_add_to_cart."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        r = s.grocery_cart_get(app_id=app_id, point_id=point_id)
        env = r if isinstance(r, dict) else {}
        cart = env.get("cart", env) if isinstance(env.get("cart"), dict) else env
        goods = cart.get("goods", []) if isinstance(cart, dict) else []
        # defensive context check: if the response echoes a DIFFERENT store than
        # requested, flag it instead of silently showing an empty cart. The store id
        # lives at payload.application.id — the cart object itself carries no appId.
        resp_app = str((env.get("application") or {}).get("id") or "")
        resp_point = str((cart.get("delivery", {}) or {}).get("pointId")
                         or cart.get("pointId") or "")
        mismatch = ""
        if resp_app and resp_app != str(app_id):
            mismatch = f"  ⚠ CART_CONTEXT_MISMATCH: ответ appId={resp_app} ≠ запрошенный {app_id}\n"
        elif resp_point and resp_point != str(point_id):
            mismatch = f"  ⚠ CART_CONTEXT_MISMATCH: ответ pointId={resp_point} ≠ запрошенный {point_id}\n"
        # id first: grocery_set_cart addresses goods BY ID, and this is the only tool
        # that lists what is in the cart. Without it the agent could read the cart and
        # still have no way to change one line of it.
        body = "\n".join(
            f"- id={g.get('id','?')} | {(g.get('name') or '')[:35]} "
            f"| x{g.get('count', 1)} | {(g.get('price') or {}).get('value', '?')}₽ "
            f"| {g.get('weight', '') or g.get('quant', '') or '-'}"
            for g in goods) or "Корзина пуста"
        return f"[store appId={app_id} pointId={point_id}]\n{mismatch}{body}"
    except Exception as e:
        return _err(e)

@mcp.tool()
async def grocery_checkout(app_id: str = "", point_id: str = "", force: bool = False,
                           account_id: str = "", expected_sum: float = 0) -> str:
    """Полный чекаут: доставка → заказ → оплата. РЕАЛЬНЫЕ ДЕНЬГИ.
    app_id/point_id — из grocery_stores() (обязательны, тот же магазин что в корзине).

    СЧЁТ СПИСАНИЯ по умолчанию — тот, которым пользователь последний раз платил
    за продукты В ПРИЛОЖЕНИИ (банк отдаёт его сам), а НЕ первый счёт с балансом.
    Хочешь другой — передай account_id из list_accounts(). Списанный счёт печатается
    в ответе.

    expected_sum — сумма, которую подтвердил пользователь. Передавай её ВСЕГДА:
    после подтверждения банк дважды пересчитывает корзину (веб-корзина, затем
    доставка — весовые товары меняются), и без этого параметра оплатится новая
    сумма молча. Расхождение больше 0.01 ₽ отменяет чекаут ДО создания заказа.

    При неопределённом результате (заказ мог создаться) повтор БЛОКИРУЕТСЯ —
    сначала grocery_attempts() и проверь заказ в приложении. force=True — только если
    пользователь ЯВНО подтвердил, что прошлого заказа нет. Всегда показывай состав и
    сумму и жди явного подтверждения перед вызовом.

    Реализация: тул асинхронный и запускает браузер Playwright в отдельном worker-потоке
    (asyncio.to_thread) — sync_playwright падает, если звать его внутри event-loop, а
    FastMCP крутит sync-тулы именно в loop. Если тул падает с Playwright-ошибкой —
    проверь `python -m playwright install chromium` (в окружении MCP)."""
    try:
        return await asyncio.to_thread(_do_grocery_checkout, app_id, point_id, force,
                                       account_id, expected_sum)
    except Exception as e:
        # errors before an attempt was created (NO_SESSION, NO_STORE_CONTEXT, etc.)
        return _err(e)


def _do_grocery_checkout(app_id: str, point_id: str, force: bool,
                         account_id: str = "", expected_sum: float = 0) -> str:
    """Sync checkout body — runs in a worker thread (no running event loop there, so
    Playwright's sync API works; calling it directly in FastMCP's loop raises
    "It looks like you are using Playwright Sync API inside the asyncio loop")."""
    from . import journal
    from . import observability as obs
    from .checkout import CheckoutError, CheckoutUnknown
    with _CHECKOUT_LOCK:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        # 1. read the cart → goods + amount + a stable cart hash
        cart_raw = s.grocery_cart_get(app_id=app_id, point_id=point_id)
        cart = cart_raw.get("cart", cart_raw) if isinstance(cart_raw, dict) else {}
        goods = cart.get("goods", []) if isinstance(cart, dict) else []
        if not goods:
            return f"[store appId={app_id} pointId={point_id}] Корзина пуста — не из чего оформлять заказ."
        amount = cart.get("goodsSum", 0) or cart.get("sum", 0) or 0
        chash = journal.cart_hash_of(goods)
        # 2. block-check: a blocking prior attempt for THIS cart → no auto-retry (#10)
        blocked, last = journal.is_retry_blocked(chash)
        if blocked and not force:
            return (f"[store appId={app_id} pointId={point_id}] BLOCKED: предыдущая попытка checkout для "
                    f"этой корзины завершилась неопределённо (status={last.get('status')}, "
                    f"attempt={last.get('attempt_id')}, order={last.get('order_id') or '-'}). Заказ мог быть "
                    f"создан/оплачен — сначала grocery_attempts() и проверь заказ в приложении. "
                    f"Принудительный повтор (force=True) — только если пользователь подтвердил отсутствие заказа.")
        # 3. new journal attempt + run the web checkout. checkout resolves the payment
        #    agreement from user/payment/account/last (capture-verified) + customer email
        #    from get-customer-information. #8/#9
        attempt_id = journal.new_attempt(app_id, point_id, chash, amount)
        obs.emit("checkout_start", attempt_id=attempt_id, app_id=app_id,
                 point_id=point_id, amount=amount, item_count=len(goods))
        try:
            r = s.grocery_checkout(app_id=app_id, point_id=point_id,
                                   sum_val=amount, attempt_id=attempt_id,
                                   account=account_id, expected_sum=expected_sum)
            # Name the debited account, like ticket_pay does — "PAID" without saying
            # from where is the one fact the user cannot check afterwards from here.
            paid_from = r.get("account") or account_id
            return (f"[store appId={app_id} pointId={point_id}] ✓ ORDER {r['order_id']} PAID. "
                    f"sum={r['sum']}₽"
                    + (f" со счёта {paid_from}" if paid_from else "")
                    + f" (attempt {attempt_id})")
        except CheckoutUnknown as e:
            return (f"[store appId={app_id} pointId={point_id}] UNKNOWN RESULT (attempt {attempt_id}): {e} "
                    f"Повтор ЗАБЛОКИРОВАН — заказ мог создаться. Проверь grocery_attempts() / grocery_order_status() и заказ в приложении.")
        except CheckoutError as e:
            journal.record(attempt_id, "checkout", "failed", error=str(e)[:160])
            obs.emit("checkout", attempt_id=attempt_id, result="failed",
                     error=str(e)[:160], blame="client")
            return _err(e)
        except Exception as e:
            # A runtime error checkout.py did NOT classify as CheckoutError/Unknown.
            # Historically this was the Playwright "Sync API inside asyncio loop" Error
            # (now impossible: we run in a worker thread), but it also covers any other
            # crash. Decide retry-safety by how far we got: if we already passed the
            # order/create point-of-no-return (last status blocking) → UNKNOWN, else
            # FAILED (safe). Without this, the attempt stayed at `started` and a retry
            # was wrongly allowed even if it crashed mid-order/create.
            # redact_text BEFORE the message reaches the UNKNOWN answer below:
            # obs.emit and journal.record scrub their own copies, but this string
            # goes verbatim into the tool result — a ConnectionError carries the
            # full URL, sessionid included.
            err_msg = f"{type(e).__name__}: {_cut(redact_text(str(e)), 120)}"
            already_blocking = journal.last_status_of_attempt(attempt_id) in journal.BLOCKING_STATUSES
            obs.emit("checkout", attempt_id=attempt_id,
                     result="unknown" if already_blocking else "failed",
                     error=err_msg, blame="client")
            if already_blocking:
                journal.record(attempt_id, "checkout", "unknown", error=err_msg)
                return (f"[store appId={app_id} pointId={point_id}] UNKNOWN RESULT (attempt {attempt_id}, "
                        f"runtime {type(e).__name__}). Дошли до order/create? Повтор ЗАБЛОКИРОВАН — "
                        f"проверь grocery_attempts()/diagnostics() и заказ в приложении. ({err_msg})")
            journal.record(attempt_id, "checkout", "failed", error=err_msg)
            return _err(e)

@mcp.tool()
def grocery_attempts(limit: int = 15) -> str:
    """Недавние попытки grocery checkout (read-only) — для reconciliation после
    неопределённого результата (UNKNOWN). Показывает status/order_id/attempt_id/sum.
    limit — сколько последних попыток показать (0 = все); в шапке видно общее число."""
    try:
        from . import journal
        rows = journal.recent(0)
        if not rows:
            return "Попыток checkout пока не было."
        # One row per ATTEMPT, not per journal line. The journal writes an `init`
        # record carrying app_id/amount and then a progress record per step carrying
        # only what that step knew — so printing the raw tail showed «appId=None ?₽»
        # for every step after the first, i.e. for almost every line.
        merged: dict = {}
        for r in rows:
            aid = r.get("attempt_id")
            if not aid:
                continue
            cur = merged.setdefault(aid, {"attempt_id": aid})
            cur.update({k: v for k, v in r.items() if v not in (None, "")})
        def render(a):
            bits = [f"- {a['attempt_id']}", a.get("status", "?"), a.get("step", "?")]
            if a.get("app_id"):
                bits.append(f"appId={a['app_id']}")
            if a.get("amount") not in (None, ""):
                bits.append(f"{a['amount']}₽")
            bits.append(f"order={a.get('order_id') or '-'}")
            tail = (a.get("error") or a.get("payment_status") or "")
            if tail:
                bits.append(_cut(tail, 60))
            return " | ".join(bits)
        # Newest first, through _rows_out: the bare [-15:] tail printed 15 attempts
        # with no count — indistinguishable from «15 attempts ever».
        items = list(merged.values())[::-1]
        return _rows_out(items, render, limit=limit, total=len(items),
                         header="Попытки checkout")
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_order_status(order_id: str, app_id: str = "") -> str:
    """Reconciliation: статус grocery-заказа по orderId (GET /api/grocery/order).
    Read-only. Проверь после UNKNOWN checkout, создался/оплатился ли заказ на бэкенде."""
    try:
        s = _require(); s.ensure_fresh()
        r = s.grocery_order_get(order_id=order_id, app_id=app_id)
        payload = r.get("payload", r) if isinstance(r, dict) else {}
        order = payload.get("order", payload) if isinstance(payload, dict) else {}
        if not isinstance(order, dict):
            order = {}
        # Real schema (capture item 691, a genuinely placed+paid order):
        # order.{id,status,paymentId,application{id,name},cart{sum,goodsSum,goods}}.
        # There is NO paymentInfo and no top-level sum — reading those made every
        # order look unpaid with an unknown sum. Payment is evidenced by paymentId,
        # and CREATED_DYNAMIC is the NORMAL status of a placed order, not a failure.
        cart = order.get("cart") or {}
        app = order.get("application") or {}
        status = order.get("status") or "?"
        summ = cart.get("sum") or cart.get("goodsSum") or "?"
        pay_id = order.get("paymentId") or ""
        return (f"order={order_id} | status={status} | sum={summ} | "
                f"app={app.get('name') or app.get('id') or '-'} | "
                f"paymentId={pay_id or '-'} | paid={'yes' if pay_id else 'no payment id'}")
    except Exception as e:
        return _err(e)


@mcp.tool()
def grocery_order_cancel(order_id: str, app_id: str = "") -> str:
    """Отменить продуктовый заказ (Город) — оплаченный или ещё нет. Деньги за
    оплаченный возвращаются на счёт списания.

    paymentId НЕ нужен (в отличие от ticket_cancel): приложение отменяет по
    одному orderId. Вердикт — payload.status ("Success"/"Failed" + code;
    605 = заказ уже отменён), внешний "status":"Ok" успехом НЕ является.

    app_id (из grocery_stores() или grocery_attempts()) не обязателен, но с ним
    тул сразу перечитает заказ и покажет фактический статус — до перечитывания
    «принято» ещё не значит CANCELED. Если тул вернул ошибку, статус заказа
    НЕИЗВЕСТЕН — grocery_order_status() или приложение."""
    try:
        s = _require(); s.ensure_fresh()
        res = s.cancel_grocery_order(order_id)
        st = str((res or {}).get("status") or "")
        code = str((res or {}).get("code") or "")
        if st.lower() == "success":
            head = f"Отмена заказа {order_id} принята (status=Success)."
        elif code == "605":
            head = f"Заказ {order_id} уже отменён (code=605) — делать ничего не нужно."
        else:
            head = (f"Отмена заказа {order_id} НЕ подтверждена: status={st or '?'}"
                    + (f", code={code}" if code else "")
                    + " — проверь grocery_order_status() и заказ в приложении.")
        if app_id:
            try:
                r = s.grocery_order_get(order_id=order_id, app_id=app_id)
                payload = r.get("payload", r) if isinstance(r, dict) else {}
                order = payload.get("order", payload) if isinstance(payload, dict) else {}
                head += f"\nПерепроверка: status={order.get('status') or '?'}"
            except Exception:
                head += "\nПерепроверка не удалась — grocery_order_status()."
        return head
    except Exception as e:
        return (_err(e) + f"\nСтатус заказа {order_id} НЕИЗВЕСТЕН — "
                "grocery_order_status() или приложение.")


# ── DIAGNOSTICS ─────────────────────────────────────────────

@mcp.tool()
def diagnostics(limit: int = 40) -> str:
    """Недавние redacted-события (checkout delivery/order/payment + refresh сессии)
    для диагностики — БЕЗ секретов. reconstruct попытку / найти последний
    подтверждённый шаг. Источник: ~/.local/share/tbank-mcp/events.jsonl."""
    try:
        from . import observability as obs
        rows = obs.recent(limit)
        if not rows:
            return "Событий пока нет (events.jsonl пуст)."
        lines = []
        for r in rows:
            parts = [f"step={r.get('step')}", f"blame={r.get('blame', '-')}"]
            if r.get("attempt_id"):
                parts.append(f"attempt={r.get('attempt_id')}")
            if r.get("app_id"):
                parts.append(f"appId={r.get('app_id')}")
            if "http_status" in r:
                parts.append(f"http={r.get('http_status')}")
            if r.get("app_code"):
                parts.append(f"code={r.get('app_code')}")
            if "amount" in r:
                parts.append(f"sum={r.get('amount')}")
            if "order_id_present" in r:
                parts.append(f"order={'Y' if r.get('order_id_present') else 'N'}")
            if "payment_id_present" in r:
                parts.append(f"payId={'Y' if r.get('payment_id_present') else 'N'}")
            if r.get("cart_set_mode"):
                parts.append(f"mode={r.get('cart_set_mode')}")
            if r.get("item_count") is not None:
                parts.append(f"items={r.get('item_count')}")
            if r.get("duration_ms") is not None:
                parts.append(f"{r.get('duration_ms')}ms")
            if r.get("result"):
                parts.append(f"result={r.get('result')}")
            if r.get("payment_status"):
                parts.append(f"payStatus={r.get('payment_status')}")
            if r.get("error"):
                parts.append(f"err={str(r.get('error'))[:50]}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


@mcp.tool()
def debug_report(runs: int = 0, top: int = 6) -> str:
    """Как этим MCP пользовались: какие тулы звали, в каком порядке, что получили
    в ответ и где застряли. Для отладки самого MCP, не для банковских задач.

    Пишется автоматически при каждом вызове любого тула (выключается TBANK_TRACE=0).
    Секретов и свободного текста в трассе нет — см. src/trace.py.

    runs — сколько последних запусков сервера взять (0 = все, что есть в файле).
    top — сколько строк показывать в каждом разделе.

    Что смотреть:
      «повторы» — один и тот же тул с теми же аргументами подряд. Агент не понял
        ответ. Это самый прямой указатель на плохую формулировку в докстринге.
      «ответы» — реальные первые строки, которые агент прочитал, с частотой.
        Отказы и «ничего не найдено» тут видно вперемешку с успехами — намеренно:
        решать, что из этого проблема, должен человек, а не таблица строк в коде.
      «переходы» — какой тул за каким. Расходится с флоу в скиле — значит скил
        читается не так, как написан."""
    try:
        rows = trace.load(runs=runs)
        if not rows:
            return (f"Трасса пуста ({trace.TRACE_FILE}). "
                    f"{'Она выключена: TBANK_TRACE=0.' if not trace.enabled() else ''}"
                    " Вызовы записываются начиная со следующего.")
        rep = trace.report(rows, top=top)
        out = [f"{rep['calls']} вызовов за {rep['runs']} запусков сервера "
               f"({trace.TRACE_FILE})", "", "ТУЛЫ (вызовы/ошибки, задержка, ответ):"]
        for t in rep["tools"][:max(top * 3, 12)]:
            out.append(f"- {t['tool']:24} n={t['n']:<4} err={t['err']:<3} "
                       f"p50={t['p50_ms']}ms p95={t['p95_ms']}ms ~{t['avg_chars']} симв.")
            for head, n in t["answers"]:
                out.append(f"      {n:>3}× {head[:110]}")
        if rep["repeats"]:
            out += ["", "ПОВТОРЫ (тот же тул, те же аргументы, подряд):"]
            out += [f"- {r['tool']} ×{r['times']} — {r['head'][:90]}"
                    for r in rep["repeats"]]
        if rep["transitions"]:
            out += ["", "ПЕРЕХОДЫ:"]
            out += [f"- {a} → {b}  ×{n}" for (a, b), n in rep["transitions"]]
        if rep["starts"]:
            out += ["", "С ЧЕГО НАЧИНАЛИ: " +
                    ", ".join(f"{t}×{n}" for t, n in rep["starts"][:top])]
        return "\n".join(out)
    except Exception as e:
        return _err(e)


# ── MESSENGER ───────────────────────────────────────────────

@mcp.tool()
def messenger_conversations(archived: bool = False, offset: int = 0) -> str:
    """Список чатов (одна страница банка).

    offset — с какого чата начать (следующая страница: offset из подсказки в
    шапке ответа). archived=True — архивные чаты."""
    try:
        s = _require(); s.ensure_fresh()
        convs = s.messenger_conversations(archived=archived, offset=offset)
        lines = []
        for c in convs:
            if not isinstance(c, dict):
                continue
            # the id MUST be complete — it is the argument to messenger_messages,
            # and it used to be cut to 24 chars with an ellipsis, so the listing
            # could not be acted on at all. Bot chats carry no title; their name is
            # the member's.
            members = c.get("members") or []
            name = (c.get("title")
                    or (members[0].get("name") if members and isinstance(members[0], dict) else "")
                    or (c.get("botInfo") or {}).get("login") or "?")
            unread = c.get("unreadMessagesCount") or 0
            last = ((c.get("message") or {}).get("content") or {}).get("text") or ""
            lines.append(
                f"- {name} | id={c.get('conversationId','')}"
                f"{f' | непрочитано: {unread}' if unread else ''}"
                f" | {(c.get('updatedAt') or '')[:16]}"
                f"{' | ' + _cut(' '.join(last.split()), 60) if last else ''}")
        if not lines:
            if offset > 0:
                return f"Больше чатов нет (offset={offset})."
            return "Чатов нет. Это не ошибка — мессенджер ответил пустым списком."
        # The header must not contain «id=» — the listing promises exactly one id
        # per chat row, and tooling greps them out by that marker.
        head = (f"Чаты: {len(lines)} (offset={offset}). Нет нужного — "
                f"offset={offset + len(lines)}; архивные — archived=True.")
        return "\n".join([head] + lines)
    except Exception as e:
        return _err(e)

@mcp.tool()
def messenger_messages(conversation_id: str, limit: int = 20, offset: int = 0,
                       max_chars: int = 400) -> str:
    """История чата, старые сверху.

    Банк отдаёт одну страницу истории; параметры листают её ЛОКАЛЬНО:
      limit     — сколько сообщений показать (0 = вся страница);
      offset    — сколько САМЫХ НОВЫХ пропустить (окно старее: offset=20, 40, …);
      max_chars — кап текста одного сообщения (0 = целиком). Обрезка всегда
                  помечена и называет полную длину."""
    try:
        s = _require(); s.ensure_fresh()
        msgs = s.messenger_messages(conversation_id)
        # oldest→newest, and keep the author and time: a bare list of 60-char text
        # fragments loses who said what, which is most of the meaning in a chat.
        msgs = sorted((m for m in msgs if isinstance(m, dict)),
                      key=lambda m: m.get("timestamp") or "")
        if not msgs:
            return "Сообщений нет."
        end = max(0, len(msgs) - max(0, offset))
        start = 0 if limit <= 0 else max(0, end - limit)
        window = msgs[start:end]
        head = (f"Чат {conversation_id}: {len(msgs)} сообщений на странице банка, "
                f"показано {len(window)} (старые выше).")
        if start > 0:
            head += f" Старее: offset={offset + len(window)}; limit=0 — вся страница."
        elif offset > 0:
            head += " Это самые старые сообщения, которые отдал банк."
        if not window:
            return head
        out = [head]
        for m in window:
            a = m.get("author") or {}
            who = a.get("name") or ("Вы" if a.get("role") == "client" else "?")
            text = " ".join(((m.get("content") or {}).get("text") or "").split())
            if not text:
                text = f"[{m.get('messageType') or 'вложение'}]"
            elif max_chars > 0 and len(text) > max_chars:
                # Not _cut: a chat message deserves more than a bare «…» — say how
                # much is missing and how to get it. This is the exact spot that
                # used to swallow the tail of long bank messages silently.
                text = (text[:max_chars]
                        + f"…[обрезано, всего {len(text)} симв.; "
                          f"max_chars=0 покажет целиком]")
            out.append(f"- [{(m.get('timestamp') or '')[:16].replace('T', ' ')}] "
                       f"{who}: {text}")
        return "\n".join(out)
    except Exception as e:
        return _err(e)

@mcp.tool()
def messenger_send(conversation_id: str, text: str) -> str:
    """Отправить сообщение в чат — НЕОБРАТИМО, его прочитает живой человек
    (обычно поддержка банка). Денег не двигает, но и отозвать нельзя.

    Покажи пользователю текст и дождись согласия, прежде чем отправлять.
    conversation_id — из messenger_conversations()."""
    try:
        s = _require(); s.ensure_fresh()
        res = s.messenger_send(conversation_id, text) or {}
        # Echoing the argument back would say "sent" even if the API answered with
        # an error envelope. Report what the server acknowledged.
        mid = ""
        if isinstance(res, dict):
            payload = res.get("payload") if isinstance(res.get("payload"), dict) else res
            mid = str(payload.get("id") or payload.get("messageId") or "")
        return (f"Отправлено в чат {conversation_id}"
                + (f", id сообщения {mid}" if mid else " (банк не вернул id сообщения)")
                + f": «{text[:80]}»")
    except Exception as e:
        return _err(e)

@mcp.tool()
def messenger_unread() -> str:
    """Чаты с непрочитанными сообщениями (по названиям, а не по сырым id)."""
    try:
        s = _require(); s.ensure_fresh()
        data = s.messenger_unread()
        ids = data.get("conversationIds") or []
        if not ids:
            return "Непрочитанных сообщений нет."
        # the endpoint returns bare ids — resolve them to chat names. An unread
        # chat outside the first page used to print «(чат без названия)»; walk up
        # to 3 pages, stopping as soon as every id has a name (request economy:
        # one page resolves everything in the common case).
        names = {}
        try:
            off = 0
            for _ in range(3):
                page = s.messenger_conversations(offset=off)
                if not page:
                    break
                for c in page:
                    if not isinstance(c, dict):
                        continue
                    cid = str(c.get("id") or c.get("conversationId") or "")
                    members = c.get("members") or []
                    title = (c.get("title") or c.get("name")
                             or (members[0].get("name") if members else "") or "")
                    if cid:
                        names[cid] = title
                off += len(page)
                if all(str(i) in names for i in ids):
                    break
        except (KeyError, TypeError, AttributeError):
            # a shape surprise in the resolve loop only costs chat names, not the
            # unread count itself — but a real session/auth error (e.g.
            # SessionExpired) must still propagate to the outer handler below,
            # not be reported as "(чат без названия)".
            pass
        lines = [f"Непрочитано в {len(ids)} чатах:"]
        for cid in ids:
            lines.append(f"- {names.get(cid) or '(чат без названия)'} | id={cid}")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


# ── MONEY ───────────────────────────────────────────────────

@mcp.tool()
def transfer_sbp_resolve(phone: str) -> str:
    """Резолвинг получателя СБП по номеру (read-only, БЕЗ денег). Возвращает банки
    получателя (маскированное имя + банк + isDefaultBank) и готовый provider_fields.
    Используй ПЕРЕД transfer()/payment_commission() для НОВОГО (несохранённого)
    получателя. provider_fields вставь в payParameters.providerFields комиссии — не
    пиши 8276 руками. Для transfer() передай bank_member_id+pointer_link_id (или
    ничего — выберется дефолт); при нескольких банках без дефолта нужен явный выбор."""
    try:
        s = _require(); s.ensure_fresh()
        cands = s.resolve_sbp_recipient(phone)
        if not cands:
            return f"{phone}: получатель не зарегистрирован в СБП (или неверный номер)."
        lines = [f"{phone}: найдено банков СБП — {len(cands)}"]
        for c in cands:
            star = " ★ДЕФОЛТ" if c["is_default_bank"] else ""
            lines.append(f"- {c['masked_fio']} | {c['bank_name']}{star}")
            lines.append(f"  providerFields: {json.dumps(c['provider_fields'], ensure_ascii=False)}")
        lines.append("payment_commission: вставь providerFields в payParameters.providerFields "
                     "(account/moneyAmount/currency/paymentType добавь сам). "
                     "transfer: передай bank_member_id + pointer_link_id, или ничего (дефолт).")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)

def _transfer_key(amount, to_account, provider, from_account) -> str:
    """Identity of a LOGICAL transfer, for the duplicate guard."""
    import hashlib
    raw = f"{provider}|{to_account}|{amount}|{from_account}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Only an UNCONFIRMED outcome blocks a repeat. journal._BLOCKING also contains
# "paid", which is right for a grocery cart — the same cart paid twice is a
# duplicate order — but wrong here: sending the same person the same amount twice
# is an ordinary thing to do, and refusing it would be the tool inventing a rule
# the user never asked for.
_TRANSFER_BLOCKING = {"posting", "unknown"}


def _transfer_blocked(key: str):
    from . import journal
    last = journal.latest_for_cart(key)
    if last and last.get("status") in _TRANSFER_BLOCKING:
        return True, last
    return False, last


@mcp.tool()
def transfer(amount: float, to_account: str, description: str = "",
             provider: str = "p2p-anybank", bank_member_id: str = "",
             masked_fio: str = "", pointer_link_id: str = "",
             from_account: str = "", force: bool = False) -> str:
    """Перевод (РЕАЛЬНЫЕ ДЕНЬГИ — подтверди с пользователем конкретную сумму и получателя).

    from_account — счёт списания из list_accounts(). Пусто = первый рублёвый Current
    с положительным балансом; это ДОГАДКА, поэтому если пользователь выбирал счёт —
    передай его явно, иначе спишется с другого.
    phone/СБП (по умолчанию): to_account=телефон. Если bank_member_id/masked_fio/
    pointer_link_id не переданы — получатель резолвится АВТОМАТИЧЕСКИ
    (transfer_sbp_resolve): выберется дефолтный банк; при нескольких банках без
    дефолта вернётся RECIPIENT_MULTIPLE_BANKS со списком.
    Между своими счетами (provider='transfer-inner') НЕ реализовано — тело платежа
    не сверено с реальным перехватом трафика; переводи между своими счетами в
    приложении.
    description — сообщение получателю.
    force=True — повторить перевод, который уже помечен как незавершённый. Только
    после того, как пользователь ПРОВЕРИЛ в приложении, что деньги не ушли.

    Возвращает paymentId — по нему потом payment_receipt(). Больше его взять негде."""
    try:
        import time
        from . import journal
        s = _require(); s.ensure_fresh()
        src = from_account or s._source_account()
        key = _transfer_key(amount, to_account, provider, src)

        blocked, prev = _transfer_blocked(key)
        if blocked and not force:
            return (f"ПОВТОР ЗАБЛОКИРОВАН: такой же перевод ({amount}₽ → {to_account}) "
                    f"уже отправлялся и его исход НЕ подтверждён "
                    f"(шаг «{(prev or {}).get('step','?')}», статус "
                    f"«{(prev or {}).get('status','?')}»). Деньги могли уйти. "
                    f"Проверь операции в приложении или list_operations('{src}', days=1). "
                    f"Если перевода нет — повтори с force=True.")

        # The client-generated payment id is what makes a RETRY idempotent: reusing
        # the previous one lets the bank recognise the repeat instead of creating a
        # second payment.
        #
        # It must therefore be reused ONLY when this really is a retry of an
        # unconfirmed attempt. `prev` is the last journal event for this key whatever
        # its status, so reusing it unconditionally meant a deliberate second identical
        # transfer — rent, a repeat top-up — carried the FIRST payment's id and the bank
        # deduplicated it away: the tool reported success and no money moved. That is
        # the mirror image of the bug this scheme exists to prevent, and it defeated
        # the choice one screen above to let a confirmed transfer through unblocked.
        #
        # Stored as an INT, deliberately. The journal redacts by value pattern, and a
        # 13-digit millisecond timestamp matches the card-number rule — as a string it
        # came back as "<card>" and would have been sent to the bank verbatim.
        # Non-string values pass through redaction untouched, and this id is not a
        # secret: its entire purpose is to be reused on a retry.
        retry_of = prev if (prev or {}).get("status") in _TRANSFER_BLOCKING else None
        upid = str(int((retry_of or {}).get("user_payment_ms") or 0)
                   or int(time.time() * 1000))
        # The recipient is masked before it reaches the journal. new_attempt's second
        # slot is a plain field and the redactor works by key NAME and by value
        # pattern — neither catches a bare 11-digit phone — so passing to_account
        # verbatim wrote it in cleartext, against journal.py's own promise never to
        # store phones or account numbers. `key` is already a hash of the full
        # identity, so nothing is lost for the duplicate check.
        masked_to = (to_account[:2] + "•" * max(0, len(to_account) - 6) + to_account[-4:]
                     if len(to_account) > 6 else "•" * len(to_account))
        attempt = journal.new_attempt(provider, masked_to, key, amount)
        journal.record(attempt, "pay", "posting", user_payment_ms=int(upid), account=src)

        try:
            res = s.transfer(amount, to_account, description, provider=provider,
                             bank_member_id=bank_member_id, masked_fio=masked_fio,
                             pointer_link_id=pointer_link_id, account=src,
                             user_payment_id=upid) or {}
        except Exception as e:
            # Not every failure is an unknown outcome, and saying so has a cost: it
            # tells the user their money may have moved and it BLOCKS the next
            # attempt. Two cases are definitely not that:
            #   * the client refused before sending — an unresolved recipient, several
            #     SBP banks with no default, a malformed phone. Nothing left here.
            #   * the bank answered with an error envelope. The request completed and
            #     was rejected, so no money moved.
            # Only a transport failure — timeout, reset, DNS — leaves the result
            # genuinely unknown, because the POST may have arrived.
            import requests as _rq
            answered_or_refused = isinstance(e, (TbankApiError, SessionExpired))
            if answered_or_refused and not isinstance(e, _rq.exceptions.RequestException):
                journal.record(attempt, "pay", "failed", user_payment_ms=int(upid),
                               error=str(e)[:160])
                return (f"Перевод НЕ выполнен: {_err(e)}\n"
                        f"Запрос не прошёл, деньги на месте. Исправь причину и повтори "
                        f"обычным вызовом — force не нужен.")
            journal.record(attempt, "pay", "unknown", user_payment_ms=int(upid),
                           error=str(e)[:160])
            return (f"ИСХОД НЕИЗВЕСТЕН: {_err(e)}\nЗапрос ушёл — деньги могли списаться. "
                    f"НЕ повторяй вслепую: проверь list_operations('{src}', days=1), "
                    f"и только если перевода нет — transfer(..., force=True).")

        payload = res.get("payload", res) if isinstance(res, dict) else {}
        pid = str(payload.get("paymentId") or payload.get("id") or "") if isinstance(payload, dict) else ""
        journal.record(attempt, "pay", "paid" if pid else "unknown",
                       user_payment_ms=int(upid), payment_id=pid)
        # commissionInfo holds THREE money objects and confusing them misreports
        # the charge: `amount` is what was sent, `commission` is the fee,
        # `amountWithCommission` is what actually left the account. The old
        # `c.get('value', c.get('amount'))` found no top-level `value`, fell back
        # to `amount` and printed the TRANSFER as its own fee — a 10 ₽ transfer
        # said «комиссия 10.0», a 150 000 ₽ one would say «комиссия 150000».
        # Shape verified against captures.xml #1477 and captures2.xml #595.
        commission = ""
        if isinstance(payload, dict):
            info = payload.get("commissionInfo")
            info = info if isinstance(info, dict) else {}
            fee = info.get("commission")
            if fee is None and not isinstance(payload.get("commission"), dict):
                fee = payload.get("commission")
            if fee is not None:
                commission = f", комиссия {_money(fee, 'RUB')}"
                total = info.get("amountWithCommission")
                fee_value = fee.get("value") if isinstance(fee, dict) else fee
                try:
                    charged = float(fee_value or 0) > 0
                except (TypeError, ValueError):
                    charged = False
                if charged and total is not None:
                    # Only worth printing when it differs from the amount sent.
                    commission += f", списано {_money(total, 'RUB')}"
        who = f" ({masked_fio})" if masked_fio else ""
        if not pid:
            return (f"Отправлено {_money(amount, 'RUB')} → {to_account}{who} со счёта {src}, "
                    f"но банк не вернул paymentId. Проверь list_operations('{src}', days=1) — "
                    f"чек по этому переводу получить не удастся.\n"
                    f"Ответ: {_json_out(payload, 400)}")
        return (f"Отправлено {_money(amount, 'RUB')} → {to_account}{who} "
                f"со счёта {src}{commission}. paymentId={pid} "
                f"(payment_receipt('{pid}') — чек).")
    except Exception as e:
        return _err(e)

@mcp.tool()
def pay_bill(provider_id: str, fields: str, amount: float, group: str = "",
             from_account: str = "", force: bool = False) -> str:
    """Оплатить счёт: ЖКХ, связь, интернет, штраф, налог. РЕАЛЬНЫЕ ДЕНЬГИ.

    provider_id и fields — из payment_providers(provider_id=…), fields — JSON вида
    {"account": "1234567890"}. Имена полей у каждого провайдера свои, угадывать их
    нельзя: тул сверяет значения с регуляркой из каталога и откажет до отправки.

    Перед оплатой всегда считается комиссия (это же и проверка тела банком), и
    итоговая сумма показывается. Показывай её пользователю и жди явного «да»
    ИМЕННО НА ЭТУ СУММУ — «оплати» без суммы подтверждением не считается.

    После оплаты проверь list_operations() — исход подтверждают операции,
    а не ответ этого тула.

    Неверный номер лицевого счёта оплачивает чужую квитанцию, и вернуть это
    сложнее, чем перевод. force=True — только если пользователь подтвердил, что
    предыдущий платёж не прошёл."""
    try:
        import time

        from . import journal
        s = _require(); s.ensure_fresh()
        try:
            vals = json.loads(fields) if isinstance(fields, str) else dict(fields or {})
        except json.JSONDecodeError as e:
            return (f"fields — это JSON-объект вида {{\"account\": \"123\"}}, "
                    f"разобрать не удалось: {e}")
        if not isinstance(vals, dict):
            return "fields должен быть JSON-ОБЪЕКТОМ {поле: значение}, а не списком."
        if amount is None or float(amount) <= 0:
            return "Сумма должна быть больше нуля."

        prov = s.find_provider(provider_id, group=group)
        if not prov:
            return (f"Провайдер {provider_id!r} не найден"
                    + (f" в группе {group!r}" if group else " (укажи group — так поиск "
                       "идёт по одной группе, а не по 100 000 провайдеров)")
                    + ". Каталог сканируется максимум на 7 страниц (700 записей), "
                    "поэтому «не найден» без group не окончателен. "
                    "Список: payment_providers(group=\"…\").")
        problems = s.validate_provider_fields(prov, vals)
        if problems:
            return ("Платёж НЕ отправлен — поля не проходят проверку провайдера "
                    f"«{prov.get('name')}»:\n" + "\n".join(f"- {p}" for p in problems)
                    + f"\nСхема полей: payment_providers(provider_id=\"{provider_id}\").")

        src = from_account or s._source_account()
        # The commission preview is both a courtesy and the bank's own validation of
        # the body — it is the step that proved this envelope is understood for bill
        # providers at all. A refusal here means the payment would have been refused.
        quote = s.payment_commission({"payParameters": {
            "account": src, "moneyAmount": float(amount), "currency": "RUB",
            "paymentType": "Payment", "provider": str(provider_id),
            "providerFields": vals}})
        q = quote if isinstance(quote, dict) else {}
        fee = ((q.get("value") or {}).get("value")
               if isinstance(q.get("value"), dict) else q.get("value"))
        total = ((q.get("total") or {}).get("value")
                 if isinstance(q.get("total"), dict) else q.get("total"))
        lo, hi = q.get("minAmount"), q.get("maxAmount")
        if lo is not None and float(amount) < float(lo):
            return f"Минимальная сумма у этого провайдера — {_money(lo, 'RUB')}."
        if hi is not None and float(amount) > float(hi):
            return f"Максимальная сумма у этого провайдера — {_money(hi, 'RUB')}."

        key = _transfer_key(amount, provider_id, "bill", src)
        blocked, prev = _transfer_blocked(key)
        if blocked and not force:
            return (f"ПОВТОР ЗАБЛОКИРОВАН: такой же платёж ({amount}₽ → "
                    f"{prov.get('name')}) уже отправлялся и его исход НЕ подтверждён "
                    f"(статус «{(prev or {}).get('status','?')}»). Деньги могли уйти. "
                    f"Проверь list_operations('{src}', days=1); если платежа нет — "
                    f"повтори с force=True.")
        retry_of = prev if (prev or {}).get("status") in _TRANSFER_BLOCKING else None
        upid = str(int((retry_of or {}).get("user_payment_ms") or 0)
                   or int(time.time() * 1000))
        attempt = journal.new_attempt(str(provider_id), "bill", key, amount)
        journal.record(attempt, "pay", "posting", user_payment_ms=int(upid), account=src)

        try:
            res = s.pay_bill(provider_id, vals, float(amount), account=src,
                             user_payment_id=upid) or {}
        except Exception as e:
            import requests as _rq
            answered = isinstance(e, (TbankApiError, SessionExpired))
            if answered and not isinstance(e, _rq.exceptions.RequestException):
                journal.record(attempt, "pay", "failed", user_payment_ms=int(upid),
                               error=str(e)[:160])
                return (f"Платёж НЕ выполнен: {_err(e)}\nЗапрос не прошёл, деньги на "
                        f"месте. Исправь причину и повтори обычным вызовом.")
            journal.record(attempt, "pay", "unknown", user_payment_ms=int(upid),
                           error=str(e)[:160])
            return (f"ИСХОД НЕИЗВЕСТЕН: {_err(e)}\nЗапрос ушёл — деньги могли "
                    f"списаться. НЕ повторяй вслепую: проверь "
                    f"list_operations('{src}', days=1), и только если платежа нет — "
                    f"pay_bill(..., force=True).")

        payload = res.get("payload", res) if isinstance(res, dict) else {}
        pid = str(payload.get("paymentId") or payload.get("id") or "") if isinstance(payload, dict) else ""
        journal.record(attempt, "pay", "paid" if pid else "unknown",
                       user_payment_ms=int(upid), payment_id=pid)
        if not pid:
            return (f"Ответ банка без paymentId — исход неясен. "
                    f"Проверь list_operations('{src}', days=1). "
                    f"Ответ: {_json_out(payload, 400)}")
        fee_txt = f", комиссия {_money(fee, 'RUB')}" if fee is not None else ""
        return (f"Оплачено {_money(amount, 'RUB')} → {prov.get('name')} "
                f"со счёта {src}{fee_txt}. paymentId={pid} "
                f"(payment_receipt('{pid}') — чек).")
    except Exception as e:
        return _err(e)


@mcp.tool()
def payment_providers(group: str = "", query: str = "", page: int = 1,
                      provider_id: str = "", pages: int = 5) -> str:
    """Каталог платёжных провайдеров (ЖКХ, связь, штрафы, налоги, интернет…) —
    только чтение, денег не двигает.

    Без аргументов печатает ГРУППЫ провайдеров — с них и начинай, дальше
    payment_providers(group="ЖКХ"). group — это НАЗВАНИЕ группы, не id.
    query — подстрока по названию провайдера внутри группы (фильтрует ТЕКУЩУЮ
    страницу). page — номер страницы каталога, шапка подсказывает следующую.

    provider_id="<id>" печатает ПОЛЯ, которые провайдер требует для платежа:
    id поля, человеческое название, обязательность, подсказку и регулярку, по
    которой значение проверяется. Это единственный источник формы платежа —
    угадывать имена полей нельзя. Поиск по id сканирует до `pages` страниц
    каталога (по 100 записей); ответ «не найден» честно называет, сколько
    страниц просмотрено — с group поиск попадает в первую страницу.

    Что с этим делать дальше: pay_bill(provider_id, fields, amount) — он сам
    проверит поля по регулярке и посчитает комиссию. Уже выставленный счёт вместе
    с готовыми полями обычно лежит в get_data("subscription_bills")."""
    try:
        s = _require(); s.ensure_fresh()
        if not group and not provider_id:
            groups = s.providers_groups()
            names = [str(g.get("name") or g.get("id") or "") for g in groups
                     if isinstance(g, dict)]
            names = [n for n in names if n]
            if not names:
                return "Групп провайдеров не вернулось."
            return ("Группы провайдеров (дальше: payment_providers(group=\"…\")):\n"
                    + "\n".join(f"- {n}" for n in sorted(names)))

        if provider_id:
            # Scan the pages of the named group (or the default page) for this id.
            found = None
            scanned, total_pages = 0, None
            for p in range(1, max(1, pages) + 1):
                pg = s.providers_compatible_page(group=group, page=p)
                scanned = p
                total_pages = int(pg.get("totalPages") or 1)
                for prov in (pg.get("providers") or []):
                    if str(prov.get("id")) == str(provider_id):
                        found = prov
                        break
                if found or p >= total_pages:
                    break
            if not found:
                # «не найден» после неполного скана — не факт, а граница поиска;
                # молчать о ней = ложный отказ на провайдере со страницы pages+1.
                where = f" в группе {group!r}" if group else ""
                if total_pages and scanned < total_pages:
                    return (f"Провайдер {provider_id!r} не найден{where} среди первых "
                            f"{scanned} страниц из {total_pages}. Уточни group "
                            f"(попадёт в первую страницу) или увеличь pages=. "
                            f"Список групп: payment_providers().")
                return (f"Провайдер {provider_id!r} не найден{where}"
                        + ". Открой список: payment_providers(group=\"…\").")
            fields = s.provider_pay_fields(found)
            head = (f"{found.get('name')} | id={found.get('id')} | "
                    f"группа={found.get('groupId')} | "
                    f"тип={found.get('paymentType') or '?'}")
            if not fields:
                return head + "\nПолей для платежа провайдер не публикует."
            lines = [head, "Поля для платежа:"]
            for f in fields:
                lines.append(
                    f"- {f['id']} — {f['name']}"
                    + ("  [ОБЯЗАТЕЛЬНО]" if f["required"] else "  [необязательно]")
                    + (f"\n    подсказка: {f['hint']}" if f["hint"] else "")
                    + (f"\n    формат: {f['regexp']}" if f["regexp"] else ""))
            return "\n".join(lines)

        pg = s.providers_compatible_page(group=group, page=page)
        provs = pg.get("providers") or []
        if query:
            q = query.lower()
            provs = [p for p in provs if q in str(p.get("name", "")).lower()]
        if not provs:
            # An unmatched group is HTTP 200 with an empty payload, not an error, so
            # say what it means instead of letting it read as "no such providers".
            return (f"В группе {group!r} ничего не найдено"
                    + (f" по запросу {query!r}" if query else "") + ".\n"
                    "Фильтр сверяется с groupId у провайдера, а он не всегда совпадает "
                    "с названием из списка групп (например «ЖКХ» → «Коммунальные "
                    "платежи»). Проверь написание по payment_providers() без аргументов.")
        total, total_pages = pg.get("totalProviders"), pg.get("totalPages")
        cur_page = pg.get("page", page)
        # No [:60] cut: the header counted len(provs) BEFORE the old slice, so a
        # full page claimed «100 из N» while printing 60 — providers 61–100 of
        # every page were silently unreachable and the header lied about it.
        head = (f"{group or 'провайдеры'}: {len(provs)} из {total or '?'} "
                f"(страница {cur_page} из {total_pages or '?'})")
        try:
            if int(cur_page) < int(total_pages or 1):
                head += f"; следующая: page={int(cur_page) + 1}"
        except (TypeError, ValueError):
            pass
        lines = [head]
        for p in provs:
            lines.append(f"- {p.get('name')} | id={p.get('id')}"
                         + ("" if p.get("isActive", True) else " | НЕАКТИВЕН"))
        lines.append("Поля конкретного провайдера: "
                     "payment_providers(provider_id=\"…\", group=\"…\")")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


@mcp.tool()
def payment_commission(body: str = "") -> str:
    """Предпросмотр комиссии (денег НЕ двигает). body обязателен — это JSON-строка.

    Форма (сверена с захватом):
      {"payParameters": {
         "account": "<счёт списания из list_accounts()>",
         "moneyAmount": 1500,
         "currency": "RUB",
         "paymentType": "Transfer",     // "Payment" для оплаты услуг
         "provider": "p2p-anybank",     // или transfer-inner / id провайдера
         "providerFields": { ... }      // для СБП — provider_fields из
       }}                               // transfer_sbp_resolve(), как есть

    НЕ пиши pointerType:"ACCOUNT" — банк отвечает INVALID_REQUEST_DATA.
    paymentType здесь обязателен, хотя в самом переводе его быть НЕ должно."""
    try:
        s = _require(); s.ensure_fresh()
        if not body.strip():
            return ("body обязателен: JSON-строка с payParameters "
                    "(account, moneyAmount, currency, paymentType, provider, "
                    "providerFields). Смотри описание тула — там форма целиком.")
        try:
            b = json.loads(body)
        except ValueError as e:
            return f"body — не JSON ({e}). Ожидается {{\"payParameters\": {{...}}}}."
        if not isinstance(b, dict) or "payParameters" not in b:
            return ("В body нет ключа payParameters. Ожидается "
                    "{\"payParameters\": {\"account\": …, \"moneyAmount\": …}}.")
        # via the client method — it form-encodes and defaults the isTransferStatus/
        # isUrgentTransfer flags. Calling _call_read directly posts JSON → 400.
        return _json_out(s.payment_commission(b), 1000)
    except Exception as e:
        return _err(e)


# ── INVEST ──────────────────────────────────────────────────

@mcp.tool()
def invest_accounts() -> str:
    """Инвест-счета: брокерские и InvestBox. brokerAccountId отсюда — единственный
    аргумент invest_portfolio/invest_operations/invest_securities."""
    try:
        s = _require(); s.ensure_client_session()
        accs = s.invest_accounts()
        if not accs:
            return "нет инвест-счетов"
        def render(a):
            return (f"- {a.get('brokerAccountId') or a.get('id') or '?'} "
                    f"| {a.get('brokerAccountType', '?')} "
                    f"| {a.get('brokerAccountStatus', '?')} "
                    f"| всего {_money(a.get('totalBalance'))} "
                    f"| доступно {_money(a.get('authBalance'))} "
                    f"| доход {_money(a.get('totalYield'))}"
                    + (" | ЗАБЛОКИРОВАН" if a.get("isBlocked") else ""))
        return "\n".join(render(a) for a in accs)
    except Exception as e:
        return _err(e)

@mcp.tool()
def invest_portfolio(broker_account_id: str, days: int = 30) -> str:
    """Статистика портфеля (ввод/вывод, купоны, дивиденды, стоимость по месяцам) за
    период. broker_account_id — из invest_accounts()."""
    try:
        s = _require(); s.ensure_client_session()
        # Этот эндпоинт хочет ДАТЫ, а не миллисекунды, как остальные чтения здесь.
        today = datetime.now().date()
        data = s.invest_portfolio(broker_account_id,
                                  (today - timedelta(days=max(1, days))).isoformat(),
                                  today.isoformat())
        if isinstance(data, dict) and data.get("errorCode"):
            return (f"Банк отказал ({data.get('errorCode')}): "
                    f"{redact_text(str(data.get('errorMessage')))[:160]}")
        return _json_out(data, 4000)
    except Exception as e:
        return _err(e)

@mcp.tool()
def invest_operations(broker_account_id: str, operation_type: str = "", limit: int = 50) -> str:
    """Брокерские операции, новые сверху. limit применяется и к запросу, и к
    выводу (0 = всё, что вернул банк).

    operation_type — фильтр по типу; пусто = все. Полного списка банк не публикует.
    Наблюдались: buy, sell, payIn, payOut, tax, taxBack (живой ответ) и outMulti
    (захват приложения). Список не полон — сначала вызови без фильтра и посмотри,
    какие типы реально пришли в ответе, потом фильтруй по ним."""
    try:
        s = _require(); s.ensure_client_session()
        ops, has_next = s.invest_operations(broker_account_id,
                                            operation_type=operation_type, limit=limit)
        if not ops:
            return "нет операций"
        def render(o):
            # `payment`, not `amount`: the field never existed under that name, so
            # every row printed «?» for the one number that matters.
            return (f"- [{str(o.get('date', ''))[:16]}] "
                    f"{_money(o.get('payment') or o.get('paymentRub'))} "
                    f"| {o.get('type', '')} | {_cut(o.get('description', ''), 40)} "
                    f"| {o.get('status', '')}")
        # limit was passed to the API AND then ignored here by a hardcoded [:50],
        # so invest_operations(limit=200) silently showed 50. has_next goes into
        # the HEADER, not more_hint: the bank holding more back is true even when
        # every fetched row is shown (limit=0). No cursor is sent — its wire name
        # is unconfirmed; a bigger limit is the capture-backed way to see more.
        head = "Брокерские операции"
        if has_next:
            head += f" (у банка есть ещё — увеличь limit, сейчас {limit})"
        return _rows_out(ops, render, limit=limit, total=len(ops), header=head)
    except Exception as e:
        return _err(e)

@mcp.tool()
def invest_securities(broker_account_id: str = "") -> str:
    """Бумаги в портфеле: тикер, количество, текущая цена, доля и доходность.
    broker_account_id — из invest_accounts(); пусто = все портфели.

    Учти: у брокерского счёта может быть НЕСКОЛЬКО портфелей (рублёвый, валютный),
    и brokerAccountId портфеля не совпадает с id счёта из invest_accounts() —
    поэтому пустой ответ на конкретный id ещё не значит «бумаг нет». Вызови без
    аргумента и посмотри, какие портфели есть."""
    try:
        s = _require(); s.ensure_client_session()
        folios = s.invest_securities(broker_account_id)
        if not folios:
            return ("Портфелей не найдено"
                    + (f" для счёта {broker_account_id}. Вызови invest_securities() "
                       f"без аргумента — id портфеля и id счёта различаются."
                       if broker_account_id else "."))
        lines = []
        for p in folios:
            lines.append(f"[{p.get('name') or '?'} | brokerAccountId={p.get('brokerAccountId')}]")
            for pos in p["positions"]:
                prices = pos.get("prices") or {}
                cur = (prices.get("currentPrice") or {})
                y = ((pos.get("yields") or {}).get("yield") or {})
                lines.append(
                    f"- {str(pos.get('ticker', '?'))[:14]:14} | {pos.get('securityType', '')[:6]:6} "
                    f"| {pos.get('currentBalance', '?')} шт "
                    f"| {_money(cur.get('value'), cur.get('currency', ''))} "
                    f"| {pos.get('portfolioPercent', '?')}% "
                    f"| доход {_money((y.get('absolute') or {}).get('value'), 'RUB')}")
            if not p["positions"]:
                lines.append("  (пусто)")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


# ── CARDS & ACCOUNT DETAILS ─────────────────────────────────

# A handful of endpoints validate the mobile *sessionid*, not just the Bearer
# token, and refuse an ANONYMOUS-level session. The CLIENT window is only ~11
# minutes (see client.ensure_client_session), so this is the normal steady state
# between re-mints — the tools below call ensure_client_session() to recover
# automatically, and this hint only fires if even that did not help.
_ANON_SESSION_CODES = ("INTERNAL_ERROR", "AccessDenied", "SESSION_IS_ABSENT")
_ANON_HINT = (
    "\nПричина: этот эндпоинт проверяет уровень мобильной сессии, а не только "
    "токен, а окно CLIENT живёт ~11 минут. Тул уже пробовал перевыпустить сессию "
    "сам. Проверь keepalive() — accessLevel должен быть CLIENT; если там "
    "ANONYMOUS, вызови refresh_session() и повтори.")

def _err_session(e) -> str:
    """_err(), plus the ANONYMOUS-session explanation when that is the cause."""
    msg = _err(e)
    if any(c in msg for c in _ANON_SESSION_CODES):
        return msg + _ANON_HINT
    return msg

def _money(m, currency: str = "") -> str:
    """Render an amount readably: '1 000.00 RUB'.

    Accepts the bank's {'value': …, 'currency': {'name': …}} shape AND a bare number
    with the currency passed separately. It used to fall back to str(m) for anything
    that was not a dict, so every caller holding a plain float printed «1000.0» — no
    thousands separator, no currency, and easy to misread by a factor of ten."""
    if isinstance(m, dict):
        cur = m.get("currency") or {}
        currency = (cur.get("name", "") if isinstance(cur, dict) else str(cur)) or currency
        m = m.get("value")
    if m is None or m == "":
        return "—"
    try:
        return f"{float(m):,.2f} {currency}".replace(",", " ").strip()
    except (TypeError, ValueError):
        return f"{m} {currency}".strip()

@mcp.tool()
def list_cards() -> str:
    """Все карты по всем счетам: id, ucid, баланс, тип.
    id — для card_operations, ucid — для card_limits/card_requisites.
    Карты, привязанные из ДРУГИХ банков, помечены «внешняя»: у них нет ucid, и
    card_limits/card_requisites по ним не работают."""
    try:
        s = _require(); s.ensure_fresh()
        cards = s.cards()
        if not cards:
            return "Карт нет."
        def row(c):
            external = c.get("accountType") == "ExternalAccount" or not c.get("ucid")
            kind = ("внешняя" if external
                    else "виртуальная" if c.get("isVirtual") else "пластик")
            bal = _money(c.get("availableBalance"), c.get("currency") or "")
            return (f"- id={c.get('id','?')} ucid={c.get('ucid') or '—'} "
                    f"| счёт {c.get('account','?')} | {kind} | {bal} "
                    f"| {(c.get('name') or c.get('accountName') or '')[:26]}")
        return "\n".join(row(c) for c in cards)
    except Exception as e:
        return _err(e)

@mcp.tool()
def card_limits(ucid: str) -> str:
    """Лимиты по карте (на покупки, на снятие) и сколько уже израсходовано.
    ucid — из list_cards()."""
    try:
        s = _require(); s.ensure_fresh()
        limits = s.card_limits(ucid)
        if not limits:
            return f"[ucid {ucid}] лимитов не возвращено."
        out = []
        for l in limits:
            cap, used = l.get("moneyAmount"), l.get("utilizedMoneyAmount")
            line = f"- {l.get('name') or l.get('id','?')} ({l.get('interval','')}): "
            line += f"израсходовано {_money(used)}"
            line += f" из {_money(cap)}" if cap else "  (лимит не задан)"
            out.append(line)
        return "\n".join(out)
    except Exception as e:
        return _err(e)

@mcp.tool()
def card_requisites(ucid: str, reveal: bool = False) -> str:
    """Реквизиты карты: держатель, срок, номер. ucid — из list_cards().

    По умолчанию номер маскируется, а CVV не выводится вообще.
    reveal=True выдаёт ПОЛНЫЙ номер и CVV — этого достаточно, чтобы платить картой.
    Ставь его ТОЛЬКО когда пользователь явным текстом попросил показать полные
    реквизиты, и предупреди, что они попадут в переписку. «Покажи мою карту» —
    это не такая просьба."""
    try:
        s = _require(); s.ensure_client_session()
        c = s.card_credentials(ucid)
        if not c:
            return f"[ucid {ucid}] реквизиты не получены."
        if reveal:
            # Full PAN + CVV are about to enter the model's context and the user's
            # transcript. Record THAT it happened (never the values) so the exposure
            # is auditable after the fact — diagnostics() will show it.
            from . import observability as obs
            obs.emit("card_reveal", ucid_present=bool(ucid), pan_revealed=True)
        pan = str(c.get("cardNumber") or "")
        exp = str(c.get("expireDate") or "")
        exp_fmt = f"{exp[:2]}/{exp[2:]}" if len(exp) == 4 else exp
        out = [f"Держатель: {c.get('cardHolder','?')}", f"Срок: {exp_fmt}"]
        if reveal:
            out.append(f"Номер: {' '.join(pan[i:i+4] for i in range(0, len(pan), 4))}")
            out.append(f"CVV: {c.get('cvv2','?')}")
            out.append("⚠️ Это полные платёжные данные — они теперь в переписке.")
        else:
            out.append(f"Номер: {pan[:4]} **** **** {pan[-4:]}" if len(pan) >= 8 else "Номер: скрыт")
            out.append("CVV: скрыт (reveal=True покажет номер и CVV)")
        return "\n".join(out)
    except Exception as e:
        return _err_session(e)

@mcp.tool()
def card_operations(card_id: str, days: int = 30, limit: int = 50,
                    desc_len: int = 40) -> str:
    """Операции по КОНКРЕТНОЙ карте. card_id — поле id из list_cards().
    Серверного фильтра по карте нет (API умеет только excludeCardIds), поэтому
    берутся операции за период и фильтруются по полю card.
    limit=0 — показать все за период.
    desc_len — ширина колонки описания (0 = описание целиком, обрезка помечена «…»)."""
    try:
        s = _require(); s.ensure_fresh()
        start, end = ms_for_period(days)
        ops = [o for o in s.list_operations(None, start, end)
               if str(o.get("card", "")) == str(card_id)]
        if not ops:
            return f"[card {card_id}] операций за {days} дн. нет."
        def when(o):
            t = o.get("operationTime") or o.get("debitingTime") or {}
            ms = t.get("milliseconds") if isinstance(t, dict) else t
            return datetime.fromtimestamp(ms / 1000).strftime("%d.%m %H:%M") if ms else "?"
        spent = sum(float((o.get("amount") or {}).get("value") or 0)
                    for o in ops if o.get("type") == "Debit")
        # The «списано» figure is over ALL operations, not over the rows printed —
        # said here because the header below also states how many are shown. The
        # digit grouping is applied to the NUMBER, not to the sentence: replacing
        # every comma in the whole string also ate the one after «дн.».
        head = (f"[card {card_id}] за {days} дн., "
                f"списано {f'{spent:,.0f}'.replace(',', ' ')} ₽ (по всем операциям)")
        def render(o):
            return (f"- [{when(o)}] {'-' if o.get('type') == 'Debit' else '+'}"
                    f"{(o.get('amount') or {}).get('value','?')} "
                    f"| {_cut(o.get('description') or '', desc_len)}")
        return _rows_out(ops, render, limit=limit, total=len(ops), header=head)
    except Exception as e:
        return _err(e)

@mcp.tool()
def account_requisites(account_id: str, currencies: str = "RUB") -> str:
    """Реквизиты счёта для перевода извне: получатель, счёт, БИК, корсчёт, ИНН/КПП.
    account_id — из list_accounts(). currencies — через запятую (RUB,USD,EUR)."""
    try:
        s = _require(); s.ensure_fresh()
        curs = tuple(c.strip().upper() for c in currencies.split(",") if c.strip())
        groups = s.account_requisites(account_id, curs or ("RUB",))
        out = []
        for g in groups:
            for r in (g.get("requisites") or []):
                out.append(
                    f"[{r.get('currency','?')}] {r.get('cardLine1','')}\n"
                    f"  Получатель: {r.get('recipient','?')}\n"
                    f"  Счёт: {r.get('recipientExternalAccount','?')}\n"
                    f"  Банк: {r.get('beneficiaryBank','?')}  БИК {r.get('bankBik','?')}\n"
                    f"  Корсчёт: {r.get('correspondentAccountNumber','?')}\n"
                    f"  ИНН {r.get('inn','?')}  КПП {r.get('kpp','?')}\n"
                    f"  Назначение: {r.get('beneficiaryInfo','')}")
        return "\n".join(out) or f"[account {account_id}] реквизитов нет."
    except Exception as e:
        return _err(e)


# ── IDENTITY DOCUMENTS ──────────────────────────────────────

_DOC_TITLES = {
    "RusNationalID": "Паспорт РФ", "RusInternationalID": "Загранпаспорт",
    "RusDriversLic": "Водительское удостоверение", "RusSNILS": "СНИЛС",
    "RusINN": "ИНН", "RusOSAGO": "ОСАГО", "RusKASKO": "КАСКО",
    "RusPTS": "ПТС", "RusVehicleRegID": "СТС",
    "TravelInsurance": "Страховка путешественника",
    "RusBirthCert": "Свидетельство о рождении",
    "RusMilitaryCard": "Военный билет", "RusMedIns": "Полис ОМС",
}

def _doc_flat(node, prefix=""):
    """prefill wraps every leaf as {"value":…,"isEntered":bool}. Flatten to dotted
    paths, keeping only leaves the bank actually holds a value for.

    Lists ARE data here, not structure to skip: a licence's categories and an
    OSAGO's drivers arrive as {"isEntered": true, "value": [...]}, and the old
    dict-only recursion dropped exactly those — entered fields, gone without a
    trace. Scalars join into one readable value; dict elements recurse with an
    index in the path."""
    out = {}
    if isinstance(node, dict):
        if "isEntered" in node:
            v = node.get("value")
            if isinstance(v, dict):
                out.update(_doc_flat(v, prefix))
            elif isinstance(v, list):
                if node.get("isEntered"):
                    out.update(_doc_flat(v, prefix))
            elif node.get("isEntered"):
                out[prefix] = v
            return out
        for k, v in node.items():
            out.update(_doc_flat(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(node, list):
        scalars = [x for x in node if not isinstance(x, (dict, list))]
        if scalars and len(scalars) == len(node):
            out[prefix] = ", ".join(str(x) for x in scalars)
        else:
            for i, item in enumerate(node):
                out.update(_doc_flat(item, f"{prefix}[{i}]"))
    return out

@mcp.tool()
def documents(kind: str = "", include_others: bool = False) -> str:
    """Документы клиента: паспорт, загранпаспорт, ВУ, СНИЛС, ИНН, ОСАГО/КАСКО, ПТС/СТС.
    kind — фильтр по названию или коду (напр. "паспорт", "RusDriversLic"); пусто = все.
    В хранилище лежат и документы РОДСТВЕННИКОВ, которые клиент когда-то вводил —
    они отсеиваются по дате рождения; include_others=True покажет и их."""
    try:
        s = _require(); s.ensure_client_session()
        docs = s.identity_documents()
        try:
            own_bd = ((s.identity_brief().get("birthDate") or {}) or {}).get("value")
        except Exception:
            own_bd = None
        want = kind.lower().strip()
        out = []
        for code, entries in sorted(docs.items()):
            title = _DOC_TITLES.get(code, code)
            if want and want not in title.lower() and want not in code.lower():
                continue
            seen = {}
            for e in entries:
                f = _doc_flat(e.get("value") or {})
                bd = f.get("person.birthDate")
                mine = own_bd is None or bd is None or bd == own_bd
                if not mine and not include_others:
                    continue
                # the same document repeats across sources; MERGE the copies. The
                # old «richest copy wins» compared field counts, so a field present
                # only in the smaller copy was silently lost with it.
                key = (f.get("serial"), f.get("number") or f.get("serialAndNumber"),
                       f.get("person.lastName"), bd)
                cur = seen.setdefault(key, {"_mine": mine})
                for k, v in f.items():
                    cur.setdefault(k, v)
            for f in seen.values():
                who = "" if f.get("_mine") else "  ⚠ не ваш документ"
                num = " ".join(str(f[k]) for k in ("serial", "number", "serialAndNumber")
                               if f.get(k))
                head = f"{title}: {num or '—'}{who}"
                # `name` usually duplicates the catalogue title, but for a code
                # _DOC_TITLES does not know, the raw code IS the title — then the
                # dropped name was the only human-readable label.
                hide = {"serial", "number", "serialAndNumber", "_mine"}
                if str(f.get("name", "")) == title:
                    hide.add("name")
                rest = [f"    {k} = {v}" for k, v in sorted(f.items())
                        if k not in hide]
                out.append("\n".join([head] + rest))
        return "\n".join(out) or f"Документов не найдено (kind={kind!r})."
    except Exception as e:
        return _err_session(e)


# ── ORDERS ACROSS EVERY VERTICAL ────────────────────────────

# Built from the verticals table rather than typed out, because the failure of a
# hand-kept tuple is silent: `museum` was missing and every exhibition order fell
# out of the feed, which reads as «no such order» rather than «this filter does
# not know that type». Adding a vertical now widens the filter by itself.
_AFISHA_TYPES = tuple(dict.fromkeys(
    t for v in VERTICALS.values() for t in v["order_types"]))
_ORDER_KINDS = {
    "афиша": _AFISHA_TYPES,
    "кино": VERTICALS["movie"]["order_types"],
    "путешествия": ("avia_ticket", "trains_ticket", "hotelBooking"),
    "продукты": ("grocery",),
}

@mcp.tool()
def orders(kind: str = "", limit: int = 10) -> str:
    """Все заказы клиента: продукты, кино, концерты, авиабилеты, ж/д, отели.
    kind — "афиша" | "кино" | "путешествия" | "продукты" | код objectType; пусто = все.
    Отсортировано по дате создания, новые сверху. limit=0 — показать все."""
    try:
        s = _require(); s.ensure_fresh()
        all_orders = s.orders()
        k = kind.lower().strip()
        types = _ORDER_KINDS.get(k, (k,) if k else ())
        picked = [o for o in all_orders
                  if not types or o.get("objectType") in types] if types else all_orders
        picked.sort(key=lambda o: str(o.get("created") or ""), reverse=True)
        if not picked:
            kinds = sorted({str(o.get("objectType")) for o in all_orders})
            return f"Заказов вида {kind!r} нет. Доступные objectType: {', '.join(kinds)}"
        def render(o):
            f = o.get("fields") or {}
            what = (f.get("eventName") or f.get("hotelName") or f.get("objectName")
                    or f.get("applicationName") or f.get("partnerName") or "")
            # paymentId is on 338 of 563 captured order records and is the ONLY
            # argument payment_receipt() takes — the docstring pointed here for it
            # while this row printed the orderId and nothing else.
            pay = o.get("paymentId")
            return (f"- {str(o.get('created',''))[:10]} | {o.get('objectType','?'):13} "
                    f"| {o.get('status','?'):15} | {o.get('amount','?'):>10} ₽ "
                    f"| {_cut(what, 34)} | id={o.get('orderId','?')}"
                    + (f" | paymentId={pay}" if pay else ""))
        return _rows_out(picked, render, limit=limit, total=len(picked),
                         header="Заказы" + (f" ({kind})" if kind else ""))
    except Exception as e:
        return _err(e)

@mcp.tool()
def order_details(order_id: str) -> str:
    """Детали одного заказа (места, зал, код брони, состав корзины).
    Работает для развлекательных заказов (кино/концерты); для продуктов —
    grocery_order_status, для поездок деталей в этом API нет, только orders()."""
    try:
        s = _require(); s.ensure_fresh()
        d = s.order_details(order_id)
        info, obj = d.get("orderInfo") or {}, d.get("objectInfo") or {}
        ev, cart = d.get("eventInfo") or {}, d.get("cartInfo") or {}
        f = info.get("fields") or {}
        out = [f"Заказ {info.get('orderId', order_id)} | {info.get('status','?')} "
               f"| создан {str(info.get('created',''))[:16]}"]
        if ev:
            out.append(f"Событие: {ev.get('eventName','?')} ({', '.join(ev.get('genres') or [])})")
        if obj:
            geo = obj.get("geo") or {}
            out.append(f"Место: {obj.get('objectName','?')}, {geo.get('address','')}")
        if info.get("reserveDate"):
            out.append(f"Сеанс: {str(info['reserveDate'])[:16]}, {f.get('hallName','')}")
        if f.get("reservationCode"):
            out.append(f"Код брони: {f['reservationCode']}")
        for el in (cart.get("cartElement") or []):
            pos = ((el.get("fields") or {}).get("seatPos") or {})
            seat = f"ряд {pos.get('row')}, место {pos.get('number')}" if pos else ""
            out.append(f"  - {el.get('price','?')} ₽ ({seat})")
        if cart.get("amount"):
            out.append(f"Итого: {cart['amount']} ₽")
        return "\n".join(out)
    except Exception as e:
        return _err(e)


# Эти две вертикали пока не подключены — но не потому, что банк отказывает.
# Захват captures-gorod.xml показывает рабочие пути к обеим; им нужен отдельный
# cookie jar, и это ещё не сделано. Формулировки ниже говорят «пока нет», а не
# «невозможно»: прошлая версия утверждала второе и оказалась неправа.
_TRAVEL_BLOCKED = {
    "avia_ticket": ("маршрут, места и пассажиров отдаёт www.tbank.ru/api/travel/flight/order, "
                    "и он требует веб-сессию поверх мобильной. Мост к ней в захвате есть "
                    "(session/webview/get_by_token отдаёт портальную сессию), но в MCP он "
                    "ещё не реализован"),
    "trains_ticket": ("вагон, места и пассажиров отдаёт trains.t-bank-app.ru/api/orders/{id}, "
                      "и он авторизуется по cookie, которую ставит сам хост в ответ на "
                      "запрос с Bearer. Отдельная сессия для этого хоста в MCP ещё не "
                      "заведена"),
}

@mcp.tool()
def travel_order_details(order_id: str) -> str:
    """Детали поездки по orderId из orders("путешествия").

    Полная карточка есть только для ОТЕЛЕЙ: даты, отель, номер, питание, гости.
    Для авиа и ж/д API отдаёт только сводку из orders() — почему, тул объяснит."""
    try:
        s = _require(); s.ensure_fresh()
        order = next((o for o in s.orders()
                      if str(o.get("orderId")) == str(order_id)), None)
        if not order:
            return f"Заказ {order_id} не найден. Список — orders(\"путешествия\")."
        kind = order.get("objectType") or "?"
        f = order.get("fields") or {}
        head = (f"Заказ {order_id} | {kind} | {order.get('status','?')} "
                f"| {order.get('amount','?')} ₽ | оформлен {str(order.get('created',''))[:10]}")
        if kind in _TRAVEL_BLOCKED:
            what = f.get("eventName") or f.get("hotelName") or ""
            when = str(f.get("endDate") or "")[:16]
            return (f"{head}\n{what}" + (f"\nЗавершение: {when}" if when else "")
                    + f"\n\nДеталей больше нет: {_TRAVEL_BLOCKED[kind]}.")
        if kind != "hotelBooking":
            return head + f"\nДетальной карточки для типа {kind} в этом API нет."
        b = s.hotel_booking(order_id)
        if not b:
            return head + "\nБронь не найдена на hotels.t-bank-app.ru."
        h = b.get("hotelData") or {}
        area = h.get("areaLocation") or {}
        rate = b.get("rateData") or {}
        out = [head,
               f"Отель: {h.get('hotelName','?')} {'★' * int(h.get('starRating') or 0)}"
               f" | {area.get('destinationName','')}, {area.get('countryName','')}",
               f"Заезд {b.get('checkInDate','?')} c {h.get('checkInTime','?')}, "
               f"выезд {b.get('checkOutDate','?')} до {h.get('checkOutTime','?')}",
               f"Статус брони: {b.get('internalStatus','?')}"]
        for room in (rate.get("rooms") or []):
            guests = ", ".join(f"{g.get('firstName','')} {g.get('lastName','')}".strip()
                               for g in (room.get("guests") or []))
            out.append(f"Номер: {room.get('name') or room.get('roomName') or '—'} "
                       f"| взрослых {room.get('adultsNumber','?')}, "
                       f"детей {room.get('childNumber','?')}")
            if room.get("mealName"):
                out.append(f"  Питание: {room['mealName']}")
            if guests:
                out.append(f"  Гости: {guests}")
        contact = b.get("contactData") or {}
        if contact:
            out.append(f"Контакты брони: {contact.get('email','')} {contact.get('phone','')}")
        if h.get("phone") or h.get("email"):
            out.append(f"Отель: тел. {h.get('phone','')} {h.get('email','')}")
        return "\n".join(out)
    except Exception as e:
        return _err(e)


# ── GROCERY NUTRITION ───────────────────────────────────────

@mcp.tool()
def grocery_good_info(good_id: str, app_id: str = "", point_id: str = "") -> str:
    """Карточка товара: состав, КБЖУ, вес, срок хранения, производитель.
    good_id — из grocery_search()/grocery_plan_order(). КБЖУ приводится на 100 г
    и на упаковку (у части сетей КБЖУ есть только текстом — он разбирается)."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        g = s.grocery_good(good_id, app_id=app_id, point_id=point_id)
        if not g:
            return f"Товар {good_id} не найден в магазине {app_id}/{point_id}."
        meta = g.get("meta") or {}
        n = s.nutrition(g)
        w = meta.get("weight") or {}
        out = [f"{g.get('name','?')}  (id={g.get('id', good_id)})",
               f"Цена: {(g.get('price') or {}).get('value','?')} ₽   "
               f"Вес: {w.get('value','?')} {w.get('unit','')}   "
               f"В наличии: {g.get('count','?')}"]
        if n["kcal"] is not None or n["protein"] is not None:
            out.append(f"КБЖУ/100 г: {n['kcal'] if n['kcal'] is not None else '?'} ккал, "
                       f"Б {n['protein']}, Ж {n['fat']}, У {n['carb']}")
            if n["kcal_pack"] is not None:
                out.append(f"На упаковку ({n['grams']:.0f} г): {n['kcal_pack']:.0f} ккал")
        else:
            out.append("КБЖУ: сеть не публикует")
        if meta.get("manufacturer"):
            out.append(f"Производитель: {meta['manufacturer']}")
        if meta.get("storage"):
            out.append(f"Хранение: {meta['storage']}")
        if meta.get("description"):
            out.append(f"Описание: {_cut(meta['description'], 300)}")
        if meta.get("ingredients"):
            # НЕ резать: аллергены стоят в ХВОСТЕ списка состава, и обрезанный
            # состав опаснее отсутствующего — он выглядит полным.
            out.append(f"Состав: {meta['ingredients']}")
        return "\n".join(out)
    except Exception as e:
        return _err(e)

def _rank_rows(rows: list[dict], sort_by: str, order: str) -> list[dict]:
    """Sort candidate rows by one attribute.

    Rows missing that attribute always go LAST, in both directions — an item whose
    calories the retailer never published must not win a "highest calories" query
    just because None happens to compare low."""
    if not sort_by:
        return rows                        # no criterion → keep the store's order
    desc = str(order).lower().startswith("desc")
    return sorted(rows, key=lambda r: (r.get(sort_by) is None,
                                       -(r[sort_by]) if desc and r.get(sort_by) is not None
                                       else (r.get(sort_by) if r.get(sort_by) is not None else 0)))

@mcp.tool()
def grocery_rank(query: str, app_id: str = "", point_id: str = "",
                 sort_by: str = "", order: str = "asc", limit: int = 8,
                 with_nutrition: bool = False) -> str:
    """Кандидаты по запросу с атрибутами, опционально отсортированные.

    Это ИНСТРУМЕНТ, а не политика: сам по себе никакой стратегии выбора не
    применяет. Стратегию задаёт вызывающий, и только когда пользователь её
    попросил — иначе sort_by пустой и порядок остаётся магазинным.

    sort_by: price | weight | kcal | kcal_pack | protein | fat | carb (пусто = без
    сортировки). order: asc | desc. Питательные поля тянутся автоматически, если
    по ним сортируем (это +1 запрос на кандидата), либо по with_nutrition=True.
    Товары, у которых сеть не публикует нужное поле, всегда уходят в конец — и при
    asc, и при desc: «нет данных» не равно нулю."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        key = sort_by.strip().lower()
        if key and key not in s.SORTABLE_KEYS:
            return (f"Неизвестное поле сортировки {sort_by!r}. "
                    f"Доступны: {', '.join(s.SORTABLE_KEYS)} (или пусто — без сортировки).")
        need_nutrition = with_nutrition or key in s.NUTRITION_KEYS
        rows, matched = s.grocery_candidates(query, app_id=app_id, point_id=point_id,
                                             limit=limit, with_nutrition=need_nutrition)
        if not rows:
            return f"По запросу {query!r} ничего не найдено в магазине {app_id}/{point_id}."
        rows = _rank_rows(rows, key, order)
        n = lambda v: "—" if v is None else f"{v:g}"   # noqa: E731 — часть КБЖУ сеть не публикует
        head = f"«{query}» — показано {len(rows)} из {matched} кандидатов"
        if len(rows) < matched:
            head += f" (limit={matched} или limit=0 — все)"
        head += f", сортировка по {key} ({'убыв' if order.lower().startswith('desc') else 'возр'}):" \
            if key else ", порядок магазина (сортировка не запрошена):"
        lines = [head]
        for r in rows:
            line = f"- {n(r.get('price')):>7} ₽ | {r.get('weight_label') or '—':>10}"
            if need_nutrition:
                kcal = f"{r['kcal']:.0f}" if r.get("kcal") is not None else "—"
                line += (f" | {kcal:>5} ккал/100г | Б{n(r.get('protein'))}"
                         f"/Ж{n(r.get('fat'))}/У{n(r.get('carb'))}")
            lines.append(line + f" | {_cut(r['name'], 42)} | id={r['id']}")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


# ── APP SEARCH ──────────────────────────────────────────────

# Pure UI scaffolding in the search response — carries no searchable entity.
_SEARCH_NOISE = {"masterWidget", "block_marker"}

def _search_rows(hits: list[dict]) -> tuple[list[dict], int]:
    """Flatten a search response into ({type, name, note, id} rows, skipped).

    Two hit shapes come back. `afisha`/`movie_main` return typed entities
    (eventId/eventName/objectName). `services` wraps results in `universal_block`
    display cards whose id only exists inside a deeplink
    ("tinkoffbank://…/Movies?movieId=104321").

    `skipped` counts hits with neither a name nor an id — dropping them used to be
    silent, and the «N результатов» header counted only the survivors, so a shape
    this parser does not know simply vanished. A hit with an id but no name is now
    KEPT as «[без названия]»: the id is actionable."""
    rows = []
    skipped = 0
    for hit in hits:
        kind = hit.get("objectType") or "?"
        src = hit.get("objectSource") or {}
        if kind == "universal_block":
            sub, sub_skipped = _search_rows(src.get("objects") or [])
            rows.extend(sub)
            skipped += sub_skipped
            continue
        if kind in _SEARCH_NOISE:
            continue
        # A VENUE hit (cinema/concerthall/theatre/museum, and the travel host's
        # airport/railway_stations) names itself in objectName and carries no
        # eventName at all — reading only the event keys dropped every one of them,
        # so searching for a cinema by name returned nothing. Its id sits both on
        # the hit and in objectSource.objectId.
        event_name = src.get("eventName") or ""
        name = event_name or src.get("objectName") or src.get("name") or ""
        note = ""
        ident = str(src.get("eventId") or src.get("objectId")
                    or src.get("id") or hit.get("id") or "")
        if not name and isinstance(src.get("title"), dict):
            name = src["title"].get("value") or ""
            note = ((src.get("titleDescription") or {}).get("value") or "")
            deeplink = (src.get("link") or {}).get("deeplink") or ""
            m = (re.search(r"[?&](?:movieId|eventId|cinemaId|id)=([^&]+)", deeplink)
                 or re.search(r"/Venue/(\d+)", deeplink))
            ident = m.group(1) if m else ident
        if not name:
            if not ident:
                skipped += 1
                continue
            name = "[без названия]"
        if not note:
            if event_name:
                # For an event, objectName is where it plays.
                parts = (src.get("objectName") or "", src.get("dateForShow") or "",
                         src.get("priceForShow") or "")
            else:
                # For a venue, objectName is already the name — what locates it is
                # the address.
                parts = (src.get("address") or "", src.get("city") or "")
            note = " · ".join(x for x in parts if x)
        rows.append({"type": kind, "name": name, "note": note, "id": ident})
    return rows, skipped

@mcp.tool()
def search_app(query: str, screen: str = "afisha", limit: int = 20) -> str:
    """Полнотекстовый поиск по разделу приложения.

    screen — СТРОГИЙ enum, угадывать бесполезно (всё остальное → 400):
      afisha     — кино, концерты, театр, выставки, спектакли (по умолчанию);
                   отдаёт eventId, готовый для cinema_schedule/concert_schedule
      movie_main — только фильмы
      services   — самый широкий: та же афиша плюс контакты из телефонной книги
                   и сервисные блоки; id приходится доставать из диплинка
      grocery    — каталог магазина, но для него есть grocery_search/grocery_rank
                   (там нужны app_id/point_id и фильтр «в наличии»)"""
    try:
        s = _require(); s.ensure_fresh()
        hits = s.app_search(query, screen=screen, limit=limit)
        rows, skipped = _search_rows(hits)
        if not rows:
            return f"По запросу {query!r} в разделе {screen!r} ничего не найдено."
        by_type: dict[str, list[dict]] = {}
        for r in rows:
            by_type.setdefault(r["type"], []).append(r)
        head = f"«{query}» в разделе {screen}: {len(rows)} результатов"
        if skipped:
            head += f" (+{skipped} нераспознанных скрыто)"
        if len(hits) >= limit > 0:
            head += f"; ровно limit={limit} — возможно, есть ещё, увеличь limit"
        out = [head]
        for kind, items in by_type.items():
            out.append(f"  {kind}:")
            for r in items:
                out.append(f"    - {_cut(r['name'], 56)}"
                           + (f" | {_cut(r['note'], 44)}" if r["note"] else "")
                           + (f" | id={r['id']}" if r["id"] else ""))
        return "\n".join(out)
    except Exception as e:
        return _err(e)


# ── CINEMA ──────────────────────────────────────────────────

@mcp.tool()
def cinema_search(query: str = "", city: str = "", limit: int = 20,
                  pages: int = 8, city_id: int = 0) -> str:
    """Найти фильм в прокате и его eventId (нужен для cinema_schedule).
    query — часть названия; пусто = вся сегодняшняя афиша города (её видно
    целиком только при limit=0 — по умолчанию показаны первые 20).
    city — город афиши, ОБЯЗАТЕЛЕН: молчаливая Москва даёт правдоподобный
    список чужого города. Известно 65 городов; если нужного нет в таблице,
    передай city_id числом (его видно в ошибке и в выдаче площадок).
    Сам eventId от города не зависит.
    pages — сколько страниц афиши сканировать (по 30 фильмов; раньше потолок
    8 страниц был зашит). Если шапка говорит «остальные НЕ проверены» —
    подними pages, limit скан не расширяет."""
    try:
        s = _require(); s.ensure_fresh()
        movies, scanned, listing = s.cinema_movies(city=city, query=query, city_id=city_id,
                                                   max_pages=pages)
        if not movies:
            return (f"В прокате ({city}) ничего не найдено по запросу {query!r} "
                    f"(просмотрено {scanned} из {listing} фильмов афиши).")
        def render(m):
            return (f"- {m.get('name','?')} [{m.get('ageRestriction','')}] "
                    f"| {', '.join(m.get('genres') or [])} | {m.get('country','')} "
                    f"| eventId={m.get('eventId','?')}")
        # Two different truncations, and they must not be confused: `limit` hides
        # matches we HAVE, `scanned < listing` means part of the afisha was never
        # looked at, so there may be matches we have not seen at all.
        header = f"В прокате ({city})" + (f" по запросу {query!r}" if query else "")
        if scanned < listing:
            header += (f" — просмотрено {scanned} из {listing} фильмов афиши, "
                       f"остальные НЕ проверены")
        return _rows_out(movies, render, limit=limit, total=len(movies),
                         header=header,
                         more_hint="Уточни query или передай limit=0.")
    except Exception as e:
        return _err(e)

@mcp.tool()
def cinema_schedule(event_id: str = "", date: str = "", cinema: str = "",
                    around: str = "", window_min: int = 90,
                    city: str = "", object_id: str = "") -> str:
    """Сеансы кино на дату. date — YYYY-MM-DD.

    Три режима:
    - object_id БЕЗ event_id — ВЕСЬ репертуар кинотеатра на день, один запрос.
      Так и надо отвечать на «что идёт завтра в этом кинотеатре»: перебирать
      афишу города по фильму и дорого, и неполно — сегодняшний список не знает
      о фильме, который идёт только завтра.
    - event_id + object_id — один фильм в одном кинотеатре.
    - event_id + city — этот фильм по всему городу, с сортировкой по расстоянию.

    objectId кинотеатра берётся из afisha_places() или из search_app().
    cinema — подстрока названия кинотеатра ("каро 11"), around — время "17:00",
    window_min — допуск в минутах вокруг него.
    city — обязателен, ЕСЛИ не задан object_id, и передаётся именем (этот
    эндпоинт берёт название, а не числовой cityId). Он же задаёт точку, от
    которой считается расстояние до кинотеатров, поэтому передавай тот же город,
    что и в cinema_search(): расписание Петербурга, отсортированное от центра
    Москвы, выглядит правдоподобно и бессмысленно. С object_id город не нужен —
    площадка его уже задаёт.
    Отдаёт objectId площадки и slotId каждого сеанса — оба нужны для
    cinema_seats() и cinema_book(), поодиночке бесполезны."""
    try:
        s = _require(); s.ensure_fresh()
        venues = s.cinema_schedule(event_id, date, city=city,
                                    object_id=object_id)
        want = cinema.lower().replace("ё", "е").split()
        target = None
        if around:
            hh, _, mm = around.partition(":")
            target = int(hh) * 60 + int(mm or 0)
        lines, shown = [], 0
        for v in venues:
            info = v.get("info") or {}
            name = str(info.get("objectName") or "")
            norm = name.lower().replace("ё", "е")
            if want and not all(w in norm for w in want):
                continue
            geo = info.get("geo") or {}
            for ev in (v.get("events") or []):
                slots = []
                for sl in (ev.get("slots") or []):
                    t = str(sl.get("startTime") or "")
                    if target is not None and ":" in t:
                        mins = int(t[:2]) * 60 + int(t[3:5])
                        if abs(mins - target) > window_min:
                            continue
                    slots.append(f"{t} — {(sl.get('prices') or {}).get('fix','?')} ₽ "
                                 f"({sl.get('hallName','')}, slotId={sl.get('slotId','?')})")
                if not slots:
                    continue
                shown += 1
                km = (geo.get("distance") or 0) / 1000.0
                # Two different questions share this response. Asked about a FILM,
                # each entry is a different cinema and the venue is the heading.
                # Asked about a CINEMA, every entry carries the SAME venue and what
                # varies is the film — printing the venue each time reads as «24
                # площадок» for one address.
                if object_id:
                    if not lines:
                        lines.append(f"{name} — {geo.get('address','')}"
                                     f" | objectId={info.get('objectId','?')}")
                    lines.append(f"  {ev.get('eventName') or '?'}")
                else:
                    lines.append(f"{name} — {geo.get('address','')}"
                                 + (f"  [{km:.1f} км]" if km else "")
                                 + f" | objectId={info.get('objectId','?')}")
                lines += [f"    {x}" for x in slots]
        if not lines:
            hint = f", фильтр «{cinema}»" if cinema else ""
            hint += f", около {around} ±{window_min} мин" if around else ""
            return (f"Сеансов на {date} не найдено ({len(venues)} кинотеатров в выдаче{hint}).")
        head = (f"{shown} фильмов в этом кинотеатре на {date}:" if object_id
                else f"{shown} площадок с подходящими сеансами на {date}:")
        return head + "\n" + "\n".join(lines)
    except Exception as e:
        return _err(e)


# ── TICKET BOOKING ──────────────────────────────────────────

def _seat_id_of(seat: dict) -> str:
    """The value order/create wants back for this seat, verbatim."""
    return str(seat.get("id") or seat.get("seatId") or "")


def _seat_rows(halls: list[dict], max_price: float = 0, row: str = "",
               kind: str = "movie", limit: int = 0) -> list[str]:
    """Vacant seats grouped by row, cheapest rows first.

    Concerts are printed differently, and must be: cinema_book needs the seat's own
    composite id ("Сектор|цена§~§ticketId|type") passed back verbatim, and this
    renderer only ever printed row and number — so the argument the tool demands was
    obtainable from no tool at all. Concert seats also frequently carry no `pos`, so
    grouping them by row collapsed the whole hall into a single «ряд —».

    `limit` overrides the display caps (40 concert seats / 24 numbers per row);
    0 keeps the defaults, and the «…ещё N» tail always says how to see the rest."""
    out = []
    concert_cap = limit if limit > 0 else 40
    row_cap = limit if limit > 0 else 24
    for hall in halls:
        seats = [s for s in (hall.get("seats") or []) if s.get("status") == "vacant"]
        if max_price:
            seats = [s for s in seats if float(s.get("price") or 0) <= max_price]
        if vertical(kind)["seat_render"] != "grid":
            if not seats:
                continue
            # Free seating hands out one entry per available TICKET, all sharing a
            # seatId — so len(seats) counts tickets, not choices, and printing them
            # straight repeats the same line dozens of times. seatsQuantity is the
            # sector's own count and is what the app shows.
            uniq: dict = {}
            for s in seats:
                uniq.setdefault(_seat_id_of(s) or len(uniq), s)
            free = int(hall.get("seatsQuantity") or 0) or len(seats)
            out.append(f"{hall.get('hallName','?')} — свободно {free}")
            for s in sorted(uniq.values(), key=lambda x: float(x.get("price") or 0))[:concert_cap]:
                pos = s.get("pos") or {}
                where = (f"ряд {pos.get('row')} место {pos.get('number')}"
                         if pos.get("row") or pos.get("number") else "без нумерации")
                sid = _seat_id_of(s)
                out.append(f"  {float(s.get('price') or 0):.0f} ₽ | {where} | "
                           + (f"seatId={sid}" if sid else "seatId=НЕТ В ОТВЕТЕ"))
            if len(uniq) > concert_cap:
                out.append(f"  …ещё {len(uniq) - concert_cap} вариантов "
                           f"(limit={len(uniq)} покажет все)")
            continue
        by_row: dict[str, list] = {}
        for s in seats:
            pos = s.get("pos") or {}
            by_row.setdefault(str(pos.get("row") or "—"), []).append(s)
        if not by_row:
            continue
        out.append(f"{hall.get('hallName','?')} — свободно {len(seats)}")
        for r in sorted(by_row, key=lambda x: (len(x), x)):
            if row and str(row) != r:
                continue
            group = sorted(by_row[r], key=lambda s: int((s.get("pos") or {}).get("number") or 0))
            prices = sorted({float(s.get("price") or 0) for s in group})
            nums = ", ".join(str((s.get("pos") or {}).get("number") or "?") for s in group[:row_cap])
            more = (f" …ещё {len(group) - row_cap} (limit={len(group)} покажет все)"
                    if len(group) > row_cap else "")
            price = (f"{prices[0]:.0f} ₽" if len(prices) == 1
                     else f"{prices[0]:.0f}–{prices[-1]:.0f} ₽")
            out.append(f"  ряд {r:>3} ({price}): {nums}{more}")
    return out

@mcp.tool()
def cinema_seats(event_id: str, slot_id: str, object_id: str,
                 row: str = "", max_price: float = 0, kind: str = "movie",
                 limit: int = 0, sector_id: str = "") -> str:
    """Свободные места на сеансе. Денег не двигает.
    slot_id и object_id — из cinema_schedule()/concert_schedule().
    row — показать только один ряд, max_price — потолок цены за место.
    kind — "кино" | "концерт" | "театр" | "выставка" (принимает и movie /
    concert / spectacle / exhibition).
    sector_id — показать один сектор; без него приходят все.
    limit — поднять кап показа (по умолчанию 40 мест / 24 номера в ряду;
    хвост «…ещё N» подсказывает значение).

    У кино места нумерованные — бронь идёт как "ряд:место". У остальных трёх
    вертикалей место опознаётся составным seatId, и его надо вернуть в
    cinema_book ЦЕЛИКОМ, как напечатано."""
    try:
        s = _require(); s.ensure_fresh()
        v = vertical(kind)
        halls = s.event_seats(event_id, slot_id, object_id, kind=kind,
                              sector_id=sector_id)
        if not halls:
            hint = (" Для свободной рассадки смотри concert_hall(kind=…) — "
                    "там места не нумеруются." if v["hall_key"] else "")
            return "Схема зала пуста." + hint
        lines = _seat_rows(halls, max_price=max_price, row=row, kind=kind, limit=limit)
        if not lines:
            return "Свободных мест по заданным условиям нет."
        nxt = ("\n\nДальше: cinema_book(event_id, slot_id, object_id, "
               f"seats=\"<seatId>,<seatId>\", kind=\"{kind}\") — передавай seatId "
               "ЦЕЛИКОМ, как напечатано выше. Это БРОНЬ без оплаты."
               if v["seat_render"] != "grid" else
               "\n\nДальше: cinema_book(event_id, slot_id, object_id, seats=\"ряд:место,…\")"
               " — это БРОНЬ без оплаты.")
        return "\n".join(lines) + nxt
    except Exception as e:
        return _err(e)

@mcp.tool()
def concert_hall(event_id: str, slot_id: str, object_id: str,
                 kind: str = "concert") -> str:
    """Секторы со свободной рассадкой (входные билеты, фан-зоны).
    kind — "концерт" или "театр": у кино места нумерованные, а у выставок
    такого экрана в API нет вовсе.

    Только чтение: примера создания заказа именно с этого экрана в захвате нет,
    поэтому бронировать отсюда MCP не умеет — только смотреть наличие. Сами
    места с их seatId видны в cinema_seats(kind=…)."""
    try:
        s = _require(); s.ensure_fresh()
        data = s.concert_hall(event_id, slot_id, object_id, kind=kind)
        info = data.get("info") or {}
        sectors = data.get("sectors") or []
        if not sectors:
            return "Секторов не найдено (возможно, у площадки нумерованные места — cinema_seats)."
        out = [f"{info.get('hallName','?')} | максимум мест в заказе: "
               f"{info.get('maxSeatsInOrder','?')}"]
        for sec in sectors:
            p = (sec.get("prices") or {}).get("fix")
            out.append(f"- {_cut(sec.get('sectorName','?'), 52)} | "
                       f"{'есть' if sec.get('isTicketsAvailable') else 'нет'} "
                       f"({sec.get('availableTickets', 0)} шт) | "
                       f"{f'{p:.0f} ₽' if p else 'цена не указана'}")
        return ("\n".join(out) +
                "\n\nОтсюда MCP не бронирует: захвата заказа именно с этого экрана нет. "
                "Места сектора со своими seatId — cinema_seats(sector_id=…, kind=…).")
    except Exception as e:
        return _err(e)

@mcp.tool()
def concert_schedule(event_id: str, kind: str = "concert",
                     object_id: str = "") -> str:
    """Показы концерта, спектакля или выставки: площадка, дата, slotId и
    objectId для cinema_seats().
    kind — "концерт" | "театр" | "выставка". Кино сюда НЕ ходит: у него показы
    привязаны к дате, это cinema_schedule(event_id, date).
    object_id — сузить до одной площадки.

    Даты в запросе нет: приходит всё будущее сразу, поэтому нужный день
    выбирай из напечатанного.
    event_id — из search_app(query, screen="afisha")."""
    try:
        s = _require(); s.ensure_fresh()
        venues = s.event_showings(event_id, kind=kind, object_id=object_id)
        if not venues:
            return f"Показов для события {event_id} не найдено."
        out = []
        for v in venues:
            info = v.get("info") or {}
            geo = info.get("geo") or {}
            out.append(f"{info.get('objectName','?')} — {geo.get('address','')} "
                       f"| objectId={info.get('objectId','?')}")
            for ev in (v.get("events") or []):
                for sl in (ev.get("slots") or []):
                    prices = sl.get("prices") or {}
                    fix, lo, hi = prices.get("fix"), prices.get("min"), prices.get("max")
                    # Theatre and exhibitions price by sector, so a single `fix`
                    # is the exception here rather than the rule.
                    if fix:
                        money = f"{fix:.0f} ₽"
                    elif lo or hi:
                        money = (f"{lo:.0f}–{hi:.0f} ₽" if lo and hi and lo != hi
                                 else f"от {(lo or hi):.0f} ₽")
                    else:
                        money = "цена по секторам"
                    out.append(f"    {str(sl.get('startDateTime',''))[:16]} | {money} "
                               f"| slotId={sl.get('slotId','?')}")
        return "\n".join(out)
    except Exception as e:
        return _err(e)

@mcp.tool()
def train_search(origin: str, destination: str, date: str, adults: int = 1,
                 children: int = 0, limit: int = 15) -> str:
    """Поиск поездов. origin/destination — ЧИСЛОВЫЕ коды станций банка
    (2000000 — Москва, 2004000 — Санкт-Петербург), date — YYYY-MM-DD.

    Резолвера «название станции → код» у банка нет. Если кода не знаешь,
    проверить пару можно train_calendar(origin, destination): она скажет, какие
    даты вообще в продаже, и на неверной паре ответит пусто.

    Купить билет через MCP нельзя: оплата уходит в webview, который headless не
    закрывается. Это поиск и сравнение."""
    try:
        s = _require(); s.ensure_fresh()
        ways, _ = s.train_search(origin, destination, date, adults=adults,
                                 children=children)
        if not ways:
            return (f"Поездов {origin}→{destination} на {date} не найдено. "
                    "Проверь коды станций через train_calendar().")

        def render(w):
            seg = (w.get("segments") or [{}])[0]
            o, d = seg.get("origin") or {}, seg.get("destination") or {}
            dep = str(seg.get("departureDateTime") or "")[11:16]
            arr = str(seg.get("arrivalDateTime") or "")[11:16]
            # The price is nested under refundablePrice.price, and the seat count
            # under places.total — neither is a flat field on the car group.
            def price_of(c):
                try:
                    return float((c.get("refundablePrice") or {}).get("price") or 0)
                except (TypeError, ValueError):
                    return 0.0

            cars = [c for c in (seg.get("carGroups") or []) if price_of(c) > 0]
            best = min(cars, key=price_of, default=None)
            seats = sum(int((c.get("places") or {}).get("total") or 0) for c in cars)
            money = (f"от {price_of(best):.0f} ₽ ({best.get('carTypeName') or ''}"
                     + (f", мест {seats}" if seats else "") + ")"
                     if best else "мест нет")
            return (f"- {dep}→{arr} | № {seg.get('displayTrainNumber', '?')} "
                    f"{_cut(seg.get('brandName') or seg.get('description') or '', 20)} | "
                    f"{_cut(o.get('stationName', ''), 22)} → "
                    f"{_cut(d.get('stationName', ''), 22)} | {money}")

        return _rows_out(ways, render, limit=limit, total=len(ways),
                         header=f"Поезда {origin}→{destination} на {date}",
                         order_note="как отдал банк",
                         more_hint=f"Передай limit={len(ways)}.")
    except Exception as e:
        return _err(e)


@mcp.tool()
def train_calendar(origin: str, destination: str, limit: int = 30) -> str:
    """Даты, на которые открыта продажа по направлению.

    Заодно дешёвая проверка пары кодов станций: на неверной паре ответ пуст."""
    try:
        s = _require(); s.ensure_fresh()
        dates = s.train_calendar(origin, destination)
        if not dates:
            return (f"По направлению {origin}→{destination} дат в продаже нет — "
                    "либо коды станций неверные, либо продажа закрыта.")
        return _rows_out(dates, lambda d: f"- {d.get('date', '?')}",
                         limit=limit, total=len(dates),
                         header=f"Даты в продаже {origin}→{destination}",
                         order_note="по возрастанию")
    except Exception as e:
        return _err(e)


@mcp.tool()
def flight_search(from_code: str, to_code: str, date: str, adults: int = 1,
                  children: int = 0, infants: int = 0,
                  only_bookable: bool = True, limit: int = 15,
                  max_batches: int = 8) -> str:
    """Поиск авиабилетов. from_code/to_code — коды IATA (MOW, LED, SVO),
    date — YYYY-MM-DD.

    Резолвера «название города → код» у банка нет. Коды вместе с названиями
    отдаёт flight_history() — оттуда их и бери, а не угадывай.

    only_bookable=True (по умолчанию) — только те предложения, что бронируются
    внутри банка; их отдаёт первый же батч, поэтому поиск быстрый. False
    дочитывает весь поток: это десятки секунд и тысячи предложений, почти все —
    от партнёров, которые уводят на свой сайт.

    Купить билет через MCP нельзя: подтверждённого шага бронирования и оплаты
    нет. Это поиск и сравнение, покупка — в приложении."""
    try:
        s = _require(); s.ensure_fresh()
        res = s.flight_search(from_code, to_code, date, adults=adults,
                              children=children, infants=infants,
                              only_bookable=only_bookable, max_batches=max_batches)
        flights, offers = res["flights"], res["offers"]
        if only_bookable:
            offers = [o for o in offers if str(o.get("vendor")) == "Tinkoff"]
        if not offers:
            return (f"Рейсов {from_code}→{to_code} на {date} не найдено"
                    + (" среди бронируемых в банке (попробуй only_bookable=False)."
                       if only_bookable else "."))
        def money(o):
            # price is {"amount": "19076.41", "currency": "RUB"}, not a number.
            try:
                return float((o.get("price") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                return 0.0

        offers = sorted(offers, key=lambda o: money(o) or 1e12)
        names = ((res.get("info") or {}).get("carrierNames") or {})

        def leg(i):
            f = flights[i] if 0 <= i < len(flights) else {}
            segs = f.get("flightSegments") or []
            if not segs:
                return ""
            dep, arr = segs[0].get("departure") or {}, segs[-1].get("arrival") or {}
            car = (segs[0].get("carriers") or {}).get("marketing") or ""
            hop = f", {len(segs) - 1} пересадка" if len(segs) > 1 else " прямой"
            dur = f.get("duration") or 0
            return (f"{names.get(car, car)} {dep.get('airport', '')}→{arr.get('airport', '')}"
                    f" {str(dep.get('time') or '')[11:16]}"
                    + (f" ({dur // 60}ч{dur % 60:02d}м{hop})" if dur else hop))

        def render(o):
            idx = o.get("flights") or []
            route = " / ".join(x for x in (leg(i) for i in idx[:2]) if x)
            bag = "с багажом" if o.get("withBaggage") else "только ручная кладь"
            ref = "возврат" if o.get("refundable") else "невозвратный"
            return (f"- {money(o):.0f} ₽ | {route or '—'} | {bag} | {ref}"
                    + (f" | offerId={o.get('offerId')}" if o.get("offerId") else ""))

        head = (f"Рейсы {from_code}→{to_code} на {date}"
                + (" (бронируемые в банке)" if only_bookable else ""))
        tail = "" if res["complete"] else (
            f"\n⚠️ Поток оборван на {res['batches']} батчах — это НЕ вся выдача. "
            "Подними max_batches, если нужно всё.")
        return _rows_out(offers, render, limit=limit, total=len(offers),
                         header=head, order_note="дешёвые сверху",
                         more_hint=f"Передай limit={len(offers)}.") + tail
    except Exception as e:
        return _err(e)


@mcp.tool()
def flight_history() -> str:
    """История авиапоисков — и единственный источник кодов IATA с названиями.

    Резолвера «название → код» у банка нет, поэтому если пользователь называет
    город словами, ищи код здесь, а не подставляй по памяти."""
    try:
        s = _require(); s.ensure_fresh()
        rows = s.flight_history()
        if not rows:
            return ("История авиапоисков пуста — кодов IATA взять неоткуда. "
                    "Попроси пользователя назвать код (MOW, LED, SVO).")

        def place(p):
            p = p or {}
            name = (p.get("city_name") or {}).get("ru") or (p.get("name") or {}).get("ru", "")
            code = p.get("code") or p.get("city_code") or "?"
            return f"{name} ({code})"

        def render(h):
            pax = h.get("passengers") or {}
            return (f"- {str(h.get('searchTime') or '')[:10]} | "
                    f"{place(h.get('from'))} → {place(h.get('to'))}"
                    + (f" | взрослых {pax.get('adults')}" if pax.get("adults") else ""))

        return _rows_out(rows, render, limit=0, total=len(rows),
                         header="История авиапоисков")
    except Exception as e:
        return _err(e)


@mcp.tool()
def shop_search(query: str, limit: int = 20, offset: int = 0) -> str:
    """Поиск товаров в маркетплейсе Т-Банка (Город → Шопинг).

    Пагинация СЕРВЕРНАЯ: offset листает выдачу, всего результатов видно в шапке.
    Печатает skuId, pointId и shopId — эту тройку принимает корзина.

    Оформить и оплатить заказ отсюда НЕЛЬЗЯ: в захвате нет подтверждённого шага
    размещения, только расчёт доставки. Собранную корзину пользователь
    оформляет в приложении."""
    try:
        s = _require(); s.ensure_fresh()
        size = limit if limit > 0 else 20
        products, partners, total = s.shop_search(query, offset=offset, size=size)
        if not products:
            return f"По запросу «{query}» товаров не найдено."
        by_shop = {str(p.get("id")): p.get("name") for p in partners}

        def render(p):
            price, old = p.get("price"), p.get("oldPrice")
            money = f"{price} ₽" if price else "цена не указана"
            if old and old != price:
                money += f" (было {old}, {p.get('discount') or ''})".rstrip(", )") + ")"
            seller = by_shop.get(str(p.get("dolyameShopId")), "")
            rating = p.get("rating") or 0
            return (f"- {money} | {_cut(p.get('name', '?'), 52)}"
                    + (f" | {seller}" if seller else "")
                    + (f" | ★{rating} ({p.get('totalRatings')})" if rating else "")
                    + ("" if p.get("available", True) else " | НЕТ В НАЛИЧИИ")
                    + f" | skuId={p.get('skuId') or p.get('id')}"
                    + f" pointId={p.get('pointId', '?')}"
                    + f" shopId={p.get('dolyameShopId', '?')}")

        nxt = (f"Следующая страница: offset={offset + len(products)}."
               if offset + len(products) < total else "")
        return _rows_out(products, render, limit=size, total=total,
                         header=f"Товары по запросу «{query}»",
                         order_note="как отдал магазин",
                         more_hint=nxt or "Это всё.")
    except Exception as e:
        return _err(e)


@mcp.tool()
def shop_cart(limit: int = 20) -> str:
    """Корзины маркетплейса — по одной на продавца.

    Оформление заказа через MCP не поддерживается: подтверждённого шага
    размещения в захвате нет. Корзину видно, оплатить её надо в приложении.

    limit — сколько позиций одной корзины показать (<=0 — все); каждая корзина
    рассчитывается отдельно, с честным «N всего, показано M»."""
    try:
        s = _require(); s.ensure_fresh()
        carts = s.shop_carts()
        if not carts:
            return "Корзины маркетплейса пусты."
        out = []
        for c in carts:
            items = c.get("items") or c.get("goods") or []

            def render(it):
                kop = it.get("totalPriceInKopecks")
                money = f"{kop / 100:.0f} ₽" if kop else ""
                return (f"  - {_cut(it.get('name', '?'), 46)} × "
                        f"{it.get('quantity', '?')}"
                        + (f" = {money}" if money else ""))

            header = (f"{c.get('merchantName') or c.get('shoppingMerchantId') or '?'}"
                      + (f" | cartId={c['cartId']}" if c.get("cartId") else ""))
            out.append(_rows_out(items, render, limit=limit, total=len(items),
                                 header=header, order_note="как в корзине"))
        return "\n\n".join(out)
    except Exception as e:
        return _err(e)


@mcp.tool()
def ticket_qr(order_id: str) -> str:
    """Сам билет по оплаченному заказу: код брони, QR и ссылка на PDF.

    Лежит это в ленте заказов, а НЕ в order_details(), который отдаёт только код
    брони. Что именно есть — зависит от партнёра: из 75 афишных заказов код
    брони был у всех, QR у 53, а Ticketland не даёт ни QR, ни PDF. Тул печатает
    то, что есть, и прямо говорит, чего нет.

    QR — это короткая строка-payload, которую показывают сканеру, а не картинка.

    Пустой ответ означает «билета ещё нет» (или бронь не оплачена — неоплаченные
    в ленту не попадают), а не «заказа не существует»."""
    try:
        s = _require(); s.ensure_fresh()
        a = s.ticket_artifacts(order_id)
        if not a.get("found"):
            return (f"Заказа {order_id} нет в ленте. Неоплаченные брони туда не "
                    "попадают — проверь order_details(), он их видит.")
        head = " | ".join(x for x in (a["event"], a["venue"], a["hall"]) if x)
        out = [f"{head or 'Заказ'} — {order_id} ({a['status']})"]
        if a["reservation_code"]:
            out.append(f"Код брони: {a['reservation_code']}")
        if a["qr"]:
            out.append(f"QR: {a['qr']}"
                       + (f" (тип {a['barcode_type']})" if a["barcode_type"] else "")
                       + " — это строка для сканера, не картинка")
        for label, url in (("PDF", a["pdf_url"]), ("Билет", a["ticket_url"])):
            if url:
                out.append(f"{label}: {url}")
        if not (a["qr"] or a["pdf_url"] or a["ticket_url"]):
            out.append(f"QR и PDF нет" + (f" — {a['partner']} их не выдаёт"
                                          if a["partner"] else "")
                       + ". На входе показывают код брони.")
            out.append("Если билет только что оплачен, он может появиться с задержкой.")
        elif a["partner"]:
            out.append(f"Партнёр: {a['partner']}")
        return "\n".join(out)
    except Exception as e:
        return _err(e)


@mcp.tool()
def afisha_catalog(kind: str = "movie", city: str = "", date_from: str = "",
                   date_to: str = "", query: str = "", city_id: int = 0,
                   limit: int = 20, pages: int = 8) -> str:
    """Афиша вертикали за ПЕРИОД дат: что идёт с date_from по date_to.

    kind — "кино" | "концерт" | "театр". У выставок каталога по датам нет —
    для них search_app(screen="afisha") или place_schedule().
    city обязателен (или city_id числом), даты — YYYY-MM-DD; одна дата = один
    день.

    Диапазон реально работает: неделя показывает заметно больше, чем сутки, —
    туда попадают разовые показы, которых в однодневной выдаче нет.

    У кино сеансы здесь НЕ приходят: их даёт cinema_schedule(event_id, date).
    У концертов и спектаклей ближайшие слоты видно сразу.
    query — фильтр по названию, местный."""
    try:
        s = _require(); s.ensure_fresh()
        events, amount = s.afisha_catalog(kind=kind, city=city, city_id=city_id,
                                          date_from=date_from, date_to=date_to,
                                          query=query, max_pages=pages)
        if not events:
            return (f"Ничего не найдено ({kind}, {city or city_id}, "
                    f"{date_from or '?'}..{date_to or date_from or '?'}"
                    + (f", запрос «{query}»" if query else "") + ").")

        def render(e):
            f = e.get("fields") or {}
            rating = (f.get("rating") or {}).get("value")
            slots = e.get("slots") or []
            when = str((slots[0] or {}).get("startDateTime") or "")[:16] if slots else ""
            return (f"- {_cut(e.get('eventName', '?'), 46)}"
                    + (f" [{f.get('ageRestriction')}]" if f.get("ageRestriction") else "")
                    + (f" | ★{rating}" if rating else "")
                    + (f" | {', '.join(e.get('genres') or [])[:34]}"
                       if e.get("genres") else "")
                    + (f" | ближайший {when}" if when else "")
                    + f" | eventId={e.get('eventId', '?')}")

        head = (f"Афиша ({kind}, {city or city_id}, "
                f"{date_from or date_to}..{date_to or date_from})")
        if query:
            head += f", совпадений с «{query}»"
        return _rows_out(events, render, limit=limit,
                         total=len(events) if query else max(amount, len(events)),
                         header=head,
                         more_hint=f"Передай limit={len(events)}, чтобы увидеть все.")
    except Exception as e:
        return _err(e)


@mcp.tool()
def afisha_places(kind: str = "movie", city: str = "", query: str = "",
                  city_id: int = 0, limit: int = 20, pages: int = 4) -> str:
    """Площадки города: кинотеатры, залы, театры, музеи — с их objectId.

    Это единственный способ узнать objectId площадки, не заходя через какое-то
    событие в ней. Дальше objectId принимают cinema_schedule(object_id=…) —
    весь репертуар кинотеатра на день, — place_schedule() и place_info().

    kind — "кино" | "концерт" | "театр" | "выставка". city обязателен (или
    city_id числом).
    query — фильтр по названию. Он МЕСТНЫЙ: у банка текстового поиска по
    площадкам нет, поэтому страницы читаются целиком до фильтрации.
    pages — сколько страниц по 100 прочитать."""
    try:
        s = _require(); s.ensure_fresh()
        places, total = s.afisha_places(kind=kind, city=city, city_id=city_id,
                                        query=query, max_pages=pages)
        if not places:
            return (f"Площадок не найдено ({kind}, {city or city_id}"
                    + (f", запрос «{query}»" if query else "") + ").")

        def render(o):
            metro = ", ".join(m.get("name", "") for m in (o.get("subways") or []) if m)
            return (f"- {_cut(o.get('name', '?'), 44)} | "
                    f"{_cut(o.get('address') or '—', 46)}"
                    + (f" | м. {_cut(metro, 28)}" if metro else "")
                    + f" | objectId={o.get('id', '?')}")

        head = f"Площадки ({kind}, {city or city_id})"
        if query:
            head += f", совпадений с «{query}»"
        return _rows_out(places, render, limit=limit,
                         total=len(places) if query else total, header=head,
                         more_hint=f"Передай limit={len(places)}, чтобы увидеть все.")
    except Exception as e:
        # A 204 here is «this vertical is not serving», not «no venues» — live,
        # only cinema answers while concert, theatre and exhibition all 204.
        msg = _err(e)
        if "204" in msg:
            msg += (f"\nЭто не «площадок нет»: раздел «{kind}» сейчас не отвечает. "
                    "Живьём отвечает только кино.")
        return msg


@mcp.tool()
def place_schedule(object_id: str, limit: int = 20, page: int = 1,
                   count: int = 50) -> str:
    """Что идёт на площадке: концерты, спектакли, выставки.

    КИНО здесь НЕТ — репертуар кинотеатра берётся
    cinema_schedule(object_id=…, date=…).
    object_id — из afisha_places() или search_app()."""
    try:
        s = _require(); s.ensure_fresh()
        events, total = s.place_schedule(object_id, page=page, count=count)
        if not events:
            return (f"На площадке {object_id} ничего не найдено. Если это "
                    "кинотеатр — cinema_schedule(object_id=…, date=…).")

        def render(e):
            dates = e.get("dates") or []
            when = str(dates[0])[:10] if dates else str(e.get("date") or "")[:10]
            pr = e.get("prices") or (e.get("fields") or {}).get("prices") or {}
            lo, hi = pr.get("min"), pr.get("max")
            money = (f"{lo:.0f}–{hi:.0f} ₽" if lo and hi and lo != hi
                     else f"от {lo:.0f} ₽" if lo else "цена не указана")
            return (f"- {_cut(e.get('eventName') or e.get('name') or '?', 46)} | "
                    f"{when or '—'} | {money} | eventId={e.get('eventId', '?')}")

        return _rows_out(events, render, limit=limit, total=total,
                         header=f"Афиша площадки {object_id}",
                         more_hint=f"Передай limit={total} или page={page + 1}.")
    except Exception as e:
        return _err(e)


@mcp.tool()
def place_info(object_id: str, with_halls: bool = False, limit: int = 10) -> str:
    """Карточка площадки: название, город, метро, залы.

    Адрес в самой карточке приходит ПУСТЫМ — во всех захваченных ответах, — так
    что with_halls=True дочитывает залы, где адрес есть.

    limit — сколько залов показать (<=0 — все), с честным «N всего, показано M»."""
    try:
        s = _require(); s.ensure_fresh()
        d = s.place_info(object_id, with_halls=with_halls)
        if not d:
            return f"Площадка {object_id} не найдена."
        info = d.get("info") if isinstance(d.get("info"), dict) else d
        geo = info.get("geo") or {}
        metro = ", ".join(m.get("name", "") for m in (info.get("subways") or []) if m)
        out = [f"{info.get('objectName') or info.get('name') or '?'} "
               f"| objectId={object_id}"]
        if geo.get("city"):
            out.append(f"Город: {geo['city']}")
        if geo.get("address"):
            out.append(f"Адрес: {geo['address']}")
        if metro:
            out.append(f"Метро: {metro}")
        halls = d.get("halls") or []

        def render_hall(h):
            hgeo = h.get("geo") or {}
            return (f"- зал {h.get('hallName') or h.get('name') or '?'}"
                    + (f" — {hgeo.get('address')}" if hgeo.get("address") else ""))

        if halls:
            out.append(_rows_out(halls, render_hall, limit=limit, total=len(halls),
                                 header="Залы"))
        if not geo.get("address") and not halls:
            out.append("Адрес в карточке пуст — вызови с with_halls=True.")
        return "\n".join(out)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cinema_book(event_id: str, slot_id: str, object_id: str, seats: str,
                kind: str = "movie", seat_type: str = "basic") -> str:
    """ЗАБРОНИРОВАТЬ места. Создаёт заказ, но НЕ платит — деньги списывает
    отдельный ticket_pay(). Неоплаченная бронь отваливается сама.

    kind — "кино" | "концерт" | "театр" | "выставка".
    seats — через запятую: для кино "7:10,7:11" (ряд:место из cinema_seats),
    для остальных — составные seatId из cinema_seats(kind=…) как есть.

    seat_type применяется ТОЛЬКО к кино: у трёх других вертикалей поля type в
    запросе нет вовсе — так в захвате.

    Покажи пользователю итоговую сумму со сбором ДО вызова ticket_pay."""
    try:
        s = _require(); s.ensure_fresh()
        t = vertical(kind)["seat_type"]
        ids = [x.strip() for x in seats.split(",") if x.strip()]
        if not ids:
            return "Не переданы места. Пример: seats=\"7:10,7:11\"."
        payload = [{"id": i, **({"type": seat_type} if t else {})} for i in ids]
        res = s.create_ticket_order(event_id, slot_id, object_id, payload, kind=kind)
        order = res.get("order") or {}
        cart = res.get("cart") or []
        if not order.get("orderId"):
            return f"Заказ не создан: {json.dumps(res, ensure_ascii=False)[:400]}"
        total = sum(float(c.get("price") or 0) for c in cart) or order.get("price")
        fee = sum(float(c.get("serviceFee") or 0) for c in cart)
        out = [f"ЗАБРОНИРОВАНО (не оплачено): заказ {order['orderId']}",
               f"{order.get('eventName','?')} | {order.get('objectName','')} "
               f"| {str(order.get('dateTime',''))[:16]}"]
        for c in cart:
            pos = ((c.get("fields") or {}).get("seatPos") or {})
            where = (f"ряд {pos.get('row')}, место {pos.get('number')}" if pos
                     else (c.get("fields") or {}).get("sectorName", ""))
            out.append(f"  - {c.get('price','?')} ₽ ({where})")
        out.append(f"Итого: {total} ₽" + (f", в т.ч. сервисный сбор {fee:.0f} ₽" if fee else ""))
        for cb in (order.get("cashbackInfos") or []):
            out.append(f"Кэшбэк: {cb.get('value')}%")
        out.append(
            f"\nОплатить: ticket_pay(\"{order['orderId']}\", {total}, "
            f"\"{order.get('nfsPaymentToken','')}\") — РЕАЛЬНЫЕ ДЕНЬГИ, сначала "
            "подтверди сумму с пользователем. Токен возвращается ТОЛЬКО здесь, "
            "order_details() его не отдаёт — не потеряй.")
        return "\n".join(out)
    except Exception as e:
        return _err(e)

@mcp.tool()
def ticket_pay(order_id: str, amount: float, nfs_payment_token: str,
               account_id: str = "") -> str:
    """ОПЛАТИТЬ бронь билета. РЕАЛЬНЫЕ ДЕНЬГИ — вызывай ТОЛЬКО после того, как
    пользователь подтвердил конкретную сумму и заказ. Сам по себе запрос
    пользователя «купи билет» подтверждением НЕ является.

    Все три первых аргумента бери из ответа cinema_book(): order_id, итоговую
    сумму и nfs_payment_token. Токен живёт только в ответе на создание заказа —
    order_details() его не отдаёт, поэтому переспросить потом будет негде.
    account_id — счёт списания (по умолчанию первый рублёвый Current)."""
    try:
        s = _require(); s.ensure_fresh()
        if not nfs_payment_token:
            return ("Нужен nfs_payment_token из ответа cinema_book() — платёжный шлюз "
                    "без него не примет заказ, а order_details() этот токен не возвращает.")
        # Cross-check against the order the bank actually holds: paying an amount
        # that disagrees with the order is how a payment gets stuck half-applied.
        info = s.order_details(order_id)
        booked = (info.get("cartInfo") or {}).get("amount")
        if booked and abs(float(booked) - float(amount)) > 0.01:
            return (f"Сумма не сходится: передано {amount} ₽, а в заказе {order_id} "
                    f"{booked} ₽. Оплату не запускаю — сверься с order_details().")
        account = account_id or s._source_account()
        res = s.pay_marketplace_order(order_id, float(amount), account, nfs_payment_token)
        stage = res.get("stage") or {}
        status = stage.get("status") or stage.get("type") or "?"
        if str(status).upper() != "SUCCESS":
            return (f"Оплата заказа {order_id} НЕ подтверждена: {json.dumps(res, ensure_ascii=False)[:300]}\n"
                    "Не повторяй вслепую — сначала проверь orders() и order_details().")
        return (f"ОПЛАЧЕНО: заказ {order_id}, {amount} ₽ со счёта {account}. "
                f"paymentId={res.get('paymentId','?')}\n"
                f"Код брони и места — order_details(\"{order_id}\").")
    except Exception as e:
        return _err(e)

@mcp.tool()
def ticket_cancel(order_id: str, kind: str = "movie", payment_id: str = "",
                  force: bool = False) -> str:
    """Отменить заказ билета. kind — "movie" или "concert".

    Отменяется заказ, у которого банк сам выставил isCancelAvailable=true — это
    видно в order_details(). Такой заказ уходит в PARTIALLY_CANCELED, а не
    CANCELED: билеты возвращают, сервисный сбор — нет, и «частично» здесь не
    ошибка.

    Заказ, помеченный isCancelAvailable=false, хост отменять отказывается:
    отвечает status=Failed с кодом и НИЧЕГО не меняет. Повторять такой вызов
    бессмысленно.

    Тул сначала читает заказ и, если банк отменять не даёт, НЕ ходит в хост
    вовсе — такой запрос всё равно ничего бы не изменил. force=True отправляет
    его всё равно.

    payment_id подставляется из заказа, если его не передать; он же лежит в
    ответе ticket_pay(). У неоплаченной брони его нет — её и не нужно отменять,
    она истекает сама.

    Если тул вернёт ошибку, считай статус НЕИЗВЕСТНЫМ (не «всё ещё
    забронировано») — проверь orders() и при необходимости отменяй через
    приложение."""
    try:
        s = _require(); s.ensure_fresh()
        # One read gives all three: the bank's own verdict on whether cancelling is
        # possible, the order status to print, and the paymentId — which used to
        # cost a scan of the whole orders feed.
        ctx = s.order_cancel_context(order_id)
        if ctx["available"] is False and not force:
            return (f"Заказ {order_id} (status={ctx['status'] or '?'}) банк отменять не "
                    "даёт: isCancelAvailable=false. Запрос не отправлен — он вернул бы "
                    "status=Failed и ничего не изменил.\nОтменяй через приложение или "
                    "поддержку. force=True — отправить всё равно.")
        payment_id = payment_id or ctx["payment_id"]
        res = s.cancel_ticket_order(order_id, kind=kind, payment_id=payment_id)
        st = str((res or {}).get("status") or "") if isinstance(res, dict) else ""
        code = str((res or {}).get("code") or "") if isinstance(res, dict) else ""
        if st.lower() == "success":
            head = f"Отмена заказа {order_id} принята (status=Success)."
            try:
                after = s.order_cancel_context(order_id)
                head += f"\nПерепроверка: status={after['status'] or '?'}"
            except Exception:
                head += "\nПерепроверка не удалась — проверь orders(\"афиша\")."
        elif st:
            # A refusal, not a failure: the envelope was Ok, the backend simply
            # said no. Retrying sends the same request to the same answer.
            head = (f"Отмена заказа {order_id} ОТКЛОНЕНА банком: status={st}"
                    + (f", code={code}" if code else "")
                    + ". Заказ не изменился, повторять бессмысленно.")
        else:
            head = (f"Отмена заказа {order_id} не подтверждена: в ответе нет status. "
                    "Статус НЕИЗВЕСТЕН.")
        return head + "\nПроверь статус и возврат: orders(\"афиша\") + list_operations()."
    except Exception as e:
        return (_err(e) + f"\nЭто ошибка запроса, а не отказ банка — запрос мог и дойти. "
                f"Статус заказа {order_id} НЕИЗВЕСТЕН, проверь orders(\"афиша\"). "
                "Если он всё ещё активен, отмени через приложение.")


# ── EXTRAS ──────────────────────────────────────────────────

@mcp.tool()
def bank_documents() -> str:
    """Справки, заказанные в банке (о движении средств, о доходах и т.п.)."""
    try:
        s = _require(); s.ensure_fresh()
        docs = s.bank_documents()
        if not docs:
            return "Справок нет."
        return "\n".join(
            f"- {d.get('title','?')} | {d.get('subtitleTop','')} | {d.get('subtitleBottom','')} "
            f"| {datetime.fromtimestamp((d.get('creationDate') or 0)/1000).strftime('%d.%m.%Y')} "
            # v2 records put the uuid in tecmId; v1 records keep it in tecmUuid
            # and use tecmId for a (negative) internal int — prefer the uuid.
            f"| id={d.get('tecmUuid') or d.get('tecmId','?')}"
            for d in docs)
    except Exception as e:
        return _err(e)

@mcp.tool()
def insurance_policies() -> str:
    """Действующие страховые полисы (ОСАГО/КАСКО/путешествия) с суммами и сроками."""
    try:
        s = _require(); s.ensure_fresh()
        data = s.insurance_policies()
        pol = data.get("Payload") if isinstance(data, dict) else data
        if isinstance(pol, dict):
            pol = pol.get("Policies") or pol.get("policies") or [pol]
        if not pol:
            return "Действующих полисов нет."
        out = []
        for p in pol:
            if not isinstance(p, dict):
                continue
            out.append(f"- {p.get('Type', p.get('type','?'))} №{p.get('PolicyNumber', p.get('policyNumber','?'))} "
                       f"| {p.get('Status', p.get('status',''))} "
                       f"| {str(p.get('FromDate', p.get('fromDate','')))[:10]} — "
                       f"{str(p.get('ToDate', p.get('toDate','')))[:10]} "
                       f"| премия {p.get('Bounty', p.get('bounty','?'))}")
        # _json_out, not dumps()[:2000]: the fallback fires exactly when the shape
        # is unknown, and a bare cut of an unknown shape is invisible data loss.
        return "\n".join(out) or _json_out(data, 2000)
    except Exception as e:
        return _err(e)

@mcp.tool()
def payment_receipt(payment_id: str, save_to: str = "") -> str:
    """Скачать PDF-чек по платежу. save_to — путь файла (по умолчанию /tmp).

    payment_id берётся ровно из пяти мест, других производителей нет:
    orders() (поле paymentId в строке заказа), grocery_order_status(),
    и ответы transfer(), pay_bill() и ticket_pay(). В list_operations() его НЕТ —
    операция и платёж нумеруются по-разному."""
    try:
        s = _require(); s.ensure_fresh()
        pdf = s.payment_receipt_pdf(payment_id)
        if not pdf.startswith(b"%PDF"):
            return f"Ответ не PDF ({len(pdf)} байт): {pdf[:200]!r}"
        path = save_to or os.path.join("/tmp", f"receipt-{payment_id}.pdf")
        with open(path, "wb") as fh:
            fh.write(pdf)
        return f"Чек сохранён: {path} ({len(pdf)} байт)"
    except Exception as e:
        return _err(e)


# ── UTILITY ─────────────────────────────────────────────────

_FLOWS_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "FLOWS.md")

# Words an agent is likely to use, per section. Matched against the query in
# addition to the section title, so a Russian request finds an English heading.
_FLOW_KEYWORDS = {
    "bootstrap": "логин вход авторизация otp смс пароль пин первый login",
    "marketplace": "маркетплейс шопинг товар товары купить магазин продавец корзина",
    "flights": "самолёт самолет авиа авиабилет перелёт перелет рейс аэропорт",
    "rail": "поезд поезда ржд купе плацкарт сапсан вокзал станция",
    "session": "сессия токен refresh keepalive expired протух",
    "read accounts": "счета счёт баланс операции покупки траты расходы категории",
    "grocery cart": "продукты еда корзина магазин вкусвилл лента самокат азбука доставка",
    "transfer": "перевод перевести деньги сбп телефону получатель комиссия оплата счёта",
    "messenger": "чат чаты поддержка сообщение написать непрочитанные",
    "invest": "инвестиции акции облигации портфель брокер бумаги доходность",
    "credit": "кредит кредиты долг задолженность график платежей рейтинг выписка",
    "cards": "карта карты реквизиты лимиты cvv пин документы паспорт снилс инн права",
    "orders": "заказы заказ история покупок отель поездка путешествия авиа поезд",
    "nutrition": "кбжу калории белки жиры углеводы питание состав диета",
    "tickets": "билет билеты кино фильм сеанс концерт афиша театр места бронь",
    "global search": "поиск найти искать search",
}


def _flow_sections() -> list[tuple[str, str]]:
    """docs/FLOWS.md split on '## ' headings → [(title, body), …]."""
    if not os.path.exists(_FLOWS_PATH):
        return []
    text = open(_FLOWS_PATH, encoding="utf-8").read()
    out = []
    for chunk in re.split(r"^## ", text, flags=re.M)[1:]:
        title, _, body = chunk.partition("\n")
        out.append((title.strip(), body.rstrip()))
    return out


@mcp.tool()
def flows(topic: str = "") -> str:
    """Гид по флоу: порядок вызовов для конкретной задачи.

    topic — что тебе нужно, своими словами: «продукты», «перевод», «билеты»,
    «карты», «заказы», «кбжу», «инвест», «кредит», «чат», «поиск», «логин».
    Без аргумента — список тем и общие правила (там же про тулы с реальными
    деньгами). Отдаёт только подходящие разделы, а не весь файл."""
    sections = _flow_sections()
    if not sections:
        return f"docs/FLOWS.md not found at {_FLOWS_PATH}"

    def body_of(name_part: str) -> str:
        for title, body in sections:
            if name_part.lower() in title.lower():
                return f"## {title}\n{body}"
        return ""

    q = topic.strip().lower()
    if not q:
        toc = "\n".join(f"- {t}" for t, _ in sections if not t.lower().startswith("notes"))
        return ("Разделы docs/FLOWS.md — вызови flows(topic) с нужным:\n" + toc +
                "\n\n" + body_of("Notes"))

    tokens = [w for w in re.split(r"[^\wа-яёА-ЯЁ]+", q) if len(w) > 2]
    scored = []
    for title, body in sections:
        hay = (title + " " + _FLOW_KEYWORDS.get(
            next((k for k in _FLOW_KEYWORDS if k in title.lower()), ""), "")).lower()
        score = sum(1 for w in tokens if w in hay)
        if score:
            scored.append((score, title, body))
    if not scored:
        toc = "\n".join(f"- {t}" for t, _ in sections if not t.lower().startswith("notes"))
        return (f"По запросу «{topic}» раздел не найден. Есть эти:\n" + toc +
                "\n\nВызови flows(topic) с одним из них.")
    scored.sort(key=lambda x: -x[0])
    return "\n\n".join(f"## {t}\n{b}" for _, t, b in scored[:3])


def main():
    mcp.run()

if __name__ == "__main__":
    main()
