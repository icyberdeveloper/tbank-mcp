"""Three regressions that were invisible until a specific endpoint was called.

1. ensure_fresh() must prefer refresh() — one request — and fall back to
   silent_relogin (authorize → step → token + a 3 s sleep) only when the
   refresh_token is dead. Both grants mint an equally privileged session; the
   preference is about cost, not access level.

2. ensure_client_session() must re-mint when the session has lapsed to ANONYMOUS.
   The CLIENT window is ~11 minutes (ping.portalSessionExpiresInSeconds ≈ 659)
   while ensure_fresh only tracks the ~2h access_token, so between re-mints the
   session-validating endpoints (card_credentials, prefill/profile documents,
   session_status) fail while everything else keeps working on the Bearer.

3. nutrition() must parse the free-text form. Only some retailers fill the
   structured protein/fat/carbohydrate/energy fields — ВкусВилл leaves all four
   empty and puts everything in `value`. Reading only the structured fields makes
   half the catalog look like it has no nutrition data at all.

    python3 tests/test_session_level_and_nutrition.py
"""
import os
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.client import MobileSession, TbankApiError  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


class FakeSession(MobileSession):
    """A session that records which re-mint path ensure_fresh chose."""

    def __init__(self, refresh_works=True, has_sso=True):
        self.calls = []
        self.refresh_works = refresh_works
        self._minted_at = 0.0          # unknown age → always re-mint
        self.expires_in = 7199
        self.sso_login_cookie = "SSO_SESSION=x" if has_sso else ""
        self.auth_step_fingerprint = "{}" if has_sso else ""
        self._on_persist = None

    def refresh(self):
        self.calls.append("refresh")
        if not self.refresh_works:
            raise TbankApiError("invalid_grant", "refresh_token dead")
        return {}

    def silent_relogin(self):
        self.calls.append("silent_relogin")
        return {}


class LevelSession(MobileSession):
    """A session whose reported access level lapses like the real ~11-min window."""

    def __init__(self, levels):
        self.levels = list(levels)      # what successive pings report
        self.pings = 0
        self.refreshes = 0
        self._minted_at = time.time()   # token is fresh — only the LEVEL lapsed
        self.expires_in = 7199
        self._on_persist = None

    def keepalive(self):
        self.pings += 1
        return {"accessLevel": self.levels[min(self.pings - 1, len(self.levels) - 1)]}

    def refresh(self):
        self.refreshes += 1
        return {}


class TokenSession(MobileSession):
    """A session whose re-mint rotates the refresh_token, like the real grant."""

    def __init__(self):
        self.refresh_token = "token-0"
        self._minted_at = 0.0
        self.expires_in = 7199
        self.sso_login_cookie = ""
        self.auth_step_fingerprint = ""
        self._on_persist = None
        self.saved = []

    def refresh(self):
        self.refresh_token = "token-1"      # the bank rotates it on every grant
        self._minted_at = time.time()
        self._persist()
        return {}


def test_session_level():
    s = FakeSession()
    s.ensure_fresh()
    check(s.calls == ["refresh"],
          f"healthy session must re-mint via refresh() alone, got {s.calls}")

    s = FakeSession(refresh_works=False)
    s.ensure_fresh()
    check(s.calls == ["refresh", "silent_relogin"],
          f"dead refresh_token must fall back to silent_relogin, got {s.calls}")

    s = FakeSession(refresh_works=False, has_sso=False)
    try:
        s.ensure_fresh()
        failures.append("no refresh_token and no SSO_SESSION must raise, not pass silently")
    except TbankApiError:
        pass
    print("  grant choice: refresh() first (cheaper), silent_relogin as fallback")


