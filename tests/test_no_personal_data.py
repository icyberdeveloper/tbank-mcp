"""No real personal data in a tracked file.

This is the odd one out in this suite, and deliberately so. Every other test here
executes code and asserts behaviour, because asserting on source TEXT proves only
that somebody typed something. This one asserts on text on purpose: the thing under
test is not the program, it is the repository as a published artifact. There is no
behaviour to run — the defect is that a value exists in a file at all.

It exists because scrubbing does not hold. A real booking code was removed from the
tickets skill in 987556b and was back in a tracked file three days later, in a test
fixture nobody connected to the earlier removal. A full audit later found the repo
owner's ИНН, their surname and a government document number sitting at the tip, each
copied out of a Burp capture while building a fixture around it. Every one of those
arrived the same way: a real response was pasted in to make a test realistic, and the
personal fields came with it.

So the rule is inverted. Rather than hunt for values that look real — which cannot be
decided by machine — every value SHAPED like personal data must be declared. A hit
that is not in ALLOWED fails, and the only way to clear it is for a person to look at
the value and say what it is. That is the whole mechanism: it forces the question at
the moment the value enters, which is the only moment anyone can answer it.

ALLOWED holds no real-world values at all. Not one of these identifies anybody, and
none was copied from a capture. If you are about to add a value you took from real
traffic, that is the case this file exists to stop.

    python3 tests/test_no_personal_data.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# Shapes that carry identity. Each has actually leaked into this repo at least once,
# except the coordinate, which leaked into the git HISTORY and is kept here so it
# cannot come back to the tip.
SHAPES = {
    "ИНН физлица (12 цифр)": re.compile(r"(?<![\d.])\d{12}(?![\d.])"),
    "счёт (20 цифр)": re.compile(r"(?<!\d)\d{20}(?!\d)"),
    "СТС / серия+номер": re.compile(r"(?<!\d)\d{4} \d{6}(?!\d)"),
    # Seven uppercase alphanumerics with at least one of each. Written as
    # lookaheads rather than a positional pattern on purpose: the first version of
    # this line was `[A-Z]{2}\d[A-Z]{2}\d[A-Z]`, derived from the SYNTHETIC
    # replacement rather than from the real code it exists to catch, and it did not
    # match the shape that actually leaked. The probe below is what found that.
    "код брони": re.compile(
        r"\b(?=[A-Z0-9]{7}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{7}\b"),
    # 6+ decimals of latitude is metre precision — a building, not a city.
    "координата (6+ знаков)": re.compile(r"(?<![\d.])\d{2}\.\d{6,}(?![\d])"),
    # Госномер: буква-3 цифры-2 буквы-регион, обе раскладки (латинский набор —
    # ГОСТ-транслитерация ABEKMHOPCTYX). Ни одна другая форма его не ловит: цифр
    # подряд там максимум три.
    "госномер": re.compile(
        r"\b(?:[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}"
        r"|[ABEKMHOPCTYX]\d{3}[ABEKMHOPCTYX]{2}\d{2,3})\b"),
    # Почта. Адрес — это человек, который в этот репозиторий не заглядывал и
    # согласия не давал. Синтетика здесь живёт на example.com.
    "e-mail": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # Телефон РФ: +7/8/7 и десять цифр. Реальный номер стороннего человека
    # (получатель переводов владельца) утёк ровно так — литералом в докстринге
    # теста — и ни одна «счётная» форма его не ловила: 11 цифр с ведущей 7/8 не
    # похожи ни на ИНН, ни на счёт. Единственная синтетика тут — +79991234567/576.
    "телефон РФ": re.compile(r"(?<!\d)(?:\+7|7|8)\d{10}(?!\d)"),
    # Внутренний id банка — счёт/карта/ucid/slotId, 9–11 цифр. Реальные id счетов и
    # карт владельца попали в тесты как литералы, а 20-значная форма их не видит: у
    # неё другой класс. 12+ цифр (paymentId/userPaymentId) сюда НЕ попадают намеренно
    # — это не идентификатор человека, и бесконечно раздувать ALLOWED ими незачем.
    "внутренний id (9–11 цифр)": re.compile(r"(?<!\d)\d{9,11}(?!\d)"),
    # Усечённый счёт: 19 цифр. Первые 19 знаков реального 20-значного счёта
    # контрагента прошли мимо \d{20} — а обрезок id это коллизия, не косметика.
    "счёт-обрезок (19 цифр)": re.compile(r"(?<!\d)\d{19}(?!\d)"),
}

# Every value in the repo matching a shape above. Synthetic, all of them.
#
# Accounts keep the real Russian prefix (40702 = организация, 40817 = физлицо,
# 30101 = корреспондентский) because the provider's regexp and the app's own
# validation reject anything else — the prefix is the protocol, the tail is the
# person. Every tail here is zeros or a counter.
ALLOWED = {
    # ИНН — счётчики из фикстур и КБЖУ-заглушек
    "000000000000", "000000000001", "000000000002", "000000000003",
    "000000000004", "000000000012", "100000000000", "100000000001",
    "400000000001", "444444444444", "100000000002",
    # не ИНН: счётчики на месте bankMemberId СБП в fixtures/recipient.json. Реальные
    # id участников СБП публичны и называют банк, а не человека, но они той же формы
    # и взяты из захвата — какой это банк, видно из brand.name, поэтому в фикстуре
    # стоят счётчики.
    "400000000002", "400000000003",
    # счета
    "30101810000000000000",   # корр. счёт синтетического банка (БИК 044525000)
    "30101810000000000001",
    "40702810000000000001",   # расчётный счёт «ООО ПРИМЕР» из фикстуры реквизитов
    "40702810900000001234",
    "40817810000000000000",
    "40817810100000001234",
    "30101810000000000009",   # второй синтетический корр. счёт, tests/test_requisites
    # СТС — пример форматирования в комментарии documents()
    "1234 567890",
    # код брони — фикстура ticket_qr
    "QQ1AB2C",
    # не код брони: заголовок платёжного QR по ГОСТ Р 56042-2014, той же формы
    "ST00012",
    # почта — только example.com
    "user@example.com",
    # координаты по умолчанию для anti-fraud блока /v1/confirm (_CONFIRM_GEO в
    # client.py): центр Москвы, публичный ориентир, НЕ из трафика — реальная гео
    # пользователя в захвате была другой (Петербург) и намеренно не использована.
    "55.751244", "37.618423",
    # телефоны — синтетика, обе формы (с плюсом ловит «телефон РФ», без плюса —
    # «внутренний id»). +79991234567 — сквозной получатель во всём наборе тестов;
    # +79991234576 — его «двойник» в докстринге test_session_level.
    "+79991234567", "+79991234576", "79991234567", "79991234576",
    # внутренние id (9–11 цифр). Все синтетические: счётчики, повторы, монотонные
    # последовательности. Проверено — ни одного нет в захватах.
    "000000000", "0000000000", "0123456789", "044525000",
    "1000000000", "10000000000", "10000000001", "10000000002", "1000000001",
    "100000001", "100000002", "100000003", "1111111111", "123456789",
    "1234567890", "132988597", "133098547", "133000001",
    "200000000", "2000000001", "2000000002", "2000000003", "2222222222",
    "3333333333", "40000000000", "7700000000", "770000001", "9999999999",
    # синтетические id счетов (5-я серия) и карт/ucid (4-я серия), заменившие
    # реальные значения владельца, которые раньше стояли литералами в тестах
    "5000000001", "5000000002", "5000000003", "4000000001", "4000000002",
    # усечённый счёт (19 цифр) — подставной «плохой» реквизит в test_requisites,
    # заменил первые 19 знаков реального счёта контрагента
    "4070281000000000001",
}

# Binary and vendored content: certificate roots are public, and their base64
# genuinely contains long digit runs.
SKIP_PREFIXES = ("ca/roots/",)
SKIP_SUFFIXES = (".png", ".jpg", ".pdf", ".pem", ".crt", ".cer", ".ico")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    # This file is excluded, and only this one. Its probes exist to HAVE the flagged
    # shapes — that is how it proves the regexes still match — so scanning itself
    # would report them forever, and the only way to quiet that is to put them in
    # ALLOWED, which the self-test below explicitly forbids.
    me = os.path.relpath(os.path.abspath(__file__), ROOT)
    return [f for f in out.split("\n")
            if f and f != me
            and not f.startswith(SKIP_PREFIXES) and not f.endswith(SKIP_SUFFIXES)]


def test_no_undeclared_identity_shaped_value_is_tracked():
    files = tracked_files()
    check(len(files) > 20, f"git ls-files returned {len(files)} files — is this a repo?")
    scanned = 0
    for rel in files:
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for label, rx in SHAPES.items():
            for value in set(rx.findall(text)):
                if value in ALLOWED:
                    continue
                line = next((i for i, l in enumerate(text.splitlines(), 1)
                             if value in l), 0)
                failures.append(
                    f"{rel}:{line} содержит значение вида «{label}».\n"
                    f"      Если оно синтетическое — впиши его в ALLOWED в "
                    f"tests/test_no_personal_data.py.\n"
                    f"      Если оно взято из реального трафика — его нельзя "
                    f"коммитить: замени и посмотри, не попало ли оно уже в историю.")
    print(f"  {scanned} трекнутых файлов, {len(SHAPES)} форм, "
          f"{len(ALLOWED)} объявленных синтетических значений")


def test_the_scan_actually_catches_a_real_looking_value():
    """A guard whose regexes silently stopped matching would pass forever.

    So the shapes are exercised against values of the class they exist to catch —
    none of these belongs to anybody; they are constructed to have the right form.
    """
    # Deliberately UNLIKE any real value. The first version reused the real СТС
    # series and the real latitude prefix with a digit appended — close enough that
    # the probes were themselves the thing this file exists to keep out.
    probes = [
        ("ИНН физлица (12 цифр)", "111122223333"),
        ("счёт (20 цифр)", "40702811111111111111"),
        ("СТС / серия+номер", "1111 222222"),
        ("код брони", "ZZ9YY8X"),
        ("координата (6+ знаков)", "11.222222"),
        ("госномер", "У999УУ999"),
        ("e-mail", "someone@nowhere.invalid"),
        ("телефон РФ", "+79995556677"),
        ("внутренний id (9–11 цифр)", "987654321"),
        ("счёт-обрезок (19 цифр)", "9999999999999999999"),
    ]
    for label, probe in probes:
        rx = SHAPES[label]
        check(bool(rx.search(probe)),
              f"форма «{label}» больше не ловит {probe!r} — регулярка сломана")
        check(probe not in ALLOWED,
              f"{probe!r} попал в ALLOWED — это значение нужной формы, не образец")

    # And the shapes must not fire on ordinary code. A guard that cries wolf gets
    # its ALLOWED list padded until it guards nothing.
    # userPaymentId (13 цифр) заодно доказывает, что «внутренний id (9–11)» не
    # тянется в длинные платёжные идентификаторы.
    benign = ('for page in range(1, 8):', 'timeout=30', '"appVersion": "7.39.1"',
              'sha256(canon)[:12]', 'userPaymentId = 1785786616973',
              'lat=55.75, lon=37.52')
    for label, rx in SHAPES.items():
        for text in benign:
            check(not rx.search(text),
                  f"форма «{label}» ложно срабатывает на {text!r}")
    print(f"  {len(probes)} форм ловят подставные значения и молчат на "
          f"{len(benign)} обычных строках")


def main():
    print("персональные данные в трекнутых файлах:")
    test_no_undeclared_identity_shaped_value_is_tracked()
    test_the_scan_actually_catches_a_real_looking_value()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
