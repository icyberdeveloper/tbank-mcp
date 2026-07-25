"""T-Bank mobile API client — self-bootstrapping, fully headless after login.

login(phone) + confirm_otp(otp) do a real SSO login (no capture needed): they
mint the mobile sessionid + access_token + refresh_token and capture the
long-lived SSO_SESSION cookie. silent_relogin() re-mints the session from
SSO_SESSION + a built-in device fingerprint (no OTP) ~every 2h, producing a
session valid for BOTH reads and the messenger tmsg.

Reads use builtin endpoint shapes (endpoints.py — static API params, no
device/session/account secrets) + the live session. `pay`/`group_pay` are
HMAC-SHA256 `x-api-signature` (key = sessionid). api/id/*.t-bank-app.ru serve a
cert by the Russian Trusted Root CA, which no OS trust store ships; tls.py builds
ca/bundle.pem from the system store plus that root, shipped in ca/roots/ and pinned
by SHA-256. Certificates are never taken from the network.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, parse_qsl

import requests

from .endpoints import BUILTIN_ENDPOINTS

MOBILE_BASE = "https://api.t-bank-app.ru"
ID_BASE = "https://id.t-bank-app.ru"
# Canonical OAuth2 token endpoint for the refresh grant. Used as the dataclass
# default AND normalized again in __post_init__, so a legacy session.json that
# stored an explicit empty "" token_url (the old default) can never make
# refresh() POST to "" (the original MissingSchema('') crash).
DEFAULT_TOKEN_URL = f"{ID_BASE}/auth/token/mobile"
# SBP "pointer type" enum for a phone-number pointer. Verified CONSTANT across all
# phone/SBP transfers in captures.xml (6 different recipients, different bankMemberId,
# always pointerType="8276") — it is NOT the recipient's bank code (that's bankMemberId),
# so it's a fixed protocol constant, analogous to currencyCode "643" for RUB.
SBP_PHONE_POINTER_TYPE = "8276"


def _need_store(app_id: str, point_id: str) -> tuple[str, str]:
    """Client-layer guard mirroring server._store(): grocery store-scope methods
    require explicit app_id/point_id — no silent 578/700 default."""
    if not app_id or not point_id:
        raise TbankApiError("NO_STORE_CONTEXT",
            "app_id/point_id required (from grocery_stores()) — no silent default store.")
    return app_id, point_id
_CA_BUNDLE = os.environ.get(
    "TBANK_CA_BUNDLE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ca", "bundle.pem"),
)
if not os.path.exists(_CA_BUNDLE):
    _CA_BUNDLE = None


def _builtin_fingerprint(device_id: str) -> str:
    """A static, generic iOS device-attributes blob used as the anti-fraud
    fingerprint at auth/step (and at refresh). It is device attributes only
    (timezone, screen, OS version, the device id) — not a secret, not a
    challenge-response. A real device produces a similar blob; a plausible
    generic one is accepted for scoring."""
    import uuid as _uuid
    blob = {
        "identifierForVendor": device_id,
        "tDeviceId": device_id,
        "mobileDeviceOs": "iOS",
        "systemVersion": "17.5.1",
        "appVersion": "7.31.6",
        "bundleId": "com.idamob.tinkoff.android",
        "timeZoneName": "Europe/Moscow",
        "language": "ru",
        "root_flag": "false",
        "jailbreak": "false",
        "emulator": 0,
        "debug": 0,
        "lockedDevice": 1,
        "autologinUsed": False,
        "screenWidth": 390,
        "screenHeight": 844,
        "screenResolution": "1170*2532",
        "screenDpi": 3,
        "systemFontSize": 17,
        "labelFontSize": 17,
        "frontCameraAvailable": True,
        "backCameraAvailable": True,
        "userAgent": "iPhone/iOS(17.5.1)/TCSMB",
        "deviceModel": "iPhone",
        "vendor": "t_ios",
        "platform": "ios",
        "randomId": _uuid.uuid4().hex,
    }
    return json.dumps(blob, ensure_ascii=False, separators=(",", ":"))


class TbankApiError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.result_code = code
        self.message = message


class SessionExpired(TbankApiError):
    """refresh_token / session no longer valid -> re-login (login+confirm_otp)."""


# query/header keys that carry live secrets — substituted fresh at call time.
_LIVE_QUERY = {"sessionid", "wuid"}
_LIVE_HEADERS = {"authorization", "cookie"}

# iOS device OS version used in the mobile User-Agent, format
# `iPhone/iOS(<ver>)/TCSMB/<appVersion>(<build>)`. Build is derived from
# app_version, e.g. 7.31.6 -> 7*1_000_000 + 31*10_000 + 6*1_000 = 7316000.
#
# Read straight off the wire: 812 captured requests to the bank's hosts carry
# `iPhone/iOS(26.5.2)/TCSMB/7.31.6(7316000)` and none carry any other version, and
# the same string appears 27 times inside request BODIES. It used to say 17.5.1,
# copied from FINGERPRINT["systemVersion"] below — so every call announced a device
# OS the app never announces.
#
# FINGERPRINT is deliberately NOT changed to match. It is sent once, at login, the
# current value demonstrably works, and nothing in any capture shows what the server
# does with it — so there is evidence for this constant and none for that one.
_IOS_VERSION = "26.5.2"

# Hosts where the real app sends X-App-Name/X-App-Version/X-Platform (capture-
# verified per-host header profile). ONLY these — everywhere else (the BFF
# api.t-bank-app.ru, lifestyle grocery, id, api-invest, ...) the app sends just
# x-lang. Injecting X-App-* on those hosts diverges from the app and BREAKS the
# grocery cart (lifestyle segments carts by client context → set "OK" but the
# goods land in a different bucket → cart reads empty). Keep this list capture-tight.
_STRICT_XAPP_HOSTS = {
    "social-api.t-bank-app.ru", "api-invest-gw.t-bank-app.ru",
    "myauto.t-bank-app.ru", "polls.tbank.ru",
    # The app sends X-App-Name/Version here too (captures2.xml #44). It was the one
    # host in either capture that does and was missing from this list.
    "cx-evolution-api.t-bank-app.ru",
}


def _count_of(good: dict, default: float = 1.0) -> float:
    """A cart line's quantity, kept NUMERIC.

    Goods sold by weight carry a fractional count — captures2.xml #1005 posts
    {"id":"606","count":0.57} alongside 0.63 and 1.35. Coercing with int() turned
    every such line into 0, and a 0 count is how this API removes a good: rebuilding
    the cart to add one item silently deleted every vegetable, fruit and meat line
    in it, while cart/set answered 200."""
    try:
        return float(good.get("count", default) if good.get("count") is not None else default)
    except (TypeError, ValueError):
        return default


def _count_out(count: float):
    """Send an int when the quantity is whole, a float when it is not — matching the
    app, which posts 2 for two packs and 0.57 for 570 g."""
    return int(count) if float(count) == int(count) else round(float(count), 3)


def _normalize_phone(phone: str) -> str:
    """Normalize a RU mobile number to the SBP `pointer` format ``+7XXXXXXXXXX``
    (the form the real app sends). Accepts +7 / 8 / 7 / bare-9… forms."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]          # 8904… → 7904…
    elif len(d) == 10 and d[:1] == "9":
        d = "7" + d              # 904… → 7904…
    if not (len(d) == 11 and d.startswith("7")):
        raise TbankApiError("INVALID_PHONE", f"not a valid RU mobile number: {phone}")
    return "+" + d


