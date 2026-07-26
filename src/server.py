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
from .client import MobileSession, TbankApiError, SessionExpired, ms_for_period
from .observability import redact_text

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
    "cinema_seats": ("Свободные места", READ),
    "concert_schedule": ("Показы концерта", READ),
    "concert_hall": ("Секторы концертной площадки", READ),
    "cinema_book": ("Бронирование мест", WRITE),
    "ticket_pay": ("Оплата брони", MONEY),
    "ticket_cancel": ("Отмена заказа", WRITE),
    # search
    "search_app": ("Поиск по приложению", READ),
    # messenger
    "messenger_conversations": ("Чаты", READ),
    "messenger_messages": ("История чата", READ),
    "messenger_unread": ("Непрочитанные", READ),
    "messenger_send": ("Отправка сообщения", WRITE),
    # money
    "transfer_sbp_resolve": ("Получатель СБП по телефону", READ),
    "payment_commission": ("Предпросмотр комиссии", READ),
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
        return redact_text(str(msg))[:300]
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
    is trimmed through a holder, and each pass re-picks whatever is biggest now."""
    full = json.dumps(data, ensure_ascii=False, default=str)
    if len(full) <= limit:
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


def _rows_out(rows, render, *, limit: int, total: int, header: str, more_hint: str = "") -> str:
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
        head += f" (новые сверху). {more_hint or f'Передай limit={total}, чтобы увидеть все.'}"
    return "\n".join([head] + [render(r) for r in shown])


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
            f"{a.get('name','')[:30]} | {(a.get('moneyAmount') or {}).get('value','?')} "
            f"{((a.get('currency') or {}).get('name','') if isinstance(a.get('currency'),dict) else a.get('currency',''))}"
            for a in accs)
    except Exception as e:
        return _err(e)

@mcp.tool()
def list_operations(account_id: str, days: int = 30, limit: int = 50) -> str:
    """Операции за период, новые сверху.

    limit — сколько показать (0 = все). В шапке всегда указано, сколько операций
    всего за период, поэтому видно, обрезан ли ответ."""
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
                    f"{(o.get('description') or '')[:40]}")
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
            lines.append(f"- {c['category'][:25]:25} {num(c['amount'], 12)} {c['share_pct']:5.1f}%")
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
def get_data(section: str, arg: str = "") -> str:
    """Универсальный getter. section = subscriptions | credit_schedule | credit_rating |
    statements | invoices | templates | contacts | cards | loans | autopayments |
    sbp | offers | gifts | services | bundles | manager | merchant_subs | profile | homes |
    cars | shortcuts | finhealth_total | finhealth_turnover | invest_accounts |
    invest_offers | invest_yield | pension | broker_margin | shared | appointments.

    Двум секциям НУЖЕН arg, иначе они возвращают пустоту (это фильтры):
      providers  — arg = список id через запятую («fns-rf,gibdd-online-rf»).
                   Перечислить все провайдеры этим эндпоинтом нельзя, только найти
                   известные по id.
      requisites — arg = телефон. Обычно вместо этого нужен transfer_sbp_resolve(phone);
                   а реквизиты СВОЕГО счёта — это account_requisites(account_id).

    (invest_portfolio/operations/securities и account_requisites — отдельные тулы.)"""
    try:
        s = _require(); s.ensure_fresh()
        return _json_out(s.get_data(section, arg), 5000)
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
            return ("Магазинов не найдено — вероятно, не задан адрес доставки в "
                    "приложении. Без магазина grocery-тулы работать не будут.")
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
        return "\n".join(render(st) for st in rows)
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_search(query: str, app_id: str = "", point_id: str = "") -> str:
    """Поиск товара по названию. app_id/point_id — из grocery_stores() (обязательны).
    Возвращает товары с тегом likely_raw (сырой/готовый)."""
    try:
        s = _require(); s.ensure_fresh()
        app_id, point_id = _store(app_id, point_id)
        results = s.grocery_search(query, app_id=app_id, point_id=point_id)
        body = "\n".join(f"- id={r['id']} | {r['name'][:40]} | {r['price']}₽ | {r.get('weight','') or '-'} | {'RAW' if r.get('likely_raw') else 'PREP'}"
            for r in results) or f"Не нашёл '{query}'"
        return f"[store appId={app_id} pointId={point_id}]\n" + body
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
        r = s.grocery_add_to_cart(json.loads(items), app_id=app_id, point_id=point_id)
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
        return (f"[store appId={app_id} pointId={point_id}] OK: goodsSum={pl['goodsSum']}"
                f" (в корзине {len(json.loads(items))} новых позиций){reset}")
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
async def grocery_checkout(app_id: str = "", point_id: str = "", force: bool = False) -> str:
    """Полный чекаут: доставка → заказ → оплата. РЕАЛЬНЫЕ ДЕНЬГИ.
    app_id/point_id — из grocery_stores() (обязательны, тот же магазин что в корзине).
    Счёт оплаты выбирается автоматически (первый Current RUB с балансом).
    При неопределённом результате (заказ мог создаться) повтор БЛОКИРУЕТСЯ —
    сначала grocery_attempts() и проверь заказ в приложении. force=True — только если
    пользователь ЯВНО подтвердил, что прошлого заказа нет. Всегда показывай состав и
    сумму и жди явного подтверждения перед вызовом.

    Реализация: тул асинхронный и запускает браузер Playwright в отдельном worker-потоке
    (asyncio.to_thread) — sync_playwright падает, если звать его внутри event-loop, а
    FastMCP крутит sync-тулы именно в loop. Если тул падает с Playwright-ошибкой —
    проверь `python -m playwright install chromium` (в окружении MCP)."""
    try:
        return await asyncio.to_thread(_do_grocery_checkout, app_id, point_id, force)
    except Exception as e:
        # errors before an attempt was created (NO_SESSION, NO_STORE_CONTEXT, etc.)
        return _err(e)


def _do_grocery_checkout(app_id: str, point_id: str, force: bool) -> str:
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
                                   sum_val=amount, attempt_id=attempt_id)
            return (f"[store appId={app_id} pointId={point_id}] ✓ ORDER {r['order_id']} PAID. "
                    f"sum={r['sum']}₽ (attempt {attempt_id})")
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
            err_msg = f"{type(e).__name__}: {str(e)[:120]}"
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
def grocery_attempts() -> str:
    """Недавние попытки grocery checkout (read-only) — для reconciliation после
    неопределённого результата (UNKNOWN). Показывает status/order_id/attempt_id/sum."""
    try:
        from . import journal
        rows = journal.recent(60)
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
                bits.append(str(tail)[:60])
            return " | ".join(bits)
        return "\n".join(render(a) for a in list(merged.values())[-15:])
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
def messenger_conversations() -> str:
    """Список чатов."""
    try:
        s = _require(); s.ensure_fresh()
        convs = s.messenger_conversations()
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
                f"{' | ' + ' '.join(last.split())[:60] if last else ''}")
        if not lines:
            return "Чатов нет. Это не ошибка — мессенджер ответил пустым списком."
        return "\n".join(lines)
    except Exception as e:
        return _err(e)

@mcp.tool()
def messenger_messages(conversation_id: str) -> str:
    """История чата."""
    try:
        s = _require(); s.ensure_fresh()
        msgs = s.messenger_messages(conversation_id)
        # oldest→newest, and keep the author and time: a bare list of 60-char text
        # fragments loses who said what, which is most of the meaning in a chat.
        msgs = sorted((m for m in msgs if isinstance(m, dict)),
                      key=lambda m: m.get("timestamp") or "")
        out = []
        for m in msgs[-20:]:
            a = m.get("author") or {}
            who = a.get("name") or ("Вы" if a.get("role") == "client" else "?")
            text = " ".join(((m.get("content") or {}).get("text") or "").split())
            if not text:
                text = f"[{m.get('messageType') or 'вложение'}]"
            out.append(f"- [{(m.get('timestamp') or '')[:16].replace('T', ' ')}] "
                       f"{who}: {text[:400]}")
        return "\n".join(out) or "Сообщений нет."
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
        # the endpoint returns bare ids — resolve them to chat names
        names = {}
        try:
            for c in s.messenger_conversations():
                cid = str(c.get("id") or c.get("conversationId") or "")
                members = c.get("members") or []
                title = (c.get("title") or c.get("name")
                         or (members[0].get("name") if members else "") or "")
                if cid:
                    names[cid] = title
        except Exception:
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
    Между своими счетами: provider='transfer-inner', to_account=счёт-получатель.
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
        ops = s.invest_operations(broker_account_id, operation_type=operation_type, limit=limit)
        if not ops:
            return "нет операций"
        def render(o):
            # `payment`, not `amount`: the field never existed under that name, so
            # every row printed «?» for the one number that matters.
            return (f"- [{str(o.get('date', ''))[:16]}] "
                    f"{_money(o.get('payment') or o.get('paymentRub'))} "
                    f"| {o.get('type', '')} | {str(o.get('description', ''))[:40]} "
                    f"| {o.get('status', '')}")
        # limit was passed to the API AND then ignored here by a hardcoded [:50],
        # so invest_operations(limit=200) silently showed 50.
        return _rows_out(ops, render, limit=limit, total=len(ops),
                         header="Брокерские операции")
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
def card_operations(card_id: str, days: int = 30, limit: int = 50) -> str:
    """Операции по КОНКРЕТНОЙ карте. card_id — поле id из list_cards().
    Серверного фильтра по карте нет (API умеет только excludeCardIds), поэтому
    берутся операции за период и фильтруются по полю card.
    limit=0 — показать все за период."""
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
                    f"| {(o.get('description') or '')[:40]}")
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
    paths, keeping only leaves the bank actually holds a value for."""
    out = {}
    if isinstance(node, dict):
        if "isEntered" in node:
            if node.get("isEntered") and not isinstance(node.get("value"), (dict, list)):
                out[prefix] = node.get("value")
            elif isinstance(node.get("value"), dict):
                out.update(_doc_flat(node["value"], prefix))
            return out
        for k, v in node.items():
            out.update(_doc_flat(v, f"{prefix}.{k}" if prefix else k))
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
                # the same document repeats across sources; keep the richest copy
                key = (f.get("serial"), f.get("number") or f.get("serialAndNumber"),
                       f.get("person.lastName"), bd)
                if len(f) > len(seen.get(key, {})):
                    seen[key] = {**f, "_mine": mine}
            for f in seen.values():
                who = "" if f.get("_mine") else "  ⚠ не ваш документ"
                num = " ".join(str(f[k]) for k in ("serial", "number", "serialAndNumber")
                               if f.get(k))
                head = f"{title}: {num or '—'}{who}"
                rest = [f"    {k} = {v}" for k, v in sorted(f.items())
                        if k not in ("serial", "number", "serialAndNumber", "_mine", "name")]
                out.append("\n".join([head] + rest))
        return "\n".join(out) or f"Документов не найдено (kind={kind!r})."
    except Exception as e:
        return _err_session(e)


