#!/usr/bin/env python3
"""T-Bank MCP — локальный скрипт логина (ВНЕ агента/LLM).

Пароль, PIN и SMS-код вводятся через getpass (не отображаются в терминале) или
читаются из env. Они НИКОГДА не попадают в контекст модели (LLM) — скрипт
запускается напрямую из шелла.

Два независимых аккаунта, один скрипт:

    .venv/bin/python login_cli.py +79991234567     → банк, session.json
    .venv/bin/python login_cli.py --myt n.ivanov   → MyT,  myt.json

Интерпретатор — из .venv репозитория: там стоят зависимости, и там же MCP берёт
тот же файл сессии. Системный python3 упадёт на импорте.

MyT — РАБОЧЕЕ приложение Т-Банка (календарь встреч, парковка в офисе). Аккаунт
другой: банковская сессия к нему доступа не даёт, и наоборот. Флаг выбирает,
какую из двух сессий обновляем; вторая при этом не трогается — поэтому логин в
MyT не выкидывает из банка, а перелогин в банке не роняет календарь.

С паролем в env:
    TBANK_PASSWORD="пароль" .venv/bin/python login_cli.py +79991234567
    MYT_PASSWORD="пароль"   .venv/bin/python login_cli.py --myt n.ivanov

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
    from src import myt
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
  .venv/bin/python login_cli.py +79991234567     — банк (счета, переводы, продукты)
  .venv/bin/python login_cli.py --myt n.ivanov   — MyT (рабочий календарь и парковка)

  TBANK_PASSWORD / MYT_PASSWORD env — пароль (или спросит через getpass)
  TBANK_PIN env — PIN, если банк попросит"""


def main():
    argv = sys.argv[1:]
    flags = {a for a in argv if a.startswith("-")}
    args = [a for a in argv if not a.startswith("-")]
    unknown = flags - {"--myt"}
    if unknown or len(args) != 1:
        if unknown:
            print(f"Неизвестный флаг: {' '.join(sorted(unknown))}\n")
        print(USAGE)
        return 1
    return login_myt(args[0]) if "--myt" in flags else login_bank(args[0])


# ── банк ────────────────────────────────────────────────────────────────────

def login_bank(phone):
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
        srv._save_session(s)
        print(f"\n[3/3] Сессия создана. sessionid={s.mobile_sessionid[:12]}…")
        _success_bank()
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
    srv._save_session(s)
    print(f"\n[3/3] Сессия создана. sessionid={s.mobile_sessionid[:12]}…")
    _success_bank()
    return 0


def _success_bank():
    print(f"\n✓ ГОТОВО! Сессия сохранена: {srv._SESSION_FILE} (права 0600).")
    print("  MCP читает этот же файл — путь совпадает без ручной настройки.")
    print("  Запусти Claude Code в этом репозитории.")
    print("  Пароль НЕ передан агенту — он работает с сохранённой сессией.")
    print("  Тулы: list_accounts, grocery_search, transfer, ...")


# ── MyT: рабочий календарь и парковка ───────────────────────────────────────

def login_myt(username):
    """grantType=password на корпоративном контуре.

    Тула логина в MCP для этого НЕТ намеренно, и флаг здесь ничего не меняет:
    рабочий пароль открывает не один счёт, а все рабочие системы, и цена его
    попадания в транскрипт несопоставима с удобством."""
    s = myt.MytSession()

    if os.environ.get("MYT_PASSWORD"):
        password = os.environ["MYT_PASSWORD"]
        print("[1/2] Пароль из MYT_PASSWORD env.")
    else:
        password = getpass.getpass("[1/2] Пароль (не отображается): ")

    # Первый вызов без кода — он и ЗАКАЗЫВАЕТ SMS. Сервер отвечает ошибкой
    # sms_required: это штатный шаг протокола, а не сбой.
    try:
        s.login(username, password)
        print("    SMS не потребовалась.")
    except TbankApiError as e:
        if e.result_code != "sms_required":
            print(f"    ОШИБКА: {e}")
            return 1
        print(f"    SMS отправлена: {e.message}")
        code = getpass.getpass("[2/2] SMS-код: ")
        try:
            s.login(username, password, code)
        except TbankApiError as e2:
            print(f"    ОШИБКА: {e2}")
            return 1

    srv._myt_session = s
    s._on_persist = lambda: srv._save_myt(s)
    srv._save_myt(s)
    print(f"\n✓ ГОТОВО! Корпоративная сессия сохранена: {srv._MYT_FILE} (права 0600).")
    print(f"  Сотрудник: {s.username}, токен живёт {s.expires_in} с и обновляется сам.")
    print("  Банковская сессия не тронута — это отдельный файл.")
    print("  Тулы: calendar_schedule, calendar_event, calendar_respond, calendar_cancel,")
    print("        parking_places, parking_book, office_bookings, myt_status.")
    print("  Пароль НЕ передан агенту.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
