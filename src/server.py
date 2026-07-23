"""T-Bank mobile API MCP server (FastMCP).

25 tools for the agent. Low-level API calls are encapsulated in high-level tools.
get_data(section) covers 60+ read endpoints in one tool.

Run: python -m src.server
"""
from __future__ import annotations

import json
import os
import traceback
from typing import Any

from mcp.server.fastmcp import FastMCP
from .client import MobileSession, TbankApiError, SessionExpired, ms_for_period

mcp = FastMCP("tbank")
_session: MobileSession | None = None
_SESSION_FILE = os.environ.get("TBANK_SESSION", "session.json")


def _blank_session():
    return MobileSession(mobile_sessionid="", refresh_token="",
        client_id="gorod-app", client_version="112.0.0",
        vendor="t_ios", origin="mobile,ib5,loyalty,platform",
        platform="ios", app_name="mobile", app_version="7.31.6")


def _save_session(s):
    try:
        d = {k: v for k, v in s.__dict__.items() if not k.startswith("_")}
        json.dump(d, open(_SESSION_FILE, "w"), ensure_ascii=False)
    except OSError:
        pass


def _load_session():
    if not os.path.exists(_SESSION_FILE):
        return None
    try:
        d = json.load(open(_SESSION_FILE))
        d.pop("_http", None)
        return MobileSession(**d)
    except Exception:
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


# ── LOGIN (4) ──────────────────────────────────────────────

@mcp.tool()
def login(phone: str) -> str:
    """Начать логин. Отправляет SMS OTP. Возвращает какой шаг следующий."""
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
    """Тихий re-login (без OTP). Вызови при SESSION EXPIRED."""
    try:
        _require().refresh()
        _save_session(_session)
        return f"OK. sessionid={_session.mobile_sessionid[:12]}…"
    except Exception as e:
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
def grocery_search(query: str) -> str:
    """Поиск товара по названию. Возвращает товары с тегом likely_raw (сырой/готовый)."""
    try:
        s = _require(); s.ensure_fresh()
        results = s.grocery_search(query)
        return "\n".join(f"- id={r['id']} | {r['name'][:40]} | {r['price']}₽ | {'RAW' if r.get('likely_raw') else 'PREP'}"
            for r in results) or f"Не нашёл '{query}'"
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_plan_order(ingredients: str) -> str:
    """Спланировать заказ: для каждого ингредиента ищет (custom_ordered → global).
    ingredients = JSON массив, напр. ["свёкла","говядина","капуста"]."""
    try:
        s = _require(); s.ensure_fresh()
        plan = s.grocery_plan_order(json.loads(ingredients))
        lines = [f"Total: {plan['total_sum']}₽"]
        for i in plan["items"]:
            lines.append(f"✓ {i['name'][:40]} | {i['price']}₽ | {i['source']}")
        if plan["missing"]:
            lines.append(f"MISSING: {', '.join(plan['missing'])}")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_add_to_cart(items: str) -> str:
    """Добавить товары в корзину. items = JSON [{id, count}, ...]."""
    try:
        s = _require(); s.ensure_fresh()
        r = s.grocery_add_to_cart(json.loads(items))
        pl = r if isinstance(r, dict) else {}
        return f"OK: goodsSum={pl.get('goodsSum','?')}"
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_cart() -> str:
    """Содержимое корзины."""
    try:
        s = _require(); s.ensure_fresh()
        r = s._call_read("grocery_cart_get")
        cart = r.get("cart", r) if isinstance(r, dict) else {}
        goods = cart.get("goods", [])
        return "\n".join(f"- {g.get('name','')[:35]} | {g.get('count',1)} | "
            f"{(g.get('price') or {}).get('value','?')}₽" for g in goods) or "Корзина пуста"
    except Exception as e:
        return _err(e)

@mcp.tool()
def grocery_checkout() -> str:
    """Полный чекаут: корзина → доставка → заказ → оплата. РЕАЛЬНЫЕ ДЕНЬГИ."""
    try:
        s = _require(); s.ensure_fresh()
        r = s.grocery_checkout()
        return f"✓ ORDER {r['order_id']} PAID. sum={r['sum']}₽"
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
