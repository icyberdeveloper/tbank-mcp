#!/usr/bin/env python3
"""T-Bank MCP — локальный скрипт логина (ВНЕ агента/LLM).

Пароль, PIN и SMS-код вводятся через getpass (не отображаются в терминале) или
читаются из env. Они НИКОГДА не попадают в контекст модели (LLM) — скрипт
запускается напрямую из шелла.

    .venv/bin/python login_cli.py +79991234567

Интерпретатор — из .venv репозитория: там стоят зависимости, и там же MCP берёт
тот же файл сессии. Системный python3 упадёт на импорте.

С паролем в env:
    TBANK_PASSWORD="пароль" .venv/bin/python login_cli.py +79991234567

Без env пароль спросит сам скрипт:
    .venv/bin/python login_cli.py +79991234567
    [2/3] SMS-код: ****
    [3/3] Пароль (не отображается): ****

После логина — запусти Claude Code в этом репозитории.
"""
import sys
import os
import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from src.client import TbankApiError
    from src import server as srv
except ModuleNotFoundError as _e:
    # Скрипт запускают руками, и первым делом — системным python3, потому что
    # так набирается быстрее. Зависимости живут в .venv репозитория, и голый
    # ModuleNotFoundError: No module named 'mcp' не подсказывает вообще ничего:
    # человек идёт ставить mcp глобально вместо того, чтобы взять готовое
    # окружение. Тут нужен весь src.server, потому что путь к файлу сессии и
    # запись на диск определены в нём — общие с MCP, чтобы не разъезжались.
    _VENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    print(f"Не хватает зависимости: {_e.name}")
    if os.path.exists(_VENV):
        print("Похоже, запущено системным python. Повтори ту же команду, но "
              "интерпретатором из окружения репозитория:")
        print(f"  {_VENV} login_cli.py …")
    else:
        print("Окружения нет. Создай его:")
        print("  python3 -m venv .venv && .venv/bin/pip install -e .")
    sys.exit(1)


USAGE = """Usage:
  .venv/bin/python login_cli.py +79991234567

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

    # Step 2: OTP from user (getpass — скрытый ввод)
    otp = getpass.getpass("[2/3] SMS-код: ")
    try:
        s.confirm_step("otp", otp)
        print("    OTP принят.")
    except TbankApiError as e:
        if "password" not in str(e.message).lower():
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
        if "pin" in str(e.message).lower():
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
    print("  Запусти Claude Code в этом репозитории.")
    print("  Пароль НЕ передан агенту — он работает с сохранённой сессией.")
    print("  Тулы: list_accounts, grocery_search, transfer, ...")


if __name__ == "__main__":
    sys.exit(main())
