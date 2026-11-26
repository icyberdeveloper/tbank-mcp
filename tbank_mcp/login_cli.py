#!/usr/bin/env python3
"""T-Bank MCP — локальный CLI логина (ВНЕ агента/LLM).

Пароль, PIN и SMS-код вводятся через getpass (не отображаются в терминале) или
читаются из env. Они НИКОГДА не попадают в контекст модели (LLM) — скрипт
запускается напрямую из шелла.

Установленный пакет (pip install tbank-mcp):

    tbank-mcp-login +79991234567

Из репозитория — интерпретатором из .venv (там стоят зависимости):

    .venv/bin/python login_cli.py +79991234567

С паролем в env:

    TBANK_PASSWORD="пароль" tbank-mcp-login +79991234567

Без env пароль спросит сам скрипт:

    tbank-mcp-login +79991234567
    [2/3] SMS-код: ****
    [3/3] Пароль (не отображается): ****

После логина запусти своего MCP-клиента: сервер читает тот же файл сессии,
путь совпадает без ручной настройки.
"""
import getpass
import os
import re
import sys

from tbank_mcp.client import TbankApiError
from tbank_mcp import server as srv

# The head of every NEXT_STEP hint: «Следующий шаг — otp.» / «Следующий шаг —
# 'complete'.» (the unknown-step form quotes the name).
_STEP_RE = re.compile(r"^Следующий шаг — '?([\w-]+)'?")


def _next_step(e: TbankApiError) -> str:
    """The step the bank named in a NEXT_STEP error; '' for any other error.

    Read from the hint's head, not by substring over the whole message: the
    generic hint for a step this CLI does not know names EVERY tool
    («confirm_otp / confirm_pin»), so `"pin" in message` was true for any
    unknown step — the CLI then sent a PIN the bank had not asked for and the
    bank answered invalid_request, with the SMS already spent."""
    if e.result_code != "NEXT_STEP":
        return ""
    m = _STEP_RE.match(str(e.message))
    return m.group(1) if m else ""


USAGE = """Usage:
  tbank-mcp-login +79991234567                  (установленный пакет)
  .venv/bin/python login_cli.py +79991234567    (из репозитория)

  TBANK_PASSWORD env — пароль (или спросит через getpass)
  TBANK_PIN env — PIN, если банк попросит"""


def main():
    # Ровно один позиционный аргумент и никаких флагов. Отбрасывать флаги молча
    # нельзя: пока в скрипте жил `--myt`, фильтр стоял в паре с отказом на
    # неизвестный флаг, а при выносе MyT отказ ушёл, фильтр остался — и команда
    # из истории шелла запускала БАНКОВСКИЙ логин с корпоративным логином вместо
    # телефона, без единого слова.
    args = sys.argv[1:]
    if len(args) != 1 or args[0].startswith("-"):
        print(USAGE)
        return 1
    return login(args[0])


def login(phone):
    s = srv._blank_session()

    # Step 1: login(phone) → SMS OTP
    print(f"[1/3] login({phone}) ...")
    try:
        msg = s.login(phone)
        print(f"    {msg}")
    except Exception as e:
        print(f"    ERROR: {e}")
        return 1

    # Step 2: код от пользователя (getpass — скрытый ввод). Обычно из SMS, но
    # аккаунт с приложением-аутентификатором банк сразу просит о totp.
    m = _STEP_RE.match(msg)
    if m and m.group(1) == "totp":
        kind, prompt = "totp", "[2/3] Код из приложения-аутентификатора: "
    else:
        kind, prompt = "otp", "[2/3] SMS-код: "
    try:
        s.confirm_step(kind, getpass.getpass(prompt))
        print("    Код принят.")
    except TbankApiError as e:
        if _next_step(e) != "password":
            print(f"    ОШИБКА: {e}")
            return 1
        # bank wants password — continue to step 3

    # Check if we already have a session (OTP was enough)
    if s.access_token:
        srv._session = s
        if not srv._save_session(s):
            return _save_failed()
        print("\n[3/3] Сессия создана.")   # sessionid не печатаем — это секрет
        _success()
        return 0

    # Step 3: password (if bank asked)
    if os.environ.get("TBANK_PASSWORD"):
        password = os.environ["TBANK_PASSWORD"]
        print("[3/3] Пароль из TBANK_PASSWORD env.")
    else:
        password = getpass.getpass("[3/3] Пароль (не отображается): ")

    try:
        s.confirm_step("password", password)
    except TbankApiError as e:
        if _next_step(e) == "pin":
            print("    Банк просит PIN.")
            if os.environ.get("TBANK_PIN"):
                pin = os.environ["TBANK_PIN"]
            else:
                pin = getpass.getpass("    PIN (не отображается): ")
            s.confirm_step("pin", pin)
        else:
            print(f"    ОШИБКА: {e}")
            return 1

    srv._session = s
    if not srv._save_session(s):
        return _save_failed()
    print("\n[3/3] Сессия создана.")   # sessionid не печатаем — это секрет
    _success()
    return 0


def _save_failed():
    """Вход состоялся, файла нет. Это провал: SMS уже потрачена, а MCP поднимет пустоту."""
    print(f"\n✗ Вход прошёл, но сессию НЕ удалось сохранить в {srv._SESSION_FILE}.")
    print(f"  Проверь права на {os.path.dirname(srv._SESSION_FILE)} и повтори вход.")
    return 1


def _success():
    print(f"\n✓ ГОТОВО! Сессия сохранена: {srv._SESSION_FILE} (права 0600).")
    print("  MCP читает этот же файл — путь совпадает без ручной настройки.")
    print("  Запусти своего MCP-клиента (Claude Code и т.п.).")
    print("  Пароль НЕ передан агенту — он работает с сохранённой сессией.")
    print("  Тулы: list_accounts, grocery_search, transfer, ...")


if __name__ == "__main__":
    sys.exit(main())