# ── ORDERS ACROSS EVERY VERTICAL ────────────────────────────

_ORDER_KINDS = {
    "афиша": ("cinema", "concerthall", "club", "sports", "other"),
    "кино": ("cinema",),
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
                    f"| {what[:34]} | id={o.get('orderId','?')}"
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


_TRAVEL_BLOCKED = {
    "avia_ticket": ("маршрут, места и пассажиров отдаёт www.tbank.ru/api/travel/flight/order, "
                    "но он требует веб-сессию, привязанную к одноразовому токену из "
                    "/v1/travel_link_auth_token — а тот отвечает INSUFFICIENT_PRIVILEGES "
                    "даже на CLIENT-сессии"),
    "trains_ticket": ("вагон, места и пассажиров отдаёт trains.t-bank-app.ru/api/orders/{id}, "
                      "но он авторизуется по cookie, которую ставит редирект "
                      "/authorization/authorize?auth_token=… — а токен для него минтит "
                      "tsocial.tinkoff.ru/api-gateway/auth/game/link-token, и нам он "
                      "возвращает ошибку B002D965"),
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
            out.append(f"Описание: {meta['description'][:300]}")
        if meta.get("ingredients"):
            out.append(f"Состав: {meta['ingredients'][:700]}")
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
        rows = s.grocery_candidates(query, app_id=app_id, point_id=point_id,
                                    limit=limit, with_nutrition=need_nutrition)
        if not rows:
            return f"По запросу {query!r} ничего не найдено в магазине {app_id}/{point_id}."
        rows = _rank_rows(rows, key, order)
        n = lambda v: "—" if v is None else f"{v:g}"   # noqa: E731 — часть КБЖУ сеть не публикует
        head = f"«{query}» — {len(rows)} кандидатов"
        head += f", сортировка по {key} ({'убыв' if order.lower().startswith('desc') else 'возр'}):" \
            if key else ", порядок магазина (сортировка не запрошена):"
        lines = [head]
        for r in rows:
            line = f"- {n(r.get('price')):>7} ₽ | {r.get('weight_label') or '—':>10}"
            if need_nutrition:
                kcal = f"{r['kcal']:.0f}" if r.get("kcal") is not None else "—"
                line += (f" | {kcal:>5} ккал/100г | Б{n(r.get('protein'))}"
                         f"/Ж{n(r.get('fat'))}/У{n(r.get('carb'))}")
            lines.append(line + f" | {r['name'][:42]} | id={r['id']}")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


# ── APP SEARCH ──────────────────────────────────────────────

# Pure UI scaffolding in the search response — carries no searchable entity.
_SEARCH_NOISE = {"masterWidget", "block_marker"}

def _search_rows(hits: list[dict]) -> list[dict]:
    """Flatten a search response into {type, name, note, id} rows.

    Two hit shapes come back. `afisha`/`movie_main` return typed entities
    (eventId/eventName/objectName). `services` wraps results in `universal_block`
    display cards whose id only exists inside a deeplink
    ("tinkoffbank://…/Movies?movieId=104321")."""
    rows = []
    for hit in hits:
        kind = hit.get("objectType") or "?"
        src = hit.get("objectSource") or {}
        if kind == "universal_block":
            rows.extend(_search_rows(src.get("objects") or []))
            continue
        if kind in _SEARCH_NOISE:
            continue
        name = src.get("eventName") or src.get("name") or ""
        note = ""
        ident = str(src.get("eventId") or src.get("id") or "")
        if not name and isinstance(src.get("title"), dict):
            name = src["title"].get("value") or ""
            note = ((src.get("titleDescription") or {}).get("value") or "")
            deeplink = (src.get("link") or {}).get("deeplink") or ""
            m = re.search(r"[?&](?:movieId|eventId|id)=([^&]+)", deeplink)
            ident = m.group(1) if m else ident
        if not name:
            continue
        if not note:
            venue = src.get("objectName") or ""
            when = src.get("dateForShow") or ""
            price = src.get("priceForShow") or ""
            note = " · ".join(x for x in (venue, when, price) if x)
        rows.append({"type": kind, "name": name, "note": note, "id": ident})
    return rows

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
        rows = _search_rows(s.app_search(query, screen=screen, limit=limit))
        if not rows:
            return f"По запросу {query!r} в разделе {screen!r} ничего не найдено."
        by_type: dict[str, list[dict]] = {}
        for r in rows:
            by_type.setdefault(r["type"], []).append(r)
        out = [f"«{query}» в разделе {screen}: {len(rows)} результатов"]
        for kind, items in by_type.items():
            out.append(f"  {kind}:")
            for r in items:
                out.append(f"    - {r['name'][:56]}"
                           + (f" | {r['note'][:44]}" if r["note"] else "")
                           + (f" | id={r['id']}" if r["id"] else ""))
        return "\n".join(out)
    except Exception as e:
        return _err(e)


# ── CINEMA ──────────────────────────────────────────────────

@mcp.tool()
def cinema_search(query: str = "", city: str = "Москва", limit: int = 20) -> str:
    """Найти фильм в прокате и его eventId (нужен для cinema_schedule).
    query — часть названия; пусто = вся сегодняшняя афиша города (её видно
    целиком только при limit=0 — по умолчанию показаны первые 20).
    city — город афиши, по умолчанию Москва. Спроси пользователя, если он его
    не назвал: молчаливая Москва даёт правдоподобный список чужого города.
    Сам eventId от города не зависит."""
    try:
        s = _require(); s.ensure_fresh()
        movies, scanned, listing = s.cinema_movies(city=city, query=query)
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
def cinema_schedule(event_id: str, date: str, cinema: str = "",
                    around: str = "", window_min: int = 90,
                    city: str = "Москва") -> str:
    """Сеансы фильма на дату. event_id — из cinema_search(), date — YYYY-MM-DD.
    cinema — подстрока названия кинотеатра ("каро 11"), around — время "17:00",
    window_min — допуск в минутах вокруг него.
    city — город, по умолчанию Москва. Он же задаёт точку, от которой считается
    расстояние до кинотеатров, поэтому передавай тот же город, что и в
    cinema_search(): расписание Петербурга, отсортированное от центра Москвы,
    выглядит правдоподобно и бессмысленно.
    Отдаёт objectId площадки и slotId каждого сеанса — оба нужны для
    cinema_seats() и cinema_book(), поодиночке бесполезны."""
    try:
        s = _require(); s.ensure_fresh()
        venues = s.cinema_schedule(event_id, date, city=city)
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
                # objectId identifies the VENUE and is required by cinema_seats /
                # cinema_book alongside the slotId. Without it the documented flow
                # dead-ends: the agent holds a slotId it cannot use and has no other
                # tool that yields a cinema objectId. concert_schedule already prints it.
                lines.append(f"{name} — {geo.get('address','')}"
                             + (f"  [{km:.1f} км]" if km else "")
                             + f" | objectId={info.get('objectId','?')}")
                lines += [f"    {x}" for x in slots]
        if not lines:
            hint = f", фильтр «{cinema}»" if cinema else ""
            hint += f", около {around} ±{window_min} мин" if around else ""
            return (f"Сеансов на {date} не найдено ({len(venues)} кинотеатров в выдаче{hint}).")
        return f"{shown} площадок с подходящими сеансами на {date}:\n" + "\n".join(lines)
    except Exception as e:
        return _err(e)


# ── TICKET BOOKING ──────────────────────────────────────────

def _seat_rows(halls: list[dict], max_price: float = 0, row: str = "") -> list[str]:
    """Vacant seats grouped by row, cheapest rows first."""
    out = []
    for hall in halls:
        seats = [s for s in (hall.get("seats") or []) if s.get("status") == "vacant"]
        if max_price:
            seats = [s for s in seats if float(s.get("price") or 0) <= max_price]
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
            nums = ", ".join(str((s.get("pos") or {}).get("number") or "?") for s in group[:24])
            more = f" …ещё {len(group) - 24}" if len(group) > 24 else ""
            price = (f"{prices[0]:.0f} ₽" if len(prices) == 1
                     else f"{prices[0]:.0f}–{prices[-1]:.0f} ₽")
            out.append(f"  ряд {r:>3} ({price}): {nums}{more}")
    return out

@mcp.tool()
def cinema_seats(event_id: str, slot_id: str, object_id: str,
                 row: str = "", max_price: float = 0, kind: str = "movie") -> str:
    """Свободные места на сеансе, по рядам. Денег не двигает.
    slot_id и object_id — из cinema_schedule()/concert_schedule().
    row — показать только один ряд, max_price — потолок цены за место.
    kind — "movie" или "concert"."""
    try:
        s = _require(); s.ensure_fresh()
        halls = s.event_seats(event_id, slot_id, object_id, kind=kind)
        if not halls:
            return ("Схема зала пуста. Для концертов со свободной рассадкой "
                    "смотри concert_hall() — там места не нумеруются.")
        lines = _seat_rows(halls, max_price=max_price, row=row)
        if not lines:
            return "Свободных мест по заданным условиям нет."
        return "\n".join(lines) + (
            "\n\nДальше: cinema_book(event_id, slot_id, object_id, seats=\"ряд:место,…\")"
            " — это БРОНЬ без оплаты.")
    except Exception as e:
        return _err(e)

@mcp.tool()
def concert_hall(event_id: str, slot_id: str, object_id: str) -> str:
    """Секторы концерта со свободной рассадкой (входные билеты, фан-зоны).
    Только чтение: примера создания заказа для такого экрана в захвате нет,
    поэтому бронировать отсюда MCP не умеет — только смотреть наличие."""
    try:
        s = _require(); s.ensure_fresh()
        data = s.concert_hall(event_id, slot_id, object_id)
        info = data.get("info") or {}
        sectors = data.get("sectors") or []
        if not sectors:
            return "Секторов не найдено (возможно, у площадки нумерованные места — cinema_seats)."
        out = [f"{info.get('hallName','?')} | максимум мест в заказе: "
               f"{info.get('maxSeatsInOrder','?')}"]
        for sec in sectors:
            p = (sec.get("prices") or {}).get("fix")
            out.append(f"- {sec.get('sectorName','?')[:52]} | "
                       f"{'есть' if sec.get('isTicketsAvailable') else 'нет'} "
                       f"({sec.get('availableTickets', 0)} шт) | "
                       f"{f'{p:.0f} ₽' if p else 'цена не указана'}")
        return "\n".join(out) + "\n\nБронирование таких секторов через MCP не реализовано."
    except Exception as e:
        return _err(e)

@mcp.tool()
def concert_schedule(event_id: str) -> str:
    """Показы концерта: площадка, дата, slotId и objectId для cinema_seats().
    event_id — из search_app(query, screen="afisha")."""
    try:
        s = _require(); s.ensure_fresh()
        venues = s.concert_schedule(event_id)
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
                    price = (sl.get("prices") or {}).get("fix")
                    out.append(f"    {str(sl.get('startDateTime',''))[:16]} | "
                               f"{f'{price:.0f} ₽' if price else 'цена по секторам'} "
                               f"| slotId={sl.get('slotId','?')}")
        return "\n".join(out)
    except Exception as e:
        return _err(e)

@mcp.tool()
def cinema_book(event_id: str, slot_id: str, object_id: str, seats: str,
                kind: str = "movie", seat_type: str = "basic") -> str:
    """ЗАБРОНИРОВАТЬ места. Создаёт заказ, но НЕ платит — деньги списывает
    отдельный ticket_pay(). Неоплаченная бронь отваливается сама.

    seats — через запятую: для кино "7:10,7:11" (ряд:место из cinema_seats),
    для концертов — составные seatId из cinema_seats(kind="concert") как есть.

    Покажи пользователю итоговую сумму со сбором ДО вызова ticket_pay."""
    try:
        s = _require(); s.ensure_fresh()
        ids = [x.strip() for x in seats.split(",") if x.strip()]
        if not ids:
            return "Не переданы места. Пример: seats=\"7:10,7:11\"."
        payload = ([{"id": i} for i in ids] if kind == "concert"
                   else [{"id": i, "type": seat_type} for i in ids])
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
def ticket_cancel(order_id: str, kind: str = "movie", payment_id: str = "") -> str:
    """Отменить заказ билета. kind — "movie" или "concert".

    payment_id нужен бэкенду наравне с order_id и подставляется из orders(),
    если его не передать. Без него хост отвечает 200 «Success», но заказ
    остаётся активным и деньги не возвращаются — так что если тул сообщает, что
    paymentId не найден, это НЕ отмена. Он же лежит в ответе ticket_pay().

    Оплаченный заказ уходит в PARTIALLY_CANCELED, а не CANCELED: билеты
    возвращают, сервисный сбор — нет.

    Если тул вернёт ошибку, считай статус НЕИЗВЕСТНЫМ (не «всё ещё
    забронировано») — проверь orders() и при необходимости отменяй через
    приложение."""
    try:
        s = _require(); s.ensure_fresh()
        # Resolved here, not left to the client, so the warning below can tell the
        # caller a paid order went unmatched — and so orders() is fetched once.
        payment_id = payment_id or s.payment_id_for_order(order_id)
        res = s.cancel_ticket_order(order_id, kind=kind, payment_id=payment_id)
        st = (res or {}).get("status") if isinstance(res, dict) else ""
        head = (f"Отмена заказа {order_id} принята (status={st})."
                if st else f"Отмена заказа {order_id} принята.")
        if not payment_id:
            # Silent no-op territory: say so instead of reporting a cancellation.
            head += ("\n⚠️ paymentId не найден в orders() — для ОПЛАЧЕННОГО заказа "
                     "такой запрос ничего не отменяет. Передай payment_id явно.")
        return head + "\nПроверь статус и возврат: orders(\"афиша\") + list_operations()."
    except Exception as e:
        return (_err(e) + f"\nСтатус заказа {order_id} НЕИЗВЕСТЕН — проверь orders(\"афиша\"). "
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
        return "\n".join(out) or json.dumps(data, ensure_ascii=False)[:2000]
    except Exception as e:
        return _err(e)

@mcp.tool()
def payment_receipt(payment_id: str, save_to: str = "") -> str:
    """Скачать PDF-чек по платежу. save_to — путь файла (по умолчанию /tmp).

    payment_id берётся ровно из четырёх мест, других производителей нет:
    orders() (поле paymentId в строке заказа), grocery_order_status(),
    и ответы transfer() и ticket_pay(). В list_operations() его НЕТ —
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
