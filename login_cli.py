#!/usr/bin/env python3
"""T-Bank MCP — локальный скрипт логина (ВНЕ агента/LLM).

Пароль и PIN вводятся через getpass (не отображаются в терминале) или
читаются из env (TBANK_PASSWORD / TBANK_PIN). Они НИКОГДА не попадают в
контекст модели (LLM) — скрипт запускается напрямую из шелла.

После успешного логина создаётся session.json (0600). Агент работает с
сохранённой сессией — ему не нужен пароль.

Usage:
    python login_cli.py +79991234567

С паролем в env:
    TBANK_PASSWORD="пароль" python login_cli.py +79991234567

Без env (пароль спросит скрипт через getpass):
    python login_cli.py +79991234567
    [2/3] SMS-код: ****
    [3/3] Пароль (не отображается): ****

После логина — запусти Claude Code в этом репозитории.
"""
import sys
import os
import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.client import MobileSession, TbankApiError
from src import server as srv


def main():
    if len(sys.argv) < 2:
        print("Usage: python login_cli.py +79991234567")
        print("  TBANK_PASSWORD env — пароль (или спросит через getpass)")
        print("  TBANK_PIN env — PIN (если банк попросит)")
        sys.exit(1)

    phone = sys.argv[1]
    s = srv._blank_session()

    # Step 1: login(phone) → SMS OTP
    print(f"[1/3] login({phone}) ...")
    try:
        msg = s.login(phone)
        print(f"    {msg}")
    except Exception as e:
        print(f"    ERROR: {e}")
        sys.exit(1)

    # Step 2: OTP from user (getpass — скрытый ввод)
    otp = getpass.getpass("[2/3] SMS-код: ")
    try:
        s.confirm_step("otp", otp)
        print("    OTP принят.")
    except TbankApiError as e:
        if "password" not in str(e.message).lower():
            print(f"    ОШИБКА: {e}")
            sys.exit(1)
        # bank wants password — continue to step 3

    # Check if we already have a session (OTP was enough)
    if s.access_token:
        srv._session = s
        srv._save_session(s)
        print(f"\n[3/3] Сессия создана. sessionid={s.mobile_sessionid[:12]}…")
        _success()
        return

    # Step 3: password (if bank asked)
    if os.environ.get("TBANK_PASSWORD"):
        password = os.environ["TBANK_PASSWORD"]
        print("[3/3] Пароль из TBANK_PASSWORD env.")
    else:
        password = getpass.getpass("[3/3] Пароль (не отображается): ")

    try:
        s.confirm_step("password", password)
    except TbankApiError as e:
        if "pin" in str(e.message).lower():
            print("    Банк просит PIN.")
            if os.environ.get("TBANK_PIN"):
                pin = os.environ["TBANK_PIN"]
            else:
                pin = getpass.getpass("    PIN (не отображается): ")
            s.confirm_step("pin", pin)
        else:
            print(f"    ОШИБКА: {e}")
            sys.exit(1)

    srv._session = s
    srv._save_session(s)
    print(f"\n[3/3] Сессия создана. sessionid={s.mobile_sessionid[:12]}…")
    _success()


def _success():
    print("\n✓ ГОТОВО! session.json сохранён (права 0600).")
    print("  Запусти Claude Code в этом репозитории.")
    print("  Пароль НЕ передан агенту — он работает с сохранённой сессией.")
    print("  Тулы: list_accounts, grocery_search, transfer, ...")


if __name__ == "__main__":
    main()