def test_client_session_window():
    """The CLIENT window (~11 min) lapses long before the ~2h token, so a session
    that ensure_fresh considers fine can still be ANONYMOUS."""
    lapsed = LevelSession(["ANONYMOUS", "CLIENT"])
    check(lapsed.ensure_client_session() == "CLIENT",
          "a lapsed session must be re-minted back to CLIENT")
    check(lapsed.refreshes == 1,
          f"exactly one re-mint expected, got {lapsed.refreshes}")

    healthy = LevelSession(["CLIENT"])
    check(healthy.ensure_client_session() == "CLIENT", "healthy session must pass")
    check(healthy.refreshes == 0,
          f"a CLIENT session must NOT be re-minted, got {healthy.refreshes}")

    # Re-minting that does not help must report the truth, not loop.
    stuck = LevelSession(["ANONYMOUS", "ANONYMOUS"])
    check(stuck.ensure_client_session() == "ANONYMOUS",
          "an unrecoverable session must report its real level")
    check(stuck.refreshes == 1, f"must not retry forever, got {stuck.refreshes}")
    print("  client window: lapsed session re-minted, healthy one left alone")


def test_persist_on_remint():
    """A re-mint that never reaches disk burns the refresh_token for the next
    process, which then degrades the session to ANONYMOUS. ensure_fresh must
    persist."""
    s = TokenSession()
    s._on_persist = lambda: s.saved.append(s.refresh_token)
    s.ensure_fresh()
    check(s.saved == ["token-1"],
          f"the rotated refresh_token must be persisted, saved={s.saved}")

    # A second ensure_fresh on a now-fresh session must not re-mint or re-save.
    s.ensure_fresh()
    check(s.saved == ["token-1"], f"fresh session must not re-mint, saved={s.saved}")

    # No hook installed (e.g. a bare client) must not raise.
    bare = TokenSession()
    bare.ensure_fresh()
    check(bare.refresh_token == "token-1", "re-mint must work without a persist hook")
    print("  persistence: rotated token saved once, no-op when already fresh")


def test_nutrition():
    # ВкусВилл (appId 204): every structured field empty, all data in `value`
    vv = MobileSession.nutrition({"meta": {
        "nutritionalValue": {"fat": "", "protein": "", "carbohydrate": "", "energy": "",
                             "value": "белки 3,3 г, жиры 3 г, углеводы 18,4 г; 113,8 ккал"},
        "weight": {"value": 160.0, "unit": "GRM"}}})
    check(vv["protein"] == 3.3 and vv["fat"] == 3.0 and vv["carb"] == 18.4,
          f"free-text БЖУ misparsed: {vv}")
    check(vv["kcal"] == 113.8, f"free-text ккал misparsed: {vv}")
    check(abs(vv["kcal_pack"] - 182.08) < 0.01,
          f"per-package kcal must scale by weight: {vv['kcal_pack']}")

    # Самокат (appId 695): structured fields present
    sm = MobileSession.nutrition({"meta": {
        "nutritionalValue": {"fat": "11.0", "protein": "4.5", "carbohydrate": "47.0",
                             "energy": "300.0 ккал",
                             "value": "Белки 4.5, жиры 11.0, углеводы 47.0, 300.0 ккал"},
        "weight": {"value": 76.0, "unit": "GRM"}}})
    check(sm["kcal"] == 300.0 and sm["protein"] == 4.5,
          f"structured nutrition misparsed: {sm}")

    # A retailer that publishes no carbs at all must report None, never 0 —
    # "not published" and "zero carbs" are different facts.
    cheese = MobileSession.nutrition({"meta": {
        "nutritionalValue": {"fat": "", "protein": "", "carbohydrate": "", "energy": "",
                             "value": "белки 26,8 г; жиры 25,2 г; 334 ккал"},
        "weight": {"value": 200.0, "unit": "GRM"}}})
    check(cheese["carb"] is None, f"missing carbs must stay None, got {cheese['carb']}")
    check(cheese["kcal"] == 334.0, f"kcal misparsed without carbs: {cheese}")

    # No nutrition block at all, and a KG weight (no per-package scaling)
    empty = MobileSession.nutrition({"meta": {"weight": {"value": 1.0, "unit": "KG"}}})
    check(all(empty[k] is None for k in ("protein", "fat", "carb", "kcal", "kcal_pack")),
          f"absent nutrition must be all None, got {empty}")
    print("  nutrition: free-text, structured, partial and absent forms all handled")


def main():
    print("session level + nutrition parsing:")
    test_session_level()
    test_client_session_window()
    test_persist_on_remint()
    test_nutrition()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
