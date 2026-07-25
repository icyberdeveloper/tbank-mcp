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
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from .client import MobileSession, TbankApiError, SessionExpired, ms_for_period
from .observability import redact_text

mcp = FastMCP("tbank")
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
    def safe(msg):
        return redact_text(str(msg))[:300]
    if isinstance(e, SessionExpired):
        return f"SESSION EXPIRED: call refresh_session(). {safe(e.message)}"
    if isinstance(e, TbankApiError):
        return f"API error ({e.result_code}): {safe(e.message)}"
    return f"{type(e).__name__}: {safe(e)}"


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
        return json.dumps(s.session_status(), ensure_ascii=False, default=str)[:1000]
    except Exception as e:
        return _err(e)

@mcp.tool()
def keepalive() -> str:
    """Пинг — продлить сессию."""
    try:
        return str(_require().keepalive())[:200]
    except Exception as e:
        return _err(e)


# ── CORE READS ──────────────────────────────────────────────

@mcp.tool()
def list_accounts() -> str:
    """Счета + карты + балансы."""
    try:
        s = _require(); s.ensure_fresh()
        accs = s.list_accounts()
        return "\n".join(f"- {a.get('id','?')} | {a.get('accountType','')} | "
            f"{a.get('name','')[:30]} | {(a.get('moneyAmount') or {}).get('value','?')} "
            f"{((a.get('currency') or {}).get('name','') if isinstance(a.get('currency'),dict) else a.get('currency',''))}"
            for a in accs)
    except Exception as e:
        return _err(e)

@mcp.tool()
def list_operations(account_id: str, days: int = 30) -> str:
    """Операции за период."""
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
        return "\n".join(
            f"- [{when(o)}] {sign(o)}{(o.get('amount') or {}).get('value','?')} "
            f"{((o.get('amount') or {}).get('currency') or {}).get('name','')} | "
            f"{(o.get('description') or '')[:40]}"
            for o in ops[:50])
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
        lines = [f"Траты за {days} дн.: {rep['total_spent']:,.2f} {rep['currency']} "
                 f"по {len(cats)} категориям (поступления {rep['total_earned']:,.2f})"]
        for c in cats:
            lines.append(f"- {c['category'][:25]:25} {c['amount']:>12,.2f} {c['share_pct']:5.1f}%")
        if not cats:
            lines.append("(категорий нет — за период не было расходных операций)")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)

@mcp.tool()
def operations_histogram(account_id: str = "", days: int = 30,
                        period: str = "day", group_by: str = "category") -> str:
    """Траты по периодам/категориям/мерчантам."""
    try:
        s = _require(); s.ensure_fresh()
        start, end = ms_for_period(days)
        return json.dumps(s.operations_histogram(account_id or None, start, end,
            period=period, group_by=group_by), ensure_ascii=False, default=str)[:4000]
    except Exception as e:
        return _err(e)

@mcp.tool()
def get_data(section: str) -> str:
    """Универсальный getter. section = subscriptions | credit_schedule | statements |
    requisites | invoices | templates | contacts | providers | cards | loans | autopayments |
    sbp | offers | gifts | services | bundles | manager | merchant_subs | profile | homes |
    cars | shortcuts | finhealth_total | finhealth_turnover | invest_accounts |
    pension | broker_margin | shared. (invest_portfolio/operations/securities — отдельные тулы.)"""
    try:
        s = _require(); s.ensure_fresh()
        return json.dumps(s.get_data(section), ensure_ascii=False, default=str)[:5000]
    except Exception as e:
        return _err(e)


# ── GROCERY ─────────────────────────────────────────────────

