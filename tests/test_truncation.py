"""Read tools must not lose records without saying so.

get_data / operations_histogram / invest_portfolio returned json.dumps(...)[:N] and
list_operations rendered ops[:50] with no count and no way to ask for more. Both
shapes lie by omission, and the agent has no way to notice:

  * On the real capture, get_data("merchant_subs") serializes to 5871 chars holding
    8 subscriptions. The 5000-char cut severed one mid-object and dropped another,
    leaving a string that still looks like data — the budget skill reads it, counts
    6, and under-reports the monthly subscription spend.
  * A 30-day list_operations returning 229 operations showed the newest 50 — about
    four days — with nothing in the output saying so, and operations 51+ were
    unreachable through any argument.

    python3 tests/test_truncation.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import server  # noqa: E402
from src.client import MobileSession  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def test_json_payload_keeps_whole_records():
    """A payload that does not fit must lose WHOLE records and say how many."""
    subs = {"subscriptions": [
        {"id": f"sub-{i}", "name": f"Подписка номер {i}", "amount": {"value": 100 + i},
         "merchant": {"title": f"Мерчант {i}", "logo": "https://x/" + "l" * 60}}
        for i in range(8)]}
    full = json.dumps(subs, ensure_ascii=False)
    check(len(full) > 900, f"fixture too small to trigger trimming ({len(full)})")

    out = server._json_out(subs, limit=900)
    check("ПОКАЗАНО" in out, f"truncation must be announced, got: {out[:120]}")

    body = out.split("\n", 1)[1]
    parsed = json.loads(body)          # the old [:N] made this impossible
    kept = parsed["subscriptions"]
    check(0 < len(kept) < 8, f"expected a partial list, got {len(kept)} of 8")
    check(f"{len(kept)} из 8" in out,
          f"the header must state how many of how many: {out.splitlines()[0]}")
    for rec in kept:
        check(set(rec) == {"id", "name", "amount", "merchant"},
              f"a kept record was cut apart: {rec}")
    check("не считай" in out.lower(),
          "the agent must be told not to compute totals from a partial answer")

    # Fits → returned untouched and parseable, with no scary header.
    small = server._json_out({"a": 1}, limit=900)
    check(small == '{"a": 1}', f"a payload that fits must be untouched, got {small!r}")


def test_untrimmable_payload_is_flagged_loudly():
    """No list to trim: the text is still cut, but it must be impossible to mistake
    for a complete answer."""
    blob = {"description": "x" * 5000}
    out = server._json_out(blob, limit=200)
    check(out.startswith("# ОТВЕТ ОБРЕЗАН"), f"missing the loud marker: {out[:80]}")
    check("НЕ валидный JSON" in out,
          "the agent must be told the remainder does not parse")
    check("5" in out.split("\n")[0], "the header should carry the real size")


def trimmed_body(out):
    """The parsed payload of a «ПОКАЗАНО …» answer, or None if _json_out fell back
    to the character cut. Returning None rather than raising is what lets the test
    report «it fell through» instead of dying inside json.loads."""
    if not out.startswith("# ПОКАЗАНО"):
        return None
    try:
        return json.loads(out.split("\n", 1)[1])
    except (ValueError, IndexError):
        return None


def test_trimming_handles_shapes_the_single_pass_gave_up_on():
    """Two ordinary payloads used to fall through to the character cut even though
    dropping whole records would have fitted — the worst of both: nothing parses AND
    records are lost."""
    # (a) Sibling lists of comparable size. Shrinking only the biggest one never gets
    # under the limit, and a single pass had nothing else to try.
    wide = {f"группа{g}": [{"id": f"{g}-{i}", "name": "Запись " + "я" * 40}
                           for i in range(20)] for g in range(4)}
    out = server._json_out(wide, limit=1500)
    parsed = trimmed_body(out)
    check(parsed is not None,
          f"a payload of several lists fell through to the char cut: {out[:110]}")
    if parsed is not None:
        check(sum(len(v) for v in parsed.values()) < 80, "nothing was actually dropped")
        for name, lst in parsed.items():
            for rec in lst:
                check(set(rec) == {"id", "name"}, f"{name}: record cut apart: {rec}")
        check("из 20" in out,
              f"the header must state the real per-list totals: {out[:200]}")

    # (b) The payload IS the list. Its path is (), which _set_in cannot address, so a
    # bare list — what several get_data sections return — was always character-cut.
    rows = [{"id": i, "name": "Операция " + "я" * 40} for i in range(60)]
    out2 = server._json_out(rows, limit=1200)
    kept = trimmed_body(out2)
    check(kept is not None,
          f"a top-level list fell through to the char cut: {out2[:110]}")
    if kept is not None:
        check(isinstance(kept, list) and 0 < len(kept) < 60,
              f"expected a partial list of records, got {type(kept).__name__}")
        check("из 60" in out2, f"the header must say how many of how many: {out2[:200]}")
        check(all(set(r) == {"id", "name"} for r in kept), "a kept record was cut apart")


def test_list_tools_report_the_total_they_are_hiding():
    rows = [{"n": i} for i in range(229)]
    out = server._rows_out(rows, lambda r: f"- {r['n']}", limit=50, total=len(rows),
                           header="[account X] операции за 30 дн.")
    head = out.splitlines()[0]
    check("229 всего" in head, f"the real total must be in the header: {head}")
    check("показано 50" in head, f"the shown count must be in the header: {head}")
    check("limit=229" in head, f"the header must say how to get the rest: {head}")
    check(len(out.splitlines()) == 51, f"expected header + 50 rows, got {len(out.splitlines())}")

    # limit=0 means everything, and then there is nothing to warn about.
    every = server._rows_out(rows, lambda r: f"- {r['n']}", limit=0, total=len(rows),
                             header="h")
    check(len(every.splitlines()) == 230, "limit=0 must render every row")
    check("limit=" not in every.splitlines()[0],
          f"a complete answer must not nag about limit: {every.splitlines()[0]}")

    # Nothing hidden → no misleading "новые сверху" promise either.
    few = server._rows_out(rows[:3], lambda r: f"- {r['n']}", limit=50, total=3, header="h")
    check("показано 3" in few and "limit=" not in few.splitlines()[0], few.splitlines()[0])


class OpsSession(MobileSession):
    def __init__(self, n):
        self.n = n

    def ensure_fresh(self, *a, **kw):
        return None

    def list_operations(self, account_id, start, end):
        return [{"operationTime": {"milliseconds": 1784658904000 - i * 3600_000},
                 "type": "Debit", "amount": {"value": 100 + i, "currency": {"name": "RUB"}},
                 "description": f"Покупка {i}"} for i in range(self.n)]


def test_list_operations_end_to_end():
    """Through the real tool: the header must expose the truncation, and limit must
    actually widen the window."""
    saved = server._require
    server._require = lambda: OpsSession(229)
    try:
        out = server.list_operations("0000000000", days=30)
        head = out.splitlines()[0]
        check("229 всего" in head and "показано 50" in head,
              f"list_operations hides its truncation: {head}")
        check(len(out.splitlines()) == 51, f"expected 50 rows, got {len(out.splitlines()) - 1}")

        wide = server.list_operations("0000000000", days=30, limit=0)
        check(len(wide.splitlines()) == 230,
              f"limit=0 must return every operation, got {len(wide.splitlines()) - 1}")

        exact = server.list_operations("0000000000", days=30, limit=229)
        check("limit=" not in exact.splitlines()[0],
              "asking for exactly the total must not still nag about limit")
    finally:
        server._require = saved


class RowsSession(MobileSession):
    """A card's operations and the client's orders, in the shapes the tools parse."""

    def __init__(self, n):
        self.n = n

    def ensure_fresh(self, *a, **kw):
        return None

    def list_operations(self, account_id, start, end):
        return [{"operationTime": {"milliseconds": 1784658904000 - i * 3600_000},
                 "type": "Debit", "card": "291395142",
                 "amount": {"value": 100 + i, "currency": {"name": "RUB"}},
                 "description": f"Покупка {i}"} for i in range(self.n)]

    def orders(self):
        return [{"orderId": f"o-{i}", "objectType": "grocery", "status": "DONE",
                 "created": f"2026-07-{(i % 28) + 1:02d}", "amount": 100 + i,
                 "fields": {"applicationName": "ВкусВилл"}} for i in range(self.n)]


def test_limit_zero_means_everything_in_every_list_tool():
    """`limit=0` is «покажи всё» in list_operations, and the docstrings say so — but
    card_operations and orders sliced with a bare rows[:limit], where 0 means the
    opposite. An agent asking for the complete answer got an empty one, under a
    header that still announced the full count."""
    saved = server._require
    server._require = lambda: RowsSession(120)
    try:
        for name, call in (("card_operations",
                            lambda lim: server.card_operations("291395142", 30, lim)),
                           ("orders", lambda lim: server.orders("", lim))):
            every = call(0)
            rows = [ln for ln in every.splitlines() if ln.startswith("- ")]
            check(len(rows) == 120,
                  f"{name}(limit=0) returned {len(rows)} rows, expected all 120 "
                  f"— 0 read as «ничего»?")
            head_all = (every.splitlines() or [""])[0]
            check("limit=" not in head_all,
                  f"{name} must not nag about limit when it showed everything: "
                  f"{head_all!r}")

            few = call(5)
            head = (few.splitlines() or [""])[0]
            shown = [ln for ln in few.splitlines() if ln.startswith("- ")]
            check(len(shown) == 5, f"{name}(limit=5) returned {len(shown)} rows")
            check("120 всего" in head and "показано 5" in head,
                  f"{name} must say what it is hiding: {head!r}")
            check("limit=120" in head, f"{name} must say how to get the rest: {head!r}")
    finally:
        server._require = saved
    print("  limit=0 means «all» in card_operations and orders, as in list_operations")


def test_real_capture_payload_survives():
    """The concrete case from the audit: 8 subscriptions must not become 6."""
    cap = os.environ.get("TBANK_CAPTURE", os.path.expanduser("~/tbank-app/captures.xml"))
    if not os.path.exists(cap):
        print("  real capture: SKIPPED (capture absent — synthetic cases above still ran)")
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import test_cart_body_matches_capture as C

    C.CAPTURE = cap
    items = C._items()
    payload = None
    for i, _ in enumerate(items):
        try:
            body = C.response_json(items, i)
        except Exception:
            continue
        p = body.get("payload") if isinstance(body, dict) else None
        if isinstance(p, dict) and isinstance(p.get("subscriptions"), list) \
                and len(p["subscriptions"]) >= 4:
            payload = p
            break
    if payload is None:
        print("  real capture: no merchant_subs payload found, skipped")
        return
    n = len(payload["subscriptions"])
    out = server._json_out(payload, 5000)
    if out.startswith("#"):
        kept = json.loads(out.split("\n", 1)[1])["subscriptions"]
        check(f"{len(kept)} из {n}" in out,
              "the trimmed real payload must state the true count")
        json.loads(out.split("\n", 1)[1])          # must still parse
        print(f"  real capture: {n} subscriptions → {len(kept)} kept, count reported")
    else:
        check(json.loads(out) == payload, "an untrimmed payload must round-trip")
        print(f"  real capture: {n} subscriptions fit whole, nothing dropped")


def main():
    print("truncation honesty:")
    test_json_payload_keeps_whole_records()
    test_untrimmable_payload_is_flagged_loudly()
    test_trimming_handles_shapes_the_single_pass_gave_up_on()
    test_list_tools_report_the_total_they_are_hiding()
    test_list_operations_end_to_end()
    test_limit_zero_means_everything_in_every_list_tool()
    test_real_capture_payload_survives()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
