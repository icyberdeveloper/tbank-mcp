"""T-Bank mobile API client — self-bootstrapping, fully headless after login.

login(phone) + confirm_otp(otp) do a real SSO login (no capture needed): they
mint the mobile sessionid + access_token + refresh_token and capture the
long-lived SSO_SESSION cookie. silent_relogin() re-mints the session from
SSO_SESSION + a built-in device fingerprint (no OTP) ~every 2h, producing a
session valid for BOTH reads and the messenger tmsg.

Reads use builtin endpoint shapes (endpoints.py — static API params, no
device/session/account secrets) + the live session. `pay`/`group_pay` are
HMAC-SHA256 `x-api-signature` (key = sessionid). api/id/*.t-bank-app.ru serve a
cert by the Russian Trusted Root CA; ca/bundle.pem is rebuilt by tls.py and
self-heals on rotation.
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
    inache: str = ""
    cookie_str: str = ""            # the cookie header to replay on reads/refresh
    sso_login_cookie: str = ""      # the LOGIN (auth_code) cookie set incl. SSO_SESSION (long-lived) — for silent re-login
    auth_step_fingerprint: str = "" # the static fingerprint blob sent at auth/step (silent re-login)
    tmsg_session_id: str = ""       # messenger JWT cookie (tm.t-bank-app.ru)
    sso_access_token: str = ""     # auth_code-grant access_token (silent re-login) for tmsg ONLY; reads use the refresh access_token
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
        # If _minted_at is 0 (loaded from legacy session without timestamp),
        # don't set it to now — that would make an old token look fresh.
        # Leave it 0 — ensure_fresh will refresh before first use.
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
        if _CA_BUNDLE:
            self._http.verify = _CA_BUNDLE
        if self.proxy:
            self._http.proxies = {"http": self.proxy, "https": self.proxy}
        # self-healing TLS: rebuild the CA bundle on startup (handles cert
        # rotation since the last run) + mount an adapter that retries on SSL
        # failure by refreshing the host's chain into the bundle.
        try:
            from . import tls as _tls
            _tls.rebuild_bundle()
            self._http.mount("https://", _tls.RobustTLSAdapter())
        except Exception:
            pass
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
        return tok

    def _basic_auth(self) -> str:
        return "Basic " + base64.b64encode(f"{self.client_id}:".encode()).decode()

    def ensure_fresh(self, max_age_s: int = 6000) -> None:
        """Re-mint the session before the access_token expires (~2h).
        Prefers silent_relogin (gives a session valid for BOTH reads and the
        messenger tmsg); falls back to refresh() if no SSO_SESSION.
        _minted_at == 0 means unknown age (legacy session) → always refresh."""
        if self._minted_at == 0 or time.time() - self._minted_at > min(max_age_s, max(60, self.expires_in - 600)):
            if self.sso_login_cookie and self.auth_step_fingerprint:
                self.silent_relogin()
            else:
                self.refresh()

    # -- reads (template-driven, cookie + Bearer, no signing) -----------------

    def _tpl(self, key: str) -> dict:
        """Resolve a read template: builtin endpoint shape first (capture-free),
        then any capture-loaded template (legacy)."""
        tpl = BUILTIN_ENDPOINTS.get(key) or self.read_templates.get(key)
        if not tpl:
            raise TbankApiError("NO_TEMPLATE", f"no endpoint shape for '{key}'")
        return tpl

    def _call_read(self, template_key: str, *, overrides: dict | None = None,
                   body: dict | None = None, path_override: str | None = None) -> Any:
        """Replay a read endpoint (builtin shape) with fresh sessionid + Bearer.

        path_override replaces the path (for parameterized endpoints like
        messenger conversations/{id}/messages)."""
        tpl = self._tpl(template_key)
        params = {k: v for k, v in tpl.get("params", {}).items()
                  if k not in _LIVE_QUERY}
        params["sessionid"] = self.mobile_sessionid
        params["deviceId"] = self.device_id
        params["oldDeviceId"] = self.old_device_id or self.device_id
        params["wuid"] = self.device_id
        # inject the common base params from the session if not in the template
        # (so builtin endpoints with minimal params still send appName/origin/etc.)
        for k, v in (("appName", self.app_name), ("appVersion", self.app_version),
                     ("origin", self.origin), ("platform", self.platform),
                     ("ccc", self.ccc), ("cpswc", self.cpswc),
                     ("connectionType", self.connection_type),
                     ("vendor", self.vendor), ("client_version", self.client_version)):
            if v and k not in params:
                params[k] = v
        if overrides:
            params.update(overrides)
        headers = {k: v for k, v in tpl.get("headers", {}).items()
                   if k.lower() not in _LIVE_HEADERS}
        headers["Authorization"] = "Bearer " + self.access_token
        # messenger (tm.t-bank-app.ru) uses ONLY the tmsgSessionID cookie, minted
        # on demand from the access_token via issueTokenBySSO; other hosts use the
        # SSO/sessionid cookie_str.
        if "tm.t-bank-app.ru" in (tpl.get("host") or ""):
            self._ensure_tmsg()
            if self.tmsg_session_id:
                headers["Cookie"] = f"tmsgSessionID={self.tmsg_session_id}"
        elif self.cookie_str:
            headers["Cookie"] = self.cookie_str
        host = tpl.get("host") or self.base_url
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
            r = self._http.post(url, params=params, json=post_body, headers=headers, timeout=30)
        elif method == "PUT":
            r = self._http.put(url, params=params, headers=headers, timeout=30)
        else:
            r = self._http.get(url, params=params, headers=headers, timeout=30)
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
        params["wuid"] = self.device_id
        if extra_query:
            params.update(extra_query)
        query = urllib.parse.urlencode(params, safe="%/,")
        host = tpl.get("host") or self.base_url
        path = tpl["path"]
        url = f"{host.rstrip('/')}/{path.lstrip('/')}?{query}"
        sig = self._sign("POST", path, query, body_str)
        headers = {k: v for k, v in tpl.get("headers", {}).items()
                   if k.lower() not in _LIVE_HEADERS}
        headers["Authorization"] = "Bearer " + self.access_token
        headers["x-api-signature"] = sig
        if self.cookie_str:
            headers["Cookie"] = self.cookie_str
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return url, headers, body_str

    def pay(self, body: str | None = None) -> Any:
        """POST v1/pay — REAL signed payment (moves money). body = raw form-encoded
        payParameters=...; None = replay the default pay body. Signed with
        x-api-signature (HMAC-SHA256, key=sessionid)."""
        tpl = self._tpl("v1_pay")
        if not tpl:
            raise TbankApiError("NO_TEMPLATE", "no v1/pay in capture")
        body_str = body if body is not None else (tpl.get("body") or "")
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
        r = self._http.post(url, json={"ssoToken": self.sso_access_token or self.access_token},
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
        # try the current access_token first (works if it's an auth_code-grant one)
        if self.access_token and not self._tmsg_expired():
            pass
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
        return self._call_read("messenger_base",
            path_override="/app/bank/messenger/conversations/unread")

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
        ov = {"start": str(start_ms), "end": str(end_ms), "period": period,
              "groupBy": group_by, "config": "allNotInner"}
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
        return self._call_read("session_status")

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
        """Read the cart for a specific store. app_id/point_id scope which
        store's cart is read — without them the backend returns the default
        (often empty) cart, which is the 'Корзина пуста' mismatch."""
        ov = {k: v for k, v in (("appId", app_id), ("pointId", point_id)) if v}
        return self._call_read("grocery_cart_get", overrides=ov or None)

    def grocery_cart_set(self, body: dict | None = None,
                         app_id: str = "", point_id: str = "") -> dict:
        """Build/set the grocery cart (POST). If body is None, gets the full
        delivery address (with details) from GET cart + uses it — the backend
        requires address.details (flat, houseType, etc.) or it crashes."""
        ov = {k: v for k, v in (("appId", app_id), ("pointId", point_id)) if v}
        if body is None:
            # get the full address (with details) from the current cart
            cart = self._call_read("grocery_cart_get", overrides=ov or None)
            if isinstance(cart, dict):
                delivery = cart.get("delivery", {}) or cart.get("cart", {}).get("delivery", {})
                addr = delivery.get("address", {})
                pid = delivery.get("pointId", point_id or "700")
                body = {
                    "goods": [], "cartSetMode": "SINGLE_CART",
                    "delivery": {"isExpress": False, "comment": "", "pointId": pid,
                                 "deliveryType": "IN_PERSON", "address": addr}}
        return self._call_read("grocery_cart_set", body=body, overrides=ov or None)

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

    def grocery_client_info(self) -> dict:
        return self._call_read("grocery_client_info")

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

    def grocery_goods(self, category_id: str = "custom_ordered_azbuka_vkusa",
                      app_id: str = "578", point_id: str = "700",
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
        """Compute payment commission (POST)."""
        return self._call_read("payment_commission", body=body)

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
        """Unread support/tracker request IDs."""
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
        # get the delivery address from client/info
        info = self._http.get("https://lifestyle.t-bank-app.ru/api/grocery/client/info",
            params={"appName": self.app_name, "appVersion": self.app_version,
                    "platform": self.platform, "origin": self.origin,
                    "deviceId": self.device_id, "oldDeviceId": self.old_device_id,
                    "sessionid": self.mobile_sessionid, "ccc": self.ccc,
                    "cpswc": self.cpswc, "connectionType": self.connection_type,
                    "inache": self.inache}, headers=base, timeout=30)
        addrs = []
        try:
            addrs = info.json().get("payload", {}).get("deliveryInfo", {}).get("addresses", [])
        except Exception:
            pass
        addr = addrs[0].get("value", "") if addrs else ""
        coords = addrs[0].get("coordinates", {}) if addrs else {}
        lat = str(coords.get("latitude", 55.754709)) if coords else "55.754709"
        lon = str(coords.get("longitude", 37.525818)) if coords else "37.525818"
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
                    if app_id and name:
                        stores.append({"appId": app_id, "name": name,
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

    def grocery_plan_order(self, ingredients: list[str], store_app_id: str = "",
                           store_point_id: str = "") -> dict:
        """Plan a grocery order. #12: custom_ordered is loaded ONCE per store and
        matched in memory (was re-read per ingredient → N+1, up to ~28 requests for 7
        ingredients). Ingredient queries are normalized (qty/units/stopwords stripped:
        'картофель 1 кг' → 'картофель'). Missing ingredients fall back to global search
        run in parallel (concurrency-capped).

        Returns {store, items, total_sum, missing, substitutions}."""
        import re as _re
        app_id = store_app_id or "578"
        point_id = store_point_id or "700"
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
            for page in range(1, 4):
                items = self._as_list(self._call_read("grocery_goods", overrides={
                    "appId": app_id, "pointId": point_id,
                    "categoryId": "custom_ordered_azbuka_vkusa",
                    "page": str(page), "count": "50"}))
                if not items:
                    break
                _custom.extend(g for g in items if isinstance(g, dict))
            return _custom

        missing = []
        for ingredient in ingredients:
            q = norm(ingredient)
            found = None
            for g in custom_once():
                name = g.get("name", "").lower().replace("ё", "е")
                if q and q in name:
                    price = g.get("price", {})
                    pv = price.get("value", 0) if isinstance(price, dict) else 0
                    found = {"id": str(g.get("id", "")), "name": g.get("name", ""),
                             "price": pv, "source": "custom_ordered", "query": q}
                    break
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
                             "price": g.get("price", 0), "source": "search", "query": q}
                    plan["items"].append(found)
                    plan["total_sum"] += found.get("price", 0) or 0
                else:
                    plan["missing"].append(ingredient)
        return plan

    def _parallel_search(self, queries: list[str], app_id: str, point_id: str,
                         max_workers: int = 4) -> dict:
        """Run global grocery searches in parallel (concurrency-capped). Returns
        {normalized_query: first_hit_dict}. #12"""
        from concurrent.futures import ThreadPoolExecutor
        out: dict[str, dict] = {}
        if not queries:
            return out

        def one(q):
            try:
                r = self.grocery_search(q, app_id=app_id, point_id=point_id)
                return q, (r[0] if r else None)
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
        ov = {"start": str(start_ms), "end": str(end_ms), "isSuspicious": "true"}
        if account_id:
            ov["account"] = account_id
        data = self._call_read("operations", overrides=ov)
        if isinstance(data, dict):
            pl = data.get("payload")
            return pl if isinstance(pl, list) else ([pl] if pl else [])
        return data if isinstance(data, list) else []

    def spending_categories(self, account_id: str | None, start_ms: int, end_ms: int) -> dict:
        """operations_histogram?groupBy=category -> {earning:[...], spending:[...]}."""
        ov = {"start": str(start_ms), "end": str(end_ms),
              "groupBy": "category", "period": "day", "config": "allNotInner"}
        data = self._call_read("operations_histogram", overrides=ov)
        payload = data.get("payload", data) if isinstance(data, dict) else {}
        spending = payload.get("spending") or []
        earning = payload.get("earning") or []
        total = 0.0
        cats = []
        for c in spending:
            if not isinstance(c, dict):
                continue
            amt = c.get("amount") or c.get("credit") or {}
            v = amt.get("value") if isinstance(amt, dict) else amt
            try:
                fv = abs(float(v))
            except (TypeError, ValueError):
                fv = 0.0
            total += fv
            cats.append({
                "category": c.get("name") or c.get("categoryName") or c.get("spendingCategory") or "?",
                "amount": round(fv, 2), "count": c.get("count") or c.get("operationsCount") or 0,
                "mcc": str(c.get("mcc")) if c.get("mcc") is not None else "",
            })
        for c in cats:
            c["share_pct"] = round(c["amount"] / total * 100, 2) if total else 0.0
        return {
            "period": {"start_ms": start_ms, "end_ms": end_ms},
            "total_spent": round(total, 2), "currency": "RUB",
            "categories": sorted(cats, key=lambda x: x["amount"], reverse=True),
            "earning_categories": len(earning),
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

    def grocery_search(self, query: str, app_id: str = "", point_id: str = "") -> list[dict]:
        """Global grocery search via search/fulltext — searches the ENTIRE store
        catalog (not just one category). Uses inStockFilter (only available
        items). Filters out prepared foods. Returns: id, name, price, weight,
        store, imageUrl. query = e.g. "свёкла"."""
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
        params = {
            "screen": "grocery", "context": "api", "applicationId": app_id or "578",
            "pointId": point_id or "700", "appName": self.app_name,
            "appVersion": self.app_version, "platform": self.platform,
            "origin": self.origin, "deviceId": self.device_id,
            "oldDeviceId": self.old_device_id, "ccc": self.ccc,
            "cpswc": self.cpswc, "connectionType": self.connection_type,
            "inache": self.inache,
        }
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
            name = src.get("name", "")
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

    def grocery_add_to_cart(self, items: list[dict], app_id: str = "578", point_id: str = "700") -> dict:
        """Add items to cart. items = [{"id": "123", "count": 1}, ...].
        Encapsulates: address.details fetch + cart/set body format."""
        # get full address (with details) from current cart
        cart = self._call_read("grocery_cart_get", overrides={"appId": app_id, "pointId": point_id})
        addr = {}
        if isinstance(cart, dict):
            delivery = cart.get("delivery", {}) or cart.get("cart", {}).get("delivery", {})
            addr = delivery.get("address", {})
        body = {
            "goods": items, "cartSetMode": "SINGLE_CART",
            "delivery": {"isExpress": False, "comment": "", "pointId": point_id,
                         "deliveryType": "IN_PERSON", "address": addr}}
        return self._call_read("grocery_cart_set", body=body, overrides={"appId": app_id, "pointId": point_id})

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

    def transfer(self, amount: float, to_account: str, description: str = "") -> Any:
        """P2P transfer (encapsulates: payment_commission + pay body + HMAC signature).
        amount = RUB, to_account = recipient account/phone, description = optional note."""
        # preview commission
        comm_body = {"payParameters": json.dumps({
            "account": to_account, "moneyAmount": amount, "currency": "RUB",
            "paymentType": "Payment", "provider": "p2p-anybank"})}
        try:
            self._call_read("payment_commission", body=comm_body)
        except Exception:
            pass
        # pay (signed — _call_signed handles the HMAC)
        pay_body = ("payParameters=" + urllib.parse.quote(json.dumps({
            "isTransferStatus": "false", "moneyAmount": amount,
            "provider": "p2p-anybank", "providerFields": {"pointerType": "ACCOUNT",
            "account": to_account, "description": description}})))
        return self.pay(pay_body)

    def get_data(self, section: str) -> Any:
        """Unified getter for banking data. section = one of:
        subscriptions, credit_schedule, statements, requisites, invoices,
        templates, contacts, providers, cards, finhealth, invest, bundles,
        manager, loans, autopayments, sbp, offers, gifts, services."""
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
        key = _SECTIONS.get(section.lower(), section)
        return self._call_read(key)


_SESSION_EXPIRED = {
    "NOT_AUTHORIZED", "SESSION_EXPIRED", "SESSION_NOT_FOUND", "NO_SESSION",
    "UNAUTHORIZED", "DEVICE_LINK_REMOVED", "REAUTH", "INVALID_SESSION",
    "invalid_grant", "invalid_token",
}


def ms_for_period(days: int = 30) -> tuple[int, int]:
    end = int(time.time() * 1000)
    return end - days * 86400 * 1000, end