@mcp.tool()
def grocery_stores() -> str:
    """Список магазинов (название, appId, доставка, кешбэк)."""
    try:
        s = _require(); s.ensure_fresh()
        stores = s.grocery_stores()
        return "\n".join(f"- {st['name']} appId={st['appId']} pointId={st['pointId']} "
            f"minSum={st.get('minOrderSum','')} cashback={st.get('cashback','')}%" for st in stores)
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
        return (f"[store appId={app_id} pointId={point_id}] OK: goodsSum={pl['goodsSum']}"
                f" (в корзине {len(json.loads(items))} новых позиций)")
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
        body = "\n".join(f"- {g.get('name','')[:35]} | x{g.get('count',1)} | "
            f"{(g.get('price') or {}).get('value','?')}₽ | {g.get('weight','') or g.get('quant','') or '-'}"
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
        rows = journal.recent(15)
        if not rows:
            return "Попыток checkout пока не было."
        return "\n".join(
            f"- {r.get('attempt_id')} | {r.get('status')} | appId={r.get('app_id')} "
            f"| {r.get('amount', '?')}₽ | order={r.get('order_id') or '-'} "
            f"| {(r.get('error') or r.get('payment_status') or '')[:60]}"
            for r in rows)
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
    """Отправить сообщение."""
    try:
        s = _require(); s.ensure_fresh()
        s.messenger_send(conversation_id, text)
        return f"Sent: {text[:50]}"
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

@mcp.tool()
def transfer(amount: float, to_account: str, description: str = "",
             provider: str = "p2p-anybank", bank_member_id: str = "",
             masked_fio: str = "", pointer_link_id: str = "") -> str:
    """Перевод (РЕАЛЬНЫЕ ДЕНЬГИ — подтверди с пользователем). Контракт сверен с захватом.
    phone/СБП (по умолчанию): to_account=телефон. Если bank_member_id/masked_fio/
    pointer_link_id не переданы — получатель резолвится АВТОМАТИЧЕСКИ (transfer_sbp_resolve):
    выберется дефолтный банк; при нескольких банках без дефолта вернётся RECIPIENT_MULTIPLE_BANKS
    со списком (тогда передай выбранные поля явно). Между своими счетами: provider='transfer-inner',
    to_account=счёт-получатель. По реквизитам юр.лица — низкоуровневый pay() с явными providerFields."""
    try:
        s = _require(); s.ensure_fresh()
        s.transfer(amount, to_account, description, provider=provider,
                   bank_member_id=bank_member_id, masked_fio=masked_fio,
                   pointer_link_id=pointer_link_id)
        return f"Sent: {amount}₽ to {to_account} (provider={provider})"
    except Exception as e:
        return _err(e)

@mcp.tool()
def payment_commission(body: str = "") -> str:
    """Предпросмотр комиссии (без денег)."""
    try:
        s = _require(); s.ensure_fresh()
        b = json.loads(body) if body else None
        # via the client method — it form-encodes and defaults the isTransferStatus/
        # isUrgentTransfer flags. Calling _call_read directly posts JSON → 400.
        return json.dumps(s.payment_commission(b), ensure_ascii=False)[:1000]
    except Exception as e:
        return _err(e)


# ── INVEST ──────────────────────────────────────────────────

@mcp.tool()
def invest_accounts() -> str:
    """Инвест-счета (InvestBox/брокерские). Возьми brokerAccountId для следующих тулов."""
    try:
        s = _require(); s.ensure_fresh()
        accs = s.invest_accounts()
        return "\n".join(f"- {a.get('brokerAccountId', a.get('id','?'))} | {a.get('name','')[:30]}" for a in accs) or "нет инвест-счетов"
    except Exception as e:
        return _err(e)

@mcp.tool()
def invest_portfolio(broker_account_id: str, days: int = 30) -> str:
    """Статистика портфеля (P&L) за период. broker_account_id — из invest_accounts()."""
    try:
        s = _require(); s.ensure_fresh()
        start, end = ms_for_period(days)
        return json.dumps(s.invest_portfolio(broker_account_id, start, end),
                          ensure_ascii=False, default=str)[:4000]
    except Exception as e:
        return _err(e)

@mcp.tool()
def invest_operations(broker_account_id: str, operation_type: str = "", limit: int = 50) -> str:
    """Брокерские операции. operation_type — фильтр (пусто = все)."""
    try:
        s = _require(); s.ensure_fresh()
        ops = s.invest_operations(broker_account_id, operation_type=operation_type, limit=limit)
        return "\n".join(f"- [{(o.get('date',''))}] {(o.get('amount') or {}).get('value','?')} | {o.get('description','')[:40]}" for o in ops[:50]) or "нет операций"
    except Exception as e:
        return _err(e)

@mcp.tool()
def invest_securities(broker_account_id: str) -> str:
    """Купленные бумаги (акции/облигации/ETF) по брокерскому счёту."""
    try:
        s = _require(); s.ensure_fresh()
        secs = s.invest_securities(broker_account_id)
        return "\n".join(f"- {sec.get('ticker', sec.get('name','?'))[:12]} | {sec.get('balance','?')} | {sec.get('name','')[:30]}" for sec in secs) or "нет бумаг"
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

def _money(m) -> str:
    """{'value': 1.0, 'currency': {'name': 'RUB'}} → '1.00 RUB'."""
    if not isinstance(m, dict):
        return str(m)
    cur = (m.get("currency") or {})
    name = cur.get("name", "") if isinstance(cur, dict) else str(cur)
    try:
        return f"{float(m.get('value', 0)):,.2f} {name}".replace(",", " ")
    except (TypeError, ValueError):
        return f"{m.get('value', '?')} {name}"

@mcp.tool()
def list_cards() -> str:
    """Все карты по всем счетам: id, ucid, баланс, тип.
    id — для card_operations, ucid — для card_limits/card_requisites."""
    try:
        s = _require(); s.ensure_fresh()
        cards = s.cards()
        if not cards:
            return "Карт нет."
        return "\n".join(
            f"- id={c.get('id','?')} ucid={c.get('ucid','?')} | счёт {c.get('account','?')} "
            f"| {'виртуальная' if c.get('isVirtual') else 'пластик'} "
            f"| {_money(c.get('availableBalance'))} | {c.get('accountName','')[:24]}"
            for c in cards)
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
    По умолчанию номер маскируется, а CVV не выводится вообще — это полные
    платёжные данные. reveal=True показывает номер и CVV целиком."""
    try:
        s = _require(); s.ensure_client_session()
        c = s.card_credentials(ucid)
        if not c:
            return f"[ucid {ucid}] реквизиты не получены."
        pan = str(c.get("cardNumber") or "")
        exp = str(c.get("expireDate") or "")
        exp_fmt = f"{exp[:2]}/{exp[2:]}" if len(exp) == 4 else exp
        out = [f"Держатель: {c.get('cardHolder','?')}", f"Срок: {exp_fmt}"]
        if reveal:
            out.append(f"Номер: {' '.join(pan[i:i+4] for i in range(0, len(pan), 4))}")
            out.append(f"CVV: {c.get('cvv2','?')}")
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
    берутся операции за период и фильтруются по полю card."""
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
        total = sum(float((o.get("amount") or {}).get("value") or 0)
                    for o in ops if o.get("type") == "Debit")
        head = (f"[card {card_id}] {len(ops)} операций за {days} дн., "
                f"списано {total:,.0f} ₽".replace(",", " "))
        body = "\n".join(
            f"- [{when(o)}] {'-' if o.get('type') == 'Debit' else '+'}"
            f"{(o.get('amount') or {}).get('value','?')} | {(o.get('description') or '')[:40]}"
            for o in ops[:limit])
        return head + "\n" + body
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
    Отсортировано по дате создания, новые сверху."""
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
        head = f"{len(picked)} заказов" + (f" ({kind})" if kind else " всего")
        lines = []
        for o in picked[:limit]:
            f = o.get("fields") or {}
            what = (f.get("eventName") or f.get("hotelName") or f.get("objectName")
                    or f.get("applicationName") or f.get("partnerName") or "")
            lines.append(
                f"- {str(o.get('created',''))[:10]} | {o.get('objectType','?'):13} "
                f"| {o.get('status','?'):15} | {o.get('amount','?'):>10} ₽ | {what[:34]} "
                f"| id={o.get('orderId','?')}")
        return head + "\n" + "\n".join(lines)
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
def cinema_search(query: str = "", city: str = "Москва") -> str:
    """Найти фильм в прокате и его eventId (нужен для cinema_schedule).
    query — часть названия; пусто = вся сегодняшняя афиша города."""
    try:
        s = _require(); s.ensure_fresh()
        movies = s.cinema_movies(city=city, query=query)
        if not movies:
            return f"В прокате ({city}) ничего не найдено по запросу {query!r}."
        return "\n".join(
            f"- {m.get('name','?')} [{m.get('ageRestriction','')}] "
            f"| {', '.join(m.get('genres') or [])} | {m.get('country','')} "
            f"| eventId={m.get('eventId','?')}"
            for m in movies[:20])
    except Exception as e:
        return _err(e)

@mcp.tool()
def cinema_schedule(event_id: str, date: str, cinema: str = "",
                    around: str = "", window_min: int = 90,
                    city: str = "Москва") -> str:
    """Сеансы фильма на дату. event_id — из cinema_search(), date — YYYY-MM-DD.
    cinema — подстрока названия кинотеатра ("каро 11"), around — время "17:00",
    window_min — допуск в минутах вокруг него.
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
def ticket_cancel(order_id: str, kind: str = "movie") -> str:
    """Отменить заказ билета. kind — "movie" или "concert".

    ⚠️ Надёжность не подтверждена: в захвате оба пути отмены отвечали 500. Если
    тул вернёт ошибку, считай статус НЕИЗВЕСТНЫМ (не «всё ещё забронировано») —
    проверь orders() и при необходимости отменяй через приложение."""
    try:
        s = _require(); s.ensure_fresh()
        s.cancel_ticket_order(order_id, kind=kind)
        return f"Отмена заказа {order_id} принята. Проверь статус: orders(\"афиша\")."
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
    """Скачать PDF-чек по операции. payment_id — поле paymentId из orders()
    или из истории операций. save_to — путь файла (по умолчанию /tmp)."""
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

_FLOWS_PATH = os.path.join(os.path.dirname(__file__), "..", "FLOWS.md")

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
    """FLOWS.md split on '## ' headings → [(title, body), …]."""
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
        return f"FLOWS.md not found at {_FLOWS_PATH}"

    def body_of(name_part: str) -> str:
        for title, body in sections:
            if name_part.lower() in title.lower():
                return f"## {title}\n{body}"
        return ""

    q = topic.strip().lower()
    if not q:
        toc = "\n".join(f"- {t}" for t, _ in sections if not t.lower().startswith("notes"))
        return ("Разделы FLOWS.md — вызови flows(topic) с нужным:\n" + toc +
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
