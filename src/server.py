"""T-Bank mobile API MCP server (FastMCP).

25 tools for the agent. Low-level API calls are encapsulated in high-level tools.
get_data(section) covers 60+ read endpoints in one tool.

Run: python -m src.server
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

from mcp.server.fastmcp import FastMCP
from .client import MobileSession, TbankApiError, SessionExpired, ms_for_period

mcp = FastMCP("tbank")
_session: MobileSession | None = None
_SESSION_FILE = os.environ.get(
    "TBANK_SESSION",
    os.path.expanduser("~/.local/share/tbank-mcp/session.json"),
)


def _blank_session():
    return MobileSession(mobile_sessionid="", refresh_token="",
        client_id="gorod-app", client_version="112.0.0",
        vendor="t_ios", origin="mobile,ib5,loyalty,platform",
        platform="ios", app_name="mobile", app_version="7.31.6")


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
        d.pop("_http", None)
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
        _session = _load_session()
    if not _session or not _session.mobile_sessionid:
        raise TbankApiError("NO_SESSION",
            "Call login(phone) first.")
    return _session


def _err(e):
    if isinstance(e, SessionExpired):
        return f"SESSION EXPIRED: call refresh_session(). {e.message}"
    if isinstance(e, TbankApiError):
        return f"API error ({e.result_code}): {e.message}"
    return f"{type(e).__name__}: {e}"


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


# ── LOGIN (4) ──────────────────────────────────────────────

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
        return f"OK. sessionid={_session.mobile_sessionid[:12]}…"
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
        return f"OK. sessionid={_session.mobile_sessionid[:12]}…"
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
        return f"OK. sessionid={_session.mobile_sessionid[:12]}…"
    except Exception as e:
        return _err(e)


# ── SESSION (3) ──────────────────────────────────────────────

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
        return f"OK. sessionid={s.mobile_sessionid[:12]}…"
    except Exception as e:
        try:
            from . import observability as obs
            obs.emit("refresh", result="error", blame="app", error=str(e)[:160])
        except Exception:
            pass
        return _err(e)

@mcp.tool()
def session_status() -> str:
    """Проверить жива ли сессия."""
    try:
        return json.dumps(_require().session_status(), ensure_ascii=False, default=str)[:1000]
    except Exception as e:
        return _err(e)

@mcp.tool()
def keepalive() -> str:
    """Пинг — продлить сессию."""
    try:
        return str(_require().keepalive())[:200]
    except Exception as e:
        return _err(e)


# ── CORE READS (5) ─────────────────────────────────────────

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
        return "\n".join(f"- [{(o.get('operationTime') or o.get('date',''))}] "
            f"{((o.get('amount') or {}).get('value','?')):>8} | {o.get('description','')[:40]}"
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
        lines = [f"Total: {rep['total_spent']} {rep['currency']}"]
        for c in rep["categories"]:
            lines.append(f"- {c['category'][:25]:25} {c['amount']:>8.0f} {c['share_pct']:5.1f}%")
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
    invest_portfolio | invest_operations | invest_securities | pension | broker_margin | shared."""
    try:
        s = _require(); s.ensure_fresh()
        return json.dumps(s.get_data(section), ensure_ascii=False, default=str)[:5000]
    except Exception as e:
        return _err(e)