@dataclass
class MobileSession:
    # tokens (rotate on refresh)
    mobile_sessionid: str
    refresh_token: str
    access_token: str = ""
    cipher_key: str = ""
    expires_in: int = 7199
    # auth artifacts (obtained at login, replayed)
    device_id: str = ""
    old_device_id: str = ""
    fingerprint: str = ""           # the static anti-fraud JSON blob
    client_id: str = ""             # from Basic auth (e.g. "gorod-app")
    basic_auth: str = ""            # full "Basic ..." header value
    client_version: str = ""
    vendor: str = ""
    origin: str = ""
    platform: str = ""
    app_name: str = ""
    app_version: str = ""
    connection_type: str = "WiFi"
    ccc: str = "true"
    cpswc: str = "true"
    inache: str = "drivetransitt"  # app routing/feature flag (constant) — sent on every request
    cookie_str: str = ""            # the cookie header to replay on reads/refresh
    sso_login_cookie: str = ""      # the LOGIN (auth_code) cookie set incl. SSO_SESSION (long-lived) — for silent re-login
    auth_step_fingerprint: str = "" # the static fingerprint blob sent at auth/step (silent re-login)
    tmsg_session_id: str = ""       # messenger JWT cookie (tm.t-bank-app.ru)
    token_url: str = DEFAULT_TOKEN_URL
    # read request templates, per endpoint key (verbatim from capture)
    read_templates: dict = field(default_factory=dict)
    # config
    base_url: str = MOBILE_BASE
    proxy: str | None = None
    _http: requests.Session = field(default_factory=requests.Session, repr=False)
    _minted_at: float = 0.0  # persisted to session.json (not just runtime)

    def __post_init__(self) -> None:
        # self-bootstrap defaults: a fresh device_id + a built-in device
        # fingerprint blob (no capture needed). login()/confirm_otp() populate
        # the SSO_SESSION + session from a real phone+OTP login.
        import uuid as _uuid
        # Persistence hook, set by the owner (server._require). Every re-mint
        # rotates the refresh_token, so a re-mint that is not written to disk
        # burns the token for the NEXT process — see _persist().
        self._on_persist = None
        # If _minted_at is 0 (loaded from legacy session without timestamp),
        # don't set it to now — that would make an old token look fresh.
        # Leave it 0 — ensure_fresh will refresh before first use.
        # Per-PROCESS memo for values that are stable for the life of a session and
        # were being re-fetched on every call: the prefill contact id (documents()
        # asked for it twice in one invocation) and the per-store areaId (a full
        # retailers download per add_to_cart, to read one field). A plain attribute,
        # not a dataclass field — _save_session serializes fields, and this must
        # never reach session.json.
        self._memo: dict = {}
        # Device facts for the payment anti-fraud block, from the environment.
        # A plain attribute for the same reason as _memo: it is machine-local
        # configuration, not session state, and must not be written to session.json
        # (nor restored from an old one, which would outlive the machine it
        # described). Unset keys fall back to PAY_DEVICE_DEFAULTS.
        self.device_profile: dict = {
            k: v for k, v in (
                ("device_screen_height", os.environ.get("TBANK_DEVICE_SCREEN_HEIGHT")),
                ("device_screen_width", os.environ.get("TBANK_DEVICE_SCREEN_WIDTH")),
                ("language", os.environ.get("TBANK_DEVICE_LANGUAGE")),
                ("timezone", os.environ.get("TBANK_DEVICE_TIMEZONE")),
            ) if v}
        if not self.device_id:
            self.device_id = str(_uuid.uuid4()).upper()
        if not self.old_device_id:
            self.old_device_id = self.device_id
        if not self.fingerprint:
            self.fingerprint = _builtin_fingerprint(self.device_id)
        if not self.auth_step_fingerprint:
            self.auth_step_fingerprint = _builtin_fingerprint(self.device_id)
        self._http.headers.update({
            "User-Agent": "okhttp/4.12.0",
            "Accept": "application/json",
            "x-lang": "ru",
        })
        if self.proxy:
            self._http.proxies = {"http": self.proxy, "https": self.proxy}
        # Build the CA bundle on startup = system store + the pinned roots in
        # ca/roots/. Cheap and offline (no openssl, no network), so it is safe to
        # do every time and it keeps a fresh clone working: ca/bundle.pem is
        # generated and gitignored, so it does not exist until this runs.
        # The adapter retries once on an SSL failure by rebuilding from the SAME
        # trusted material — it never learns a certificate from the peer.
        _bundle_path = _CA_BUNDLE  # latched at import; may be None on a fresh machine
        try:
            from . import tls as _tls
            _tls.rebuild_bundle()
            self._http.mount("https://", _tls.RobustTLSAdapter())
            _bundle_path = _tls.BUNDLE  # canonical path — now exists (rebuild built it)
        except Exception:
            pass
        # Set verify AFTER rebuild_bundle, re-checked at runtime. The module-level
        # _CA_BUNDLE is evaluated ONCE at import: on a fresh machine where ca/bundle.pem
        # didn't exist yet, it latches to None and the old `if _CA_BUNDLE: verify=...`
        # (run BEFORE rebuild) never set verify → requests fell back to system CAs (no
        # Russian Trusted Root CA) → SSL CERTIFICATE_VERIFY_FAILED. (#latch-bug)
        if _bundle_path and os.path.exists(_bundle_path):
            self._http.verify = _bundle_path
        # Normalize token_url: a legacy session.json may have stored an explicit
        # "" (the old default). An empty value would make refresh() POST to "",
        # so force the canonical default. The dataclass default alone can't
        # override an explicit empty string passed at construction time.
        if not self.token_url:
            self.token_url = DEFAULT_TOKEN_URL
        # DON'T set _minted_at = time.time() here — it would make old tokens
        # look fresh on reload. _minted_at is set by login/refresh/renew,
        # or stays 0 (from legacy session) → ensure_fresh will refresh before use.
        if not self._minted_at:
            self._minted_at = 0  # explicit: 0 means "unknown age → refresh before first use"
        # login state (cid + otp token persisted between login() and confirm_otp())
        self._login_cid: str = ""
        self._login_token: str = ""
        self._login_cookie: str = ""

    # -- headless refresh ----------------------------------------------------

    def _persist(self) -> None:
        """Write the session out after a re-mint, if the owner installed a hook.

        This is not an optimisation — it is required for correctness. `refresh()`
        rotates the refresh_token, so a process that re-mints and exits without
        saving leaves the NEXT process holding a spent token; that one then has to
        fall back to the slower silent_relogin, and if the SSO cookie has also
        lapsed it fails outright and every tool starts answering SESSION EXPIRED.
        """
        hook = getattr(self, "_on_persist", None)
        if hook is None:
            return
        try:
            hook()
        except Exception:
            # Persisting is best-effort: a read-only HOME must not break reads.
            pass

    def _refresh_body(self) -> dict:
        # EXACT fields of the refresh grant (10 fields). client_id is
        # in the Basic header, not the body. No client_assertion, no redirect_uri.
        return {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "device_id": self.device_id,
            "appName": self.app_name,
            "appVersion": self.app_version,
            "origin": self.origin,
            "platform": self.platform,
            "vendor": self.vendor,
            "client_version": self.client_version,
            "fingerprint": self.fingerprint,
        }

    def refresh(self) -> dict:
        """Re-mint the mobile sessionid headlessly (proven to work). Stores the
        rotated refresh_token + new sessionid + access_token."""
        headers = {
            "Authorization": self.basic_auth or self._basic_auth(),
            "Accept": "application/json",
            "X-SSO-No-Adapter": "true",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "okhttp/4.12.0",
            "x-lang": "ru",
        }
        if self.cookie_str:
            headers["Cookie"] = self.cookie_str
        r = self._http.post(self.token_url, data=self._refresh_body(),
                            headers=headers, timeout=30)
        tok = self._unwrap(r)
        if not isinstance(tok, dict) or (
            "access_token" not in tok and "mobile" not in tok
        ):
            err = tok.get("error") if isinstance(tok, dict) else None
            msg = (tok.get("error_description") or tok.get("status")
                   or "(no token in response)") if isinstance(tok, dict) else str(tok)[:200]
            if str(err).lower() in ("invalid_grant", "invalid token"):
                raise SessionExpired(str(err), str(msg))
            raise TbankApiError(str(err) if err else "NOT_A_TOKEN_RESPONSE", str(msg)[:300])
        self.access_token = tok.get("access_token", self.access_token)
        self.refresh_token = tok.get("refresh_token", self.refresh_token)
        self.expires_in = tok.get("expires_in", self.expires_in) or self.expires_in
        mobile = tok.get("mobile") or {}
        self.mobile_sessionid = mobile.get("sessionid", self.mobile_sessionid)
        self.cipher_key = mobile.get("cipher_key", self.cipher_key)
        self._minted_at = time.time()
        self._persist()          # the refresh_token just rotated — never lose it
        return tok

    def _basic_auth(self) -> str:
        return "Basic " + base64.b64encode(f"{self.client_id}:".encode()).decode()

    def ensure_fresh(self, max_age_s: int = 6000) -> None:
        """Re-mint the session before the access_token expires (~2h).

        Prefers refresh() — the refresh_token grant — simply because it is ONE
        request, against silent_relogin's authorize → step → token dance plus a 3 s
        propagation sleep. silent_relogin stays as the fallback for a dead or
        rotated refresh_token.

        Both grants mint an equally privileged sessionid (measured: CLIENT with
        portalSessionExpiresInSeconds 659 vs 656). An earlier version of this
        comment claimed only refresh() yielded CLIENT — that was a misreading of
        ensure_client_session's ~11-minute window, see there.

        This only tracks the ~2h access_token. Tools that need a CLIENT-level
        SESSION must call ensure_client_session() instead — that window is ~11
        minutes and lapses long before the token does.
        _minted_at == 0 means unknown age (legacy session) → always re-mint."""
        if self._minted_at == 0 or time.time() - self._minted_at > min(max_age_s, max(60, self.expires_in - 600)):
            try:
                self.refresh()
            except Exception:
                if not (self.sso_login_cookie and self.auth_step_fingerprint):
                    raise
                self.silent_relogin()

    def ensure_client_session(self) -> str:
        """Guarantee a CLIENT-level sessionid, for the few endpoints that check it.

        The sessionid's CLIENT window is MUCH shorter than the access_token that
        ensure_fresh() tracks: /v1/ping reports `portalSessionExpiresInSeconds`
        ≈ 659 right after a re-mint — about 11 minutes — against the token's ~2h.
        Once it lapses the same sessionid reads back as accessLevel ANONYMOUS /
        userId 1111, and only the handful of session-validating endpoints notice
        (card_credentials, prefill/profile documents, session_status); everything
        else keeps working on the Bearer, which is why the lapse looks like a
        random failure rather than an expiry.

        So: ping, and re-mint if the window has closed. Costs one extra request,
        and only the tools that actually need CLIENT should call it."""
        self.ensure_fresh()
        def level():
            try:
                return (self.keepalive() or {}).get("accessLevel")
            except TbankApiError:
                return None
        current = level()
        if current == "CLIENT":
            return current
        self.refresh()
        return level() or "UNKNOWN"

    # -- reads (template-driven, cookie + Bearer, no signing) -----------------

    def _tpl(self, key: str) -> dict:
        """Resolve a read template: builtin endpoint shape first (capture-free),
        then any capture-loaded template (legacy)."""
        tpl = BUILTIN_ENDPOINTS.get(key) or self.read_templates.get(key)
        if not tpl:
            raise TbankApiError("NO_TEMPLATE", f"no endpoint shape for '{key}'")
        return tpl

    def _mobile_ua(self) -> str:
        """The mobile User-Agent derived from the session (NOT hardcoded):
        ``iPhone/iOS(<ver>)/TCSMB/<appVersion>(<build>)``. The numeric build is
        derived from app_version (7.31.6 -> 7316000); the iOS device version is the
        constant ``_IOS_VERSION``. Returns '' for non-iOS / unknown app_version."""
        if self.platform != "ios" or not self.app_version:
            return ""
        try:
            a, b, c = (int(x) for x in self.app_version.split("."))
            build = a * 1_000_000 + b * 10_000 + c * 1_000
        except ValueError:
            return ""
        return f"iPhone/iOS({_IOS_VERSION})/TCSMB/{self.app_version}({build})"

    def _mobile_headers(self, host_url: str = "") -> dict:
        """Mobile-client headers the real app sends, derived from session attrs.
        ``X-Lang``/``Accept-Language``/``Accept``/mobile ``User-Agent`` are sent on
        basically every API host → injected always. But ``X-App-Name``/``Version``/
        ``Platform`` are sent ONLY on ``_STRICT_XAPP_HOSTS`` (capture-verified
        per-host profile). Injecting them elsewhere diverges from the app and breaks
        the grocery cart on lifestyle. An explicit template header still wins
        (setdefault below)."""
        from urllib.parse import urlparse
        hn = (urlparse(host_url).hostname or host_url or "").lower()
        h: dict[str, str] = {"X-Lang": "ru", "Accept-Language": "ru",
                             "Accept": "application/json"}
        if hn in _STRICT_XAPP_HOSTS:
            if self.app_name:
                h["X-App-Name"] = self.app_name
            if self.app_version:
                h["X-App-Version"] = self.app_version
            if self.platform:
                h["X-Platform"] = self.platform
        ua = self._mobile_ua()
        if ua:
            h["User-Agent"] = ua
        return h

    def _call_read(self, template_key: str, *, overrides: dict | None = None,
                   body: dict | None = None, path_override: str | None = None) -> Any:
        """Replay a read endpoint (builtin shape) with fresh sessionid + Bearer.

        path_override replaces the path (for parameterized endpoints like
        messenger conversations/{id}/messages)."""
        tpl = self._tpl(template_key)
        params = {k: v for k, v in tpl.get("params", {}).items()
                  if k not in _LIVE_QUERY}
        # Most hosts read the mobile sessionid from `sessionid`; the prefill-profile
        # and insurance hosts spell it `sessionId` and reject the lowercase form.
        params[tpl.get("session_param") or "sessionid"] = self.mobile_sessionid
        params["deviceId"] = self.device_id
        params["oldDeviceId"] = self.old_device_id or self.device_id
        params["wuid"] = self.device_id
        # inject the common base params from the session if not in the template
        # (so builtin endpoints with minimal params still send appName/origin/etc.)
        # inache is the app's routing/feature flag (constant "drivetransitt") — the
        # real client sends it on EVERY request; centralizing it here (default in the
        # dataclass) closes the gap for the ~8 templates that had empty params and
        # omitted it (cars, finhealth presets, my_home, payment_shortcuts, ...).
        for k, v in (("appName", self.app_name), ("appVersion", self.app_version),
                     ("origin", self.origin), ("platform", self.platform),
                     ("ccc", self.ccc), ("cpswc", self.cpswc),
                     ("connectionType", self.connection_type),
                     ("vendor", self.vendor), ("client_version", self.client_version),
                     ("inache", self.inache)):
            if v and k not in params:
                params[k] = v
        if overrides:
            params.update(overrides)
        host = tpl.get("host") or self.base_url
        headers = {k: v for k, v in tpl.get("headers", {}).items()
                   if k.lower() not in _LIVE_HEADERS}
        # Inject the mobile-client headers the real app sends on this host:
        # x-lang/Accept-Language/Accept/UA always; X-App-Name/Version/Platform ONLY
        # on _STRICT_XAPP_HOSTS (elsewhere the app sends just x-lang — injecting
        # X-App-* there breaks the lifestyle grocery cart). setdefault ⇒ an explicit
        # template header or Authorization/Cookie below still wins.
        for k, v in self._mobile_headers(host).items():
            headers.setdefault(k, v)
        headers["Authorization"] = "Bearer " + self.access_token
        # messenger (tm.t-bank-app.ru) uses ONLY the tmsgSessionID cookie, minted
        # on demand from the access_token via issueTokenBySSO; other hosts use the
        # SSO/sessionid cookie_str.
        if "tm.t-bank-app.ru" in host:
            self._ensure_tmsg()
            if self.tmsg_session_id:
                headers["Cookie"] = f"tmsgSessionID={self.tmsg_session_id}"
        elif self.cookie_str:
            headers["Cookie"] = self.cookie_str
        path = path_override or tpl["path"]
        url = f"{host.rstrip('/')}/{path.lstrip('/')}"
        method = (tpl.get("method") or "GET").upper()
        if method == "POST":
            post_body = body
            if post_body is None and tpl.get("body"):
                raw = tpl["body"]
                try:
                    post_body = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    post_body = raw
            if tpl.get("form"):
                # A few endpoints (payment_commission) take
                # application/x-www-form-urlencoded, not JSON — posting JSON there
                # returns INVALID_REQUEST_DATA. Dict values are JSON-encoded fields.
                data = {k: (json.dumps(v, ensure_ascii=False)
                            if isinstance(v, (dict, list)) else v)
                        for k, v in (post_body or {}).items()}
                r = self._http.post(url, params=params, data=data, headers=headers, timeout=30)
            else:
                r = self._http.post(url, params=params, json=post_body, headers=headers, timeout=30)
        elif method == "PUT":
            r = self._http.put(url, params=params, headers=headers, timeout=30)
        else:
            r = self._http.get(url, params=params, headers=headers, timeout=30)
        if tpl.get("raw"):
            # A few endpoints answer with bytes, not JSON (payment_receipt_pdf →
            # application/pdf). _unwrap would raise HTTP_200 on the undecodable body.
            r.raise_for_status()
            return r.content
        return self._unwrap(r)

    # ---- signed requests (v\d/(pay|group_pay) — x-api-signature) ----------

    def _sign(self, method: str, path: str, query: str, body: str) -> str:
        """Reproduce the T-Bank x-api-signature (verified against a real capture).

        msg = METHOD + "\\n" + path_tail + ["\\n"+query] + ["\\n"+body]
        path_tail = the path from the v\\d segment onward (e.g. "/v1/pay").
        key = the mobile sessionid. alg = HMAC-SHA256, base64(NO_WRAP).
        """
        m = re.search(r"(/v\d+.*)$", path)
        path_tail = m.group(1) if m else path
        msg = method + "\n" + path_tail
        if query:
            msg += "\n" + query
        if body:
            msg += "\n" + body
        digest = hmac.new(self.mobile_sessionid.encode("utf-8"),
                          msg.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    # The device / anti-fraud / 3-D Secure profile the app sends with EVERY /v1/pay,
    # in BOTH the query string and the form body — the same 11 keys in both, verified
    # against captures.xml #1477 (p2p-anybank) and captures2.xml #595
    # (transfer-inner-third-party). It feeds the fraud decision and the 3DS callback
    # URL, so a payment without it is not the request the bank expects to see for
    # this device. Values match the captured device profile deliberately: the
    # deviceId being replayed is that device's, and a screen size that disagreed with
    # it would be the inconsistency the anti-fraud system looks for.
    # The 3DS / anti-fraud block, sent in BOTH the query and the form on every
    # payment. Two kinds of value live here and they must not be confused:
    #
    #   protocol constants — colorDepth, debug, emulator, jailbreak, javaEnabled,
    #     javaScriptEnabled, notificationUrl. The app sends these verbatim.
    #   device facts — device_screen_height/width, language, timezone. These
    #     described the ONE phone the traffic was captured from: a 1260×2736 screen,
    #     the ru-CY locale (a Cyprus-region device, which also says where its owner
    #     was) and UTC+3.
    #
    # Baking the second group in means every user of this MCP claims to be that
    # phone, in that region, in that timezone — a fingerprint that is both wrong and
    # someone else's. They are overridable per session (TBANK_DEVICE_* below), and
    # the captured values remain the default because a plausible coherent device is
    # better than a blank or a random one, and no capture shows what the gate does
    # with an unfamiliar profile.
    PAY_DEVICE_CONSTANTS = {
        "colorDepth": "24",
        "debug": "0",
        "emulator": "0",
        "jailbreak": "false",
        "javaEnabled": "false",
        "javaScriptEnabled": "true",
        "notificationUrl": "https://api.t-bank-app.ru/v1/3ds",
    }
    PAY_DEVICE_DEFAULTS = {
        "device_screen_height": "2736",
        "device_screen_width": "1260",
        "language": "ru-CY",
        "timezone": "180",
    }

    @property
    def PAY_DEVICE_PROFILE(self) -> dict:
        """The full block: constants + this session's device facts."""
        # getattr, not self.device_profile: __post_init__ sets it, and the test
        # sessions build the object without running it.
        override = getattr(self, "device_profile", None) or {}
        return {**self.PAY_DEVICE_CONSTANTS, **self.PAY_DEVICE_DEFAULTS,
                **{k: str(v) for k, v in override.items() if v}}

    def _call_signed(self, template_key: str, body_str: str,
                     extra_query: dict | None = None) -> Any:
        """POST a signed request (private; only pay_execute/human use)."""
        url, headers, body_str = self._signed_parts(template_key, body_str, extra_query)
        r = self._http.post(url, data=body_str, headers=headers, timeout=30)
        return self._unwrap(r)

    # NOTE: pay / payment_gate_pay / grocery_order_create / checkout_process_order
    # are REAL money-moving operations, exposed as MCP tools per the user's request.
    # They are NOT test-called by the assistant — only invoked deliberately.

    def _signed_parts(self, template_key: str, body_str: str,
                      extra_query: dict | None = None) -> tuple[str, dict, str]:
        """Build the signed POST request parts (url, headers, body)."""
        tpl = self._tpl(template_key)
        if not tpl:
            raise TbankApiError("NO_TEMPLATE", f"no endpoint shape for '{template_key}'")
        params = {k: v for k, v in tpl.get("params", {}).items() if k not in _LIVE_QUERY}
        params["sessionid"] = self.mobile_sessionid
        # The real /v1/pay carries deviceId + oldDeviceId and NO wuid (wuid is the
        # web/portal identifier; it appears on www.tbank.ru calls, not on this one).
        # The signature covers the query string, so what goes here is what gets signed.
        params["deviceId"] = self.device_id
        params["oldDeviceId"] = self.old_device_id or self.device_id
        params.update(self.PAY_DEVICE_PROFILE)
        if extra_query:
            params.update(extra_query)
        query = urllib.parse.urlencode(params, safe="%/,")
        host = tpl.get("host") or self.base_url
        path = tpl["path"]
        url = f"{host.rstrip('/')}/{path.lstrip('/')}?{query}"
        sig = self._sign("POST", path, query, body_str)
        headers = {k: v for k, v in tpl.get("headers", {}).items()
                   if k.lower() not in _LIVE_HEADERS}
        # The signature covers METHOD + path + query + body, never the headers, so
        # these are free to match the app — and they must. The query declares
        # platform=ios and a whole iOS device profile, while the User-Agent came
        # from the requests.Session default and said `okhttp/4.12.0`: an Android
        # HTTP client posting an iPhone's anti-fraud block. The captured native
        # /v1/pay sends the mobile UA together with X-Lang and this Accept.
        for k, v in self._mobile_headers(host).items():
            headers.setdefault(k, v)
        # _mobile_headers defaults Accept to application/json, which is right for the
        # read endpoints. The captured pay asks for html — overridden, not defaulted.
        headers["Accept"] = ("text/html,application/xhtml+xml,application/xml;"
                             "q=0.9,*/*;q=0.8")
        headers["Authorization"] = "Bearer " + self.access_token
        headers["x-api-signature"] = sig
        if self.cookie_str:
            headers["Cookie"] = self.cookie_str
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8;"
        return url, headers, body_str

    def pay(self, body: str | None = None) -> Any:
        """POST v1/pay — REAL signed payment (moves money). body = raw form-encoded
        payParameters=...; None = replay the default pay body. Signed with
        x-api-signature (HMAC-SHA256, key=sessionid).

        The device/anti-fraud fields are prepended here rather than by every caller,
        so no payment can go out without them."""
        tpl = self._tpl("v1_pay")
        if not tpl:
            raise TbankApiError("NO_TEMPLATE", "no v1/pay in capture")
        body_str = body if body is not None else (tpl.get("body") or "")
        if "payParameters=" in body_str and "notificationUrl=" not in body_str:
            prefix = urllib.parse.urlencode(self.PAY_DEVICE_PROFILE)
            body_str = prefix + "&" + body_str
        return self._call_signed("v1_pay", body_str)

    def _tmsg_expired(self) -> bool:
        """Decode the tmsg JWT exp; True if missing or within 60s of expiry."""
        if not self.tmsg_session_id:
            return True
        try:
            payload = self.tmsg_session_id.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
            return exp <= time.time() + 60
        except Exception:
            return True

    def messenger_issue_token(self) -> str:
        """Mint a fresh tmsgSessionID JWT from the current access_token.
        POST /app/bank/api/v1/session/issueTokenBySSO {ssoToken: <access_token>}
        -> result.jwt. Stores + returns it. Lets the messenger work headlessly
        (re-mint whenever the tmsg nears its ~1h expiry)."""
        url = "https://tm.t-bank-app.ru/app/bank/api/v1/session/issueTokenBySSO"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "okhttp/4.12.0", "x-lang": "ru"}
        if self.cookie_str:
            headers["Cookie"] = self.cookie_str
        r = self._http.post(url, json={"ssoToken": self.access_token},
                           headers=headers, timeout=30)
        data = self._unwrap(r)
        jwt = ""
        if isinstance(data, dict):
            jwt = data.get("jwt", "") if "jwt" in data else (data.get("result", {}) or {}).get("jwt", "")
        if jwt:
            self.tmsg_session_id = jwt
        return jwt

    def _ensure_tmsg(self) -> None:
        """Ensure a valid tmsg for messenger. If missing/expired, do a silent
        gorod-app re-login (SSO_SESSION + fingerprint, NO OTP) to get a fresh
        SSO-valid access_token, then mint the tmsg via issueTokenBySSO."""
        if not self._tmsg_expired():
            return
        # mint tmsg from the current access_token; if it fails (refresh token is
        # SSO-invalid), do a silent re-login to get a fresh auth_code access_token.
        try:
            self.messenger_issue_token()
            if self.tmsg_session_id:
                return
        except TbankApiError:
            pass
        # silent re-login -> fresh SSO-valid access_token, then mint tmsg
        self.silent_relogin()
        self.messenger_issue_token()

    def silent_relogin(self) -> dict:
        """Silent gorod-app re-login (NO OTP): uses the long-lived SSO_SESSION +
        the static fingerprint blob. auth/authorize(gorod-app, SSO_SESSION) ->
        auth/step(fingerprint) -> code -> /auth/token/mobile (auth_code) ->
        fresh SSO-valid access_token + mobile sessionid + refresh_token.

        This is how the phone re-mints a login access_token for the messenger
        tmsg. Persistent as long as the SSO_SESSION cookie is alive (days/weeks).
        """
        if not self.sso_login_cookie or not self.auth_step_fingerprint:
            raise TbankApiError("NO_SSO_SESSION", "no SSO_SESSION cookie / fingerprint "
                                "— call login(phone)+confirm_otp(otp) first to get SSO_SESSION.")
        claims = ('{"id_token":{"given_name":{"essential":true},'
                  '"phone_number":{"essential":true},"picture":{"essential":true},'
                  '"api_sso_id":{"essential":true}}}')
        state = str(__import__("uuid").uuid4()).upper()
        params = {"claims": claims, "client_version": self.client_version,
                  "state": state, "redirect_uri": "mobile://", "response_type": "code",
                  "cpswc": "true", "device_id": self.device_id, "client_id": "gorod-app",
                  "ccc": "true", "response_mode": "json", "display": "json",
                  "vendor": self.vendor}
        # use a session with the SSO cookies in the jar (so SSO_CONVERSATION_CSRF
        # from the authorize Set-Cookie auto-attaches to the step call)
        base = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0"}
        r = self._http.get("https://id.t-bank-app.ru/auth/authorize", params=params,
                          headers={**base, "Cookie": self.sso_login_cookie}, timeout=30)
        rj = self._unwrap(r)
        cid = rj.get("cid")
        if not cid:
            raise TbankApiError("NO_CID", f"authorize: {json.dumps(rj)[:200]}")
        # copy SSO cookies into the session jar so the step auto-sends CSRF
        for c in self.sso_login_cookie.split(";"):
            c = c.strip()
            if "=" in c:
                k, v = c.split("=", 1)
                self._http.cookies.set(k, v, domain="id.t-bank-app.ru")
        body = "step=fingerprint&fingerprint=" + urllib.parse.quote(
            self.auth_step_fingerprint, safe="")
        r2 = self._http.post(
            f"https://id.t-bank-app.ru/auth/step?cid={cid}&ccc=true&cpswc=true",
            data=body,
            headers={**base, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30)
        r2j = self._unwrap(r2)
        code = r2j.get("code")
        if not code:
            raise TbankApiError("NO_CODE", f"step: {json.dumps(r2j)[:200]}")
        tb = (f"device_id={self.device_id}&client_version={self.client_version}"
              f"&grant_type=authorization_code&appVersion={self.app_version or '7.31.6'}"
              f"&origin={self.origin}&vendor={self.vendor}&code={code}"
              f"&platform={self.platform}&appName={self.app_name}"
              f"&redirect_uri=mobile%3A%2F%2F")
        r3 = self._http.post(
            "https://id.t-bank-app.ru/auth/token/mobile?ccc=true&cpswc=true",
            data=tb,
            headers={**base, "Authorization": self._basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded",
                     "X-SSO-No-Adapter": "true", "Cookie": self.sso_login_cookie},
            timeout=30)
        tok = self._unwrap(r3)
        if not isinstance(tok, dict) or "access_token" not in tok:
            raise TbankApiError("NO_TOKEN", f"token/mobile: {str(tok)[:200]}")
        # silent_relogin gives a session valid for BOTH reads and the messenger
        # tmsg (unlike the refresh_grant session, which is read-only). Update the
        # unified session: access_token + mobile.sessionid + refresh_token.
        self.access_token = tok["access_token"]
        self.refresh_token = tok.get("refresh_token", self.refresh_token)
        self.expires_in = tok.get("expires_in", self.expires_in) or self.expires_in
        mobile = tok.get("mobile") or {}
        self.mobile_sessionid = mobile.get("sessionid", self.mobile_sessionid)
        self.cipher_key = mobile.get("cipher_key", self.cipher_key)
        self._minted_at = time.time()
        self.tmsg_session_id = ""  # force tmsg re-mint with the fresh access_token
        # the freshly-minted session needs ~3s to propagate before mobile reads
        # accept it (else INSUFFICIENT_PRIVILEGES). silent_relogin runs ~every 2h,
        # so this sleep is negligible.
        time.sleep(3.0)
        self._persist()          # same reason as in refresh(): the token rotated
        return tok

    # -- login (self-bootstrap: phone + SMS OTP -> SSO_SESSION + session) ----

    def login(self, phone: str) -> str:
        """Start a real SSO login (no capture needed). POSTs the phone number to
        auth/step; the bank sends an SMS OTP. Returns a message asking to call
        confirm_otp(otp) with the code. Stores cid + the otp-step token.
        phone = full international form, e.g. +79991234567."""
        base = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0"}
        claims = ('{"id_token":{"given_name":{"essential":true},'
                  '"phone_number":{"essential":true},"picture":{"essential":true},'
                  '"api_sso_id":{"essential":true}}}')
        state = str(__import__("uuid").uuid4()).upper()
        params = {"claims": claims, "client_version": self.client_version,
                  "state": state, "redirect_uri": "mobile://", "response_type": "code",
                  "cpswc": "true", "device_id": self.device_id, "client_id": "gorod-app",
                  "ccc": "true", "response_mode": "json", "display": "json",
                  "vendor": self.vendor}
        # authorize (no SSO_SESSION) — the jar captures SSO_CONVERSATION_CSRF
        r = self._http.get(f"{ID_BASE}/auth/authorize", params=params, headers=base, timeout=30)
        rj = self._unwrap(r)
        cid = rj.get("cid")
        if not cid:
            raise TbankApiError("NO_CID", f"authorize: {json.dumps(rj)[:200]}")
        self._login_cid = cid
        # step=phone — triggers the SMS OTP
        body = ("step=phone&phone=" + urllib.parse.quote(phone, safe="")
                + "&fingerprint=" + urllib.parse.quote(self.auth_step_fingerprint, safe=""))
        r2 = self._http.post(f"{ID_BASE}/auth/step?cid={cid}&ccc=true&cpswc=true",
                            data=body, headers={**base, "Content-Type": "application/x-www-form-urlencoded"},
                            timeout=30)
        r2j = self._unwrap(r2)
        self._login_token = r2j.get("token", "") or ""
        next_step = r2j.get("step", "")
        if next_step == "otp":
            return ("SMS OTP sent. Call confirm_otp(<code>) with the code from the SMS.")
        if next_step == "password":
            return ("Password step. Call confirm_password(<your account password>).")
        if next_step == "pin":
            return ("PIN step. Call confirm_pin(<your app PIN>).")
        # unknown step — return it so the user can pick the right confirm_*
        return (f"Next step: '{next_step}'. Call confirm_otp / confirm_password / "
                f"confirm_pin accordingly. (cid stored.) resp: {json.dumps(r2j)[:200]}")

    def confirm_step(self, kind: str, value: str) -> dict:
        """Finish the login: submit the OTP (kind='otp') or PIN (kind='pin') or
        password (kind='password'), get the auth code, exchange it at
        auth/token/mobile -> session. Captures SSO_SESSION. Chains the token
        from each step's response to the next."""
        if not self._login_cid:
            raise TbankApiError("NO_LOGIN", "call login(phone) first")
        base = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0"}
        body = f"step={kind}&{kind}=" + urllib.parse.quote(str(value), safe="")
        if self._login_token:
            body += f"&token={self._login_token}"
        r = self._http.post(f"{ID_BASE}/auth/step?cid={self._login_cid}&ccc=true&cpswc=true",
                           data=body, headers={**base, "Content-Type": "application/x-www-form-urlencoded"},
                           timeout=30)
        # parse the response directly (auth/step doesn't use resultCode envelope)
        try:
            rj = r.json()
        except Exception:
            raise TbankApiError("HTTP_" + str(r.status_code), r.text[:300])
        # chain the token from this response to the next step
        new_token = rj.get("token", "")
        if new_token:
            self._login_token = new_token
        # if error in the response, raise with full detail
        if rj.get("error"):
            raise TbankApiError(str(rj.get("error")),
                                json.dumps(rj, ensure_ascii=False)[:300])
        code = rj.get("code")
        if not code:
            # not finished yet — another step (e.g. otp -> password)
            raise TbankApiError("NO_CODE", json.dumps(rj, ensure_ascii=False)[:300])
        # exchange the code for the mobile session
        tb = (f"device_id={self.device_id}&client_version={self.client_version}"
              f"&grant_type=authorization_code&appVersion={self.app_version or '7.31.6'}"
              f"&origin={self.origin}&vendor={self.vendor}&code={code}"
              f"&platform={self.platform}&appName={self.app_name}"
              f"&redirect_uri=mobile%3A%2F%2F")
        r3 = self._http.post(f"{ID_BASE}/auth/token/mobile?ccc=true&cpswc=true",
                           data=tb, headers={**base, "Authorization": self._basic_auth(),
                                             "Content-Type": "application/x-www-form-urlencoded",
                                             "X-SSO-No-Adapter": "true"}, timeout=30)
        tok = self._unwrap(r3)
        if not isinstance(tok, dict) or "access_token" not in tok:
            raise TbankApiError("NO_TOKEN", f"token/mobile: {str(tok)[:200]}")
        self.access_token = tok["access_token"]
        self.refresh_token = tok.get("refresh_token", self.refresh_token)
        self.expires_in = tok.get("expires_in", self.expires_in) or self.expires_in
        mobile = tok.get("mobile") or {}
        self.mobile_sessionid = mobile.get("sessionid", self.mobile_sessionid)
        self.cipher_key = mobile.get("cipher_key", self.cipher_key)
        self._minted_at = time.time()
        # capture the SSO_SESSION cookie from the jar (set during login) for
        # silent re-login + messenger.
        self.sso_login_cookie = "; ".join(
            f"{c.name}={c.value}" for c in self._http.cookies
            if c.domain and "t-bank-app.ru" in c.domain)
        self.cookie_str = self.sso_login_cookie
        self.tmsg_session_id = ""
        self._login_cid = self._login_token = ""
        time.sleep(3.0)  # propagation, like silent_relogin
        return tok

    
    def confirm_otp(self, otp: str) -> dict:
        """Submit the SMS OTP (alias for confirm_step('otp', otp))."""
        return self.confirm_step("otp", otp)

# ---- messenger / support chat (tm.t-bank-app.ru) — Bearer+cookie, no sig ----

    def messenger_conversations(self, archived: bool = False, offset: int = 0) -> list[dict]:
        ov = {"use_is_archived": str(archived).lower(), "offset": str(offset)}
        return self._as_list(self._call_read("messenger_base",
                       path_override="/app/bank/messenger/conversations/mobile", overrides=ov))

    def messenger_messages(self, conversation_id: str, direction: str = "before",
                           message_id: str = "") -> list[dict]:
        ov = {"direction": direction}
        if message_id:
            ov["messageId"] = message_id
        return self._as_list(self._call_read("messenger_base", overrides=ov,
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages"))

    def messenger_hints(self, conversation_id: str) -> list[dict]:
        return self._as_list(self._call_read("messenger_base",
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/hints"))

    def messenger_faq(self, conversation_id: str) -> list[dict]:
        return self._as_list(self._call_read("messenger_base",
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/faq"))

    def messenger_unread(self) -> dict:
        """Conversations with unread messages. Uses its OWN template, not
        messenger_base: this path content-negotiates and 406s on the generic
        `application/json` header. Returns {groups, conversationIds, screens}."""
        data = self._call_read("messenger_unread")
        return data if isinstance(data, dict) else {}

    def messenger_send_message(self, conversation_id: str, body: dict | None = None) -> dict:
        """POST a message to a conversation (WRITE). Replays the request body or override."""
        return self._call_read("messenger_send", body=body,
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages")

    def messenger_mark_read(self, conversation_id: str, message_id: str) -> Any:
        return self._call_read("messenger_base",
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages/{message_id}/markRead")

    # ---- extended read tools (Tier-1, template-driven, unsigned) ----------

    def operations_histogram(self, account_id: str | None, start_ms: int, end_ms: int,
                             period: str = "day", group_by: str = "category") -> dict:
        # the app scopes this one by "accounts" (plural) and always sends timeZone
        ov = {"start": str(start_ms), "end": str(end_ms), "period": period,
              "groupBy": group_by, "config": "allNotInner", "timeZone": "+03:00"}
        if account_id:
            ov["accounts"] = account_id
        return self._call_read("operations_histogram", overrides=ov)

    def list_regular_payments(self, activity_types: str = "payment") -> list[dict]:
        d = self._call_read("list_regular_payments", overrides={"activityTypes": activity_types})
        return self._as_list(d)

    def active_loans(self) -> list[dict]:
        return self._as_list(self._call_read("active_loans"))

    def credit_accounts_list(self) -> list[dict]:
        return self._as_list(self._call_read("credit_accounts_list"))

    def credit_account_payments(self, account: str) -> list[dict]:
        return self._as_list(self._call_read("payments_credit_accounts", overrides={"accounts": account}))

    def cashback_summary(self, loyalty_id: str, codes: str = "lifestyle,targetCashback") -> list[dict]:
        return self._as_list(self._call_read("bonuses_aggregated",
                                             overrides={"loyaltyId": loyalty_id, "codes": codes}))

    def invest_accounts(self) -> list[dict]:
        return self._as_list(self._call_read("investbox_accounts"))

    def invest_portfolio(self, broker_account_id: str, start: str, end: str,
                          currency: str = "RUB") -> dict:
        return self._call_read("ca_portfolio_statistics",
                                overrides={"brokerAccountId": broker_account_id,
                                           "from": start, "to": end, "currency": currency})

    def invest_operations(self, broker_account_id: str, operation_type: str = "",
                           limit: int = 50) -> list[dict]:
        ov = {"brokerAccountId": broker_account_id, "limit": str(limit)}
        if operation_type:
            ov["operationType"] = operation_type
        return self._as_list(self._call_read("ca_operations", overrides=ov))

    def invest_securities(self, broker_account_id: str) -> list[dict]:
        return self._as_list(self._call_read("purchased_securities",
                                             overrides={"brokerAccountId": broker_account_id}))

    def session_status(self) -> dict:
        # www.tbank.ru/api/common/v1/session_status (web gateway) — confirmed WORKING
        # with the mobile session: the SSO cookie in cookie_str authenticates it.
        # (Audit flagged www.tbank.ru as a 'different realm', but live use proves it
        # returns accessLevel/SSO TTL/userId — do NOT reroute or 'fix'.)
        return self._call_read("session_status")

    def _call_userinfo(self) -> dict:
        """GET /userinfo/userinfo (id.t-bank-app.ru) — the gorod-app SSO IdP OIDC
        UserInfo. Capture-verified shape: client_id=<gorod-app> + ccc/cpswc +
        Authorization: Bearer + the t-bank-app.ru jar cookies (which carry the SSO
        tracking set). The generic _call_read OMITS client_id and adds mobile-BFF
        params, which the IdP rejects with HTTP 401 (research-workflow confirmed
        across 3 captures). Reuses self.client_id (= gorod-app), no new literal."""
        r = self._http.get(
            "https://id.t-bank-app.ru/userinfo/userinfo",
            params={"ccc": "true", "cpswc": "true", "client_id": self.client_id or "gorod-app"},
            headers={"Accept": "*/*",
                     "Authorization": "Bearer " + self.access_token,
                     **({"Cookie": self.cookie_str} if self.cookie_str else {})},
            timeout=30)
        return self._unwrap(r)

    def keepalive(self) -> Any:
        """POST v1/ping — keep the mobile session alive (unsigned)."""
        return self._call_read("ping")

    def unread_count(self) -> dict:
        return self._call_read("notification_count")

    def profile_lite(self) -> dict:
        return self._call_read("profile_own_lite")

    def shopping_favorites(self) -> list[dict]:
        return self._as_list(self._call_read("shopping_favorites"))

    def shopping_cart(self) -> list[dict]:
        return self._as_list(self._call_read("shopping_cart"))

    # ---- grocery (Город) shopping + checkout + payment (cookie/Bearer, no sig) ----

    def grocery_cart_get(self, app_id: str = "", point_id: str = "") -> dict:
        """Read the cart for a specific store. Only appId scopes the cart — the
        real app never sends pointId on this endpoint (pointId lives in the
        cart/set body under delivery). point_id is accepted for call-site symmetry."""
        ov = {"appId": app_id} if app_id else None
        return self._call_read("grocery_cart_get", overrides=ov)

    def grocery_cart_set(self, body: dict | None = None,
                         app_id: str = "", point_id: str = "") -> dict:
        """Set the grocery cart (POST). With body=None this CLEARS the store's cart
        (goods: []). The delivery block — address with full details, plus areaId for
        retailers that require it — is resolved by _grocery_delivery."""
        if body is None:
            body = {"goods": [], "cartSetMode": "SINGLE_CART",
                    "delivery": self._grocery_delivery(app_id, point_id)}
        return self._call_read("grocery_cart_set", body=body,
                               overrides={"appId": app_id} if app_id else None)

    def grocery_cart_check(self) -> dict:
        return self._call_read("grocery_cart_check")

    def grocery_order_get(self, order_id: str = "", app_id: str = "") -> dict:
        """Look up a grocery order by orderId (GET /api/grocery/order). For
        reconciliation after an UNKNOWN checkout (#10)."""
        ov = {k: v for k, v in (("orderId", order_id), ("appId", app_id)) if v}
        return self._call_read("grocery_order_get", overrides=ov or None)

    def grocery_order_create(self, body: dict | None = None) -> dict:
        """Create a grocery order (POST, replays the request body or override)."""
        return self._call_read("grocery_order_create", body=body)

    def grocery_deliveries(self, body: dict | None = None) -> list[dict]:
        return self._as_list(self._call_read("grocery_deliveries", body=body))

    def grocery_retailers(self) -> list[dict]:
        return self._as_list(self._call_read("grocery_retailers"))

    def grocery_catalog(self) -> list[dict]:
        return self._as_list(self._call_read("grocery_catalog"))

    def grocery_categories(self) -> list[dict]:
        return self._as_list(self._call_read("grocery_categories"))

    def grocery_unseen_orders(self) -> dict:
        return self._call_read("grocery_unseen_orders")

    # (grocery_client_info is defined further down, next to _grocery_delivery — an
    # identical stub used to sit here and was silently shadowed by it, so an edit to
    # this one changed nothing.)

    # ---- shopping cart-building (browse products + fill the cart) ----

    def shopping_change_qty(self, body: dict | None = None) -> dict:
        """POST carts/change-items-quantity — add/remove/change qty of a cart
        item (the granular cart-fill op). Replays the request body or override."""
        return self._call_read("shopping_change_qty", body=body)

    def shopping_cart_detail(self, body: dict | None = None) -> dict:
        """POST carts/cart-detail-info — full cart detail (items, prices, delivery)."""
        return self._call_read("shopping_cart_detail", body=body)

    def store_products(self) -> list[dict]:
        """Browse/search store products (to find items to add to the cart)."""
        return self._as_list(self._call_read("store_products"))

    def store_product(self, product_id: str) -> dict:
        """Product details (PDP) by id — use before adding to cart."""
        return self._call_read("store_product",
            path_override=f"/mybank/api/shopping/mobile/v1/product/{product_id}")

    def store_categories(self) -> list[dict]:
        """Store categories (browse the catalog)."""
        return self._as_list(self._call_read("store_categories"))

    def sphere_categories(self) -> list[dict]:
        """Sphere (Город) categories."""
        return self._as_list(self._call_read("sphere_categories"))

    def grocery_goods(self, category_id: str = "",
                      app_id: str = "", point_id: str = "",
                      page: int = 1) -> list[dict]:
        """Grocery goods (Город catalog items). Pass category_id to browse a
        category, page for pagination."""
        return self._as_list(self._call_read("grocery_goods", overrides={
            "appId": app_id, "pointId": point_id, "categoryId": category_id,
            "page": str(page), "count": "50"}))

    def grocery_popular(self) -> list[dict]:
        """Popular grocery items."""
        return self._as_list(self._call_read("grocery_popular"))

    def payment_methods(self) -> list[dict]:
        """Available payment methods for a checkout."""
        return self._as_list(self._call_read("payment_methods"))

    def payment_gate_pay(self, body: dict | None = None) -> dict:
        """Pay for a marketplace order (cookie-only, NO signature). MONEY OP.
        Replays the payment body or uses the override. Default
        dry_run=False — pass a fresh body (orderId/amount/account) for a new pay."""
        return self._call_read("payment_gate_pay", body=body)

    def payment_commission(self, body: dict | None = None) -> dict:
        """Commission preview (POST /v1/payment_commission), no money moved.

        The real app sends application/x-www-form-urlencoded with a single
        ``payParameters`` field holding the JSON-encoded parameters — NOT a JSON
        body (capture item 1469). Posting JSON returns INVALID_REQUEST_DATA.
        ``isTransferStatus``/``isUrgentTransfer`` are string "false" in every
        captured request; default them so callers need not know."""
        p = (body or {}).get("payParameters") or body or {}
        p = dict(p)
        p.setdefault("isTransferStatus", "false")
        p.setdefault("isUrgentTransfer", "false")
        return self._call_read("payment_commission", body={"payParameters": p})

    def checkout_process_order(self, body: dict | None = None) -> dict:
        return self._call_read("checkout_process_order", body=body)

    # ---- named read tools (each a real, described MCP tool) ----

    def get_requisites(self) -> list[dict]:
        """Account requisites (account number / corr / bank — for transfers)."""
        return self._as_list(self._call_read("get_requisites"))

    def subscription_all(self) -> list[dict]:
        """All subscriptions (recurring services)."""
        return self._as_list(self._call_read("subscription_all"))

    def subscription_all_bills(self) -> list[dict]:
        """All subscription bills."""
        return self._as_list(self._call_read("subscription_all_bills"))

    def subscription_bills(self) -> list[dict]:
        """Subscription bills."""
        return self._as_list(self._call_read("subscription_bills"))

    def account_details(self) -> dict:
        """Account details (full)."""
        return self._call_read("account_details")

    def full_debt_amount(self) -> dict:
        """Full debt amount (credit)."""
        return self._call_read("full_debt_amount")

    def payment_templates(self) -> list[dict]:
        """Saved payment templates (favorite recipients)."""
        return self._as_list(self._call_read("payment_templates"))

    def invoices_to_pay(self) -> list[dict]:
        """Invoices/money requests to pay."""
        return self._as_list(self._call_read("invoices_to_pay"))

    def get_invoices(self) -> list[dict]:
        """Get invoices."""
        return self._as_list(self._call_read("get_invoices"))

    def my_invoices(self) -> list[dict]:
        """My invoices (money requests issued)."""
        return self._as_list(self._call_read("my_invoices"))

    def available_cards(self) -> list[dict]:
        """Available cards (issuable)."""
        return self._as_list(self._call_read("available_cards"))

    def statements(self) -> list[dict]:
        """Account statements."""
        return self._as_list(self._call_read("statements"))

    def statement_exist(self) -> dict:
        """Whether a statement exists."""
        return self._call_read("statement_exist")

    def credit_payment_schedule(self) -> list[dict]:
        """Credit payment schedule."""
        return self._as_list(self._call_read("credit_payment_schedule"))

    def credit_rating(self) -> dict:
        """Credit rating."""
        return self._call_read("credit_rating")

    def credit_recommendations(self) -> list[dict]:
        """Credit recommendations."""
        return self._as_list(self._call_read("credit_recommendations"))

    def manager_info(self) -> dict:
        """Personal manager info."""
        return self._call_read("manager_info")

    def bank_info(self) -> dict:
        """Bank info (branches/contacts)."""
        return self._call_read("bank_info")

    def autopayments(self) -> list[dict]:
        """Autopayments."""
        return self._as_list(self._call_read("autopayments"))

    def sbp_subscriptions(self) -> list[dict]:
        """SBP (SBP-by-Phone) subscriptions."""
        return self._as_list(self._call_read("sbp_subscriptions"))

    def providers_compatible(self) -> list[dict]:
        """Compatible payment providers (for bill payments)."""
        return self._as_list(self._call_read("providers_compatible"))

    def client_offers(self) -> list[dict]:
        """Client offers."""
        return self._as_list(self._call_read("client_offers"))

    def gift_for_recipient(self) -> list[dict]:
        """Gifts for recipient."""
        return self._as_list(self._call_read("gift_for_recipient"))

    def finhealth_balance_total(self) -> dict:
        """Finhealth: total balance metric."""
        return self._call_read("finhealth_balance_total")

    def finhealth_balance_turnover(self) -> dict:
        """Finhealth: balance turnover metric."""
        return self._call_read("finhealth_balance_turnover")

    def finhealth_invest_turnover(self) -> dict:
        """Finhealth: invest turnover metric."""
        return self._call_read("finhealth_invest_turnover")

    def p2p_countries(self) -> list[dict]:
        """P2P transfer countries."""
        return self._as_list(self._call_read("p2p_countries"))

    def services(self) -> list[dict]:
        """Connected services."""
        return self._as_list(self._call_read("services"))

    def invest_pension_profile(self) -> dict:
        """Invest pension profile."""
        return self._call_read("invest_pension_profile")

    def investbox_offers(self) -> list[dict]:
        """InvestBox deposit offers."""
        return self._as_list(self._call_read("investbox_offers"))

    def investbox_product_yield(self) -> list[dict]:
        """InvestBox product yield."""
        return self._as_list(self._call_read("investbox_product_yield"))

    def broker_margin(self) -> dict:
        """Broker margin attributes."""
        return self._call_read("broker_margin")

    def invest_offers(self) -> list[dict]:
        """Invest offers (virtual stock)."""
        return self._as_list(self._call_read("invest_offers"))

    def bundles_all(self) -> list[dict]:
        """All bundles (premium service bundles)."""
        return self._as_list(self._call_read("bundles_all"))

    # ---- audit-found extras ----

    def detected_merchant_subscriptions(self) -> list[dict]:
        """Recurring third-party billing detected from card statements (merchant, price, next payment date)."""
        return self._as_list(self._call_read("detected_merchant_subscriptions"))

    def user_profile(self) -> dict:
        """Canonical bank identity profile (name, phone, email, siebel_id)."""
        return self._call_read("user_profile")

    def broker_portfolio_accounts(self) -> list[dict]:
        """Brokerage accounts with total amount + expected yield (P&L)."""
        return self._as_list(self._call_read("broker_portfolio_accounts"))

    def my_homes(self) -> list[dict]:
        """Linked homes (Мой дом) with address, price, utility providers."""
        return self._as_list(self._call_read("my_homes"))

    def my_home_activities(self) -> list[dict]:
        """Per-home utility bills to pay and subscription bills."""
        return self._as_list(self._call_read("my_home_activities"))

    def my_cars(self) -> list[dict]:
        """Saved vehicles (make, model, reg number, VIN)."""
        return self._as_list(self._call_read("my_cars"))

    def payment_shortcuts(self) -> list[dict]:
        """Payment shortcuts (favorite recipients / autopay deeplinks)."""
        return self._as_list(self._call_read("payment_shortcuts"))

    def unread_support_requests(self) -> list[dict]:
        """csc.tbank.ru support/tracker realm — uses a WEBSESS/support session, NOT the
        mobile session. The mobile Bearer+cookie auth here will likely be REJECTED
        (401/403). Not wired to any MCP tool / get_data section today (orphan).
        Capture-verify the support-session auth before exposing it."""
        return self._as_list(self._call_read("unread_support_requests"))

    def resolve_payment_qr(self, body: dict | None = None) -> dict:
        """Resolve a QR payload to a payment provider (no money moved)."""
        return self._call_read("resolve_payment_qr", body=body)

    def merchant_brand(self) -> list[dict]:
        """Merchant brand metadata (logos/colors) by merchant id."""
        return self._as_list(self._call_read("merchant_brand"))

    def money_request_public_page(self) -> list[dict]:
        """Public share link for a money request."""
        return self._as_list(self._call_read("money_request_public_page"))

    def finhealth_account_presets(self) -> dict:
        """Finhealth tracked-account preset (which accounts are in metrics)."""
        return self._call_read("finhealth_account_presets")

    def get_ip(self) -> dict:
        """Egress IP of the session (connectivity/geo sanity)."""
        return self._call_read("get_ip")

    def push_unread_count(self) -> dict:
        """Unread push-notification count."""
        return self._call_read("push_unread_count")

    def business_account_info(self) -> list[dict]:
        """Business account info."""
        return self._as_list(self._call_read("business_account_info"))

    def shared_resources_owned(self) -> list[dict]:
        """Shared resources I own."""
        return self._as_list(self._call_read("shared_resources_owned"))

    def shared_resources(self) -> list[dict]:
        """Shared resources (accessed)."""
        return self._as_list(self._call_read("shared_resources"))

    def contact_list(self) -> list[dict]:
        """Contact list (saved recipients)."""
        return self._as_list(self._call_read("contact_list"))

    def providers_groups(self) -> list[dict]:
        """Payment provider groups (for bill payments)."""
        return self._as_list(self._call_read("providers_groups"))

    def providers_compatible_page(self) -> list[dict]:
        """Compatible payment providers (paged)."""
        return self._as_list(self._call_read("providers_compatible_page"))

    def atm_withdrawal_qrs(self) -> list[dict]:
        """ATM withdrawal QRs."""
        return self._as_list(self._call_read("atm_withdrawal_qrs"))

    def check_rating(self) -> dict:
        """Check rating."""
        return self._call_read("check_rating")

    def credit_collection_info(self) -> dict:
        """Credit collection info."""
        return self._call_read("credit_collection_info")

    def active_account_options(self) -> list[dict]:
        """Active account options."""
        return self._as_list(self._call_read("active_account_options"))

    def appointment_deliveries(self) -> list[dict]:
        """Active appointment deliveries."""
        return self._as_list(self._call_read("appointment_deliveries"))

    def grocery_stores(self) -> list[dict]:
        """List available grocery stores for the delivery address."""
        base = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0",
                "Authorization": "Bearer " + self.access_token}
        if self.cookie_str:
            base["Cookie"] = self.cookie_str
        # The delivery address, through the shared accessor rather than a second
        # hand-rolled request to the same URL: a cold-start add_to_cart resolves the
        # address here AND in _grocery_delivery, which was two identical calls.
        addrs = []
        try:
            addrs = ((self.grocery_client_info().get("deliveryInfo") or {})
                     .get("addresses") or [])
        except TbankApiError:
            pass
        if not addrs:
            raise TbankApiError("NO_DELIVERY_ADDRESS",
                "У аккаунта нет сохранённого адреса доставки. Добавь адрес в приложении "
                "Т-Банка (Город), затем вызови grocery_stores() снова.")
        addr = addrs[0].get("value", "")
        coords = addrs[0].get("coordinates") or {}
        if not coords.get("latitude") or not coords.get("longitude"):
            raise TbankApiError("NO_DELIVERY_ADDRESS",
                "Сохранённый адрес без координат — обнови адрес в приложении Т-Банка (Город).")
        lat = str(coords.get("latitude"))
        lon = str(coords.get("longitude"))
        # retailers needs ALL these params (from capture)
        params = {"appName": self.app_name, "appVersion": self.app_version,
                  "platform": self.platform, "origin": self.origin,
                  "deviceId": self.device_id, "oldDeviceId": self.old_device_id,
                  "sessionid": self.mobile_sessionid, "ccc": self.ccc,
                  "cpswc": self.cpswc, "connectionType": self.connection_type,
                  "inache": self.inache, "includeMultipleRetailers": "true",
                  "includeClosedRetailers": "false", "address": addr,
                  "latitude": lat, "longitude": lon,
                  "tabsBlockType": "RECOMMENDATION", "v": "2"}
        r = self._http.get("https://lifestyle.t-bank-app.ru/api/grocery/retailers",
                          params=params, headers=base, timeout=30)
        stores = []
        try:
            for cat in r.json().get("payload", {}).get("categories", []):
                for ret in cat.get("retailers", []):
                    app_id = str(ret.get("appId", ""))
                    name = (ret.get("info", {}) or {}).get("name", "")
                    point_id = str((ret.get("delivery", {}) or {}).get("pointId", ""))
                    min_sum = (ret.get("delivery", {}) or {}).get("minOrderSum", 0)
                    nearest = (ret.get("delivery", {}) or {}).get("nearestTime", {})
                    cashback = (ret.get("info", {}) or {}).get("cashback", {})
                    # areaId identifies the retailer's delivery zone for this address.
                    # Retailers that have one (ВкусВилл, Лента) REQUIRE it in the
                    # cart/set body — omitting it makes the backend reject the cart.
                    # This is the only endpoint that ever returns it.
                    area_id = str((ret.get("delivery", {}) or {}).get("areaId", "") or "")
                    if app_id and name:
                        stores.append({"appId": app_id, "name": name, "areaId": area_id,
                                       "pointId": point_id, "minOrderSum": min_sum,
                                       "deliveryTime": f"{nearest.get('from','')}-{nearest.get('to','')} min" if nearest.get("to") else "",
                                       "deliveryPrice": nearest.get("price", 0),
                                       "cashback": cashback.get("value", ""),
                                       "category": cat.get("name", "")})
        except Exception:
            pass
        # dedupe by (appId, pointId) — the retailers list can repeat a store (#14)
        seen = set()
        uniq = []
        for st in stores:
            key = (st.get("appId"), st.get("pointId"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(st)
        return uniq

    def _resolve_custom_ordered_id(self, app_id: str, point_id: str) -> str:
        """Discover the per-store 'previously ordered' (Вы заказывали) category id.
        The id is store-suffixed (e.g. custom_ordered_<store>) and NOT
        constructible client-side — sibling custom ids carry random/date suffixes.
        The server lists it in GET /api/grocery/catalog?appId=&pointId= (the real
        app calls it with appId/pointId) under blocks[type=='Categories'].list,
        as the item whose id starts with 'custom_ordered' (label 'Вы заказывали').
        Returns '' when the store has no order history (block absent) → caller
        falls back to global search. Verified for appId=578; degrades safely
        for other stores."""
        data = self._call_read("grocery_catalog", overrides={"appId": app_id, "pointId": point_id})
        blocks = []
        if isinstance(data, dict):
            blocks = data.get("blocks") or []
            if not blocks and isinstance(data.get("payload"), dict):
                blocks = data["payload"].get("blocks") or []
        fallback = ""
        for block in blocks:
            items = block.get("list") if isinstance(block, dict) else None
            if not isinstance(items, list):
                continue
            for cat in items:
                if not isinstance(cat, dict):
                    continue
                cid = cat.get("id")
                if isinstance(cid, str) and cid.startswith("custom_ordered"):
                    # prefer the labeled 'Вы заказывали' match; else keep first custom_ordered
                    if "заказывал" in str(cat.get("name") or "").lower():
                        return cid
                    if not fallback:
                        fallback = cid
        return fallback

    def grocery_plan_order(self, ingredients: list[str], store_app_id: str = "",
                           store_point_id: str = "") -> dict:
        """Plan a grocery order. #12: custom_ordered is loaded ONCE per store and
        matched in memory (was re-read per ingredient → N+1, up to ~28 requests for 7
        ingredients). Ingredient queries are normalized (qty/units/stopwords stripped:
        'картофель 1 кг' → 'картофель'). Missing ingredients fall back to global search
        run in parallel (concurrency-capped).

        Returns {store, items, total_sum, missing, substitutions}."""
        import re as _re
        app_id, point_id = _need_store(store_app_id, store_point_id)
        plan = {"store": app_id, "items": [], "total_sum": 0,
                "missing": [], "substitutions": []}

        def norm(s: str) -> str:
            s = s.lower().strip().replace("ё", "е")
            # strip "1 кг", "2 шт", "100 г", "0.5 л" — quantities + common units
            s = _re.sub(r"\b\d+([.,]\d+)?\s*(кг|г|гр|грамм|л|мл|литр|шт|упак|пачк|банк|дол|зубч)?\b", " ", s)
            s = _re.sub(r"\b(сырой|сырая|сырого|свежий|свежая|очищен|вкусн)\S*", " ", s)
            return _re.sub(r"\s+", " ", s).strip()

        # 1. load custom_ordered ONCE (up to 3 pages), match all ingredients in memory
        _custom = None

        def custom_once():
            nonlocal _custom
            if _custom is not None:
                return _custom
            _custom = []
            # Discover the per-store 'previously ordered' category id dynamically
            # (no hardcoded store id — works for any store that has order history;
            # stores without it return '' and we fall through to global search).
            category_id = self._resolve_custom_ordered_id(app_id, point_id)
            if not category_id:
                return _custom
            for page in range(1, 4):
                items = self._as_list(self._call_read("grocery_goods", overrides={
                    "appId": app_id, "pointId": point_id,
                    "categoryId": category_id,
                    "page": str(page), "count": "50"}))
                if not items:
                    break
                _custom.extend(g for g in items if isinstance(g, dict))
            return _custom

        missing = []
        for ingredient in ingredients:
            q = norm(ingredient)
            found = None
            # collect every previously-ordered match, then score them the same way
            # as search hits — taking the first match picked whatever the history
            # happened to list first, tiny packs and dried forms included.
            cands = []
            for g in custom_once():
                name = (g.get("name") or "").lower().replace("ё", "е")
                if q and q in name:
                    price = g.get("price", {})
                    weight = g.get("weight", {})
                    cands.append({
                        "id": str(g.get("id", "")), "name": g.get("name", ""),
                        "price": price.get("value", 0) if isinstance(price, dict) else 0,
                        "weight": (f"{weight.get('value','')} {weight.get('unit','')}".strip()
                                   if isinstance(weight, dict) else ""),
                        "likely_raw": name.startswith(q)})
            best = self._pick_candidate(cands, q)
            if best:
                found = {**best, "source": "custom_ordered", "query": q}
            if found:
                plan["items"].append(found)
                plan["total_sum"] += found.get("price", 0) or 0
            else:
                missing.append((ingredient, q))

        # 2. global search for the rest — in parallel (concurrency-capped), #12
        if missing:
            queries = [q for _, q in missing if q]
            hits = self._parallel_search(queries, app_id, point_id)
            for ingredient, q in missing:
                g = hits.get(q)
                if g:
                    found = {"id": g.get("id", ""), "name": g.get("name", ""),
                             "price": g.get("price", 0), "source": "search", "query": q,
                             "weight": g.get("weight", ""),
                             "likely_raw": g.get("likely_raw", False)}
                    plan["items"].append(found)
                    plan["total_sum"] += found.get("price", 0) or 0
                else:
                    plan["missing"].append(ingredient)
        return plan

    # Forms that are the ingredient in name only — a recipe asking for "чеснок"
    # does not want ground dried garlic. Only penalized when the ingredient itself
    # is not a spice (see _pick_candidate).
    _NOT_THE_FRESH_THING = ("сушен", "молот", "приправа", "смесь", "концентрат",
                            "экстракт", "ароматизат", "в горшочке", "семена")
    _SPICE_QUERIES = ("приправ", "перец", "специ", "паприк", "куркум", "зира",
                      "кориц", "лавров", "базилик", "орегано", "хмели")
    # single-serving packs (30 g of sour cream, 10 g of dried garlic) satisfy the
    # text match but not the recipe
    _MIN_SANE_GRAMS = 50.0

    @staticmethod
    def _grams(item: dict) -> float:
        """Package weight in grams, 0 when unknown. weight is like '100 GRM'."""
        raw = (item.get("weight") or "").strip()
        if not raw:
            return 0.0
        parts = raw.split()
        try:
            val = float(parts[0])
        except (ValueError, IndexError):
            return 0.0
        unit = (parts[1] if len(parts) > 1 else "").upper()
        if unit in ("KGRM", "KG", "KGM", "LT", "L"):
            return val * 1000.0
        return val

    @staticmethod
    def _qualifier_stems(full_query: str, used_query: str) -> list[str]:
        """Stems of the words dropped when falling back to a looser query.

        Falling back from "яйца куриные" to "яйца" throws away the part that says
        WHICH eggs, and the loose query happily matches "Яйца перепелиные копченые".
        Keep the dropped words so scoring can still prefer a name that mentions them."""
        full = set((full_query or "").lower().replace("ё", "е").split())
        used = set((used_query or "").lower().replace("ё", "е").split())
        return [w[:5] for w in (full - used) if len(w) >= 4]

    def _pick_candidate(self, results: list[dict], query: str,
                        qualifiers: list[str] | None = None) -> dict | None:
        """Choose the best search hit for an ingredient.

        The planner used to take results[0], and grocery_search sorts by
        (likely_raw, price) — so it always picked the CHEAPEST raw-looking hit.
        That is how "чеснок" became 10 g of dried ground garlic (52₽, beating fresh
        at 118₽) and "сметана" became a 30 g single-serving cup (55₽). Score instead:
        prefer a real, sanely-sized raw ingredient, and only then prefer cheap."""
        if not results:
            return None
        q = (query or "").lower().replace("ё", "е")
        want_spice = any(w in q for w in self._SPICE_QUERIES)

        def score(it: dict) -> tuple:
            name = (it.get("name") or "").lower().replace("ё", "е")
            grams = self._grams(it)
            # spices are legitimately dried and sold in 20 g jars — neither penalty
            # applies when the ingredient itself is a spice
            wrong_form = (not want_spice
                          and any(w in name for w in self._NOT_THE_FRESH_THING))
            too_small = (not want_spice) and 0.0 < grams < self._MIN_SANE_GRAMS
            price = it.get("price")
            price = price if isinstance(price, (int, float)) else 10 ** 6
            # a dropped qualifier ("куриные", "докторская") outranks everything:
            # the right product in the wrong size beats the wrong product
            missed_qualifier = bool(qualifiers) and not any(s in name for s in qualifiers)
            # lower is better, field order = priority
            return (missed_qualifier, wrong_form, too_small,
                    not it.get("likely_raw", False), not name.startswith(q), price)

        return min(results, key=score)

    def _search_best(self, query: str, app_id: str, point_id: str) -> dict | None:
        """Search an ingredient, loosening the query until something sane matches.

        Accepts a loose-query hit only if it still honours the words that were
        dropped; otherwise it keeps looking and falls back to the best seen."""
        fallback = None
        for variant in self._query_variants(query):
            try:
                r = self.grocery_search(variant, app_id=app_id, point_id=point_id)
            except Exception:
                continue
            quals = self._qualifier_stems(query, variant)
            best = self._pick_candidate(r, variant, qualifiers=quals)
            if not best:
                continue
            name = (best.get("name") or "").lower().replace("ё", "е")
            if not quals or any(s in name for s in quals):
                return best
            fallback = fallback or best
        return fallback

    @staticmethod
    def _query_variants(q: str) -> list[str]:
        """Progressively looser queries. The catalog search matches on a literal
        substring of the product name, so a multi-word or inflected request finds
        nothing: "колбаса докторская" misses "Колбаса вареная ... Докторская", and
        "яйца куриные" misses "Яйцо куриное". Fall back to the head noun, then to
        its stem so a plural still matches the singular."""
        out = [q]
        head = q.split()[0] if q.split() else q
        if head != q:
            out.append(head)
        # "яйца" -> "яйц" matches "Яйцо куриное"; keep >=3 chars so the stem stays
        # specific enough not to match arbitrary products
        if len(head) >= 4:
            out.append(head[:-1])
        if len(head) >= 6:
            out.append(head[:-2])
        seen, uniq = set(), []
        for v in out:
            if v and v not in seen:
                seen.add(v)
                uniq.append(v)
        return uniq

    def _parallel_search(self, queries: list[str], app_id: str, point_id: str,
                         max_workers: int = 4) -> dict:
        """Run global grocery searches in parallel (concurrency-capped). Returns
        {normalized_query: best_hit_dict}. #12"""
        from concurrent.futures import ThreadPoolExecutor
        out: dict[str, dict] = {}
        if not queries:
            return out

        def one(q):
            try:
                return q, self._search_best(q, app_id, point_id)
            except Exception:
                return q, None

        workers = max(1, min(max_workers, len(queries)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for q, hit in ex.map(one, queries):
                if hit:
                    out[q] = hit
        return out

    @staticmethod
    def _as_list(d: Any) -> list[dict]:
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            # grocery API: payload = {"list": [...]} → _unwrap returns {"list": [...]}
            if "list" in d and isinstance(d["list"], list):
                return d["list"]
            pl = d.get("payload")
            if isinstance(pl, list):
                return pl
            if isinstance(pl, dict):
                return [pl]
            if "payload" in d:
                return [d["payload"]]
            return [d]
        return []

    def list_accounts(self) -> list[dict]:
        data = self._call_read("accounts_light")
        if isinstance(data, dict):
            return data.get("payload") or data.get("accounts") or [data]
        return data

    def list_operations(self, account_id: str | None, start_ms: int, end_ms: int) -> list[dict]:
        """Operations for a period, filtered to one account.

        ``isSuspicious`` is a per-operation FIELD, not a filter flag to set: passing
        ``isSuspicious=true`` narrows the result to fraud-flagged operations, which is
        normally none — the capture has one such request (item 105) returning an empty
        list while every request without it returns 273-440 operations. Sending it
        unconditionally made this tool always answer "no operations".

        The real app also does not scope /v1/operations by account (it fetches all and
        filters client-side on the operation's ``account`` field) — so do the same."""
        ov = {"start": str(start_ms), "end": str(end_ms)}
        data = self._call_read("operations", overrides=ov)
        if isinstance(data, dict):
            pl = data.get("payload")
            ops = pl if isinstance(pl, list) else ([pl] if pl else [])
        else:
            ops = data if isinstance(data, list) else []
        if account_id:
            ops = [o for o in ops
                   if isinstance(o, dict) and str(o.get("account", "")) == str(account_id)]
        return ops

    @staticmethod
    def _histogram_side(side: dict | None) -> tuple[float, dict[str, float]]:
        """Sum one side of an operations_histogram payload by category.

        The shape is a TREE, not a list: {summary:{value}, intervals:[{summary,
        aggregated:[{groupBy, amount:{value}, category:{id,name}}], start, end}]}
        — one interval per `period` (31 of them for a 30-day daily request), each
        holding that day's categories. Iterating the side itself yields the two
        dict KEYS, which is what the previous version did: every entry failed the
        isinstance(dict) test, so the tool reported Total 0 and no categories on
        a payload whose summary was 3.87M RUB (captures.xml #52).
        """
        if not isinstance(side, dict):
            return 0.0, {}
        by_cat: dict[str, float] = {}
        for iv in side.get("intervals") or []:
            if not isinstance(iv, dict):
                continue
            for a in iv.get("aggregated") or []:
                if not isinstance(a, dict):
                    continue
                name = ((a.get("category") or {}).get("name")
                        or a.get("groupBy") or a.get("groupByKey") or "?")
                amt = a.get("amount") or {}
                try:
                    val = abs(float(amt.get("value") if isinstance(amt, dict) else amt))
                except (TypeError, ValueError):
                    continue
                by_cat[name] = by_cat.get(name, 0.0) + val
        # The bank's own total is authoritative; summing the tree is the fallback
        # (they agree to the kopeck on the capture, but a partial page would not).
        summary = side.get("summary") or {}
        try:
            total = abs(float(summary.get("value")))
        except (TypeError, ValueError):
            total = sum(by_cat.values())
        return total, by_cat

    def spending_categories(self, account_id: str | None, start_ms: int, end_ms: int) -> dict:
        """operations_histogram?groupBy=category, flattened to per-category totals."""
        ov = {"start": str(start_ms), "end": str(end_ms), "groupBy": "category",
              "period": "day", "config": "allNotInner", "timeZone": "+03:00"}
        if account_id:
            ov["accounts"] = account_id
        data = self._call_read("operations_histogram", overrides=ov)
        payload = data.get("payload", data) if isinstance(data, dict) else {}
        total, by_cat = self._histogram_side(payload.get("spending"))
        earned, _ = self._histogram_side(payload.get("earning"))
        cats = [{"category": name, "amount": round(amount, 2),
                 "share_pct": round(amount / total * 100, 2) if total else 0.0}
                for name, amount in by_cat.items()]
        currency = (((payload.get("spending") or {}).get("summary") or {})
                    .get("currency") or {}).get("name") or "RUB"
        return {
            "period": {"start_ms": start_ms, "end_ms": end_ms},
            "total_spent": round(total, 2), "currency": currency,
            "categories": sorted(cats, key=lambda x: x["amount"], reverse=True),
            "total_earned": round(earned, 2),
        }

    # -- low-level envelope --------------------------------------------------

    def _unwrap(self, resp: requests.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise TbankApiError("HTTP_" + str(resp.status_code), resp.text[:500])
        if isinstance(data, dict):
            code = data.get("resultCode") or data.get("error") or ""
            if code and code not in ("OK", "0", "success", ""):
                msg = data.get("errorMessage") or data.get("error_description") or data.get("plainMessage") or ""
                lc = str(code)
                if lc in _SESSION_EXPIRED or "session" in lc.lower() or "authoriz" in lc.lower() or lc == "invalid_grant":
                    raise SessionExpired(lc, str(msg))
                raise TbankApiError(lc, str(msg))
            # The lifestyle/Город envelope signals failure with HTTP 200 +
            # {"status":"Error","payload":{"message":..,"code":..,"blame":..}} — it uses
            # neither resultCode nor error, so the check above misses it and the ERROR
            # payload gets returned as a success value. That is how a rejected
            # grocery cart/set surfaced as `OK: goodsSum=?`: the caller read goodsSum
            # off an error body. "Ok"/"ok"/"OK" and "Error" are the only status values
            # across all 447 enveloped responses in the capture.
            st = data.get("status")
            if isinstance(st, str) and st.lower() == "error":
                err = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                ec = str(err.get("code") or "Error")
                em = str(err.get("message") or err.get("plainMessage") or "")
                if ec in _SESSION_EXPIRED or "session" in ec.lower() or "authoriz" in ec.lower():
                    raise SessionExpired(ec, em)
                raise TbankApiError(ec, em)
            # unwrap envelope: payload (mobile API) or result (messenger)
            if "payload" in data:
                return data["payload"]
            if "result" in data:
                return data["result"]
            return data
        return data

    # ---- HIGH-LEVEL ENCAPSULATED TOOLS (for the agent) ----

    # Smart grocery search: category selection + prepared-food filter
    _GROCERY_CATEGORIES = {
        "свекл": "ОФ", "капуст": "ОФ", "картоф": "ОФ", "морков": "ОФ",
        "лук": "ОФ", "чеснок": "ОФ", "огурц": "ОФ", "помидор": "ОФ",
        "яблок": "ОФ", "банан": "ОФ", "зелён": "ОФ", "салат": "ОФ",
        "говядин": "МК", "свинин": "МК", "куриц": "МК", "колбас": "МК",
        "мяс": "МК", "фарш": "МК", "сосиск": "МК", "сардель": "МК",
        "молок": "МЛ", "сыр": "МЛ", "твор": "МЛ", "сметан": "МЛ",
        "йогурт": "МЛ", "масл": "МЛ", "яйц": "МЛ", "кефир": "МЛ",
        "томатн": "БК", "мук": "БК", "сахар": "БК", "соль": "БК",
        "круп": "БК", "рис": "БК", "греч": "БК", "макарон": "БК",
        "вермиш": "БК", "хлеб": "БК", "уксус": "БК", "перец": "БК",
        "вод": "ВН", "напит": "ВН", "сок": "ВН", "чай": "КЧ",
        "кофе": "КЧ", "морож": "ЗМ",
    }
    _PREPARED_FOOD_WORDS = (
        "готов", "салат", "боул", "зразы", "пельмен", "голубцы", "винегрет",
        "бифштекс", "сельд", "котлет", "суп", "соус", "пюре", "тушен",
        "бульон", "бутерброд", "ролл", "бургер", "пицц", "шаурм", "воки",
        "жарен", "варен", "запекан", "запеч", "гриль", "маринов",
        "нарезка", "ассорти", "боул", "паназиат", "ризотт", "паэл",
        "рагу", "жюльен", "тартар", "карпаччо", "чипс", "снек",
    )

    # `screen` is a strict server-side enum, not a free-form hint: anything else
    # answers 400 Bad Request (probed ~35 plausible names live — main, search,
    # cinema, concert_main, feed … all rejected).
    #   services   — widest: cinema, concert, concerthall, theatre, exhibition,
    #                spectacle, movie, movie_collection
    #   afisha     — the same entertainment set, slightly narrower
    #   movie_main — movies only
    #   grocery    — store catalog (needs applicationId + pointId)
    SEARCH_SCREENS = ("services", "afisha", "movie_main", "grocery")

    def _search_params(self, screen: str, **extra) -> dict:
        """Common query string for the search host."""
        params = {
            "screen": screen, "appName": self.app_name,
            "appVersion": self.app_version, "platform": self.platform,
            "origin": self.origin, "deviceId": self.device_id,
            "oldDeviceId": self.old_device_id, "ccc": self.ccc,
            "cpswc": self.cpswc, "connectionType": self.connection_type,
            "inache": self.inache,
        }
        params.update({k: v for k, v in extra.items() if v not in (None, "")})
        return params

    def _search_post(self, params: dict, body: dict) -> dict:
        r = self._http.post("https://search.t-bank-app.ru/search/fulltext",
                            params=params, json=body,
                            headers={"Accept": "application/json",
                                     "User-Agent": "okhttp/4.12.0",
                                     "Authorization": "Bearer " + self.access_token},
                            timeout=30)
        try:
            return r.json().get("payload") or {}
        except ValueError:
            return {}

    def app_search(self, text: str, screen: str = "services",
                   limit: int = 20) -> list[dict]:
        """Full-text search across an app section. Returns the raw hits
        (objectType + objectSource) — the caller decides how to render them.

        The body is minimal on purpose: the real app also sends a `toggles` map of
        ~12 feature flags, but the endpoint answers identically without it
        (verified live), so there is nothing to keep in sync."""
        if screen not in self.SEARCH_SCREENS:
            raise TbankApiError("BAD_SEARCH_SCREEN",
                f"unknown screen {screen!r}; valid: {', '.join(self.SEARCH_SCREENS)}")
        payload = self._search_post(
            self._search_params(screen),
            {"text": text, "maxObjectsCount": max(1, limit), "screenContext": {}})
        hits = payload.get("sortedByScoreObjects") or []
        return [h for h in hits if isinstance(h, dict)][:limit]

    def grocery_search(self, query: str, app_id: str = "", point_id: str = "") -> list[dict]:
        """Global grocery search via search/fulltext — searches the ENTIRE store
        catalog (not just one category). Uses inStockFilter (only available
        items). Filters out prepared foods. Returns: id, name, price, weight,
        store, imageUrl. query = e.g. "свёкла"."""
        _need_store(app_id, point_id)
        q = query.lower().strip().replace("ё", "е")
        # POST search/fulltext (global search across the store)
        base = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0"}
        search_body = {
            "searchTypes": ["grocery_goods", "grocery_categories"],
            "filters": [{"name": "inStockFilter", "type": "grocery_goods",
                         "mode": "always", "value": True}],
            "maxObjectsCount": 30,
            "sortTypes": [{"type": "grocery_goods", "name": "default"}],
            "text": query.replace("ё", "е"),
        }
        params = self._search_params("grocery", context="api",
                                     applicationId=app_id, pointId=point_id)
        r = self._http.post("https://search.t-bank-app.ru/search/fulltext",
                           params=params, json=search_body,
                           headers={**base, "Authorization": "Bearer " + self.access_token},
                           timeout=30)
        try:
            data = r.json()
        except Exception:
            return []
        hits = data.get("payload", {}).get("sortedByScoreObjects", [])
        results = []
        for hit in hits:
            if hit.get("objectType") != "grocery_goods":
                continue
            src = hit.get("objectSource", {})
            if not src:
                continue
            name = src.get("name") or ""
            name_norm = name.lower().replace("ё", "е")
            # must match the query
            if q not in name_norm:
                continue
            # no filter — classify: is this likely a raw ingredient?
            prep_words = ("с ", "соус", "маринован", "квашен", "солен", "тушен",
                           "салат", "суп", "пюре", "запекан", "жарен", "варен",
                           "голубцы", "винегрет", "бифштекс", "котлет", "боул",
                           "пельмен", "рагу", "бутерброд", "нарезка", "зразы",
                           "соусом", "с сыром", "с чесноком", "с яблоком",
                           "с майонез", "от бренд", "шефа", "паст", "пудинг")
            starts = name_norm.startswith(q)
            has_prep = any(pw in name_norm for pw in prep_words)
            likely_raw = starts and not has_prep
            price = src.get("price", {})
            pv = price.get("value", "?") if isinstance(price, dict) else price
            weight = src.get("weight", {})
            wv = weight.get("value", "") if isinstance(weight, dict) else ""
            wu = weight.get("unit", "") if isinstance(weight, dict) else ""
            results.append({
                "id": str(src.get("goodForeignId", src.get("id", ""))),
                "name": name, "price": pv,
                "weight": f"{wv} {wu}".strip(),
                "unit": wu,
                "inStock": True,  # inStockFilter is applied to the search
                "likely_raw": likely_raw,
                "appId": str(app_id),
                "pointId": str(point_id),
                "store_app_id": str(src.get("applicationId", app_id)),
                "imageUrl": src.get("imageUrl", ""),
            })
            if len(results) >= 10:
                break
        # sort: likely_raw first, then by price
        results.sort(key=lambda r: (not r.get("likely_raw", False), r.get("price", 999) if isinstance(r.get("price"), (int, float)) else 999))
        return results

    # How long one client/info answer is reused. Short on purpose: unlike areaId,
    # the delivery address CAN change — the user edits it in the app — so this is a
    # burst cache, not a session cache.
    CLIENT_INFO_TTL = 60.0

    def grocery_client_info(self) -> dict:
        """GET /api/grocery/client/info — the account's grocery profile. Carries
        payload.deliveryInfo.{address,deliveryType,comment}: the saved delivery block
        the app uses to seed a cart in a store the user has never ordered from.

        Memoised for CLIENT_INFO_TTL seconds because a single cold-start
        grocery_add_to_cart asked for it twice: once through _grocery_delivery to
        seed the address, then again inside grocery_stores() while resolving areaId.
        Same request, same answer, back to back."""
        memo = getattr(self, "_memo", None)
        if memo is not None:
            at, cached = memo.get("client_info", (0.0, None))
            if cached is not None and time.time() - at < self.CLIENT_INFO_TTL:
                return cached
        info = self._call_read("grocery_client_info")
        info = info if isinstance(info, dict) else {}
        if memo is not None and info:
            memo["client_info"] = (time.time(), info)
        return info

    def _grocery_delivery(self, app_id: str, point_id: str, cart: dict | None = None) -> dict:
        """Build the ``delivery`` block that cart/set requires for this store.

        Two things bit us here, both capture-verified:

        * The address cannot come from the store's own cart alone. For a store the
          user has never used there IS no cart, so the address resolves to ``{}`` and
          the cart/set is rejected — which means no cart is ever created, so the next
          attempt finds no cart either. A permanent deadlock, not a transient miss.
          The app seeds from client/info instead; we prefer the store's own cart (it
          may carry a store-specific address) and fall back to client/info.
        * ``areaId`` is per-retailer and REQUIRED by the retailers that publish one
          (ВкусВилл appId=204, Лента appId=246); Азбука Вкуса (578) has none and its
          real cart/set bodies omit the key entirely. Only the retailers list returns
          it, so resolve it from there and omit the key when the store has none.
        """
        delivery: dict = {}
        try:
            if cart is None:
                cart = self._call_read("grocery_cart_get", overrides={"appId": app_id})
            if isinstance(cart, dict):
                inner = cart.get("cart") if isinstance(cart.get("cart"), dict) else cart
                delivery = inner.get("delivery") or {}
        except TbankApiError:
            delivery = {}
        addr = delivery.get("address") or {}
        comment = delivery.get("comment", "")
        # the cart GET spells it deliveryToDoor; cart/set expects deliveryType
        dtype = delivery.get("deliveryType") or delivery.get("deliveryToDoor") or ""
        if not addr:
            di = self.grocery_client_info().get("deliveryInfo") or {}
            addr = di.get("address") or {}
            dtype = dtype or di.get("deliveryType") or ""
            comment = comment or di.get("comment", "")
        if not addr:
            raise TbankApiError("NO_DELIVERY_ADDRESS",
                "У аккаунта нет сохранённого адреса доставки. Добавь адрес в приложении "
                "Т-Банка (Город), затем повтори.")
        # All 14 captured cart/set bodies carry address.details.streetWithType, but no
        # GET we read returns it — the app fills it in client-side. In every captured
        # address the street name already carries its type (both "<name> проезд" and
        # "улица <name>" forms occur), so street is the value to copy.
        details = addr.get("details")
        if isinstance(details, dict) and details.get("street") and not details.get("streetWithType"):
            addr = dict(addr)
            addr["details"] = {**details, "streetWithType": details["street"]}
        out = {"isExpress": bool(delivery.get("isExpress", False)),
               "comment": comment or "", "pointId": str(point_id),
               "deliveryType": dtype or "IN_PERSON", "address": addr}
        # areaId is a property of the retailer + point, not of the cart, so it does
        # not change between calls. Resolving it meant downloading the whole
        # retailers catalogue on EVERY add_to_cart just to read one field.
        memo = getattr(self, "_memo", None)
        memo_key = f"areaId:{app_id}:{point_id}"
        if memo is not None and memo_key in memo:
            area_id = memo[memo_key]
        else:
            area_id = ""
            found = False
            for st in self.grocery_stores():
                if str(st.get("appId")) == str(app_id) and str(st.get("pointId")) == str(point_id):
                    area_id = str(st.get("areaId") or "")
                    found = True
                    break
            # Only a real answer is memoised. A miss means the catalogue did not list
            # this store on this call — a transient read, a store not yet chosen —
            # and caching "" would drop areaId from every later cart write for the
            # whole process. ВкусВилл needs it: without areaId cart/set answers 200
            # and saves nothing, so the failure would be silent and permanent.
            if memo is not None and found:
                memo[memo_key] = area_id
        if area_id:
            out["areaId"] = area_id
        return out

    def grocery_add_to_cart(self, items: list[dict], app_id: str = "", point_id: str = "") -> dict:
        """Add items to cart. items = [{"id": "123", "count": 1}, ...].
        Resolves the delivery block (address + areaId) and merges with what is
        already in the cart."""
        _need_store(app_id, point_id)
        try:
            cart = self.grocery_cart_get(app_id=app_id, point_id=point_id)
        except TbankApiError:
            cart = {}
        delivery = self._grocery_delivery(app_id, point_id, cart=cart)
        # cart/set REPLACES the whole cart — every captured body resends the full
        # goods list (item [369] posts 6 goods, [375] posts 5 after a removal). Posting
        # only the new items would silently drop everything added earlier, so merge.
        merged: dict[str, float] = {}
        order: list[str] = []
        for g in self._goods_of(cart):
            gid = str(g.get("id", ""))
            if not gid:
                continue
            if gid not in merged:
                order.append(gid)
            merged[gid] = merged.get(gid, 0) + _count_of(g)
        for it in items:
            gid = str(it.get("id", ""))
            if not gid:
                continue
            if gid not in merged:
                order.append(gid)
            merged[gid] = merged.get(gid, 0) + _count_of(it)
        goods = [{"id": gid, "count": _count_out(merged[gid])} for gid in order]
        return self._grocery_cart_write(goods, app_id, delivery)

    def _grocery_cart_write(self, goods: list[dict], app_id: str, delivery: dict) -> dict:
        """POST the FULL goods list. cart/set replaces the cart wholesale — that is
        also how the app removes an item: capture item [369] posts 6 goods, [375]
        posts 5 after a removal. There is no delete endpoint."""
        body = {"goods": goods, "cartSetMode": "SINGLE_CART", "delivery": delivery}
        return self._call_read("grocery_cart_set", body=body,
                               overrides={"appId": app_id})

    def grocery_set_cart(self, items: list[dict], app_id: str = "", point_id: str = "",
                         clear: bool = False) -> dict:
        """Set ABSOLUTE counts. `count: 0` removes a good; goods not mentioned keep
        their current count. `clear=True` empties the cart and ignores `items`.

        The counterpart of grocery_add_to_cart, which is relative (+N). Without this
        the cart could only ever grow: re-adding a good to "correct" it added again."""
        _need_store(app_id, point_id)
        try:
            cart = self.grocery_cart_get(app_id=app_id, point_id=point_id)
        except TbankApiError as e:
            # cart/set REPLACES the cart, so proceeding on a failed read would post
            # only what the caller named and delete everything else. An empty cart and
            # an unreadable one look identical from here, so refuse rather than guess.
            raise TbankApiError("CART_READ_FAILED",
                f"Не удалось прочитать корзину ({e}), а запись заменяет её целиком — "
                f"продолжать нельзя, иначе остальные товары будут удалены. "
                f"Повтори позже или проверь grocery_cart(app_id, point_id).") from e
        delivery = self._grocery_delivery(app_id, point_id, cart=cart)
        if clear:
            return self._grocery_cart_write([], app_id, delivery)

        wanted = {str(it.get("id", "")): _count_of(it, default=0.0)
                  for it in items if str(it.get("id", ""))}
        goods, seen = [], set()
        for g in self._goods_of(cart):
            gid = str(g.get("id", ""))
            if not gid or gid in seen:
                continue
            seen.add(gid)
            count = wanted.get(gid, _count_of(g))
            if count > 0:
                goods.append({"id": gid, "count": _count_out(count)})
        # Ids the caller named that are not in the cart yet are additions.
        for gid, count in wanted.items():
            if gid not in seen and count > 0:
                goods.append({"id": gid, "count": _count_out(count)})
        return self._grocery_cart_write(goods, app_id, delivery)

    @staticmethod
    def _goods_of(cart: Any) -> list[dict]:
        """Goods out of a cart GET payload (payload.cart.goods), [] if none."""
        if not isinstance(cart, dict):
            return []
        inner = cart.get("cart") if isinstance(cart.get("cart"), dict) else cart
        goods = inner.get("goods") if isinstance(inner, dict) else None
        return goods if isinstance(goods, list) else []

    def grocery_cart_goods(self, app_id: str = "", point_id: str = "") -> list[dict]:
        """Goods currently in the store's cart, [] if the cart is empty or errors."""
        try:
            return self._goods_of(self.grocery_cart_get(app_id=app_id, point_id=point_id))
        except TbankApiError:
            return []

    def grocery_checkout(self, app_id: str = "", point_id: str = "",
                         client_email: str = "", account: str = "",
                         sum_val: float = 0, attempt_id: str | None = None) -> dict:
        """Full grocery checkout (web flow): deliveries → order/create → payment_gate_pay.
        `app_id`/`point_id` scope the store; the payment agreement is resolved
        inside checkout from user/payment/account/last; `sum_val` is a mobile-cart
        fallback sum (the post-delivery WEB sum is used inside); `attempt_id` records
        progress in the journal. Raises checkout.CheckoutError (safe to retry) or
        checkout.CheckoutUnknown (order may exist — retry must be blocked)."""
        from .checkout import checkout as _checkout
        return _checkout(self, app_id=app_id, point_id=point_id, client_email=client_email,
                         sum_val=sum_val, account=account, attempt_id=attempt_id)

    def messenger_send(self, conversation_id: str, text: str) -> dict:
        """Send a text message to a conversation. Encapsulates the vendor
        Content-Type + body format."""
        import uuid as _uuid
        body = {"content": text, "clientSideId": str(_uuid.uuid4()),
                "assistant": {"inputType": "default"}}
        return self._call_read("messenger_send", body=body,
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages")

    def _source_account(self) -> str:
        """First Current RUB account id with a positive balance — the payer/source
        for transfers (capture: payParameters.account = 10-char source id)."""
        for a in (self.list_accounts() or []):
            if not isinstance(a, dict) or (a.get("accountType") or "") != "Current":
                continue
            money = a.get("moneyAmount") or {}
            bal = money.get("value", 0) if isinstance(money, dict) else 0
            try:
                bal = float(bal)
            except (TypeError, ValueError):
                bal = 0.0
            if bal <= 0:
                continue
            cur = a.get("currency")
            cn = cur.get("name") if isinstance(cur, dict) else cur
            if cn and str(cn).upper() not in ("RUB", "RUBLES", "РОССИЙСКИЙ РУБЛЬ", "₽"):
                continue
            if a.get("id"):
                return str(a["id"])
        raise TbankApiError("NO_SOURCE_ACCOUNT",
            "no Current RUB account with a positive balance to use as transfer source")

    def resolve_sbp_recipient(self, phone: str) -> list[dict]:
        """Resolve a phone to its SBP recipient banks (GET /v1/get_requisites,
        capture-verified). READ-ONLY — no money moves. A phone can map to SEVERAL
        banks (the recipient has accounts in multiple SBP banks), so the caller
        picks one (prefer isDefaultBank=True). Returns [{bank_member_id,
        masked_fio, pointer_link_id, bank_name, bank_id, is_default_bank,
        provider_fields}] per bank — provider_fields is the ready SBP providerFields
        object (pointerType from the SBP_PHONE_POINTER_TYPE constant) to paste into
        payment_commission(). Empty list = not registered in SBP / wrong number."""
        ptr = _normalize_phone(phone)
        r = self._call_read("get_requisites", overrides={
            "pointerType": "phone", "pointer": ptr,
            "pointerSource": "external", "withTinkoff": "true", "gapBanks": "true",
        })
        items = r if isinstance(r, list) else (
            (r.get("payload") if isinstance(r, dict) else None) or [])
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            df = {d.get("name"): d.get("value")
                  for d in (it.get("displayFields") or []) if isinstance(d, dict)}
            brand = it.get("brand") or {}
            bmi, mfio, plid = (str(df.get("bankMemberId", "")),
                               str(df.get("maskedFIO", "")),
                               str(it.get("pointerLinkId", "")))
            out.append({
                "bank_member_id": bmi,
                "masked_fio": mfio,
                "pointer_link_id": plid,
                "bank_name": str(brand.get("name", "")),
                "bank_id": str(brand.get("id", "")),
                "is_default_bank": bool(it.get("isDefaultBank")),
                # ready SBP providerFields — paste into payment_commission() so the
                # agent never hand-writes the 8276 pointer-type code.
                "provider_fields": {"pointerType": SBP_PHONE_POINTER_TYPE,
                                    "pointer": ptr, "bankMemberId": bmi,
                                    "maskedFIO": mfio, "pointerLinkId": plid},
            })
        return out

    def transfer(self, amount: float, to_account: str, description: str = "",
                 provider: str = "p2p-anybank", pointer_type: str = SBP_PHONE_POINTER_TYPE,
                 bank_member_id: str = "", masked_fio: str = "",
                 pointer_link_id: str = "", account: str = "",
                 user_payment_id: str = "") -> Any:
        """Transfer via signed /v1/pay (REAL money). Body shape is capture-verified
        (the old body invented pointerType='ACCOUNT' and was rejected). The signing
        mechanism (_signed_parts) is unchanged — it was proven byte-exact.

        phone/SBP (default: provider='p2p-anybank', pointer_type='8276'):
          to_account = recipient phone. If bank_member_id/masked_fio/pointer_link_id
          are NOT passed, the recipient is AUTO-RESOLVED via resolve_sbp_recipient()
          (GET /v1/get_requisites): the default bank is picked, or the single match;
          if several banks with no default → RECIPIENT_MULTIPLE_BANKS (surface list,
          never silently pick). For a NEW recipient, call transfer_sbp_resolve(phone)
          first to show the user the candidate banks, then pass the chosen fields.
          pay body is capture-verified (providerFields.pointerType='8276', pointer
          '+7XXXXXXXXXX'). paymentType='Transfer' belongs to payment_commission,
          NOT to pay — no real pay body carries it.
        between own accounts (provider='transfer-inner'): to_account = target account;
          providerFields = {'bankContract': to_account}.
        by details (provider='transfer-legal'): NOT supported. It needs explicit
          providerFields (bankBik/bankAcnt/inn/kpp/...) and no capture shows the
          shape, so there is nothing to replay. There is no lower-level tool to
          fall back to either — the MCP exposes no raw pay(); this path belongs to
          the app.

        `account` = the payer account id (from list_accounts). Empty falls back to the
        first Current RUB with a positive balance — which is a GUESS, and was
        previously the only behaviour: the user could be asked which account to debit,
        answer, and be debited from a different one anyway.

        `user_payment_id` is the client-generated id the app sends on every payment
        (a millisecond timestamp). Pass the SAME value to retry a transfer whose
        outcome you did not see — that is what stops a timeout from becoming two
        payments. A fresh value per call provides no idempotency at all.

        `description` becomes providerFields.message (capture: captures2.xml #595) —
        it used to be accepted, documented and then silently dropped.

        For commission preview, call payment_commission() separately before this.
        ALWAYS confirm with the user. Returns the bank's payload (paymentId, …)."""
        src = account or self._source_account()
        if provider == "transfer-inner":
            pf = {"bankContract": to_account}
        elif provider == "transfer-legal":
            raise TbankApiError("NOT_SUPPORTED",
                "Перевод по банковским реквизитам (БИК/счёт/ИНН) через MCP не "
                "реализован: нужны providerFields (bankBik/bankAcnt/inn/kpp/…), "
                "формы которых нет ни в одном захвате, а угадывать тело платежа "
                "нельзя. Низкоуровневого тула для этого тоже нет — отправь "
                "пользователя платить в приложение.")
        else:  # p2p-anybank (phone / SBP)
            # The caller's CHOICE is the two ids. maskedFIO is a display name the
            # bank echoes back, not part of the routing — and requiring it here meant
            # an agent that followed the docs (which promise "bank_member_id +
            # pointer_link_id") left it empty, the gate opened, and auto-resolution
            # silently replaced the bank the user had picked and confirmed. Same
            # person, different account, and invisible: the result line prints the
            # recipient only when masked_fio is set.
            if not (bank_member_id and pointer_link_id):
                # Auto-resolve the recipient via get_requisites (read-only). Pick the
                # default bank if any, else the single match; if several with NO
                # default, refuse + surface the list — money safety: never silently
                # pick a bank (could send to the wrong bank/account).
                resolved = self.resolve_sbp_recipient(to_account)
                if not resolved:
                    raise TbankApiError("RECIPIENT_NOT_RESOLVED",
                        f"{to_account} is not registered in SBP (or the number is wrong). "
                        "Call transfer_sbp_resolve(phone) to check.")
                pick = next((x for x in resolved if x["is_default_bank"]), None)
                if pick is None and len(resolved) == 1:
                    pick = resolved[0]
                if pick is None:
                    raise TbankApiError("RECIPIENT_MULTIPLE_BANKS",
                        f"{to_account} maps to {len(resolved)} SBP banks — pick one:\n" +
                        "\n".join(f"  - {x['masked_fio']} | {x['bank_name']} | "
                                  f"bankMemberId={x['bank_member_id']} | pointerLinkId={x['pointer_link_id']}"
                                  for x in resolved) +
                        "\nPass the chosen bank_member_id + pointer_link_id to transfer().")
                bank_member_id = pick["bank_member_id"]
                masked_fio = pick["masked_fio"]
                pointer_link_id = pick["pointer_link_id"]
            elif not masked_fio:
                # The ids came from the caller, so the routing is already decided.
                # Look up the display name only — never let this overwrite the choice.
                try:
                    match = next((x for x in self.resolve_sbp_recipient(to_account)
                                  if str(x.get("bank_member_id")) == str(bank_member_id)), None)
                    masked_fio = (match or {}).get("masked_fio", "")
                except TbankApiError:
                    masked_fio = ""
            pf = {"pointerType": pointer_type, "pointer": _normalize_phone(to_account),
                  "bankMemberId": bank_member_id, "maskedFIO": masked_fio,
                  "pointerLinkId": pointer_link_id}
        if description:
            # The app carries the note here, not as a top-level field
            # (captures2.xml #595: providerFields.message = "Hi").
            pf["message"] = description
        pay_params = {"provider": provider, "currency": "RUB", "account": src,
                      "moneyAmount": amount, "providerFields": pf,
                      "isTransferStatus": "false", "isUrgentTransfer": "false",
                      # Present in every real pay body; absent from ours until now.
                      "cellularService": "WiFi", "frontCamera": "true",
                      "userPaymentId": user_payment_id or str(int(time.time() * 1000))}
        # NOTE: no paymentType here. `paymentType: "Transfer"` was added from a
        # capture — but of /v1/payment_commission, where it IS required. No real
        # /v1/pay body in either capture carries it (checked all three: captures.xml
        # #1423 and #1477, captures2.xml #595). Sending it is an invention.
        body = "payParameters=" + urllib.parse.quote(json.dumps(pay_params))
        return self.pay(body)


    # ---- cards, limits, requisites ---------------------------------------

    def account_cards(self, account_id: str) -> list[dict]:
        """Cards issued on one account. Each card carries BOTH an `id` and a
        `ucid` — /v1/limits and /v1/card_credentials key off the **ucid**, while
        an operation's `card` field holds the **id**. Mixing them up silently
        returns another card's data."""
        data = self._call_read("account_cards", overrides={"id": str(account_id)})
        return data if isinstance(data, list) else []

    def cards(self) -> list[dict]:
        """Every card across every account, annotated with its account.

        ONE request. accounts_light already embeds the cards under `cards` (and
        `card` for an ExternalAccount), so the old fan-out — one /v1/account_cards
        per account, 12 extra round-trips on this user, most of them 400s for
        deposits and invest accounts — bought nothing. It bought less, in fact:
        the per-account response carries only id/ucid/position/flags, while the
        embedded one also has name, status, paymentSystem, the masked number and
        the expiry, which is what list_cards actually prints.

        account_cards() stays for the fields only it has (availableBalance,
        canBeRemoved); it is just no longer on this path."""
        out: list[dict] = []
        for acc in self.list_accounts():
            aid = str(acc.get("id") or "")
            if not aid:
                continue
            embedded = acc.get("cards")
            if not isinstance(embedded, list):
                one = acc.get("card")
                embedded = [one] if isinstance(one, dict) else []
            money = acc.get("moneyAmount") if isinstance(acc.get("moneyAmount"), dict) else {}
            for c in embedded:
                if not isinstance(c, dict):
                    continue
                c = dict(c)
                c["account"] = aid
                c["accountName"] = acc.get("name") or ""
                c["accountType"] = acc.get("accountType") or ""
                # The per-account endpoint had availableBalance; the embedded card
                # does not. The account's balance is the right number anyway — cards
                # in a multicard cluster all spend from it.
                c.setdefault("availableBalance", money.get("value"))
                c["currency"] = ((money.get("currency") or {}).get("name")
                                 if isinstance(money.get("currency"), dict) else "")
                out.append(c)
        return out

    def card_limits(self, ucid: str) -> list[dict]:
        data = self._call_read("card_limits", overrides={"ucid": str(ucid)})
        return data if isinstance(data, list) else []

    def _credentials_fingerprint(self) -> str:
        """The device blob /v1/card_credentials expects — the ###-delimited UA
        form (NOT the JSON fingerprint used at auth/step)."""
        ua = self._mobile_ua() or "iPhone/iOS/TCSMB"
        return f"{ua}###1170x2532x32###-180###false###false###"

    def card_credentials(self, ucid: str) -> dict:
        """Full card number + CVV + expiry for one card. Sensitive: the caller
        decides whether to show or mask it."""
        ov = {
            "ucid": str(ucid),
            "fingerprint": self._credentials_fingerprint(),
            "fingerprint_change_date": "0",
            "mobile_device_os": self.platform or "ios",
            "mobile_device_os_version": _IOS_VERSION,
            "mobile_device_model": "iPhone",
        }
        data = self._call_read("card_credentials", overrides=ov)
        return data if isinstance(data, dict) else {}

    def account_requisites(self, account_id: str,
                           currencies: tuple = ("RUB",)) -> list[dict]:
        """Bank details for an account. The `account` param repeats once per
        currency (`<id>;RUB`) — a list value becomes repeated query params."""
        accounts = [f"{account_id};{c}" for c in currencies]
        data = self._call_read("account_group_requisites",
                               overrides={"account": accounts})
        return data if isinstance(data, list) else []

    # ---- identity documents ----------------------------------------------

    def prefill_contact_id(self) -> str:
        """The contact id every prefill/profile path is built from.

        Memoised: documents() needs it for BOTH the document list and the holder's
        brief, so one tool call issued the identical request twice."""
        memo = getattr(self, "_memo", None)
        if memo is not None and memo.get("prefill_contact_id"):
            return memo["prefill_contact_id"]
        data = self._call_read("prefill_contact")
        contacts = (data or {}).get("contacts") or []
        if not contacts:
            raise TbankApiError("NO_CONTACT", "prefill profile returned no contact")
        cid = str(contacts[0].get("id") or "")
        if memo is not None:
            memo["prefill_contact_id"] = cid
        return cid

    def identity_documents(self) -> dict:
        """Every document the bank holds, grouped by kind (RusNationalID,
        RusDriversLic, RusInternationalID, RusSNILS, RusINN, RusOSAGO, …).

        Includes RELATIVES' documents the client once entered, so the caller must
        separate them — see documents() in server.py, which matches on birthDate."""
        cid = self.prefill_contact_id()
        data = self._call_read(
            "prefill_documents",
            path_override=f"/api/prefill/profile/contact/{cid}/document/all")
        return (data or {}).get("documents") or {}

    def identity_brief(self) -> dict:
        """The account holder's own birthDate/sex — the key that tells their
        documents apart from a relative's."""
        cid = self.prefill_contact_id()
        data = self._call_read(
            "prefill_userinfo_brief",
            path_override=f"/api/prefill/profile/contact/{cid}/userinfo/brief")
        return (data or {}).get("brief") or {}

    # ---- orders (every vertical) -----------------------------------------

    def orders(self) -> list[dict]:
        """All orders across groceries, cinema, concerts, flights, trains and
        hotels — the app's single "Заказы" feed. Newest first is NOT guaranteed;
        sort on `created`."""
        data = self._call_read("orders_list")
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        return lst if isinstance(lst, list) else []

    def order_details(self, order_id: str) -> dict:
        """Full detail for one entertainment order (hall, seats, QR, cast)."""
        data = self._call_read("order_get", overrides={"orderId": str(order_id)})
        return data if isinstance(data, dict) else {}

    def hotel_booking(self, booking_id: str) -> dict:
        """Full detail for a hotel booking: dates, hotel, room, guests, meals.

        Unlike the flight and rail hosts, this one accepts the plain mobile Bearer
        — no per-service link token — so it works straight from a mobile session."""
        data = self._call_read(
            "hotel_booking",
            path_override=f"/api/v1/hotels/bookings/{booking_id}")
        return data if isinstance(data, dict) else {}

    # ---- grocery item detail + nutrition ---------------------------------

    def grocery_good(self, good_id: str, app_id: str = "", point_id: str = "") -> dict:
        app_id, point_id = _need_store(app_id, point_id)
        data = self._call_read("grocery_good", overrides={
            "appId": str(app_id), "pointId": str(point_id), "goodId": str(good_id)})
        return (data or {}).get("good") or {}

    @staticmethod
    def nutrition(good: dict) -> dict:
        """КБЖУ per 100 g, plus per-package totals.

        Two shapes in the wild and only one is structured: Самокат (appId 695)
        fills meta.nutritionalValue.{protein,fat,carbohydrate,energy}; ВкусВилл
        (204) leaves all four empty and puts everything in the free-text `value`
        ("белки 3,3 г, жиры 3 г, углеводы 18,4 г; 113,8 ккал"). Parse the text
        whenever a structured field is missing, else half the catalog reads as
        "no data"."""
        meta = (good or {}).get("meta") or {}
        nv = meta.get("nutritionalValue") or {}
        text = str(nv.get("value") or "")

        def num(x):
            try:
                return float(str(x).replace(",", ".").split()[0])
            except (ValueError, IndexError, AttributeError):
                return None

        out = {"protein": num(nv.get("protein")), "fat": num(nv.get("fat")),
               "carb": num(nv.get("carbohydrate")), "kcal": num(nv.get("energy"))}
        if text:
            low = text.lower().replace(",", ".")
            for key, stem in (("protein", "белк"), ("fat", "жир"), ("carb", "углевод")):
                if out[key] is None:
                    m = re.search(stem + r"\w*\D{0,4}?([\d.]+)", low)
                    if m:
                        out[key] = num(m.group(1))
            if out["kcal"] is None:
                m = re.search(r"([\d.]+)\s*ккал", low)
                if m:
                    out["kcal"] = num(m.group(1))
        weight = meta.get("weight") or {}
        grams = weight.get("value") if str(weight.get("unit", "")).upper() == "GRM" else None
        out["grams"] = grams
        # kcal figures are per 100 g by convention; scale to the actual package
        out["kcal_pack"] = (out["kcal"] * grams / 100.0
                            if out["kcal"] is not None and grams else None)
        out["raw"] = text
        return out

    # Attributes grocery_candidates can annotate and grocery_rank can sort on.
    # `price` and `weight` come free with the search; the rest cost one extra
    # request per candidate, so they are opt-in.
    NUTRITION_KEYS = ("kcal", "kcal_pack", "protein", "fat", "carb")
    SORTABLE_KEYS = ("price", "weight") + NUTRITION_KEYS

    def grocery_candidates(self, query: str, app_id: str = "", point_id: str = "",
                           limit: int = 8, with_nutrition: bool = False) -> list[dict]:
        """Search `query` and return the candidates as plain attribute rows.

        This deliberately applies NO selection policy — it is the capability, not
        the strategy. Ranking ("cheapest", "lowest calorie", "most protein") is the
        caller's decision; see grocery_rank in server.py and the grocery skill.

        with_nutrition costs one extra /api/grocery/good request per candidate, so
        it is off unless the caller actually needs those fields. A good whose
        nutrition the retailer does not publish keeps None — "not published" is a
        different fact from zero and must not be flattened into one."""
        found = self.grocery_search(query, app_id=app_id, point_id=point_id)
        picked = found[:max(1, limit)]
        rows = []
        for item in picked:
            row = dict(item)
            # search returns weight as a display string ("160.0 GRM"); keep that
            # for output and add a numeric grams field to sort on. _grams reports
            # 0.0 for "no weight given" — keep that as None so an unknown weight
            # sorts as unknown rather than as the lightest item.
            row["weight_label"] = item.get("weight") or ""
            row["weight"] = self._grams(item) or None
            rows.append(row)

        if with_nutrition and rows:
            # One /api/grocery/good per candidate, and they do not depend on each
            # other — issued in sequence this was the whole latency of a ranked
            # search (8 round-trips before the first line of output). requests'
            # Session is thread-safe for concurrent requests on separate
            # connections, and the pool is capped, so a small fan-out is safe here.
            from concurrent.futures import ThreadPoolExecutor

            blank = {k: None for k in ("kcal", "kcal_pack", "protein", "fat",
                                       "carb", "grams")}

            def fetch(item):
                try:
                    good = self.grocery_good(item["id"], app_id=app_id, point_id=point_id)
                    return self.nutrition(good)
                except (TbankApiError, KeyError, ValueError):
                    return dict(blank)

            with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
                for row, n in zip(rows, pool.map(fetch, picked)):
                    row.update({k: n.get(k) for k in self.NUTRITION_KEYS})
                    if n.get("grams"):
                        row["weight"] = n["grams"]
        return rows

    # ---- cinema ----------------------------------------------------------

    _TRANSLIT = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", " ": "_",
        "-": "-",
    }

    @classmethod
    def _translit(cls, s: str) -> str:
        out = "".join(cls._TRANSLIT.get(ch, cls._TRANSLIT.get(ch.lower(), ch))
                      if not ch.isascii() else ch for ch in s)
        # each word capitalised, separators kept: Москва→Moskva,
        # Санкт-Петербург→Sankt-Peterburg, Нижний Новгород→Nizhniy_Novgorod
        return re.sub(r"(^|[_\-])([a-z])",
                      lambda m: m.group(1) + m.group(2).upper(), out)

    PAGE = 30                      # what the collection endpoint returns per page

    def cinema_movies(self, city: str = "Москва", query: str = "",
                      max_pages: int = 8) -> tuple[list[dict], int, int]:
        """Movies playing today in `city`, as (matches, scanned, listing_total).

        The collection code is city-derived ("Segodnya-v_kino_Moskva"); it is only a
        way to reach an eventId, which is itself city-independent and is what the
        schedule endpoint wants.

        The listing has no server-side search, so matching is ours and every page has
        to be seen. A previous version stopped as soon as ONE page held a match,
        which made a named search cheap and wrong: matches on later pages vanished,
        and the caller then reported the survivors as the complete count. Pages after
        the first are fetched concurrently instead — page 1 states the true total, so
        the page count is known after one request and the rest cost one round trip.

        `scanned` is returned so the caller can tell "these are all of them" from
        "these are all of them among the first N" when max_pages binds."""
        code = "Segodnya-v_kino_" + self._translit(city)
        q = query.lower().replace("ё", "е") if query else ""

        def matches(e):
            return not q or q in str(e.get("name", "")).lower().replace("ё", "е")

        def fetch(page: int) -> tuple[list, int]:
            data = self._call_read("events_collection", body={"genres": []},
                                   overrides={"collectionCode": code,
                                              "page": str(page), "count": str(self.PAGE)})
            coll = (data or {}).get("collection") or {}
            return [e for e in (coll.get("events") or []) if isinstance(e, dict)], \
                   int(coll.get("amount") or 0)

        out, total = fetch(1)
        pages = min(max(1, max_pages), -(-total // self.PAGE) if total else 1)
        if pages > 1 and len(out) < total:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, pages - 1)) as pool:
                for events, _ in pool.map(fetch, range(2, pages + 1)):
                    out.extend(events)
        return [e for e in out if matches(e)], len(out), total

    # Sorting anchor when the caller gives no coordinates. It is the centre of the
    # default city, NOT the user's location — the app sends the device's GPS. Named
    # and paired with `city` so the two cannot silently disagree: a Petersburg
    # listing sorted by distance from Moscow would look plausible and be nonsense.
    CITY_CENTRES = {
        "Москва": (55.7558, 37.6173),
        "Санкт-Петербург": (59.9386, 30.3141),
        "Екатеринбург": (56.8389, 60.6057),
        "Новосибирск": (55.0084, 82.9357),
        "Казань": (55.7963, 49.1088),
    }

    def cinema_schedule(self, event_id: str, date: str, city: str = "Москва",
                        latitude: float = 0.0, longitude: float = 0.0) -> list[dict]:
        """Showtimes for one movie on one date (YYYY-MM-DD), one entry per cinema.

        The location only sorts by distance — the whole city is returned either way.
        Pass latitude/longitude to sort around a real point; omitted, the centre of
        `city` is used, and for a city not in CITY_CENTRES the distance sort is
        dropped rather than anchored somewhere arbitrary."""
        body = {"date": date, "eventId": str(event_id), "city": city}
        if not (latitude or longitude):
            latitude, longitude = self.CITY_CENTRES.get(city, (0.0, 0.0))
        if latitude or longitude:
            body["sort"] = {"by": "distance"}
            body["location"] = {"latitude": latitude, "longitude": longitude}
        data = self._call_read("schedule_movie", body=body)
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        return lst if isinstance(lst, list) else []

    # ---- ticket booking (cinema + concerts) ------------------------------

    def event_seats(self, event_id: str, slot_id: str, object_id: str,
                    kind: str = "movie") -> list[dict]:
        """Hall layout for one showing: every seat with status and price.

        Cinemas key seats as "row:number". Concerts use a composite string
        ("Фанзона|5000§~§54093386|default") that must be passed back to
        order/create verbatim — it encodes sector, price and ticket id."""
        key = "scheme_sectors_concert" if kind == "concert" else "scheme_sectors_movie"
        data = self._call_read(key, overrides={
            "eventId": str(event_id), "slotId": str(slot_id),
            "objectId": str(object_id)})
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        return lst if isinstance(lst, list) else []

    def concert_hall(self, event_id: str, slot_id: str, object_id: str) -> dict:
        """Free-seating concert venues answer here instead of /sectors: sectors
        with `freeSeating: true` and a ticket count rather than a seat grid.

        READ ONLY on purpose — the capture has no order/create example for this
        purchase screen, so the request body for it is unknown and this client
        will not invent one."""
        data = self._call_read("scheme_hall_concert", overrides={
            "eventId": str(event_id), "slotId": str(slot_id),
            "objectId": str(object_id)})
        return data if isinstance(data, dict) else {}

    def concert_schedule(self, event_id: str) -> list[dict]:
        """Showings of one concert. Unlike movies these are not date-scoped."""
        data = self._call_read("schedule_concert", body={"eventId": str(event_id)})
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        return lst if isinstance(lst, list) else []

    def create_ticket_order(self, event_id: str, slot_id: str, object_id: str,
                            seats: list[dict], kind: str = "movie") -> dict:
        """Reserve seats. Creates an order and moves NO money — payment is a
        separate call. An order left unpaid expires by itself.

        seats: [{"id": "7:10", "type": "basic"}] for cinemas; concerts take the
        composite seatId and no type."""
        key = "order_create_concert" if kind == "concert" else "order_create_movie"
        body = {"eventId": str(event_id), "slotId": str(slot_id),
                "objectId": str(object_id), "seats": seats}
        data = self._call_read(key, body=body)
        return data if isinstance(data, dict) else {}

    def pay_marketplace_order(self, order_id: str, amount: float,
                              account: str, nfs_payment_token: str) -> dict:
        """MONEY OPERATION. Pay for a marketplace order (cinema/concert ticket).

        Cookie/Bearer only — no HMAC signature, unlike /v1/pay. `nfs_payment_token`
        and the amount both come from the create_ticket_order response; passing an
        amount that disagrees with the order is how you get a stuck payment."""
        body = {
            "amount": {"amount": amount, "type": "simple", "currencyCode": "643"},
            "paymentMethod": {"type": "agreement", "agreement": str(account)},
            "flow": {"orderId": str(order_id), "type": "marketplace",
                     "nfsPaymentToken": str(nfs_payment_token)},
        }
        data = self._call_read("payment_gate_pay_mobile", body=body)
        return data if isinstance(data, dict) else {}

    def cancel_ticket_order(self, order_id: str, kind: str = "movie") -> Any:
        """Cancel a ticket order. The capture shows this answering 500
        ("Сервис временно недоступен") for both the movie-specific and the generic
        path, so treat a failure as "unknown, check the app", not "still booked"."""
        key = "order_cancel_movie" if kind == "movie" else "order_cancel"
        return self._call_read(key, overrides={"orderId": str(order_id)}, body={})

    # ---- extras ----------------------------------------------------------

    def bank_documents(self) -> list[dict]:
        """Bank-issued certificates (справки). Answers with a BARE list."""
        data = self._call_read("bank_documents")
        return data if isinstance(data, list) else []

    def insurance_policies(self) -> Any:
        """Active insurance policies. This host capitalises its envelope
        (`Payload`/`ResultCode`), so _unwrap passes the whole body through."""
        return self._call_read("insurance_policies")

    def payment_receipt_pdf(self, payment_id: str) -> bytes:
        data = self._call_read("payment_receipt_pdf",
                               overrides={"paymentId": str(payment_id)})
        return data if isinstance(data, bytes) else bytes(data or b"")

    # Sections whose endpoint is a FILTER and returns nothing useful without an
    # argument. `providers` hits /providers/compatible/filter, which the app calls as
    # ?ids=fns-rf,gibdd-online-rf,… — with no ids it is a filter with no filter, so
    # the tool could never list anything. `requisites` is the SBP pointer lookup and
    # needs the phone.
    _SECTION_ARG = {"providers": "ids", "requisites": "pointer"}

    def get_data(self, section: str, arg: str = "") -> Any:
        """Unified getter for banking data.

        `arg` is required by the sections in _SECTION_ARG (providers → a
        comma-separated id list; requisites → a phone). Passing it for any other
        section is ignored."""
        _SECTIONS = {
            "subscriptions": "subscription_all", "subscription_bills": "subscription_all_bills",
            "credit_schedule": "credit_payment_schedule", "credit_rating": "credit_rating",
            "statements": "statements", "requisites": "get_requisites",
            "invoices": "invoices_to_pay", "templates": "payment_templates",
            "contacts": "contact_list", "providers": "providers_compatible",
            "cards": "available_cards", "loans": "active_loans",
            "autopayments": "autopayments", "sbp": "sbp_subscriptions",
            "offers": "client_offers", "gifts": "gift_for_recipient",
            "services": "services", "bundles": "bundles_all",
            "manager": "manager_info", "merchant_subs": "detected_merchant_subscriptions",
            "profile": "user_profile", "homes": "my_homes",
            "cars": "my_cars", "shortcuts": "payment_shortcuts",
            "finhealth_presets": "finhealth_account_presets",
            "finhealth_total": "finhealth_balance_total",
            "finhealth_turnover": "finhealth_balance_turnover",
            "finhealth_invest": "finhealth_invest_turnover",
            "invest_accounts": "investbox_accounts",
            "invest_offers": "investbox_offers", "invest_yield": "investbox_product_yield",
            "broker_margin": "broker_margin", "pension": "invest_pension_profile",
            "shared_owned": "shared_resources_owned", "shared": "shared_resources",
            "business_info": "business_account_info",
            "appointments": "appointment_deliveries",
            "qr_resolve": "resolve_payment_qr",
        }
        if section.lower() == "profile":
            # /userinfo/userinfo needs client_id=gorod-app + no mobile-BFF params —
            # route through _call_userinfo, not the generic _call_read (which 401s).
            return self._call_userinfo()
        key = _SECTIONS.get(section.lower(), section)
        arg_key = self._SECTION_ARG.get(section.lower())
        if arg_key:
            if not arg:
                raise TbankApiError("ARG_REQUIRED",
                    f"get_data('{section}') is a filter endpoint and returns nothing "
                    f"without an argument: pass {arg_key}. "
                    + ("Provider ids look like 'fns-rf', 'gibdd-online-rf' "
                       "(capture-verified) — they cannot be enumerated through this "
                       "endpoint, only looked up."
                       if arg_key == "ids" else
                       "For a recipient lookup prefer transfer_sbp_resolve(phone), "
                       "which parses the same response; for YOUR OWN account details "
                       "use account_requisites(account_id) — a different endpoint."))
            return self._call_read(key, overrides={arg_key: arg})
        return self._call_read(key)


_SESSION_EXPIRED = {
    "NOT_AUTHORIZED", "SESSION_EXPIRED", "SESSION_NOT_FOUND", "NO_SESSION",
    "UNAUTHORIZED", "DEVICE_LINK_REMOVED", "REAUTH", "INVALID_SESSION",
    "invalid_grant", "invalid_token",
}


def ms_for_period(days: int = 30) -> tuple[int, int]:
    end = int(time.time() * 1000)
    return end - days * 86400 * 1000, end
