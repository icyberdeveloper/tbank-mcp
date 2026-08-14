"""Product-name matching: deterministic token-AND hygiene + honest plan_order confidence.

The live 32-line grocery run failed mostly on brittle matching. These assert the
deterministic MCP layer (client._name_matches) against the real traps we hit, and that
a low-confidence plan_order pick is flagged «⚠ проверь», not a blind ✓.

What is DELIBERATELY not asserted here: cross-script and true synonyms
(«сникерс»→SNICKERS, «помидоры»→«томаты»). Those are resolved by the agent via the web,
not by a dictionary in code — so the matcher must NOT fake them, and the tests check that
it doesn't.

    python3 tests/test_matching.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tbank-match-")
os.environ["TBANK_TRACE_FILE"] = os.path.join(_TMP, "calls.jsonl")
os.environ["TBANK_ATTEMPTS"] = os.path.join(_TMP, "attempts.jsonl")
os.environ["TBANK_EVENTS"] = os.path.join(_TMP, "events.jsonl")

from src import server                                            # noqa: E402
from src.client import MobileSession                              # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


M = MobileSession._name_matches


def test_hygiene_matches_what_substring_could_not():
    # the backtick in «Lay`s» + the stopwords «со вкусом» must not block the match
    check(M("lay's краб", "Чипсы Lay`s со вкусом Краба") == 1.0,
          "backtick + stopwords must still match «lay's краб»")
    # order-free, stopword-free: «фарш из индейки» must hit «Фарш индейки»
    check(M("фарш из индейки", "Фарш индейки охлаждённый") == 1.0,
          "the stopword «из» between tokens must be dropped")
    # a 5-char stem links the colloquial «сгущёнка» to the catalogue «сгущённое»
    check(M("сгущёнка", "Молоко цельное сгущённое варёное") == 1.0,
          "«сгущёнка» must stem-match «сгущённое»")
    print("  hygiene: backtick / stopword / stem now match where substring failed")


def test_matcher_does_not_fake_synonyms_or_transliteration():
    # these are the agent's job via the web — the deterministic layer must return 0,
    # not a wrong confident match
    check(M("помидоры", "Томаты сливовидные весовые") == 0.0,
          "«помидоры»≠«томаты» is a synonym — must NOT be faked by the matcher")
    check(M("сникерс", "Батончик SNICKERS Лесной орех") == 0.0,
          "«сникерс»≠«SNICKERS» is transliteration — must NOT be faked by the matcher")
    print("  restraint: synonyms/translit are left for the web, not invented in code")


def test_false_positives_are_rejected():
    check(M("магнат", "Магний B6 таблетки шипучие") == 0.0,
          "«магнат»→магна must not match «магний»→магни (share only магн)")
    check(M("кола", "Колбаса варёная докторская") == 0.0,
          "short token «кола» must match in full, not prefix «кол» of «колбаса»")
    check(M("splat", "Мыло Vetyver Splash") == 0.0,
          "«splat» must not match «splash»")
    print("  precision: магнат≠магний, кола≠колбаса, splat≠splash")


def test_missing_qualifier_lowers_confidence():
    # «фарш индейки» against a chicken mince: «индейки» is absent → recall 0.5,
    # which must fall below the ✓ threshold
    conf = M("фарш индейки", "Фарш куриный Петелинка")
    check(conf == 0.5, f"a dropped qualifier must halve the score, got {conf}")
    check(conf < MobileSession.GROCERY_MATCH_OK,
          "0.5 must be below the ✓ threshold → «⚠ проверь»")
    print("  confidence: a wrong-flavour pick scores below the ✓ threshold")


def test_plan_order_flags_low_confidence_pick():
    """The whole point: a plausible-but-wrong pick must not hide as ✓."""
    class Stub(MobileSession):
        def __init__(self):
            self._memo = {}

        def ensure_fresh(self, *a, **k):
            return None

        def grocery_plan_order(self, *a, **k):
            return {"store": "204", "total_sum": 100, "missing": [], "items": [
                {"id": "1", "name": "Фарш куриный Петелинка", "price": 220,
                 "weight": "450 г", "likely_raw": True, "source": "search",
                 "query": "фарш индейки", "match": 0.5},
                {"id": "2", "name": "Молоко сгущённое варёное", "price": 196,
                 "weight": "380 г", "likely_raw": False, "source": "search",
                 "query": "сгущёнка", "match": 1.0}]}

    saved = server._require
    server._require = lambda: Stub()
    try:
        out = server.grocery_plan_order('["фарш индейки","сгущёнка"]',
                                        app_id="204", point_id="5980")
    finally:
        server._require = saved
    check("⚠ проверь" in out and "фарш индейки" in out,
          f"low-confidence pick must be flagged with its query: {out!r}")
    check("✓ id=2" in out,
          f"the confident pick must keep ✓: {out!r}")
    print("  plan_order: low-confidence → «⚠ проверь», confident → ✓")


def test_pick_candidate_prefers_higher_match_over_cheaper():
    """The planner must pick «Lay`s Краб» over a CHEAPER «Lay`s Куриные» — a fuller
    token match is the more-right product. This is the bug a live plan_order exposed:
    _pick_candidate ignored the match score and took cheapest-of-anything."""
    s = MobileSession.__new__(MobileSession)  # pure scorer, no __init__ needed
    results = [
        {"name": "Чипсы Lay`s Max Куриные крылышки барбекю", "price": 193,
         "match": 0.5, "likely_raw": False},
        {"name": "Чипсы Lay`s со вкусом Краба", "price": 238,
         "match": 1.0, "likely_raw": False},
    ]
    best = s._pick_candidate(results, "lay's краб")
    check(best is not None and "Краба" in best.get("name", ""),
          f"a fuller match must beat a cheaper partial match, got: {best}")
    print("  pick: higher token-match beats cheaper price (Краб over Куриные)")


def main():
    print("matching:")
    test_hygiene_matches_what_substring_could_not()
    test_pick_candidate_prefers_higher_match_over_cheaper()
    test_matcher_does_not_fake_synonyms_or_transliteration()
    test_false_positives_are_rejected()
    test_missing_qualifier_lowers_confidence()
    test_plan_order_flags_low_confidence_pick()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