# ── GROCERY (6) ────────────────────────────────────────────

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
        body = "\n".join(f"- id={r['id']} | {r['name'][:40]} | {r['price']}₽ | {'RAW' if r.get('likely_raw') else 'PREP'}"
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
            lines.append(f"✓ {i['name'][:40]} | {i['price']}₽ | {i['source']}")
        if plan["missing"]:
            lines.append(f"MISSING: {', '.join(plan['missing'])}")
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
        return f"[store appId={app_id} pointId={point_id}] OK: goodsSum={pl.get('goodsSum','?')}"
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
        cart = r.get("cart", r) if isinstance(r, dict) else {}
        goods = cart.get("goods", []) if isinstance(cart, dict) else []
        # defensive context check: if the response echoes a DIFFERENT store than
        # requested, flag it instead of silently showing an empty cart.
        resp_app = str(cart.get("applicationId") or cart.get("appId") or "")
        resp_point = str((cart.get("delivery", {}) or {}).get("pointId")
                         or cart.get("pointId") or "")
        mismatch = ""
        if resp_app and resp_app != str(app_id):
            mismatch = f"  ⚠ CART_CONTEXT_MISMATCH: ответ appId={resp_app} ≠ запрошенный {app_id}\n"
        elif resp_point and resp_point != str(point_id):
            mismatch = f"  ⚠ CART_CONTEXT_MISMATCH: ответ pointId={resp_point} ≠ запрошенный {point_id}\n"
        body = "\n".join(f"- {g.get('name','')[:35]} | {g.get('count',1)} | "
            f"{(g.get('price') or {}).get('value','?')}₽" for g in goods) or "Корзина пуста"
        return f"[store appId={app_id} pointId={point_id}]\n{mismatch}{body}"
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_checkout(app_id: str = "", point_id: str = "", force: bool = False) -> str:
    """Полный чекаут: доставка → заказ → оплата. РЕАЛЬНЫЕ ДЕНЬГИ.
    app_id/point_id — из grocery_stores() (обязательны, тот же магазин что в корзине).
    Счёт оплаты выбирается автоматически (первый Current RUB с балансом).
    При неопределённом результате (заказ мог создаться) повтор БЛОКИРУЕТСЯ —
    сначала grocery_attempts() и проверь заказ в приложении. force=True — только если
    пользователь ЯВНО подтвердил, что прошлого заказа нет. Всегда показывай состав и
    сумму и жди явного подтверждения перед вызовом."""
    try:
        from . import journal
        from .checkout import select_payment_account, CheckoutError, CheckoutUnknown
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
        # 2. block-check: an unknown/paid prior attempt for THIS cart → no auto-retry (#10)
        blocked, last = journal.is_retry_blocked(chash)
        if blocked and not force:
            return (f"[store appId={app_id} pointId={point_id}] BLOCKED: предыдущая попытка checkout для "
                    f"этой корзины завершилась неопределённо (status={last.get('status')}, "
                    f"attempt={last.get('attempt_id')}, order={last.get('order_id') or '-'}). Заказ мог быть "
                    f"создан/оплачен — сначала grocery_attempts() и проверь заказ в приложении. "
                    f"Принудительный повтор (force=True) — только если пользователь подтвердил отсутствие заказа.")
        # 3. select a payment account (rejects empty — fixes the old empty-agreement bug, #9)
        account = select_payment_account(s)
        # 4. new journal attempt + run the web checkout
        attempt_id = journal.new_attempt(app_id, point_id, chash, amount)
        try:
            r = s.grocery_checkout(app_id=app_id, point_id=point_id, account=account,
                                   sum_val=amount, attempt_id=attempt_id)
            return (f"[store appId={app_id} pointId={point_id}] ✓ ORDER {r['order_id']} PAID. "
                    f"sum={r['sum']}₽ (attempt {attempt_id})")
        except CheckoutUnknown as e:
            return (f"[store appId={app_id} pointId={point_id}] UNKNOWN RESULT (attempt {attempt_id}): {e} "
                    f"Повтор ЗАБЛОКИРОВАН — заказ мог создаться. Проверь grocery_attempts() и заказ в приложении.")
        except CheckoutError as e:
            journal.record(attempt_id, "failed", "failed", error=str(e)[:160])
            return _err(e)
    except Exception as e:
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


# ── DIAGNOSTICS (1) ────────────────────────────────────────

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


# ── MESSENGER (4) ──────────────────────────────────────────

@mcp.tool()
def messenger_conversations() -> str:
    """Список чатов."""
    try:
        s = _require(); s.ensure_fresh()
        convs = s.messenger_conversations()
        return "\n".join(f"- {c.get('title','?')[:25]} id={c.get('conversationId','')[:24]}…"
            for c in convs if isinstance(c, dict))
    except Exception as e:
        return _err(e)

@mcp.tool()
def messenger_messages(conversation_id: str) -> str:
    """История чата."""
    try:
        s = _require(); s.ensure_fresh()
        msgs = s.messenger_messages(conversation_id)
        return "\n".join(f"- {((m.get('content') or {}).get('text',''))[:60]}" for m in msgs[:20])
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
    """Непрочитанные."""
    try:
        s = _require(); s.ensure_fresh()
        return json.dumps(s.messenger_unread(), ensure_ascii=False)[:500]
    except Exception as e:
        return _err(e)


# ── MONEY (2) ──────────────────────────────────────────────

@mcp.tool()
def transfer(amount: float, to_account: str, description: str = "") -> str:
    """Перевод (P2P/СБП). РЕАЛЬНЫЕ ДЕНЬГИ — подтверди с пользователем."""
    try:
        s = _require(); s.ensure_fresh()
        s.transfer(amount, to_account, description)
        return f"Sent: {amount}₽ to {to_account}"
    except Exception as e:
        return _err(e)

@mcp.tool()
def payment_commission(body: str = "") -> str:
    """Предпросмотр комиссии (без денег)."""
    try:
        s = _require(); s.ensure_fresh()
        b = json.loads(body) if body else None
        return json.dumps(s._call_read("payment_commission", body=b), ensure_ascii=False)[:1000]
    except Exception as e:
        return _err(e)


# ── UTILITY (1) ────────────────────────────────────────────

@mcp.tool()
def flows() -> str:
    """Гид по флоу (заказ продуктов, переводы, логин, мессенджер, инвест)."""
    path = os.path.join(os.path.dirname(__file__), "..", "FLOWS.md")
    return open(path, encoding="utf-8").read()[:6000] if os.path.exists(path) else "FLOWS.md not found"


def main():
    mcp.run()

if __name__ == "__main__":
    main()
