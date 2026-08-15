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
import binascii
import decimal
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from .endpoints import BUILTIN_ENDPOINTS, VERTICALS, VERTICAL_ALIASES, APP_VERSION
from .observability import _redact_value

MOBILE_BASE = "https://api.t-bank-app.ru"
ID_BASE = "https://id.t-bank-app.ru"
# Canonical OAuth2 token endpoint for the refresh grant. Used as the dataclass
# default AND normalized again in __post_init__, so a legacy session.json that
# stored an explicit empty "" token_url (the old default) can never make
# refresh() POST to "" (the original MissingSchema('') crash).
DEFAULT_TOKEN_URL = f"{ID_BASE}/auth/token/mobile"
# workflowType the bank puts on a get_requisites candidate that is the recipient's
# own T-Bank account rather than an SBP route to another bank. Such a candidate has
# no bankMemberId — there is no member to route to.
TBANK_INNER_WORKFLOW = "TinkoffInner"
# SBP "pointer type" enum for a phone-number pointer. Verified CONSTANT across all
# phone/SBP transfers in captures.xml (6 different recipients, different bankMemberId,
# always pointerType="8276") — it is NOT the recipient's bank code (that's bankMemberId),
# so it's a fixed protocol constant, analogous to currencyCode "643" for RUB.
# Serialises every path that re-mints the session.
#
# grocery_checkout is the only async tool and it offloads its body to a worker
# thread (asyncio.to_thread), which leaves FastMCP's event loop free to run any
# other sync tool — against the SAME global MobileSession. Two threads entering
# ensure_fresh together each POST the refresh_token; the grant rotates it, so the
# second gets invalid_grant, falls through to silent_relogin (authorize → step →
# token, plus a propagation wait) and both then write their own access_token,
# sessionid and rotated refresh_token over each other. What lands on disk is a mix.
#
# Module-level rather than per-instance because there is exactly one session per
# server process, and because a per-instance lock has to be created somewhere —
# and the tests build sessions without running __post_init__.
_MINT_LOCK = threading.RLock()

# Cookies EVERY bank host receives, and the only ones.
#
# The capture is unambiguous: across seventeen t-bank-app.ru hosts the app sends
# exactly these three, while SSO_SESSION, SSO_SESSION_STATE, SSO_CONVERSATION_CSRF_*
# and sso_uaid appear on id.t-bank-app.ru and nowhere else. SSO_SESSION is the
# host-only, HttpOnly credential that mints a session WITHOUT an SMS — the whole
# point of silent_relogin — and it was going to every host we talk to, because
# cookie_str was assigned the entire login jar.
_WIDE_COOKIES = ("__P__wuid", "api_sso_id", "sso_used")


def wide_cookies(cookie_str: str) -> str:
    """Keep only the cookies every host is supposed to get, in their given order."""
    out = []
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if part.split("=", 1)[0].strip() in _WIDE_COOKIES:
            out.append(part)
    return "; ".join(out)


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
        "appVersion": APP_VERSION,
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


class UnreadableResponse(TbankApiError, requests.exceptions.RequestException):
    """The server answered and the answer could not be read.

    Deliberately BOTH a TbankApiError and a RequestException, because on the money
    path those two mean opposite things. The money tools classify a TbankApiError as
    «the bank answered with an error envelope, so nothing moved» and a
    RequestException as «the request may have arrived, the outcome is unknown».

    A body we cannot parse is the second kind, even on HTTP 200 — a proxy or WAF
    interstitial means something processed the request; what it did is exactly what
    we cannot see. Raising a plain TbankApiError here told the user their money was
    safe on the one occasion nobody can know that."""


class PaymentConfirmationRequired(TbankApiError):
    """The bank ACCEPTED a /v1/pay request but is holding it for a second factor
    (resultCode WAITING_CONFIRMATION): an SMS OTP, a push approval or an in-app
    approval. This is neither a refusal nor a completed payment — it is a resumable
    state, and treating it as a terminal `failed` is the bug this class fixes.

    Money has NOT moved yet, but a pending payment now EXISTS on the backend keyed by
    its ``userPaymentId``. A fresh /v1/pay with a NEW userPaymentId would create a
    SECOND pending payment; the same id must be carried into the confirmation instead.
    So this reads as blocking, not as safe-to-retry.

    A plain TbankApiError keeps only (code, message); _unwrap() would raise one and
    let the rest of the envelope fall on the floor. The WAITING_CONFIRMATION envelope
    (capture-verified) carries, at the TOP level, everything /v1/confirm needs to
    continue:

        {"resultCode":"WAITING_CONFIRMATION",
         "operationTicket":"<uuid>",          # -> initialOperationTicket on /v1/confirm
         "initialOperation":"pay",            # -> initialOperation on /v1/confirm
         "confirmations":["SMSBYID"],         # -> confirmationType on /v1/confirm
         "confirmationData":{"SMSBYID":{"codeLength":4,"paymentId":"<id>",...}}}

    All of it is preserved here. ``payload`` is already redacted at construction. The
    OTP is deliberately NOT a field: it is entered later, by the user, into
    confirm_payment(), rides the wire as ``secretValue`` (a 'secret'-keyed field the
    redactor scrubs) and is never logged."""

    def __init__(self, code: str, message: str, *, http_status=None, payload=None,
                 operation_ticket: str = "", initial_operation: str = "pay",
                 confirmation_type: str = "", confirmations=None,
                 code_length: int = 0, payment_id: str = "",
                 user_payment_id: str = "", request_id: str = "",
                 method: str = "POST", url: str = ""):
        super().__init__(code, message)
        self.http_status = http_status
        self.payload = payload if isinstance(payload, dict) else {}
        self.operation_ticket = str(operation_ticket or "")
        self.initial_operation = str(initial_operation or "pay")
        # The LITERAL type the bank wants echoed back (e.g. "SMSBYID"), not a friendly
        # label — /v1/confirm rejects a normalised value.
        self.confirmation_type = str(confirmation_type or "")
        self.confirmations = [str(c) for c in (confirmations
                              or ([confirmation_type] if confirmation_type else []))]
        self.code_length = int(code_length or 0)
        # The bank's own paymentId, embedded in the challenge — the same id the confirm
        # returns, so it is known even before the code is entered (for reconciliation).
        self.payment_id = str(payment_id or "")
        self.user_payment_id = str(user_payment_id or "")
        self.request_id = str(request_id or "")
        self.method = method
        self.url = url


# Result codes that mean "accepted, pending a second factor" — a continuation, not a
# failure. WAITING_CONFIRMATION is the capture-verified money-path code. The others
# are defensive spellings of the same state; none has been seen in a capture, so none
# drives a request shape — they only route a response into the pending branch instead
# of the terminal-failure branch.
_PAYMENT_CONFIRMATION_CODES = {
    "WAITING_CONFIRMATION", "CONFIRMATION_NEEDED", "CONFIRMATION_REQUIRED",
    "NEED_CONFIRMATION", "NEED_CONFIRM",
}


# query/header keys that carry live secrets — substituted fresh at call time.
_LIVE_QUERY = {"sessionid", "wuid"}
_LIVE_HEADERS = {"authorization", "cookie"}


def delivery_eta(nearest: dict | None, now=None) -> tuple[float | None, str]:
    """A store's nearest delivery slot as (minutes_until_it_lands, human_label).

    `nearestTime` comes in two shapes and they are NOT interchangeable — in the
    capture 55 of 80 retailers use one and 25 the other:

      type=Relative — `from`/`to` are MINUTES as strings, and `from` is often ""
        ("Самокат: to=15" means within 15 minutes).
      type=Absolute — `from`/`to` are ISO-8601 TIMESTAMPS of a booked slot
        ("METRO: 2026-07-22T08:00 → 11:00", i.e. tomorrow morning).

    The old code formatted both as f"{from}-{to} min", which turned the majority
    shape into "2026-07-22T08:00:00+03:00-2026-07-22T11:00:00+03:00 min" — an
    absolute date range labelled as minutes. Nothing caught it because
    grocery_stores() never printed the field.

    Minutes are measured to the END of the window: that is the "you will have it by"
    number, and it is the only figure comparable across both shapes. A store with no
    slot, or whose window has already passed (stale data), returns None — which sorts
    LAST in both directions, the same rule the nutrition ranking uses. Unknown is not
    zero, and a stale slot must never win "fastest"."""
    import datetime as _dt

    nearest = nearest or {}
    kind = str(nearest.get("type") or "")
    raw_from, raw_to = nearest.get("from"), nearest.get("to")
    if not raw_to:
        return None, ""

    if kind == "Absolute":
        try:
            end = _dt.datetime.fromisoformat(str(raw_to))
            start = _dt.datetime.fromisoformat(str(raw_from)) if raw_from else None
        except ValueError:
            return None, ""
        ref = (now if isinstance(now, _dt.datetime)
               else _dt.datetime.fromtimestamp(now if now is not None else time.time(),
                                               tz=end.tzinfo or _dt.timezone.utc))
        if end.tzinfo is None:
            end = end.replace(tzinfo=ref.tzinfo)
        if start is not None and start.tzinfo is None:
            start = start.replace(tzinfo=ref.tzinfo)
        ref = ref.astimezone(end.tzinfo)
        days = (end.date() - ref.date()).days
        day = {0: "сегодня", 1: "завтра"}.get(days) or end.strftime("%d.%m")
        span = (f"{start:%H:%M}–{end:%H:%M}" if start else f"до {end:%H:%M}")
        label = f"{day} {span}"
        minutes = (end - ref).total_seconds() / 60.0
        return (minutes if minutes > 0 else None), label

    # Relative (and anything else that carries plain numbers)
    try:
        to_min = float(str(raw_to).strip())
    except ValueError:
        return None, ""
    try:
        from_min = float(str(raw_from).strip()) if str(raw_from or "").strip() else None
    except ValueError:
        from_min = None
    label = (f"{from_min:g}–{to_min:g} мин" if from_min is not None
             else f"до {to_min:g} мин")
    return to_min, label

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

# /v1/confirm carries the device geo in its anti-fraud block. It is a fraud SIGNAL,
# not a validated field (the captures show it varying — Moscow on one, Petersburg on
# another), and this session has no stored location, so a stable neutral default
# (Moscow centre) is sent; TBANK_GEO_LAT/LON override it.
_CONFIRM_GEO = (os.environ.get("TBANK_GEO_LAT", "55.751244"),
                os.environ.get("TBANK_GEO_LON", "37.618423"))

# Hosts where the real app sends X-App-Name/X-App-Version/X-Platform (capture-
# verified per-host header profile). ONLY these — everywhere else (the BFF
# api.t-bank-app.ru, lifestyle grocery, id, api-invest, ...) the app sends just
# x-lang. Injecting X-App-* on those hosts diverges from the app and BREAKS the
# grocery cart (lifestyle segments carts by client context → set "OK" but the
# goods land in a different bucket → cart reads empty). Keep this list capture-tight.
# Rollback switch for the query scoping below. TBANK_QUERY_PROFILE=legacy restores
# the previous behaviour byte-for-byte (wuid everywhere, vendor/client_version
# injected wherever the session has them) without touching session.json, so
# reverting needs no re-login.
_LEGACY_QUERY = os.environ.get("TBANK_QUERY_PROFILE", "").lower() == "legacy"


# The Accept the app really sends, per host class — capture-verified.
#
# `_NATIVE_ACCEPT` is not a choice the app made: it is the Apple URL-loading
# default that appears when no Accept is set, which is why it is identical across
# every native host (api, api-invest*, my-home, ms-loyalty, shortcuts, …). Where
# the app's own SDKs DO set one — the lifestyle Город/Афиша module, search, id —
# it is application/json. WKWebView-originated calls send */*.
#
# The code sent application/json to all of them. That works today, and the captures
# say why it is safe either way: of the 128 templates present in the captures with
# both sides recorded, every response is application/json regardless of what was
# asked for. The two exceptions are already handled by template-level headers
# (payment_receipt_pdf → application/pdf) or parse fine anyway (/v1/ping answers
# Content-Type: text/html with a JSON body, and _unwrap never looks at the header).
#
# So this is fidelity, not a bug fix — and the direction is toward a header ending
# in */*;q=0.8, a strict superset of application/json. It is OFF by default all the
# same: 63 templates live on the one host that changes, there is no staging
# environment, and the only endpoint whose Content-Type demonstrably moves is the
# free one (`keepalive`) — start the rollout there.
#
#   TBANK_ACCEPT_PROFILE unset | "json"  → today's behaviour, byte-for-byte
#                        "auto"          → the captured profile everywhere
#                        "host,host,…"   → the captured profile on those hosts only
_NATIVE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_JSON_ACCEPT = "application/json"

_HOST_ACCEPT = {
    "lifestyle.t-bank-app.ru": _JSON_ACCEPT,
    "id.t-bank-app.ru": _JSON_ACCEPT,
    "www.tbank.ru": "*/*",
    "webview.t-bank-app.ru": "*/*",
    "trains.t-bank-app.ru": "*/*",
}
# Paths whose host says one thing and whose own capture says another: the lifestyle
# superapp shelf is served by the same host as Город but answers the native default.
_PATH_ACCEPT = {
    ("lifestyle.t-bank-app.ru", "/api/orders/list"): _NATIVE_ACCEPT,
    ("lifestyle.t-bank-app.ru", "/api/order"): _NATIVE_ACCEPT,
    ("lifestyle.t-bank-app.ru", "/api/event/movie"): _NATIVE_ACCEPT,
}


def _accept_profile_hosts() -> set[str] | None:
    """None ⇒ profile off (send json, as before). Empty set ⇒ on everywhere."""
    raw = os.environ.get("TBANK_ACCEPT_PROFILE", "").strip().lower()
    if not raw or raw == "json":
        return None
    if raw == "auto":
        return set()
    return {h.strip() for h in raw.split(",") if h.strip()}


def _accept_for(hostname: str, path: str = "") -> str:
    """The Accept for this host+path. Always returns a value, so the session-level
    default (set on requests.Session at construction) never fills the gap — changing
    only this function would otherwise be a no-op."""
    enabled = _accept_profile_hosts()
    if enabled is None or (enabled and hostname not in enabled):
        return _JSON_ACCEPT
    hit = _PATH_ACCEPT.get((hostname, str(path or "")))
    return hit or _HOST_ACCEPT.get(hostname, _NATIVE_ACCEPT)


def _wants_wuid(host: str, path: str) -> bool:
    """Does the real app send `wuid` to this host+path?

    Only www.tbank.ru, and only under /api/common/. It is absent from all 410
    captured api.t-bank-app.ru requests and all 235 lifestyle ones, and from the
    /api/supreme/lifestyle/* checkout paths on www.tbank.ru itself."""
    hn = (urlparse(host).hostname or host or "").lower()
    return hn == "www.tbank.ru" and str(path or "").startswith("/api/common/")


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


def _reject_unkeyed(items: Any) -> None:
    """Refuse a cart write whose entries carry no ``id``, BEFORE anything is posted.

    The cart loops skipped such an entry silently. cart/set then replaced the cart
    with the unchanged goods list and answered 200 with a goodsSum, so the server
    layer reported success and counted the caller's INPUT — "OK: 3 новых позиций"
    for zero items added. Nothing about that told the caller its key name was wrong.
    Refusing here costs nothing: no request has been made yet."""
    if not isinstance(items, list):
        raise TbankApiError("BAD_ITEMS",
            f"items должен быть списком объектов [{{\"id\": \"123\", \"count\": 1}}], "
            f"а пришло {type(items).__name__}.")
    bad = [it for it in items
           if not isinstance(it, dict) or not str(it.get("id", "") or "").strip()]
    if bad:
        keys = sorted({k for it in bad if isinstance(it, dict) for k in it})
        raise TbankApiError("BAD_ITEMS",
            f"{len(bad)} из {len(items)} позиций без ключа \"id\" — они были бы "
            f"молча пропущены, а корзина осталась бы прежней. "
            f"Нужно [{{\"id\": \"123\", \"count\": 1}}]"
            + (f"; пришли ключи: {', '.join(keys)}." if keys else ".")
            + " id товара берётся из grocery_search / grocery_rank.")


def _next_step_hint(resp: dict) -> str:
    """What the caller must do next, from the `step` the bank names in an
    /auth/step response. Shared by login() and confirm_step() — the latter used to
    dump the raw JSON instead, so the normal otp → password hop surfaced to the
    agent as «API error (NO_CODE): {…}» with the answer inside the blob."""
    step = str((resp or {}).get("step", "") or "")
    tool = {"otp": "confirm_otp(<код из СМС>)",
            "password": "confirm_password(<пароль от аккаунта>)",
            "pin": "confirm_pin(<PIN приложения>)"}.get(step)
    if tool:
        return f"Следующий шаг — {step}. Вызови {tool}."
    return (f"Следующий шаг — '{step or 'неизвестен'}'. Подходящий тул: confirm_otp / "
            f"confirm_password / confirm_pin. Ответ: "
            f"{json.dumps(resp, ensure_ascii=False)[:200]}")


def _wait_for_propagation(probe, *, timeout_s: float = 8.0, interval_s: float = 0.3) -> None:
    """Poll `probe` until it stops raising, or the deadline passes.

    A freshly-minted session needs a moment before mobile reads accept it
    (else INSUFFICIENT_PRIVILEGES) — a blind sleep either waits when it
    didn't need to, or not long enough. This degrades to "waited the full
    deadline and proceeded anyway" on timeout, so it is never worse than the
    blind sleep it replaces."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            probe()
            return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return
        time.sleep(interval_s)


def money_amount(amount) -> float | int:
    """The value that goes into `moneyAmount`, in the form the app sends it.

    Three things happen here, and each has a way of going wrong quietly:

    * NOT FINITE is refused. json.dumps writes NaN and Infinity as bare `NaN` /
      `Infinity` — not JSON at all — and it would go into a SIGNED payment body.
      An amount that came out of arithmetic can be either.
    * QUANTISED to kopecks. A computed amount arrives as 7866.666666666667 and was
      sent verbatim; the duplicate-guard key is built from that string too, so two
      attempts at «the same» payment could stop recognising each other.
    * WHOLE amounts stay integers. All eleven captured bodies — /v1/pay and
      /v1/payment_commission across four capture files — carry
      `"moneyAmount":23600`, never `23600.0`. float(amount) produced the second form.

    Non-positive is left to the callers: each has a better message for it, and
    refusing here would replace «Сумма должна быть больше нуля» with a type error."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise TbankApiError("INVALID_AMOUNT",
                            f"сумма должна быть числом, получено {amount!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise TbankApiError("INVALID_AMOUNT",
                            f"сумма не является конечным числом: {amount!r}")
    # Decimal, not round(). round(100.005, 2) is 100.0, because 100.005 is really
    # 100.00499999999999545 in binary — half a kopeck lost, silently, on money.
    # Quantising the DECIMAL text of the value rounds what the caller meant.
    kopecks = decimal.Decimal(str(value)).quantize(decimal.Decimal("0.01"),
                                                   rounding=decimal.ROUND_HALF_UP)
    return int(kopecks) if kopecks == kopecks.to_integral_value() else float(kopecks)


def _excerpt(text, limit: int = 200) -> str:
    """A cut that says how much it dropped.

    `s[:200]` is indistinguishable from the whole thing, which is the entire
    complaint the truncation audit made about the rest of the codebase — and these
    are DIAGNOSTIC strings, where the missing part is often the interesting part: a
    proxy interstitial cut at 200 characters looks like the server said nothing.
    server._cut marks with a bare «…»; here the size is worth naming, because the
    reader is deciding whether to go and look at the full response."""
    s = str(text or "")
    if len(s) <= limit:
        return s
    return f"{s[:limit]}… (+{len(s) - limit} симв.)"


def _response_filename(headers) -> str:
    """The filename the SERVER states for a download, or ''.

    Three spellings, most reliable first:
      * `x-amz-meta-filename-base64` — the object store's own copy, exact bytes,
        with no quoting or encoding left to interpret. This is what the messenger
        actually sends;
      * `Content-Disposition: …filename*=UTF-8''…` (RFC 5987);
      * `Content-Disposition: …filename="…"`, whose value the messenger
        percent-encodes ("Otchet_%D0%BE%D1%82…"), so it is unquoted either way — a
        name that was NOT encoded contains no '%' and survives unchanged.

    The result is untrusted text like any other bank string: it names nothing on
    disk until the caller has scrubbed it."""
    # requests hands over a case-insensitive mapping, but this must not depend on
    # that: a plain dict from a test or another transport spells its keys however
    # it likes, and a header lookup that misses simply returns no name at all.
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    try:
        raw = lower.get("x-amz-meta-filename-base64") or ""
        if raw:
            return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        pass
    cd = lower.get("content-disposition") or ""
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if not m:
        m = re.search(r'filename\s*=\s*"([^"]*)"', cd, re.I) or \
            re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if not m:
        return ""
    try:
        return urllib.parse.unquote(m.group(1).strip(), errors="strict")
    except (UnicodeDecodeError, ValueError):
        return m.group(1).strip()


def _as_json_envelope(blob: bytes):
    """Parse `blob` as JSON if it plausibly IS one, else None.

    For routes that answer with a document but report failure as JSON. A real
    .xlsx/.pdf/.png starts with its own magic and never parses, so the guards are
    cheap; the size cap keeps a 60MB attachment from being decoded just to find
    out it is not an error object."""
    if not blob or len(blob) > 65536 or blob[:1] not in (b"{", b"["):
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


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


# ---- payment QR (ГОСТ Р 56042-2014) --------------------------------------
#
# The QR printed on every Russian invoice. Header is "ST0001" + one digit naming
# the encoding of the rest (1 = win-1251, 2 = utf-8, 3 = koi8-r), then Key=Value
# pairs joined by "|". `Sum` is in KOPECKS; every other value is text.
#
# The app does not parse it locally — it posts the string untouched to
# /providers/providers/qr/resolve and gets back the `transfer-legal` provider with
# every field pre-filled (captures_payreq.xml #538). We parse it anyway, for two
# reasons: the tool can show the recipient before spending a request, and a QR the
# bank does not recognise still has a readable payee.
QR_PAYMENT_PREFIX = "ST0001"

# QR key (lowercased) → the providerFields id transfer-legal wants. The first seven
# are the bank's OWN mapping, not a reading of the standard: the QR in
# captures_payreq.xml #538 carries exactly Name/PersonalAcc/BankName/BIC/CorrespAcc/
# PayeeINN/KPP/Sum, and the bank echoed each one back as the defaultValue of the
# field named here.
#
# `purpose` is the exception and is marked so on purpose: that QR carried no Purpose
# (the payer typed «За товар» by hand afterwards), so the pairing comes from the
# standard, where Purpose IS назначение платежа, and not from an observed response.
# Nothing rests on it being right — a missing or misread purpose is refused before
# the payment by the provider's own `required` flag, never silently paid.
QR_TO_PROVIDER_FIELD = {
    "name": "addressee",
    "personalacc": "bankAcnt",
    "bankname": "bankName",
    "bic": "bankBik",
    "correspacc": "bankCorrAcnt",
    "payeeinn": "inn",
    "kpp": "kpp",
    "purpose": "comment",       # from the standard; the capture has no Purpose
    # ЖКХ block. The provider publishes `account` («Номер лицевого счета», optional)
    # and the standard puts the payer's personal account in PersAcc — a утилита QR
    # carries it and it was being dropped, so the payment went out with the field
    # the receiving side uses to match the payer left empty.
    "persacc": "account",
}

# The two values the `nds` List field accepts. Not cosmetic: this is the VAT mark
# that goes on the payment order the recipient's bank shows their accountant.
NDS_INCLUDED = "323"      # «НДС включен»
NDS_EXEMPT = "322"        # «НДС не облагается» — the provider's own defaultValue
NDS_LABELS = {NDS_INCLUDED: "НДС включен", NDS_EXEMPT: "НДС не облагается"}


def payment_qr_hash(qr: str) -> str:
    """`barcodeHash` as the app computes it: sha1 over the QR string's utf-8 bytes.

    Verified: sha1(qr.encode("utf-8")).hexdigest() reproduces the hash the app sent
    alongside the QR in captures_payreq.xml #538, byte for byte."""
    return hashlib.sha1(str(qr).encode("utf-8")).hexdigest()


def parse_payment_qr(qr: str) -> dict:
    """Split a ГОСТ Р 56042-2014 payment QR into requisites, WITHOUT the network.

    Returns {"format", "fields", "requisites", "amount", "hash"} where `requisites`
    is already keyed by transfer-legal's own field ids and `amount` is in RUBLES
    (the QR carries kopecks) or None when the QR names no sum — an open invoice the
    payer fills in.

    Raises QR_NOT_PAYMENT for anything that is not this format; a link, a loyalty
    card or an SBP QR would otherwise be silently read as a set of blank requisites."""
    text = (qr or "").strip()
    if not text.upper().startswith(QR_PAYMENT_PREFIX):
        raise TbankApiError("QR_NOT_PAYMENT",
            "это не платёжный QR по ГОСТ Р 56042-2014 — такая строка должна "
            f"начинаться с {QR_PAYMENT_PREFIX} (например ST00012|Name=…|"
            "PersonalAcc=…|BIC=…). Получено: " + (_excerpt(text, 60) or "пусто"))
    head, _, rest = text.partition("|")
    fields: dict[str, str] = {}
    for chunk in rest.split("|"):
        key, sep, value = chunk.partition("=")
        if sep and key.strip():
            fields[key.strip()] = value
    requisites: dict[str, str] = {}
    for key, value in fields.items():
        dst = QR_TO_PROVIDER_FIELD.get(key.lower())
        if dst and value.strip():
            requisites[dst] = value.strip()
    amount = None
    raw_sum = next((v for k, v in fields.items() if k.lower() == "sum"), "")
    # isdecimal, not isdigit: isdigit admits superscripts and circled digits, which
    # int() then refuses — a ValueError out of a parser whose whole job is to refuse
    # cleanly. The kopeck conversion below is only valid for decimal digits anyway.
    if str(raw_sum).strip().isdecimal():
        # Kopecks. Read as rubles this would pay 100× the invoice — worth the
        # explicit conversion and the round(), which keeps 2360000 → 23600.0 and
        # not 23599.999999999996.
        amount = round(int(raw_sum) / 100.0, 2)
    return {"format": head, "fields": fields, "requisites": requisites,
            "amount": amount, "hash": payment_qr_hash(text)}


# Every afisha listing is scoped by a numeric cityId, and the bank publishes no
# directory for it — the app has the mapping compiled in. This table was walked
# live: cityId 1..70 against the venue directory, each id resolved to a name
# through a venue's own schedule. 65 answered; 20, 41, 48, 65 and 68 are holes,
# and the run stopped at 70 with the list continuing alphabetically, so this is
# the popular head rather than everything the bank knows.
#
# It is code, not a data file, so it ships inside the wheel. Anything outside it
# is still reachable — the tools take an explicit city_id — but nothing here is
# guessed from a name.
CITY_IDS = {
    1: "Москва", 2: "Санкт-Петербург", 3: "Краснодар", 4: "Новосибирск",
    5: "Томск", 6: "Вологда", 7: "Чебоксары", 8: "Тольятти", 9: "Пермь",
    10: "Екатеринбург", 11: "Красноярск", 12: "Ростов-на-Дону", 13: "Сочи",
    14: "Ижевск", 15: "Тула", 16: "Набережные Челны", 17: "Казань",
    18: "Хабаровск", 19: "Ульяновск", 21: "Улан-Удэ", 22: "Иркутск", 23: "Уфа",
    24: "Белгород", 25: "Волгоград", 26: "Тюмень", 27: "Ейск", 28: "Кемерово",
    29: "Севастополь", 30: "Нижний Новгород", 31: "Самара", 32: "Челябинск",
    33: "Омск", 34: "Воронеж", 35: "Саратов", 36: "Химки", 37: "Зеленоград",
    38: "Балашиха", 39: "Домодедово", 40: "Красногорск", 42: "Сергиев Посад",
    43: "Люберцы", 44: "Наро-Фоминск", 45: "Мытищи", 46: "Щелково",
    47: "Гатчина", 49: "Барнаул", 50: "Абакан", 51: "Альметьевск",
    52: "Армавир", 53: "Архангельск", 54: "Астрахань", 55: "Балаково",
    56: "Бийск", 57: "Биробиджан", 58: "Брянск", 59: "Великий Новгород",
    60: "Владивосток", 61: "Владикавказ", 62: "Владимир", 63: "Волгодонск",
    64: "Грозный", 66: "Иваново", 67: "Йошкар-Ола", 69: "Калининград",
    70: "Калуга",
}


def _norm_city(s: str) -> str:
    return str(s).strip().lower().replace("ё", "е")


_CITY_BY_NAME = {_norm_city(n): i for i, n in CITY_IDS.items()}
# What people actually type.
_CITY_BY_NAME.update({"спб": 2, "питер": 2, "санкт петербург": 2, "мск": 1,
                      "екб": 10, "нижний": 30, "ростов": 12, "н.новгород": 30})


def city_id_of(city: str = "", city_id: int | str = 0) -> str:
    """Numeric cityId for an afisha call, as the string the bodies carry.

    An explicit city_id always wins — it is the escape hatch for a city outside
    the table. A name that is not in the table RAISES rather than falling back to
    Moscow: a Moscow listing answering a question about Kazan looks entirely
    plausible and is entirely wrong, which is the one failure mode worth an error."""
    if str(city_id).strip() not in ("", "0"):
        return str(city_id).strip()
    if not str(city).strip():
        raise TbankApiError("CITY_REQUIRED",
                            "не назван город; передай city или city_id")
    found = _CITY_BY_NAME.get(_norm_city(city))
    if found:
        return str(found)
    near = [n for n in CITY_IDS.values()
            if _norm_city(city)[:4] and _norm_city(city)[:4] in _norm_city(n)]
    raise TbankApiError(
        "UNKNOWN_CITY",
        f"города {city!r} нет в таблице cityId"
        + (f"; похожие: {', '.join(near[:5])}" if near else "")
        + ". Если он есть в банке, передай city_id числом.")


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", " ": "_",
    "-": "-",
}


def translit_city(s: str) -> str:
    """City name in the spelling the `Segodnya-v_kino_*` shelf codes use.

    This is a reconstruction of the server's own convention, not a guess at a
    name: the live shelf lists hand back exactly Segodnya-v_kino_Sankt-Peterburg,
    Segodnya-v_kino_Sochi, Skoro-v_kino_Kazan. It stays only as the fallback for
    when the shelf list comes back empty — which the Moscow one does when that
    backend is having a moment, and Moscow is the last city where guessing wrong
    would be noticed late.

    It does NOT generalise to other shelf families: those spell the same city
    Moskva, moscow and msk depending on the shelf, so nothing but the server's
    own list can be trusted for them."""
    out = "".join(_TRANSLIT.get(ch, _TRANSLIT.get(ch.lower(), ch))
                  if not ch.isascii() else ch for ch in s)
    return re.sub(r"(^|[_\-])([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), out)


def vertical(kind: str) -> dict:
    """The VERTICALS row for `kind`, or an error naming what is accepted.

    Every afisha call used to pick its path with `"concert" if kind == "concert"
    else <movie>`, so a typo — or a vertical nobody had wired up yet — silently
    booked a cinema seat instead of failing. An unknown kind raises here."""
    key = VERTICAL_ALIASES.get(str(kind).strip().lower())
    if not key:
        raise TbankApiError(
            "UNKNOWN_KIND",
            f"unknown kind {kind!r}; use one of: {', '.join(VERTICALS)} "
            f"(кино, концерт, театр, выставка)")
    return VERTICALS[key]


@dataclass
class MobileSession:
    # tokens (rotate on refresh)
    mobile_sessionid: str
    refresh_token: str
    access_token: str = ""
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
    trains_cookie: str = ""         # rail host cookie (trains.t-bank-app.ru)
    trains_cookie_at: float = 0.0   # when it was minted (unix seconds)
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
                ("model", os.environ.get("TBANK_DEVICE_MODEL")),
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
        if self._wide_cookie():
            headers["Cookie"] = self._wide_cookie()
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
        if not self._needs_mint(max_age_s):
            return
        with _MINT_LOCK:
            # Checked again INSIDE the lock. Without this the lock only queues the
            # threads: each would take its turn and re-mint, rotating the
            # refresh_token once per waiter instead of once in total.
            if not self._needs_mint(max_age_s):
                return
            try:
                self.refresh()
            except Exception:
                if not (self.sso_login_cookie and self.auth_step_fingerprint):
                    raise
                self.silent_relogin()

    def _needs_mint(self, max_age_s: int = 6000) -> bool:
        return (self._minted_at == 0
                or time.time() - self._minted_at
                > min(max_age_s, max(60, self.expires_in - 600)))

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

    def _mobile_headers(self, host_url: str = "", path: str = "") -> dict:
        """Mobile-client headers the real app sends, derived from session attrs.
        ``X-Lang``/``Accept-Language``/``Accept``/mobile ``User-Agent`` are sent on
        basically every API host → injected always. But ``X-App-Name``/``Version``/
        ``Platform`` are sent ONLY on ``_STRICT_XAPP_HOSTS`` (capture-verified
        per-host profile). Injecting them elsewhere diverges from the app and breaks
        the grocery cart on lifestyle. An explicit template header still wins
        (setdefault below)."""
        hn = (urlparse(host_url).hostname or host_url or "").lower()
        h: dict[str, str] = {"X-Lang": "ru", "Accept-Language": "ru",
                             "Accept": _accept_for(hn, path)}
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
        # The messenger host has its own UA header, and it is NOT the same string:
        # bundle:appVersion; sdk; iOS:version; device:model. Derived here so it
        # cannot drift from _IOS_VERSION the way the frozen template literal did
        # (it claimed iOS 17.5.1 and carried no device segment at all).
        if hn == "tm.t-bank-app.ru" and self.app_version:
            h["Tmsg-User-Agent"] = (
                f"com.idamob.tinkoff.android:{self.app_version}; "
                f"tmsg-sdk-iOS:1.0.0; iOS:{_IOS_VERSION}; device:{self.device_model}")
        return h

    def _call_read(self, template_key: str, *, overrides: dict | None = None,
                   body: dict | list | None = None,
                   path_override: str | None = None,
                   return_response: bool = False) -> Any:
        """Replay a read endpoint (builtin shape) with fresh sessionid + Bearer.

        path_override replaces the path (for parameterized endpoints like
        messenger conversations/{id}/messages)."""
        tpl = self._tpl(template_key)
        params = {k: v for k, v in tpl.get("params", {}).items()
                  if k not in _LIVE_QUERY}
        # Most hosts read the mobile sessionid from `sessionid`; the prefill-profile
        # and insurance hosts spell it `sessionId` and reject the lowercase form.
        # Some hosts want none of the native client context. The webview-served
        # ones carry only appName/appVersion/platform and answer 400 to the rest,
        # which is the same class of divergence that once broke the lifestyle cart:
        # sending what the app does not send is not free.
        lean = bool(tpl.get("no_base_params"))
        if not lean:
            params[tpl.get("session_param") or "sessionid"] = self.mobile_sessionid
            params["deviceId"] = self.device_id
            params["oldDeviceId"] = self.old_device_id or self.device_id
        elif tpl.get("session_param"):
            # A lean host may still key its session off ONE named query param.
            params[tpl["session_param"]] = self.mobile_sessionid
        host = tpl.get("host") or self.base_url
        path = path_override or tpl["path"]
        # wuid is the WEB portal's device identifier. It went on every request to
        # every host; the app sends it only to www.tbank.ru, and only under
        # /api/common/ (never on the /api/supreme/lifestyle/* checkout paths). On a
        # native call it is a value the app never puts there — the same reasoning
        # that already removed it from /v1/pay.
        if _LEGACY_QUERY or _wants_wuid(host, path):
            params["wuid"] = self.device_id
        # inject the common base params from the session if not in the template
        # (so builtin endpoints with minimal params still send appName/origin/etc.)
        # inache is the app's routing/feature flag (constant "drivetransitt") — the
        # real client sends it on EVERY request; centralizing it here (default in the
        # dataclass) closes the gap for the ~8 templates that had empty params and
        # omitted it (cars, finhealth presets, my_home, payment_shortcuts, ...).
        # vendor/client_version are NOT here: they belong to the OIDC authorize call,
        # which builds its own query and never reaches _call_read, so injecting them
        # was pure divergence on every other host.
        base = [("appName", self.app_name), ("appVersion", self.app_version),
                ("origin", self.origin), ("platform", self.platform),
                ("ccc", self.ccc), ("cpswc", self.cpswc),
                ("connectionType", self.connection_type),
                ("inache", self.inache)]
        if _LEGACY_QUERY:
            base += [("vendor", self.vendor), ("client_version", self.client_version)]
        for k, v in base:
            if v and k not in params and not lean:
                params[k] = v
        if overrides:
            params.update(overrides)
        headers = {k: v for k, v in tpl.get("headers", {}).items()
                   if k.lower() not in _LIVE_HEADERS}
        # Inject the mobile-client headers the real app sends on this host:
        # x-lang/Accept-Language/Accept/UA always; X-App-Name/Version/Platform ONLY
        # on _STRICT_XAPP_HOSTS (elsewhere the app sends just x-lang — injecting
        # X-App-* there breaks the lifestyle grocery cart). setdefault ⇒ an explicit
        # template header or Authorization/Cookie below still wins.
        for k, v in self._mobile_headers(host, path).items():
            headers.setdefault(k, v)
        # A few hosts are authorised by cookie alone and the app sends no Bearer to
        # them at all; carrying one there is another silent divergence.
        if not tpl.get("no_bearer"):
            headers["Authorization"] = "Bearer " + self.access_token
        cookie = self._cookie_for(host)
        if cookie:
            headers["Cookie"] = cookie
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
        if return_response:
            # The caller wants the response itself, not a parsed body: a download
            # whose FILENAME lives in the headers, not in the bytes. Status handling
            # is theirs too — this returns 401s and 500s unraised.
            return r
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

    # The hardware identifier. Deliberately NOT in PAY_DEVICE_DEFAULTS: that dict is
    # spread wholesale into the /v1/pay query AND form (see _signed_parts), and no
    # captured pay request carries a `model` key — putting it there would fix
    # card_credentials by adding a new divergence to the money path.
    DEVICE_MODEL = "iPhone18,4"

    @property
    def device_model(self) -> str:
        """Hardware identifier for the endpoints that ask for one (card_credentials,
        the messenger's Tmsg-User-Agent). TBANK_DEVICE_MODEL overrides it."""
        return str((getattr(self, "device_profile", None) or {}).get("model")
                   or self.DEVICE_MODEL)

    @property
    def PAY_DEVICE_PROFILE(self) -> dict:
        """The full block: constants + this session's device facts."""
        # getattr, not self.device_profile: __post_init__ sets it, and the test
        # sessions build the object without running it.
        override = getattr(self, "device_profile", None) or {}
        # `model` is filtered out on purpose — it belongs to the device, not to the
        # payment block, and no captured /v1/pay carries it.
        return {**self.PAY_DEVICE_CONSTANTS, **self.PAY_DEVICE_DEFAULTS,
                **{k: str(v) for k, v in override.items() if v and k != "model"}}

    def _call_signed(self, template_key: str, body_str: str,
                     extra_query: dict | None = None) -> Any:
        """POST a signed request (private; only pay_execute/human use)."""
        url, headers, body_str = self._signed_parts(template_key, body_str, extra_query)
        r = self._http.post(url, data=body_str, headers=headers, timeout=30)
        return self._unwrap(r)

    # NOTE: `pay` is a REAL money-moving operation, used by transfer()/pay_bill()'s
    # MCP tools. It is NOT test-called by the assistant — only invoked deliberately.
    # payment_gate_pay/grocery_order_create/checkout_process_order (below) are NOT
    # on this path: grocery_checkout drives checkout.py's own Playwright-based
    # fetches instead, and these three client methods have no caller at all.

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
        # `:` is safe too: both captured /v1/pay requests send the notificationUrl's
        # scheme separator literally in the QUERY (`https://…`), while the copy in
        # the form body is percent-encoded — the app really does differ between the
        # two. The signature covers whatever we send, so this is fidelity, not a fix.
        query = urllib.parse.urlencode(params, safe="%/,:")
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
        for k, v in self._mobile_headers(host, path).items():
            headers.setdefault(k, v)
        # The captured pay asks for the native default. Set explicitly rather than
        # left to _accept_for, because this one is verified against the capture and
        # must not follow the profile switch in either direction.
        headers["Accept"] = _NATIVE_ACCEPT
        headers["Authorization"] = "Bearer " + self.access_token
        headers["x-api-signature"] = sig
        if self._wide_cookie():
            headers["Cookie"] = self._wide_cookie()
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
        if self._wide_cookie():
            headers["Cookie"] = self._wide_cookie()
        r = self._http.post(url, json={"ssoToken": self.access_token},
                           headers=headers, timeout=30)
        data = self._unwrap(r)
        jwt = ""
        if isinstance(data, dict):
            jwt = data.get("jwt", "") if "jwt" in data else (data.get("result", {}) or {}).get("jwt", "")
        if jwt:
            self.tmsg_session_id = jwt
        return jwt

    def _wide_cookie(self) -> str:
        """cookie_str, narrowed to what every host is supposed to receive.

        Applied where the value is USED, not only where it is assigned: a
        session.json written before this existed still holds the whole login jar,
        and it is loaded straight into the field."""
        return wide_cookies(self.cookie_str)

    def _cookie_for(self, host: str) -> str:
        """The Cookie header this host expects, or "".

        Hosts do not agree on how the session reaches them, and the disagreement
        is not a detail: the messenger accepts ONLY its own minted JWT, and
        sending it the SSO cookie instead of that authorises nothing. Keeping the
        per-host answer in one place is what stops a new host from inheriting
        whichever branch happened to be last."""
        if "tm.t-bank-app.ru" in host:
            # Minted on demand from the access_token via issueTokenBySSO.
            self._ensure_tmsg()
            return f"tmsgSessionID={self.tmsg_session_id}" if self.tmsg_session_id else ""
        if "trains.t-bank-app.ru" in host:
            self._ensure_trains()
            return self.trains_cookie
        if "webview.t-bank-app.ru" in host:
            # The shopping webview sends no Authorization at all — 179 captured
            # requests, not one Bearer — and authorises on cookies whose sessionID
            # and sso_api_session both hold the very access_token we already have.
            # deviceId rides uppercase there, as the app writes it.
            return (f"sessionID={self.access_token}; "
                    f"sso_api_session={self.access_token}; "
                    f"deviceId={(self.device_id or '').upper()}")
        return self._wide_cookie()

    TRAINS_TTL = 3600.0     # the Set-Cookie expiry runs ~2h; re-mint well inside it

    def _ensure_trains(self) -> None:
        """Mint the rail host's own cookie, in an ISOLATED jar.

        One request does it: GET https://trains.t-bank-app.ru/ with the ordinary
        mobile Bearer answers with Set-Cookie carrying sessionID and the travel
        session id, and the search API accepts those.

        The isolation is not tidiness. That same response also clears the cookie
        for the tbank.ru domain, so minting this inside the shared jar would race
        every other host mid-flight."""
        if self.trains_cookie and time.time() - self.trains_cookie_at < self.TRAINS_TTL:
            return
        jar = requests.Session()
        try:
            from . import tls as _tls
            _tls.rebuild_bundle()
            jar.mount("https://", _tls.RobustTLSAdapter())
            jar.verify = _tls.BUNDLE
        except Exception:
            pass
        jar.get("https://trains.t-bank-app.ru/",
                params={"iswebview": "true", "os": "ios", "language": "ru",
                        "appName": self.app_name, "appVersion": self.app_version},
                headers={"Authorization": "Bearer " + self.access_token,
                         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                         "User-Agent": self._mobile_ua() or "okhttp/4.12.0"},
                timeout=30, allow_redirects=True)
        got = jar.cookies.get_dict()
        wanted = [f"{k}={v}" for k, v in got.items()
                  if k in ("sessionID", "SSO_ID", "_T_travel_session_id",
                           "SSO_ID_TOKEN", "SSO_VALIDATION")]
        if wanted:
            self.trains_cookie = "; ".join(wanted)
            self.trains_cookie_at = time.time()
            self._persist()

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
              f"&grant_type=authorization_code&appVersion={self.app_version or APP_VERSION}"
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
        self._minted_at = time.time()
        self.tmsg_session_id = ""  # force tmsg re-mint with the fresh access_token
        # the freshly-minted session needs a moment to propagate before mobile
        # reads accept it (else INSUFFICIENT_PRIVILEGES) — poll instead of a
        # blind wait, since most of the time it's ready sooner than 3s.
        _wait_for_propagation(self.keepalive)
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
        return _next_step_hint(r2j)

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
        # if error in the response, raise with full detail — redact BEFORE the
        # 300-char cut, not after: a token/phone that lands near the boundary
        # would otherwise survive as a truncated (still readable) fragment.
        if rj.get("error"):
            raise TbankApiError(str(rj.get("error")),
                                json.dumps(_redact_value(rj), ensure_ascii=False)[:300])
        code = rj.get("code")
        if not code:
            # Not an error: the login is alive and the bank named the NEXT step in
            # the response. This used to dump the raw JSON under "NO_CODE", so the
            # ordinary first-device flow (otp → password) read to the agent as a
            # failure, with the tool it needed to call next sitting unread in the
            # blob. login() has parsed the same field all along.
            raise TbankApiError("NEXT_STEP", _next_step_hint(rj))
        # exchange the code for the mobile session
        tb = (f"device_id={self.device_id}&client_version={self.client_version}"
              f"&grant_type=authorization_code&appVersion={self.app_version or APP_VERSION}"
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
        self._minted_at = time.time()
        # capture the SSO_SESSION cookie from the jar (set during login) for
        # silent re-login + messenger.
        self.sso_login_cookie = "; ".join(
            f"{c.name}={c.value}" for c in self._http.cookies
            if c.domain and "t-bank-app.ru" in c.domain)
        # NOT the whole jar. sso_login_cookie keeps every cookie because
        # silent_relogin replays it against id.t-bank-app.ru, which is the one host
        # that issued SSO_SESSION and the one host that should ever see it again.
        self.cookie_str = wide_cookies(self.sso_login_cookie)
        self.tmsg_session_id = ""
        self._login_cid = self._login_token = ""
        _wait_for_propagation(self.keepalive)  # propagation, like silent_relogin
        return tok

# ---- messenger / support chat (tm.t-bank-app.ru) — Bearer+cookie, no sig ----

    # The messenger answers an expired token with HTTP 200 and an error object in
    # the BODY: [{"errorCode": "AUTH_REQUIRED", "errorMessage": "Token inactive"}].
    # It is a list, so it flowed straight through as if it were the conversations,
    # and the tool rendered it as one chat with an empty id — an error displayed as
    # content, which is the worst way to fail: nothing to retry, nothing to read.
    _TMSG_AUTH_CODES = {"AUTH_REQUIRED", "TOKEN_EXPIRED", "UNAUTHORIZED"}

    # conversation_id/message_id are agent-supplied arguments — possibly copied
    # from text the agent just read in a chat message — spliced unvalidated and
    # unencoded into an f-string request path below. Real ids are alphanumeric
    # (plus hyphen/underscore); this is the one choke point that rejects a
    # path-breaking or injected value (e.g. containing "/", "..", "?", "#")
    # before it ever reaches a URL.
    _SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

    @staticmethod
    def _safe_id(value: str, what: str) -> str:
        v = str(value or "")
        if not MobileSession._SAFE_ID_RE.match(v):
            raise TbankApiError("BAD_ID", f"{what} содержит недопустимые символы: {v!r}")
        return v

    @staticmethod
    def _tmsg_auth_error(data) -> str:
        rec = data[0] if isinstance(data, list) and data else data
        if isinstance(rec, dict) and rec.get("errorCode") in MobileSession._TMSG_AUTH_CODES:
            return str(rec.get("errorMessage") or rec["errorCode"])
        return ""

    @staticmethod
    def _tmsg_error(data) -> tuple[str, str]:
        """(code, message) for ANY messenger error record, ('','') otherwise.

        The auth subset above earns a token re-mint; every other errorCode has to
        surface as an error all the same. _as_list wraps a lone error dict as
        [error], and the renderers do not check that an element looks like a
        message — so a 404 {"errorCode":"CONVERSATION_NOT_FOUND"} was PRINTED as a
        chat message. That is worse than an empty list: there is nothing to retry
        and nothing to read, and the agent believes it read the conversation."""
        rec = data[0] if isinstance(data, list) and data else data
        if isinstance(rec, dict) and rec.get("errorCode"):
            return str(rec["errorCode"]), str(rec.get("errorMessage") or "")
        return "", ""

    def _messenger_read(self, *, path_override=None, overrides=None, key="messenger_base"):
        """One messenger read, re-minting the token if the server says it is dead.

        _tmsg_expired() only decodes the JWT's own `exp`, so a token the SERVER has
        invalidated early still looks fine locally and no re-mint is attempted."""
        data = self._call_read(key, path_override=path_override, overrides=overrides)
        why = self._tmsg_auth_error(data)
        if not why:
            code, msg = self._tmsg_error(data)
            if code:
                raise TbankApiError(code, msg)
            return data
        self.tmsg_session_id = ""             # force a re-mint, then try once more
        self._ensure_tmsg()
        data = self._call_read(key, path_override=path_override, overrides=overrides)
        why = self._tmsg_auth_error(data)
        if why:
            raise SessionExpired("TMSG_AUTH_REQUIRED",
                f"Мессенджер отклонил токен даже после переоформления ({why}). "
                f"Вызови refresh_session() и повтори.")
        return data

    def _messenger_write(self, *, path_override, body, key="messenger_send"):
        """One messenger WRITE (POST/PUT with a body), with the SAME verdict and
        token re-mint the reads get through _messenger_read.

        The writes used to call _call_read directly and skip all of it. The messenger
        signals a dead token with HTTP 200 and [{"errorCode":"AUTH_REQUIRED"}] in the
        body (see _tmsg_auth_error) — for a read that surfaced as an empty list; for a
        SEND it flowed back as a success with no message id and was reported to the
        user as «Отправлено», while nothing reached the support agent. A send is
        irreversible and read by a person: it has to fail loudly, not silently no-op.
        So the auth shape earns one re-mint and retry, every other errorCode raises,
        and only a clean response returns."""
        data = self._call_read(key, body=body, path_override=path_override)
        why = self._tmsg_auth_error(data)
        if not why:
            code, msg = self._tmsg_error(data)
            if code:
                raise TbankApiError(code, msg)
            return data
        self.tmsg_session_id = ""             # force a re-mint, then try once more
        self._ensure_tmsg()
        data = self._call_read(key, body=body, path_override=path_override)
        why = self._tmsg_auth_error(data)
        if why:
            raise SessionExpired("TMSG_AUTH_REQUIRED",
                f"Мессенджер отклонил токен даже после переоформления ({why}). "
                f"Вызови refresh_session() и повтори.")
        code, msg = self._tmsg_error(data)
        if code:
            raise TbankApiError(code, msg)
        return data

    def messenger_conversations(self, archived: bool = False, offset: int = 0) -> list[dict]:
        ov = {"use_is_archived": str(archived).lower(), "offset": str(offset)}
        return self._as_list(self._messenger_read(
            path_override="/app/bank/messenger/conversations/mobile", overrides=ov))

    def messenger_messages(self, conversation_id: str, direction: str = "before",
                           message_id: str = "") -> list[dict]:
        conversation_id = self._safe_id(conversation_id, "conversation_id")
        ov = {"direction": direction}
        if message_id:
            ov["messageId"] = self._safe_id(message_id, "message_id")
        return self._as_list(self._messenger_read(overrides=ov,
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages"))

    def messenger_hints(self, conversation_id: str) -> list[dict]:
        conversation_id = self._safe_id(conversation_id, "conversation_id")
        return self._as_list(self._messenger_read(
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/hints"))

    def messenger_faq(self, conversation_id: str) -> list[dict]:
        conversation_id = self._safe_id(conversation_id, "conversation_id")
        return self._as_list(self._messenger_read(
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/faq"))

    def messenger_unread(self) -> dict:
        """Conversations with unread messages. Uses its OWN template, not
        messenger_base: this path content-negotiates and 406s on the generic
        `application/json` header. Returns {groups, conversationIds, screens}."""
        # Through _messenger_read, not _call_read: this path used to skip both the
        # auth detection and the token re-mint, and then coerced anything that was
        # not a dict to {} — so the documented rejection shape (HTTP 200 with
        # [{"errorCode":"AUTH_REQUIRED"}], a LIST) became «Непрочитанных сообщений
        # нет.» with nothing to retry and nothing to read.
        data = self._messenger_read(key="messenger_unread")
        if not isinstance(data, dict):
            raise TbankApiError("MESSENGER_BAD_SHAPE",
                f"мессенджер ответил не объектом: {_excerpt(data)}")
        return data

    def messenger_send_message(self, conversation_id: str, body: dict | None = None) -> dict:
        """POST a message to a conversation (WRITE). Replays the request body or override."""
        conversation_id = self._safe_id(conversation_id, "conversation_id")
        return self._messenger_write(body=body,
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages")

    def messenger_mark_read(self, conversation_id: str, message_id: str) -> Any:
        """Mark one message read. Its own template, not messenger_base: the captured
        request is a PUT with markRead's own vendor content types, while
        messenger_base is a GET that would send `application/json` — the same
        content-negotiation mistake that made messenger_unread answer 406."""
        conversation_id = self._safe_id(conversation_id, "conversation_id")
        message_id = self._safe_id(message_id, "message_id")
        return self._messenger_write(key="messenger_mark_read", body=None,
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages/{message_id}/markRead")

    # A fileId is base64-ish and may carry '=' padding, which _SAFE_ID_RE rejects.
    # '=' is a sub-delim: legal inside a path segment and unable to break out of
    # one, unlike the '/', '?', '#' and '..' that stay excluded.
    _SAFE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_=-]{1,256}$")

    def messenger_file(self, conversation_id: str, file_id: str) -> tuple[bytes, str]:
        """(bytes, filename) for one chat attachment — content.fileId of a
        messageType="file" message.

        The name comes from the RESPONSE, not from the caller. The message record
        carries a fileName, but making the agent copy it back in was a round trip
        through the model for a value the server states itself — twice, in fact:
        `x-amz-meta-filename-base64` (exact bytes, no quoting to get wrong) and
        percent-encoded in Content-Disposition. It is still untrusted text: the
        caller sanitises it before it becomes a path.

        The pair is the key: a fileId is scoped to its conversation, and the same
        fileId under another of the user's own conversations answers 401.

        Not through _messenger_read, which parses JSON — this route returns the raw
        document. But it shares the messenger's worst trait: a dead token comes back
        as HTTP 200 with a JSON error envelope in the body. Unchecked, those 119
        bytes are what gets written to disk and reported as the file — a saved error
        that opens as a corrupt document. So the envelope is detected in the bytes,
        and an auth failure earns the same single re-mint as every other messenger
        read before it is called expired."""
        conversation_id = self._safe_id(conversation_id, "conversation_id")
        if not self._SAFE_FILE_ID_RE.match(str(file_id or "")):
            raise TbankApiError("BAD_ID", f"file_id содержит недопустимые символы: {file_id!r}")
        path = f"/app/bank/messenger/conversations/{conversation_id}/files/{file_id}"

        def _get():
            r = self._call_read("messenger_file", path_override=path,
                                return_response=True)
            st = getattr(r, "status_code", 200)
            if st in (401, 403):
                raise TbankApiError("NOT_AUTHORIZED",
                    "Мессенджер не отдал файл: fileId не принадлежит этому чату. "
                    "conversation_id и file_id должны быть из ОДНОГО сообщения "
                    "(messenger_messages).")
            if st == 500:
                raise TbankApiError("FILE_NOT_FOUND",
                    "Мессенджер ответил 500 — так он отвечает на несуществующий "
                    "fileId. Возьми fileId из messenger_messages.")
            if st >= 400:
                raise TbankApiError(f"HTTP_{st}", _excerpt(getattr(r, "text", "")))
            return r

        r = _get()
        if self._tmsg_auth_error(_as_json_envelope(r.content)):
            self.tmsg_session_id = ""
            self._ensure_tmsg()
            r = _get()
            why = self._tmsg_auth_error(_as_json_envelope(r.content))
            if why:
                raise SessionExpired("TMSG_AUTH_REQUIRED",
                    f"Мессенджер отклонил токен даже после переоформления ({why}). "
                    f"Вызови refresh_session() и повтори.")
        blob = r.content or b""
        code, msg = self._tmsg_error(_as_json_envelope(blob))
        if code:
            raise TbankApiError(code, msg)
        if not blob:
            raise TbankApiError("EMPTY_FILE", "Мессенджер отдал пустой файл (0 байт).")
        return blob, _response_filename(r.headers)

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
        """The brokerage and InvestBox accounts, unwrapped from payload.accounts.

        _as_list knows about `list` and `payload`; this endpoint answers
        {"accounts": [...]}, which it therefore returned as ONE element — the whole
        envelope — so the caller printed a single row with no brokerAccountId. That
        id is the only argument invest_portfolio/operations/securities take, so the
        entire investment side was unreachable through its own entry point while
        get_data("invest_accounts") held the same data all along.

        Unwrapped here rather than by teaching _as_list about "accounts": the shape
        belongs to this endpoint, and a generic rule would start guessing at every
        other payload that happens to carry a key by that name."""
        data = self._call_read("investbox_accounts")
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return [a for a in data["accounts"] if isinstance(a, dict)]
        return self._as_list(data)

    def invest_portfolio(self, broker_account_id: str, date_from: str, date_to: str,
                          currency: str = "RUB", resolution: str = "MONTH") -> dict:
        """Portfolio statistics. `date_from`/`date_to` are ISO **DATES** (2026-01-31).

        They used to be passed the millisecond timestamps every other read here
        takes, and /api/v1/user/portfolio/statistics answered 400 «неверный формат
        входных данных» — which _json_out then printed as if it were the portfolio.
        The captured call also carries resolution and include_cash_in_periods; without
        them the response has no `dates` series at all."""
        return self._call_read("ca_portfolio_statistics",
                                overrides={"brokerAccountId": broker_account_id,
                                           "from": date_from, "to": date_to,
                                           "currency": currency,
                                           "resolution": resolution,
                                           "include_cash_in_periods": "true"})

    # A generous stand-in for "everything" when limit<=0 — the bank has no
    # actual "all" mode, so a large upstream ask is the closest honest answer,
    # same idea as grocery_search's maxObjectsCount: max(30, limit).
    OPERATIONS_ALL_LIMIT = 200

    def invest_operations(self, broker_account_id: str, operation_type: str = "",
                           limit: int = 50) -> tuple[list[dict], bool]:
        """(operations, has_next). has_next comes from the answer's envelope — the
        request itself is untouched (no cursor param: its wire name is not in any
        capture; raising `limit` is the confirmed way to see more).

        limit<=0 sends OPERATIONS_ALL_LIMIT upstream, not a literal 0 — the bank
        does not treat 0 as "no cap", it treats it as "almost nothing" (one
        malformed row instead of the real history, confirmed live)."""
        upstream_limit = limit if limit > 0 else self.OPERATIONS_ALL_LIMIT
        ov = {"brokerAccountId": broker_account_id, "limit": str(upstream_limit)}
        if operation_type:
            ov["operationType"] = operation_type
        data = self._call_read("ca_operations", overrides=ov)
        # {"items": [...], "hasNext": …, "nextCursor": …} — _as_list does not know
        # this key and returned the envelope as a single element, so the caller
        # printed one row reading «- [] ? |». hasNext used to be dropped here too,
        # so the tool could not say the bank was holding more operations back.
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return ([o for o in data["items"] if isinstance(o, dict)],
                    bool(data.get("hasNext")))
        return self._as_list(data), False

    def invest_securities(self, broker_account_id: str = "") -> list[dict]:
        """Positions, as [{brokerAccountId, name, positions: [...]}, …].

        The endpoint takes NO account filter — the captured call sends only sort
        parameters and returns every portfolio. Passing brokerAccountId to it did not
        error, it just came back with `portfolios: []`, so the tool reported an empty
        portfolio for an account holding millions. Filtering is ours, done here."""
        data = self._call_read("purchased_securities",
                               overrides={"stocksSort": "by_name", "stocksSortOrder": "asc",
                                          "bondsSort": "by_name", "bondsSortOrder": "asc",
                                          "etfSort": "by_name", "etfSortOrder": "asc"})
        out = []
        for p in (data.get("portfolios") or []) if isinstance(data, dict) else []:
            acc = (p.get("brokerAccount") or {}) if isinstance(p, dict) else {}
            if broker_account_id and str(acc.get("brokerAccountId")) != str(broker_account_id):
                continue
            out.append({"brokerAccountId": acc.get("brokerAccountId"),
                        "name": acc.get("name") or acc.get("brokerCyrillic") or "",
                        "positions": [x for x in (p.get("positions") or [])
                                      if isinstance(x, dict)]})
        return out

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
                     **({"Cookie": self._wide_cookie()} if self._wide_cookie() else {})},
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

    def qr_providers(self, qr: str) -> list[dict]:
        """Ask the bank what a scanned QR means. Read-only, no money.

        Answers with the provider records that can pay it — for an invoice QR that
        is `transfer-legal`, with every field carrying the `defaultValue` the bank
        itself read out of the QR. That is worth having even though parse_payment_qr
        reads the same string locally: the bank's answer is what proves the QR is
        payable at all, and it names the provider for QRs that are not invoices.

        The app sends the same three values in the query AND as the JSON body
        (captures_payreq.xml #538); both are reproduced rather than picking the one
        that happens to work, because which one the gate reads is not observable."""
        params = {"barcodeHash": payment_qr_hash(qr), "qr": str(qr),
                  "frontendFeatureFlag": "SHAWithSubs"}
        res = self._call_read("resolve_payment_qr", body=dict(params),
                              overrides=dict(params))
        # _unwrap already peels the `payload` envelope; keep the .get for the shape
        # where it does not (a bare providersList).
        data = res if isinstance(res, dict) else {}
        data = data.get("payload", data) if isinstance(data.get("payload"), dict) else data
        providers = (data.get("providersList") or {}).get("providers")
        return [p for p in (providers or []) if isinstance(p, dict)]

    def bank_by_bik(self, bik: str) -> dict:
        """Bank name + correspondent account for a БИК (GET /v1/bank_info?bik=…).

        The app calls this the moment a QR resolves, and it is the reason a payment
        by hand-typed requisites needs only the БИК: the corr account and the bank's
        legal name come back from here rather than from the payer's memory.

        Separate from bank_info(), which takes no argument and answers with the
        bank's own branches/contacts."""
        digits = re.sub(r"\D", "", str(bik or ""))
        if len(digits) != 9:
            raise TbankApiError("INVALID_BIK",
                f"БИК — это 9 цифр, получено {bik!r}")
        return self._call_read("bank_info", overrides={"bik": digits}) or {}

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
        """Payment provider groups — 19 of them live («ЖКХ», «Мобильная связь»,
        «Госуслуги», …).

        The payload nests them: the endpoint answers a LIST whose single element is
        {"groups": [...]}, so `_as_list` alone handed back that wrapper and every
        caller read zero groups. Verified live 2026-07-26."""
        data = self._call_read("providers_groups")
        for item in self._as_list(data):
            if isinstance(item, dict) and isinstance(item.get("groups"), list):
                return [g for g in item["groups"] if isinstance(g, dict)]
        # Already flat (or an unexpected shape) — return what is usable.
        return [g for g in self._as_list(data)
                if isinstance(g, dict) and (g.get("name") or g.get("id"))]

    def providers_compatible_page(self, group: str = "", page: int = 1,
                                  page_size: int = 100) -> dict:
        """One page of the provider catalogue, with each provider's FIELD SCHEMA.

        `group` is a group NAME as `providers_groups()` prints it («Переводы»,
        «ЖКХ», …), not an id. Returns the raw providersPage:
        {page, providers[], totalPages, totalProviders, storageId, updateTime}.

        Each provider carries `fields[]`, and every field has `id`, `name`,
        `regexp`, `hint`, `keyboard`, `type` and `usageTypes[]` — the last one is
        what says whether the field is required for a payment (`code: "Pay"`) or
        only for saving a template. That schema is the only thing that makes a
        composed payment checkable before it is sent."""
        ov = {"page": str(page), "pageSize": str(page_size)}
        if group:
            ov["groups"] = self.GROUP_ALIASES.get(group.strip(), group.strip())
        data = self._call_read("providers_compatible_page", overrides=ov)
        if isinstance(data, dict):
            return data.get("providersPage") or data
        return {}

    # The two endpoints disagree, and the filter matches the PROVIDER's groupId,
    # not the name the groups list prints. Verified live 2026-07-26: «ЖКХ» returns
    # an empty page while «Коммунальные платежи» returns 63 889 providers, and the
    # groups list spells one group with a comma that the groupId does not have.
    # A wrong group here is not an error — it is HTTP 200 with an empty payload.
    GROUP_ALIASES = {
        "ЖКХ": "Коммунальные платежи",
        "Интернет, ТВ и телефония": "Интернет ТВ и телефония",
    }

    @staticmethod
    def provider_pay_fields(provider: dict) -> list[dict]:
        """The fields THIS provider needs for a payment, in the app's own order.

        A field counts when its usageTypes carry a `Pay` entry; `required` and
        `editable` come from that same entry, not from the field record."""
        out = []
        for f in (provider.get("fields") or []):
            if not isinstance(f, dict):
                continue
            pay = next((u for u in (f.get("usageTypes") or [])
                        if isinstance(u, dict) and u.get("code") == "Pay"), None)
            if pay is None or not pay.get("visible", True):
                continue
            out.append({"id": f.get("id"), "name": f.get("name"),
                        "regexp": f.get("regexp") or "", "hint": f.get("hint") or "",
                        "type": f.get("type") or "", "required": bool(pay.get("required")),
                        "editable": bool(pay.get("editable", True)),
                        "order": pay.get("order", 0)})
        out.sort(key=lambda x: (x["order"], str(x["id"])))
        return out

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
        if self._wide_cookie():
            base["Cookie"] = self._wide_cookie()
        # The delivery address, through the shared accessor rather than a second
        # hand-rolled request to the same URL: a cold-start add_to_cart resolves the
        # address here AND in _grocery_delivery, which was two identical calls.
        addrs = ((self.grocery_client_info().get("deliveryInfo") or {})
                 .get("addresses") or [])
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
        payload = self._unwrap(r)
        stores = []
        categories = payload.get("categories", []) if isinstance(payload, dict) else []
        for cat in categories:
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
                eta, window = delivery_eta(nearest)
                if app_id and name:
                    # address/addressCount: the store list is built for ONE
                    # profile address (the first), and the answer never said
                    # which — with several saved addresses the agent could not
                    # tell what «доставка за 30 мин» was relative to.
                    stores.append({"appId": app_id, "name": name, "areaId": area_id,
                                   "pointId": point_id, "minOrderSum": min_sum,
                                   "etaMin": eta, "deliveryWindow": window,
                                   "deliveryPrice": nearest.get("price", 0),
                                   "cashback": cashback.get("value", ""),
                                   "category": cat.get("name", ""),
                                   "address": addr, "addressCount": len(addrs)})
        # dedupe by (appId, pointId) — the retailers list can repeat a store (#14)
        seen = set()
        uniq = []
        for st in stores:
            key = (st.get("appId"), st.get("pointId"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(st)
        # Seed _grocery_delivery's areaId memo from what we just fetched — a
        # grocery_stores() call immediately followed by add_to_cart for the same
        # store used to re-download this whole catalogue a second time just to
        # read the one field (areaId) this call already has in hand.
        memo = getattr(self, "_memo", None)
        if memo is not None:
            for st in uniq:
                memo[f"areaId:{st.get('appId')}:{st.get('pointId')}"] = st.get("areaId", "")
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
                gname = g.get("name", "")
                name = gname.lower().replace("ё", "е")
                m = self._name_matches(q, gname) if q else 0.0
                if m > 0:
                    price = g.get("price", {})
                    weight = g.get("weight", {})
                    cands.append({
                        "id": str(g.get("id", "")), "name": gname,
                        "price": price.get("value", 0) if isinstance(price, dict) else 0,
                        "weight": (f"{weight.get('value','')} {weight.get('unit','')}".strip()
                                   if isinstance(weight, dict) else ""),
                        "match": m,
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
                             "match": g.get("match", 0.0),
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

    # Words that carry no product identity — dropping them lets an order-free
    # token match work: «фарш из индейки» must match «Фарш индейки».
    _MATCH_STOPWORDS = frozenset(
        "из для со с по и на в от до без вкусом со_вкусом".split())

    @staticmethod
    def _norm_match(s: str) -> str:
        """Canonicalize a string for matching — deterministic hygiene, NO dictionaries.
        Lowercase + ё→е; strip the apostrophe family INCLUDING the backtick (that one
        char is what hid «Чипсы Lay`s» from a `lay's` query); hyphen/slash/punctuation
        → space so word boundaries are honoured. Cross-script and true synonyms are
        NOT handled here — that is the agent's job via the web."""
        s = (s or "").lower().replace("ё", "е")
        for ch in "`'’‘‛´":
            s = s.replace(ch, "")
        for ch in "-/.,:;()\"«»":
            s = s.replace(ch, " ")
        return " ".join(s.split())

    @staticmethod
    def _tokens(s: str) -> list[str]:
        return [t for t in MobileSession._norm_match(s).split()
                if t and t not in MobileSession._MATCH_STOPWORDS]

    @staticmethod
    def _tok_match(t: str, w: str) -> bool:
        """Does query token `t` match name word `w`? Short tokens (<6) must match in
        full — so «кола» hits «Кола» but not «колбаса»; long tokens accept a 5-char
        stem — so «сгущенка» hits «сгущённое» while «магнат» does NOT hit «магний»
        (they share only «магн», 4 < 5)."""
        n = 0
        for a, b in zip(t, w):
            if a != b:
                break
            n += 1
        m = min(len(t), len(w))
        need = 5 if m >= 6 else m
        return n >= need

    @staticmethod
    def _name_matches(query: str, name: str) -> float:
        """Fraction 0..1 of query tokens present in `name` (token-AND, order-free,
        stopword-free, stemmed via _tok_match). 1.0 = every query token found. 0 =
        no match (skip). A partial value (e.g. кетчуп «с помидорами» for «помидоры»
        would still be 1.0 here — the false-positive guard is _pick_candidate's
        scoring plus the confidence flag, not this recall metric)."""
        qt = MobileSession._tokens(query)
        if not qt:
            return 0.0
        nt = MobileSession._tokens(name)
        hit = sum(1 for t in qt if any(MobileSession._tok_match(t, w) for w in nt))
        return hit / len(qt)

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
            # token-recall FIRST among the soft signals: «lay's краб» must beat
            # «Lay`s Max Куриные» even though куриные is cheaper — a fuller match is a
            # more-right product. Without this the planner picked cheapest-of-anything.
            low_match = -round(it.get("match", 1.0), 3)
            # lower is better, field order = priority
            return (missed_qualifier, wrong_form, too_small, low_match,
                    not it.get("likely_raw", False), not name.startswith(q), price)

        return min(results, key=score)

    def _search_best(self, query: str, app_id: str, point_id: str) -> dict | None:
        """Search an ingredient, loosening the query until something sane matches.

        Accepts a loose-query hit only if it still honours the words that were
        dropped; otherwise it keeps looking and falls back to the best seen."""
        fallback = None
        for variant in self._query_variants(query):
            # limit=0: _pick_candidate must see every match, not the top ten.
            r, _, _ = self.grocery_search(variant, app_id=app_id,
                                          point_id=point_id, limit=0)
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
            return q, self._search_best(q, app_id, point_id)

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
            kept = [o for o in ops
                    if isinstance(o, dict) and str(o.get("account", "")) == str(account_id)]
            # The fetch is unscoped and the filter runs here, so an id of the wrong
            # KIND — a card id where an account id belongs — matches nothing and the
            # answer reads as «этот счёт не использовался», not «такого счёта нет».
            # A non-empty fetch that filters to nothing is the one case where those
            # differ, and only this layer can tell them apart.
            if ops and not kept:
                present = sorted({str(o.get("account")) for o in ops
                                  if isinstance(o, dict) and o.get("account")})
                raise TbankApiError("NO_SUCH_ACCOUNT",
                    f"среди {len(ops)} операций за период нет ни одной по счёту "
                    f"{account_id!r}. Счета с операциями: {', '.join(present[:8])}"
                    + (" …" if len(present) > 8 else "")
                    + ". Похоже, передан id не того вида — id счёта берётся из "
                      "list_accounts(), а не из list_cards().")
            ops = kept
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

    # 401/403 mean the credential was rejected, whatever the body says. Kept out of
    # the generic HTTP path so ensure_fresh's re-login still triggers on them.
    _AUTH_STATUS = (401, 403)

    def _unwrap(self, resp: requests.Response) -> Any:
        ok = 200 <= resp.status_code < 300
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            # HTTP 200 and an unparseable body. See UnreadableResponse: the request
            # was processed by SOMETHING, so this is an unknown outcome, not a
            # confirmed non-event.
            raise UnreadableResponse("HTTP_" + str(resp.status_code),
                                     _excerpt(resp.text, 500))
        if isinstance(data, dict):
            # api.tinsurance.ru envelopes with a capital `ResultCode`; matching only
            # the lowercase spelling handed its ERROR envelope back as data, and the
            # tool printed «Действующих полисов нет.» for a failed request.
            code = (data.get("resultCode") or data.get("ResultCode")
                    or data.get("error") or "")
            # Case-insensitive on the VALUE as well as the key. api.tinsurance.ru
            # says {"ResultCode": "Ok"} — capital R, lowercase k — so reading the
            # capital key without also relaxing the value turned that host's every
            # SUCCESS into «API error (Ok)». Caught by a live sweep, not by a test:
            # no fixture carried this host's exact spelling.
            if code and str(code).lower() not in ("ok", "0", "success", ""):
                msg = data.get("errorMessage") or data.get("error_description") or data.get("plainMessage") or ""
                lc = str(code)
                # Accepted-pending-a-second-factor comes back through THIS branch (a
                # non-ok resultCode), but it is not a failure: raise the resumable
                # exception that keeps the envelope instead of the terminal one that
                # discards it. Checked before SessionExpired so a confirmation is
                # never mistaken for a dead session.
                if lc.upper() in _PAYMENT_CONFIRMATION_CODES:
                    raise self._payment_confirmation_error(resp, data, lc, str(msg))
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
            # The body parsed and claimed nothing was wrong — but the STATUS did.
            #
            # This runs AFTER the envelope checks on purpose. The OIDC token endpoint
            # answers HTTP 400 with {"error":"invalid_grant"}, and that mapping to
            # SessionExpired is what drives the silent re-login; checking the status
            # first would turn it into a generic HTTP_400 and break re-login on the
            # one path that recovers a dead session. So a body that names its own
            # error still produces that error, and the status is the fallback.
            #
            # Before this existed, `_call_read` never looked at the status either, so
            # a 404 whose body was ordinary JSON came back to the caller AS THE
            # PAYLOAD: tm answers 404 {"errorCode":"FAQ_NOT_FOUND",…} and
            # webview 404 {"message":"Not Found"}, and ~40 read tools rendered that as
            # an empty list — «ничего не найдено» for a request that failed.
            if not ok:
                raise self._status_error(resp, data)
            # unwrap envelope: payload (mobile API) or result (messenger)
            if "payload" in data:
                return data["payload"]
            if "result" in data:
                return data["result"]
            return data
        if not ok:
            raise self._status_error(resp, data)
        return data

    def _payment_confirmation_error(self, resp: requests.Response, data: dict,
                                    code: str, msg: str) -> "PaymentConfirmationRequired":
        """Build the resumable error from a WAITING_CONFIRMATION envelope.

        Field names are capture-verified; everything sits at the TOP LEVEL, not under
        ``payload`` —

            operationTicket, initialOperation, confirmations:[<type>],
            confirmationData:{<type>:{codeLength, paymentId, codeType}}

        The confirmation type is kept LITERAL (e.g. "SMSBYID") because /v1/confirm
        echoes it back verbatim. The whole body is still stored redacted under
        ``.payload`` for reconciliation."""
        confirmations = data.get("confirmations")
        confirmations = [str(c) for c in confirmations] if isinstance(confirmations, list) else []
        ctype = confirmations[0] if confirmations else str(data.get("confirmationType") or "")
        cdata = data.get("confirmationData") if isinstance(data.get("confirmationData"), dict) else {}
        detail = cdata.get(ctype) if isinstance(cdata.get(ctype), dict) else {}
        request_id = (resp.headers.get("X-Tracking-Id")
                      or resp.headers.get("x-tracking-id")
                      or str(data.get("trackingId") or "")) or ""
        return PaymentConfirmationRequired(
            code, msg,
            http_status=resp.status_code,
            payload=_redact_value(data) if isinstance(data, dict) else {},
            operation_ticket=str(data.get("operationTicket") or data.get("operation_ticket") or ""),
            initial_operation=str(data.get("initialOperation") or "pay"),
            confirmation_type=ctype,
            confirmations=confirmations,
            code_length=int(detail.get("codeLength") or 0),
            payment_id=str(detail.get("paymentId") or ""),
            request_id=request_id,
            method="POST",
            url="",
        )

    def _status_error(self, resp: requests.Response, data: Any) -> TbankApiError:
        """The error for a non-2xx whose body did not declare one itself."""
        msg = ""
        if isinstance(data, dict):
            for key in ("errorMessage", "message", "error_description",
                        "plainMessage", "detail"):
                if data.get(key):
                    msg = str(data[key])
                    break
            # The server's own code is the machine-readable half and is what an
            # agent can act on ("FAQ_NOT_FOUND" says retrying is pointless, where
            # the prose does not). Keep both when both exist.
            if data.get("errorCode"):
                msg = f"{data['errorCode']}: {msg}" if msg else str(data["errorCode"])
        code = str(resp.status_code)
        if not msg:
            msg = resp.text
        # Excerpted once, here, on the way out — not at each of the branches above.
        # The first version marked only the fallback, so a body carrying a 4 KB
        # `message` field sailed through whole while a raw body was cut: the same
        # defect, one level up, and invisible until a test fed it 4 000 characters.
        msg = _excerpt(msg)
        if resp.status_code in self._AUTH_STATUS:
            return SessionExpired("HTTP_" + code, msg)
        return TbankApiError("HTTP_" + code, msg)

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
    # The per-vertical screens below were rejected by that probe under other names;
    # these spellings are the ones the app actually sends (counted in
    # captures-gorod.xml: exhibition_main 53, concerts_main 26, spectacle_main 22).
    #   concerts_main   — concerts only (note the plural)
    #   spectacle_main  — theatre
    #   exhibition_main — exhibitions
    SEARCH_SCREENS = ("services", "afisha", "movie_main", "grocery",
                      "concerts_main", "spectacle_main", "exhibition_main")

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
        payload = self._unwrap(r)
        return payload if isinstance(payload, dict) else {}

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

    # search/fulltext is a paged, relevance-ranked service — SOME ceiling on объектов
    # is unavoidable, you cannot ask it for the infinite catalogue. These two ARE that
    # ceiling, and it is NOT silent: server.grocery_search compares `fetched` against
    # grocery_fetch_cap() and, when the scan saturates, says so in the header (a rare
    # match may rank past the ceiling — that is the honest signal, not «нет в
    # магазине»). The floor keeps a small `limit` scanning a useful ranking window
    # instead of ranking within its own handful; PROBE_FETCH lifts the limit=0
    # («дай всё») ceiling well past the floor.
    GROCERY_SEARCH_FLOOR = 30       # network's natural page — the min ranking pool
    GROCERY_PROBE_FETCH = 100       # the limit=0 ceiling
    GROCERY_MATCH_OK = 0.67         # plan_order: below this the pick is «⚠ проверь», not ✓

    @staticmethod
    def grocery_fetch_cap(limit: int) -> int:
        floor = MobileSession.GROCERY_SEARCH_FLOOR
        return max(floor, limit) if limit else MobileSession.GROCERY_PROBE_FETCH

    def grocery_search(self, query: str, app_id: str = "", point_id: str = "",
                       limit: int = 10) -> tuple[list[dict], int, int]:
        """Global grocery search via search/fulltext — searches the ENTIRE store
        catalog (not just one category). Uses inStockFilter (only available
        items). Returns (rows, matched, fetched): rows — up to `limit` matches
        (0 = all of them), matched — how many hits matched the query, fetched —
        how many goods the search service returned at all. query = e.g. "свёкла".

        fetched is capped by grocery_fetch_cap(limit): when fetched == that cap the
        network was saturated and `matched` is a lower bound — more matches may sit
        past the object ceiling, unreached. limit=0 does NOT mean "the whole
        catalog", it means "all of the fetched objects".

        The old shape collected matches with a `break` at 10 and SORTED AFTER the
        break: a cheaper match at position 11 of the server page was silently
        unreachable, and «cheapest» meant cheapest of an arbitrary first ten."""
        _need_store(app_id, point_id)
        q = query.lower().strip().replace("ё", "е")
        # POST search/fulltext (global search across the store)
        base = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0"}
        search_body = {
            "searchTypes": ["grocery_goods", "grocery_categories"],
            "filters": [{"name": "inStockFilter", "type": "grocery_goods",
                         "mode": "always", "value": True}],
            "maxObjectsCount": self.grocery_fetch_cap(limit),
            "sortTypes": [{"type": "grocery_goods", "name": "default"}],
            "text": query.replace("ё", "е"),
        }
        params = self._search_params("grocery", context="api",
                                     applicationId=app_id, pointId=point_id)
        r = self._http.post("https://search.t-bank-app.ru/search/fulltext",
                           params=params, json=search_body,
                           headers={**base, "Authorization": "Bearer " + self.access_token},
                           timeout=30)
        payload = self._unwrap(r)
        hits = payload.get("sortedByScoreObjects", []) if isinstance(payload, dict) else []
        results = []
        fetched = 0
        for hit in hits:
            if hit.get("objectType") != "grocery_goods":
                continue
            fetched += 1
            src = hit.get("objectSource", {})
            if not src:
                continue
            name = src.get("name") or ""
            name_norm = name.lower().replace("ё", "е")
            # Token-AND match instead of literal substring: order-free, stopword-free,
            # punctuation-folded — so «фарш из индейки» hits «Фарш индейки» and a
            # `lay's` query hits «Lay`s» (backtick). score is 0..1; 0 = skip.
            score = self._name_matches(query, name)
            if score == 0:
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
                "match": score,
                "appId": str(app_id),
                "pointId": str(point_id),
                "store_app_id": str(src.get("applicationId", app_id)),
                "imageUrl": src.get("imageUrl", ""),
            })
        # sort BEFORE the cut: full matches first (score desc), then likely_raw, then
        # price — so `limit` keeps the best matches, not whichever ten arrived first.
        results.sort(key=lambda r: (-r.get("match", 0), not r.get("likely_raw", False), r.get("price", 999) if isinstance(r.get("price"), (int, float)) else 999))
        matched = len(results)
        if limit > 0:
            results = results[:limit]
        return results, matched, fetched

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
        already in the cart.

        An entry whose key is not exactly ``id`` is refused, not skipped: the old
        loop dropped it silently, the cart came back with an unchanged goodsSum, and
        the tool reported "OK, N new items" for zero items added. `goodId`,
        `good_id` and `product_id` are all plausible guesses for a caller reading
        goods ids out of a search result."""
        _need_store(app_id, point_id)
        _reject_unkeyed(items)
        try:
            cart = self.grocery_cart_get(app_id=app_id, point_id=point_id)
        except TbankApiError as e:
            # cart/set is a FULL REPLACE, so this read is load-bearing: whatever it
            # returns becomes the entire cart. Treating a failed read as an empty
            # cart posted only the new items and DELETED everything already there,
            # while the tool printed an ordinary success line. grocery_set_cart
            # already refuses here for the same reason; this path did not.
            raise TbankApiError("CART_READ_FAILED",
                f"не удалось прочитать корзину перед добавлением ({e}). "
                f"Корзина НЕ изменена — запись заменяет её целиком, а нечитаемая "
                f"корзина не то же самое, что пустая. Повтори позже.") from e
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
        posts 5 after a removal. There is no delete endpoint.

        `cartSetMode` escalates exactly the way the app escalates it. `SINGLE_CART`
        is refused with app code 268 — whose text is the generic "Сервис временно
        недоступен", not anything about carts — once a cart exists for a DIFFERENT
        retailer. The app answers that by resending the identical body with
        `SINGLE_CART_WITH_OTHER_CART_RESET`, which succeeds: captures2.xml [1073]
        fails and [1077] succeeds, and the two bodies differ in this field alone.

        Sending the reset mode unconditionally would work too, and would silently
        discard other retailers' carts on every write. So try the narrow mode first
        and escalate only on 268, then flag it: the caller has to be able to tell
        the user their other cart is gone. An empty goods list never escalates —
        clearing a cart is accepted in the narrow mode."""
        from . import observability as obs

        body = {"goods": goods, "cartSetMode": "SINGLE_CART", "delivery": delivery}

        def _write(mode: str) -> dict:
            body["cartSetMode"] = mode
            started = time.time()
            try:
                res = self._call_read("grocery_cart_set", body=body,
                                      overrides={"appId": app_id})
            except TbankApiError as e:
                code = str(getattr(e, "result_code", "") or "")
                obs.emit("cart_set", app_id=app_id, item_count=len(goods),
                         cart_set_mode=mode, app_code=code,
                         blame=obs.blame_of(200, code),
                         duration_ms=int((time.time() - started) * 1000))
                raise
            obs.emit("cart_set", app_id=app_id, item_count=len(goods),
                     cart_set_mode=mode, http_status=200, blame="ok",
                     duration_ms=int((time.time() - started) * 1000))
            return res

        try:
            return _write("SINGLE_CART")
        except TbankApiError as e:
            if str(getattr(e, "result_code", "")) != "268" or not goods:
                raise
            res = _write("SINGLE_CART_WITH_OTHER_CART_RESET")
            if isinstance(res, dict):
                res = dict(res)
                res["otherCartsReset"] = True
            return res

    def grocery_set_cart(self, items: list[dict], app_id: str = "", point_id: str = "",
                         clear: bool = False) -> dict:
        """Set ABSOLUTE counts. `count: 0` removes a good; goods not mentioned keep
        their current count. `clear=True` empties the cart and ignores `items`.

        The counterpart of grocery_add_to_cart, which is relative (+N). Without this
        the cart could only ever grow: re-adding a good to "correct" it added again.

        Entries without an ``id`` key are refused — see grocery_add_to_cart."""
        _need_store(app_id, point_id)
        if not clear:
            _reject_unkeyed(items)
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
        """Goods currently in the store's cart. Raises TbankApiError like any other
        read — this used to swallow it and return [], which conflated "the cart is
        really empty" with "the re-read failed", right after a confirmed-successful
        write. Callers that want a fallback (grocery_add_to_cart already does)
        catch it themselves; this method must not decide that for them."""
        return self._goods_of(self.grocery_cart_get(app_id=app_id, point_id=point_id))

    def grocery_checkout(self, app_id: str = "", point_id: str = "",
                         client_email: str = "", account: str = "",
                         sum_val: float = 0, attempt_id: str | None = None,
                         expected_sum: float = 0, dry_run: bool = False) -> dict:
        """Full grocery checkout (web flow): deliveries → order/create → payment_gate_pay.
        `app_id`/`point_id` scope the store; `account` names the account to debit and
        wins over the bank's last-used one when given; `sum_val` is a mobile-cart
        fallback sum (the post-delivery WEB sum is used inside); `expected_sum` is the
        amount the user approved and refuses the checkout, before the order exists, if
        the backend's final number diverges; `dry_run` stops after the delivery step
        and returns the quote without creating or paying for an order; `attempt_id`
        records progress in the journal. Raises checkout.CheckoutError (safe to retry)
        or checkout.CheckoutUnknown (order may exist — retry must be blocked)."""
        from .checkout import checkout as _checkout
        return _checkout(self, app_id=app_id, point_id=point_id, client_email=client_email,
                         sum_val=sum_val, account=account, attempt_id=attempt_id,
                         expected_sum=expected_sum, dry_run=dry_run)

    def messenger_send(self, conversation_id: str, text: str) -> dict:
        """Send a text message to a conversation. Encapsulates the vendor
        Content-Type + body format."""
        import uuid as _uuid
        conversation_id = self._safe_id(conversation_id, "conversation_id")
        body = {"content": text, "clientSideId": str(_uuid.uuid4()),
                "assistant": {"inputType": "default"}}
        return self._messenger_write(body=body,
            path_override=f"/app/bank/messenger/conversations/{conversation_id}/messages")

    def ruble_source_accounts(self) -> list[dict]:
        """Every Current RUB account with a positive balance — the debit candidates,
        in the bank's order. [{id, name, balance}]. `_source_account` returns the
        first; a picker offers all of them."""
        out = []
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
                out.append({"id": str(a["id"]), "name": str(a.get("name") or ""),
                            "balance": bal})
        return out

    def _source_account(self) -> str:
        """First Current RUB account id with a positive balance — the payer/source
        for transfers (capture: payParameters.account = 10-char source id)."""
        accts = self.ruble_source_accounts()
        if accts:
            return accts[0]["id"]
        # Names the way out. This is a guess the caller can always override, and the
        # override is documented on the tools but was absent from the one message the
        # agent actually reads when the guess fails.
        raise TbankApiError("NO_SOURCE_ACCOUNT",
            "не нашёл рублёвый счёт Current с положительным балансом для списания. "
            "Посмотри list_accounts() и укажи счёт явно: "
            "transfer(..., from_account=\"…\") или ticket_pay(..., account_id=\"…\").")

    def _requisites(self, ptr: str, source: str) -> list[dict]:
        """One GET /v1/get_requisites for one pointerSource, parsed into candidates.

        The app sends the two sources with DIFFERENT query params — the internal
        lookup carries neither withTinkoff nor gapBanks — and both shapes are in
        captures.xml, so neither is guessed."""
        extra = ({"withTinkoff": "true", "gapBanks": "true"}
                 if source == "external" else {})
        r = self._call_read("get_requisites", overrides={
            "pointerType": "phone", "pointer": ptr,
            "pointerSource": source, **extra,
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
            # ready providerFields — paste into payment_commission() so the agent
            # never hand-writes the 8276 pointer-type code. bankMemberId is OMITTED,
            # not blanked, when the bank did not send one: the T-Bank-internal
            # commission body in captures.xml has no such key, and an empty string
            # there is a value we have never seen the app send.
            pf = {"pointerType": SBP_PHONE_POINTER_TYPE, "pointer": ptr,
                  "maskedFIO": mfio, "pointerLinkId": plid}
            if bmi:
                pf["bankMemberId"] = bmi
            out.append({
                "bank_member_id": bmi,
                "masked_fio": mfio,
                "pointer_link_id": plid,
                "bank_name": str(brand.get("name", "")),
                "bank_id": str(brand.get("id", "")),
                "is_default_bank": bool(it.get("isDefaultBank")),
                "workflow_type": str(it.get("workflowType", "")),
                "is_tbank": str(it.get("workflowType", "")) == TBANK_INNER_WORKFLOW,
                "provider_fields": pf,
            })
        return out

    def resolve_recipient(self, phone: str) -> list[dict]:
        """Resolve a phone to every account it can be paid to (GET
        /v1/get_requisites, capture-verified). READ-ONLY — no money moves.

        TWO requests, because the bank answers two different questions and the app
        asks both, in this order:

          pointerSource=internal → the recipient's T-BANK account, if they are a
            T-Bank client. workflowType='TinkoffInner', maskedFIO and pointerLinkId,
            and NO bankMemberId — an internal transfer is not routed through SBP.
          pointerSource=external → the recipient's SBP banks.
            workflowType='SBPTransfer', each with its own bankMemberId.

        Asking only the second one is why a recipient the user could plainly see in
        the app came back as «Sber and VTB, no T-Bank»: `withTinkoff=true` on the
        external call does NOT fold the internal answer in — measured on the same
        phone in captures.xml, the external list is Sber+VTB and the internal one is
        the T-Bank account, and nothing about the external response hints that a
        second list exists.

        A phone can therefore map to SEVERAL candidates, so the caller picks one
        (prefer isDefaultBank=True). Returns [{bank_member_id, masked_fio,
        pointer_link_id, bank_name, bank_id, is_default_bank, workflow_type,
        is_tbank, provider_fields}], T-Bank first. Empty list = neither a T-Bank
        client nor registered in SBP (or the number is wrong).

        RAISES THE SESSION LEVEL FIRST. This endpoint validates the mobile sessionid,
        not just the Bearer, and its CLIENT window is ~11 minutes against the token's
        ~2h — so ensure_fresh(), which re-mints on a ~100-minute schedule, leaves it
        being called with an ANONYMOUS session for most of that interval. The bank's
        answer to that is `REQUEST_RATE_LIMIT_EXCEEDED — Слишком много попыток
        проверить банки получателя`, which reads as a volume limit and is nothing of
        the sort: measured live, it fires on the FIRST call in 14 minutes, with the
        same deviceId and IP, and the identical call succeeds seconds after a re-mint.

        Here rather than in the tools: transfer() resolves a second time on its own
        (for the display name), so a guard on the tool layer would miss that path."""
        self.ensure_client_session()
        ptr = _normalize_phone(phone)
        # The internal lookup is the cheap one and the one whose absence caused the
        # bug, so it goes first and its failure is NOT swallowed: a T-Bank recipient
        # silently missing from the list is exactly the state this method exists to
        # end. Both calls hit the same endpoint with the same session, so there is no
        # failure mode where one is reachable and the other is not.
        return self._requisites(ptr, "internal") + self._requisites(ptr, "external")

    def transfer(self, amount: float, to_account: str, description: str = "",
                 provider: str = "p2p-anybank", pointer_type: str = SBP_PHONE_POINTER_TYPE,
                 bank_member_id: str = "", masked_fio: str = "",
                 pointer_link_id: str = "", account: str = "",
                 user_payment_id: str = "") -> Any:
        """Transfer via signed /v1/pay (REAL money). Body shape is capture-verified
        (the old body invented pointerType='ACCOUNT' and was rejected). The signing
        mechanism (_signed_parts) is unchanged — it was proven byte-exact.

        phone/SBP (default: provider='p2p-anybank', pointer_type='8276'):
          to_account = recipient phone. If pointer_link_id is NOT passed, the
          recipient is AUTO-RESOLVED via resolve_recipient() (GET
          /v1/get_requisites, both pointerSources): the default is picked, or the
          single match; if several with no default → RECIPIENT_MULTIPLE_BANKS
          (surface list, never silently pick). For a NEW recipient, call
          transfer_sbp_resolve(phone) first to show the user the candidates, then
          pass the chosen fields. A candidate that is the recipient's own T-BANK
          account has no bankMemberId — pass its pointer_link_id and leave
          bank_member_id empty, and the key is omitted from providerFields.
          pay body is capture-verified (providerFields.pointerType='8276', pointer
          '+7XXXXXXXXXX'). paymentType='Transfer' belongs to payment_commission,
          NOT to pay — no real pay body carries it.

          Both flavours are pinned to a real signed /v1/pay that the bank answered
          200 to: the SBP one to captures.xml #1477, the T-Bank-internal one to
          captures-pay.xml (same provider and envelope, providerFields WITHOUT
          bankMemberId). Neither is an inference from a commission preview.
        between own accounts (provider='transfer-inner'): NOT supported for the
          PAYMENT. providerFields = {'bankContract': to_account} is a plausible
          envelope, but unlike p2p-anybank (capture-verified) there is no captured
          /v1/pay for this provider to check it against — refused until one exists.
          Transfer between own accounts in the app in the meantime.
        by details (provider='transfer-legal'): use transfer_legal() instead — the
          payment is implemented, but it needs nine requisite fields this signature
          has nowhere to put. Calling transfer() with it raises WRONG_METHOD.

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
        ALWAYS confirm with the user.

        Returns (payload, recipient): the bank's payload (paymentId, commissionInfo)
        and the masked recipient name that actually went into the signed body.

        The second half exists because it used to be thrown away. When the caller
        passes the two routing ids but no name, this method looks the name up — one
        request to /v1/get_requisites — puts it in providerFields.maskedFIO, and
        used to let it die with the local variable. server.transfer then built its
        confirmation line from its OWN masked_fio argument, still empty, so the one
        line a person reads before money moves showed a phone number and no name —
        while the bank had told us «Мария П.» and we had paid a request for it.

        A tuple rather than an extra key in the payload: that dict is the bank's,
        it is printed verbatim when no paymentId comes back, and inventing a field
        in it would show the user something the bank never sent."""
        src = account or self._source_account()
        if provider == "transfer-inner":
            # providerFields={'bankContract': to_account} would be the plausible
            # envelope, but no captured /v1/pay exists for this provider to check
            # it against — unlike p2p-anybank, which has direct capture references.
            # Refused rather than sent unverified against real money.
            raise TbankApiError("NOT_SUPPORTED",
                "Перевод между своими счетами (provider='transfer-inner') через "
                "MCP не реализован. Тело providerFields={'bankContract': "
                "to_account} выглядит правдоподобно, но ни разу не сверено с "
                "реальным перехваченным /v1/pay для этого провайдера — угадывать "
                "конверт на платеже нельзя. Перевод между своими счетами — в "
                "приложении.")
        elif provider == "transfer-legal":
            # No longer a refusal for want of a capture — captures_payreq.xml #578 is
            # a real signed /v1/pay for this provider. It is a refusal because this
            # signature cannot carry the payment: transfer() routes by ONE recipient
            # id, and a payment to a legal entity needs nine (account, БИК, corr
            # account, bank name, payee, ИНН, КПП, purpose, VAT mark), each with its
            # own format the bank enforces. transfer_legal() takes them.
            raise TbankApiError("WRONG_METHOD",
                "Перевод по банковским реквизитам делается не через transfer(), а "
                "через transfer_legal(amount, fields=…) — у него девять полей "
                "(bankAcnt/bankBik/bankCorrAcnt/bankName/addressee/inn/kpp/comment/"
                "nds), которые в сигнатуру transfer() не помещаются. Тул: "
                "transfer_requisites(...), а прочитать QR со счёта — payment_qr(qr).")
        else:  # p2p-anybank (phone / SBP)
            # The caller's CHOICE is the two ids. maskedFIO is a display name the
            # bank echoes back, not part of the routing — and requiring it here meant
            # an agent that followed the docs (which promise "bank_member_id +
            # pointer_link_id") left it empty, the gate opened, and auto-resolution
            # silently replaced the bank the user had picked and confirmed. Same
            # person, different account, and invisible: the result line prints the
            # recipient only when masked_fio is set.
            # pointer_link_id ALONE is the caller's explicit choice — the gate used to
            # demand bank_member_id too, and a T-Bank-internal recipient does not have
            # one. With the old gate, picking the recipient's T-Bank account was not
            # expressible: passing its link id with an empty member id looked like
            # "nothing chosen" and re-resolved to some SBP bank instead.
            if not pointer_link_id:
                # Auto-resolve the recipient via get_requisites (read-only). Pick the
                # default bank if any, else the single match; if several with NO
                # default, refuse + surface the list — money safety: never silently
                # pick a bank (could send to the wrong bank/account).
                resolved = self.resolve_recipient(to_account)
                if not resolved:
                    raise TbankApiError("RECIPIENT_NOT_RESOLVED",
                        f"{to_account} has no T-Bank account and is not registered in "
                        "SBP (or the number is wrong). "
                        "Call transfer_sbp_resolve(phone) to check.")
                pick = next((x for x in resolved if x["is_default_bank"]), None)
                if pick is None and len(resolved) == 1:
                    pick = resolved[0]
                if pick is None:
                    raise TbankApiError("RECIPIENT_MULTIPLE_BANKS",
                        f"{to_account} maps to {len(resolved)} accounts — pick one:\n" +
                        "\n".join(f"  - {x['masked_fio']} | {x['bank_name']}"
                                  + (" (счёт в Т-Банке, перевод внутри банка)"
                                     if x["is_tbank"] else "")
                                  + f" | bankMemberId={x['bank_member_id'] or '—'}"
                                  f" | pointerLinkId={x['pointer_link_id']}"
                                  for x in resolved) +
                        "\nPass the chosen pointer_link_id (+ bank_member_id for an "
                        "SBP bank; a T-Bank account has none) to transfer().")
                bank_member_id = pick["bank_member_id"]
                masked_fio = pick["masked_fio"]
                pointer_link_id = pick["pointer_link_id"]
            elif not masked_fio:
                # The ids came from the caller, so the routing is already decided.
                # Look up the display name only — never let this overwrite the choice.
                #
                # This lookup IS load-bearing, contrary to a first reading of it: the
                # value goes into pf["maskedFIO"], i.e. into the SIGNED body, which is
                # what tests/test_transfer.py pins. What it does NOT reach is the
                # confirmation line the user reads — server.transfer builds that from
                # its own masked_fio argument, so a name resolved here at the cost of
                # a request is still absent from the sentence a person checks before
                # the money moves.
                try:
                    # An empty bank_member_id means the caller chose the recipient's
                    # T-Bank account, and that candidate is the one with no member id
                    # — so the same comparison selects it, deliberately.
                    match = next((x for x in self.resolve_recipient(to_account)
                                  if str(x.get("bank_member_id")) == str(bank_member_id)), None)
                    masked_fio = (match or {}).get("masked_fio", "")
                except TbankApiError:
                    # Left EMPTY, not filled with a placeholder: this goes into the
                    # signed body, and inventing a name there is telling the bank
                    # something untrue. The routing is unaffected — it is the two ids.
                    masked_fio = ""

            pf = {"pointerType": pointer_type, "pointer": _normalize_phone(to_account),
                  "maskedFIO": masked_fio, "pointerLinkId": pointer_link_id}
            if bank_member_id:
                # Present for an SBP route (captures.xml #1477), ABSENT for a T-Bank
                # -internal one: the captured internal commission body has no such
                # key, and the bank answers a bodiless bankMemberId with
                # unfinishedFlag=true — "recipient not fully identified" — which is
                # exactly what a blank string would reproduce.
                pf["bankMemberId"] = bank_member_id
        if description:
            # The app carries the note here, not as a top-level field
            # (captures2.xml #595: providerFields.message = "Hi").
            pf["message"] = description
        pay_params = {"provider": provider, "currency": "RUB", "account": src,
                      "moneyAmount": money_amount(amount), "providerFields": pf,
                      "isTransferStatus": "false", "isUrgentTransfer": "false",
                      # Present in every real pay body; absent from ours until now.
                      "cellularService": "WiFi", "frontCamera": "true",
                      "userPaymentId": user_payment_id or str(int(time.time() * 1000))}
        # NOTE: no paymentType here. `paymentType: "Transfer"` was added from a
        # capture — but of /v1/payment_commission, where it IS required. No real
        # /v1/pay body in either capture carries it (checked all three: captures.xml
        # #1423 and #1477, captures2.xml #595). Sending it is an invention.
        body = "payParameters=" + urllib.parse.quote(json.dumps(pay_params))
        return self.pay(body), masked_fio

    # Short on purpose, like CLIENT_INFO_TTL: a burst cache for the common
    # payment_providers(provider_id=…) → pay_bill(provider_id) pair, not a
    # session-long one. The catalogue itself doesn't change within a session.
    PROVIDER_TTL = 60.0

    def find_provider(self, provider_id: str, group: str = "",
                      max_pages: int = 7) -> dict:
        """One provider record from the catalogue, {} if not found.

        Walks the pages of `group` when given (cheap: the group filter narrows
        63 889 utility providers to one page), otherwise searches the ungrouped
        catalogue, which is 100k+ providers — so a group is strongly preferred.

        Memoised for PROVIDER_TTL seconds, keyed by (group, provider_id): the
        typical flow calls payment_providers(provider_id=…) to show the fields,
        then pay_bill(provider_id) moments later — same scan, same answer."""
        pid = str(provider_id)
        key = f"provider:{group}:{pid}"
        memo = getattr(self, "_memo", None)
        if memo is not None:
            at, cached = memo.get(key, (0.0, None))
            if cached is not None and time.time() - at < self.PROVIDER_TTL:
                return cached
        # The app looks a provider up by id with ONE filtered request; the page
        # walk downloads up to seven pages of 362 KB (~2.5 MB) to find the same
        # record. Same host, same shape — /providers/compatible/filter?ids= answers
        # with the full fields[] schema (captures: 4 providers, 15 KB).
        try:
            got = self._call_read("providers_compatible", overrides={"ids": pid})
            # `providers`, read by name. _as_list understands `list` and `payload`
            # only, so it wrapped this envelope as ONE element and the id never
            # matched — the same shape mismatch that made four other tools print a
            # single useless row.
            found = (got or {}).get("providers") if isinstance(got, dict) else got
            for prov in (found or []):
                if isinstance(prov, dict) and str(prov.get("id")) == pid:
                    if memo is not None:
                        memo[key] = (time.time(), prov)
                    return prov
        except TbankApiError:
            # The filter is an optimisation, not the contract: if it is unavailable
            # or answers something unexpected, the page walk below still finds it.
            pass
        for page in range(1, max(1, int(max_pages or 7)) + 1):
            pg = self.providers_compatible_page(group=group, page=page)
            for prov in (pg.get("providers") or []):
                if str(prov.get("id")) == pid:
                    if memo is not None:
                        memo[key] = (time.time(), prov)
                    return prov
            if page >= int(pg.get("totalPages") or 1):
                break
        return {}

    def validate_provider_fields(self, provider: dict, fields: dict) -> list[str]:
        """Complaints about `fields` against the provider's own schema, [] if clean.

        The catalogue publishes a `regexp` per field and a `required` flag per usage,
        and this is the only thing standing between a typo and a stranger's utility
        account being paid. Checked BEFORE the request, so a bad value costs nothing."""
        problems: list[str] = []
        schema = self.provider_pay_fields(provider)
        known = {f["id"]: f for f in schema}
        for f in schema:
            val = fields.get(f["id"])
            if val in (None, ""):
                if f["required"]:
                    problems.append(
                        f"нет обязательного поля {f['id']} ({f['name']})"
                        + (f" — {f['hint']}" if f["hint"] else ""))
                continue
            rx = f["regexp"]
            if rx:
                try:
                    if not re.fullmatch(rx, str(val)):
                        problems.append(
                            f"{f['id']} ({f['name']}) не подходит под формат {rx}"
                            + (f" — {f['hint']}" if f["hint"] else ""))
                except re.error:
                    # A schema we cannot compile is not the caller's fault; say so
                    # rather than letting a broken pattern block a valid payment.
                    problems.append(f"{f['id']}: регулярку провайдера не удалось "
                                    f"разобрать ({rx!r}) — проверь значение сам")
        for k in fields:
            if k not in known:
                problems.append(f"поле {k!r} провайдер не принимает; допустимые: "
                                + ", ".join(sorted(known)))
        return problems

    def pay_bill(self, provider_id: str, fields: dict, amount: float,
                 account: str = "", user_payment_id: str = "") -> Any:
        """Pay a service bill (utilities, fines, taxes, internet…). REAL MONEY.

        Same signed /v1/pay envelope transfer() uses — capture-verified for the
        transfer providers — with this provider's own providerFields. `paymentType`
        is deliberately absent: it belongs to payment_commission, and no captured
        pay body carries it.

        The caller is expected to have validated `fields` against the catalogue and
        previewed the commission; this method does neither, so that the server layer
        can report each failure with its own message."""
        src = account or self._source_account()
        pay_params = {"provider": str(provider_id), "currency": "RUB", "account": src,
                      "moneyAmount": money_amount(amount), "providerFields": dict(fields),
                      "cellularService": "WiFi", "frontCamera": "true",
                      "userPaymentId": user_payment_id or str(int(time.time() * 1000))}
        body = "payParameters=" + urllib.parse.quote(json.dumps(pay_params, ensure_ascii=False))
        return self.pay(body)

    def transfer_legal(self, amount: float, fields: dict, account: str = "",
                       user_payment_id: str = "", from_qr: bool = False) -> Any:
        """Pay a legal entity / sole trader by bank requisites. REAL MONEY.

        Body is capture-verified against captures_payreq.xml #578 — a real signed
        /v1/pay for provider `transfer-legal`, 23 600 ₽ from an invoice QR, answered
        200 with a paymentId. Relative to the p2p envelope it keeps isTransferStatus
        and isUrgentTransfer, and adds `paidByPhoto: "QR"` when the requisites were
        scanned rather than typed. `paymentType` stays out, as on every other pay.

        `fields` are the providerFields ids, not human names: bankAcnt (20 digits),
        bankBik (9), bankCorrAcnt, bankName, addressee, inn (10 or 12), kpp (9),
        comment (назначение платежа — the bank requires it) and nds (NDS_EXEMPT /
        NDS_INCLUDED). parse_payment_qr() produces this dict directly.

        This method does NOT validate: the server layer checks the values against
        the provider's own published regexps first, so each complaint can name the
        field. `user_payment_id` is the retry key — same semantics as transfer()."""
        src = account or self._source_account()
        vals = {k: str(v) for k, v in dict(fields or {}).items() if str(v).strip()}
        vals.setdefault("nds", NDS_EXEMPT)
        pay_params = {"moneyAmount": money_amount(amount), "currency": "RUB",
                      "frontCamera": "true", "cellularService": "WiFi",
                      "provider": "transfer-legal",
                      "isTransferStatus": "false", "isUrgentTransfer": "false",
                      "account": src,
                      "userPaymentId": user_payment_id or str(int(time.time() * 1000)),
                      "providerFields": vals}
        if from_qr:
            # The app marks a scanned payment; the bank echoes it into the operation.
            # Only set when the requisites really came from a QR — claiming a scan
            # that did not happen is telling the fraud engine something false.
            pay_params["paidByPhoto"] = "QR"
        # ensure_ascii=False: the captured body carries the payee's name and purpose
        # as percent-encoded utf-8 (%D0%9E%D0%9E%D0%9E), never as \uXXXX escapes.
        body = "payParameters=" + urllib.parse.quote(
            json.dumps(pay_params, ensure_ascii=False))
        return self.pay(body)

    # ---- payment confirmation (WAITING_CONFIRMATION continuation) ---------

    def confirm_payment(self, *, operation_ticket: str, otp: str,
                        initial_operation: str = "pay",
                        confirmation_type: str = "SMSBYID") -> Any:
        """Submit the second-factor code for a /v1/pay held at WAITING_CONFIRMATION.

        Capture-verified: a real POST /v1/confirm that completes a held legal-entity
        transfer and answers 200 with the payload's paymentId.

        Two things make this UNLIKE every other money call and unlike the login OTP:

          * It is NOT signed and NOT Bearer-authorised. The captured /v1/confirm
            carried neither `x-api-signature` nor an `Authorization` header — it
            authorises on the session COOKIE plus the `sessionid` query param. So it
            goes out through a bare POST, not _call_signed and not _call_read (which
            would add the Bearer this endpoint does not want).
          * The OTP rides as `secretValue`, alongside the ticket from the pay
            response (`initialOperationTicket`), the operation (`initialOperation`,
            "pay") and the literal type (`confirmationType`, e.g. "SMSBYID"). The
            rest of the body is the app's standard device/anti-fraud block.

        `secretValue` carries 'secret', so the observability redactor scrubs it; the
        OTP is passed straight to the bank and written nowhere else. On success the
        resultCode-OK envelope unwraps to {paymentId, commissionInfo, extraFields}."""
        if not operation_ticket:
            raise TbankApiError("NO_TICKET", "confirm_payment needs the operationTicket "
                                "from the WAITING_CONFIRMATION response")
        p = self.PAY_DEVICE_PROFILE
        lat, lon = _CONFIRM_GEO
        # Field order mirrors the captured body (fidelity; the server ignores order).
        fields = {
            "deviceId": self.device_id,
            "initialOperation": initial_operation or "pay",
            "confirmationType": confirmation_type or "SMSBYID",
            "appVersion": self.app_version or APP_VERSION,
            "mobile_device_model": self.device_model,
            "mobile_device_os_version": _IOS_VERSION,
            "secretValue": str(otp),
            "root_flag": "false",
            "screen_height": p.get("device_screen_height", "2736"),
            "appName": self.app_name,
            "fingerprint": self._credentials_fingerprint(),
            "connectionType": self.connection_type,
            "device_type": "phone",
            "origin": self.origin,
            "screen_dpi": "3",
            "device_location_availability": "when_user",
            "mobile_device_os": "iOS",
            "longitude": str(lon),
            "latitude": str(lat),
            "platform": self.platform,
            "initialOperationTicket": operation_ticket,
            "screen_width": p.get("device_screen_width", "1260"),
        }
        query = urllib.parse.urlencode({"sessionid": self.mobile_sessionid,
                                        "ccc": self.ccc, "cpswc": self.cpswc})
        host = (self._tpl("v1_pay") or {}).get("host") or self.base_url
        url = f"{host.rstrip('/')}/v1/confirm?{query}"
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8;",
                   "Accept": _NATIVE_ACCEPT, "X-Lang": "ru", "Accept-Language": "ru",
                   "User-Agent": self._mobile_ua()}
        if self._wide_cookie():
            headers["Cookie"] = self._wide_cookie()
        r = self._http.post(url, data=urllib.parse.urlencode(fields),
                            headers=headers, timeout=30)
        return self._unwrap(r)


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
        form (NOT the JSON fingerprint used at auth/step).

        The screen and timezone come from PAY_DEVICE_PROFILE, the same source the
        /v1/pay anti-fraud block uses, so TBANK_DEVICE_SCREEN_* reaches both. They
        were hardcoded here as 1170x2532 while all five captured card_credentials
        requests send 1260x2736 — the value already sitting in PAY_DEVICE_DEFAULTS
        a few hundred lines up. One session claiming two different screens is
        exactly the inconsistency the note above PAY_DEVICE_CONSTANTS warns about,
        and this is the endpoint that returns a PAN and a CVV."""
        ua = self._mobile_ua() or "iPhone/iOS/TCSMB"
        p = self.PAY_DEVICE_PROFILE
        w = p.get("device_screen_width", "1260")
        h = p.get("device_screen_height", "2736")
        tz = str(p.get("timezone", "180")).lstrip("+-")
        return f"{ua}###{w}x{h}x32###-{tz}###false###false###"

    def card_credentials(self, ucid: str) -> dict:
        """Full card number + CVV + expiry for one card. Sensitive: the caller
        decides whether to show or mask it."""
        ov = {
            "ucid": str(ucid),
            "fingerprint": self._credentials_fingerprint(),
            "fingerprint_change_date": "0",
            "mobile_device_os": self.platform or "ios",
            "mobile_device_os_version": _IOS_VERSION,
            "mobile_device_model": self.device_model,
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
                           limit: int = 8, with_nutrition: bool = False,
                           ) -> tuple[list[dict], int]:
        """Search `query` and return (candidate rows, how many matched in total).

        This deliberately applies NO selection policy — it is the capability, not
        the strategy. Ranking ("cheapest", "lowest calorie", "most protein") is the
        caller's decision; see grocery_rank in server.py and the grocery skill.
        `limit <= 0` means every match; the matched count is returned so the
        caller's header can say «N из M» instead of presenting N as everything.

        with_nutrition costs one extra /api/grocery/good request per candidate, so
        it is off unless the caller actually needs those fields. A good whose
        nutrition the retailer does not publish keeps None — "not published" is a
        different fact from zero and must not be flattened into one."""
        found, matched, _ = self.grocery_search(query, app_id=app_id,
                                                point_id=point_id, limit=0)
        picked = found if limit <= 0 else found[:limit]
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
        return rows, matched

    # ---- cinema ----------------------------------------------------------

    PAGE = 30                      # what the collection endpoint returns per page

    def afisha_collection_code(self, prefix: str, kind: str = "movie",
                               city: str = "", city_id: int | str = 0) -> str:
        """The server's own collectionCode for a shelf, e.g. "Segodnya-v_kino_".

        This used to be built by transliterating the city name, which is a guess
        that happens to be right for Moscow. The server publishes the codes, and
        its own spelling is not consistent — the same city appears as Moskva,
        moscow and msk across different shelves — so nothing derived from the name
        could have covered them all."""
        cid = city_id_of(city, city_id)
        # `getattr(...) or {}` would be a bug here: an EMPTY memo is falsy, so the
        # fallback would hand back a fresh dict every call and cache nothing.
        memo = getattr(self, "_memo", None)
        if memo is None:
            memo = self._memo = {}
        shelves = memo.setdefault("afisha_shelves", {})
        key = (vertical(kind)["service"], cid)
        if key not in shelves:
            data = self._call_read("events_by_service", overrides={
                "service": key[0], "cityId": cid})
            shelves[key] = [c for c in ((data or {}).get("collections") or [])
                            if isinstance(c, dict)]
        found = next((str(c.get("code")) for c in shelves[key]
                      if str(c.get("code") or "").startswith(prefix)), "")
        if found:
            return found
        # An empty shelf list is a live condition, not a contract: Moscow's came
        # back empty while Petersburg's was full. Fall back to the convention the
        # server itself uses for this family rather than reporting no cinema.
        name = city or CITY_IDS.get(int(cid) if str(cid).isdigit() else -1, "")
        return prefix + translit_city(name) if name else ""

    def cinema_movies(self, city: str = "", query: str = "",
                      max_pages: int = 8,
                      city_id: int | str = 0) -> tuple[list[dict], int, int]:
        """Movies playing today in `city`, as (matches, scanned, listing_total).

        The collection code comes from the server's own shelf list; it is only a
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
        code = self.afisha_collection_code("Segodnya-v_kino_", "movie",
                                           city=city, city_id=city_id)
        if not code:
            raise TbankApiError(
                "NO_TODAY_SHELF",
                f"у города {city or city_id} нет полки «сегодня в кино» — "
                "возможно, в нём нет проката")
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

    def cinema_schedule(self, event_id: str = "", date: str = "", city: str = "",
                        latitude: float = 0.0, longitude: float = 0.0,
                        object_id: str = "") -> list[dict]:
        """Showtimes on one date (YYYY-MM-DD). Three shapes, all capture-shaped:

        * object_id alone — EVERYTHING that cinema plays that day, in one request.
          This is the only way to a cinema's repertoire: no other endpoint answers
          «what is on at this venue» for film, and the alternative is asking the
          schedule of every film in the city one by one. Verified live against Каро
          11: 24 films, 48 showings, one call — and one of those films was missing
          from the city listing entirely, because that listing is today's and the
          film only ran the next day.
        * event_id + object_id — one film at one cinema.
        * event_id + city — that film across the city, sorted by distance.

        The city rides as a NAME here, not as the numeric cityId the rest of the
        afisha uses; the captured bodies carry "city": "Москва". It is not
        defaulted, because a Moscow listing is a plausible-looking answer to a
        question about somewhere else. With object_id the city is not sent at all —
        the venue already fixes it.

        The location only sorts by distance — the whole city is returned either way.
        Pass latitude/longitude to sort around a real point; omitted, the centre of
        `city` is used, and for a city not in CITY_CENTRES the distance sort is
        dropped rather than anchored somewhere arbitrary."""
        if not (event_id or object_id):
            raise TbankApiError("NO_TARGET",
                                "нужен event_id (фильм) или object_id (кинотеатр)")
        body: dict = {"date": date}
        if object_id:
            body["objectId"] = str(object_id)
            if event_id:
                body["eventId"] = str(event_id)
            data = self._call_read("schedule_movie", body=body)
            lst = (data or {}).get("list") if isinstance(data, dict) else data
            return lst if isinstance(lst, list) else []
        if not str(city).strip():
            raise TbankApiError("CITY_REQUIRED",
                                "не назван город; cinema_schedule требует city "
                                "или object_id кинотеатра")
        body["eventId"] = str(event_id)
        body["city"] = city
        if not (latitude or longitude):
            latitude, longitude = self.CITY_CENTRES.get(city, (0.0, 0.0))
        if latitude or longitude:
            body["sort"] = {"by": "distance"}
            body["location"] = {"latitude": latitude, "longitude": longitude}
        data = self._call_read("schedule_movie", body=body)
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        return lst if isinstance(lst, list) else []

    CATALOG_PAGE = 20

    @staticmethod
    def _date_bounds(date_from: str, date_to: str) -> dict:
        """{from, to} covering whole days. The +03:00 is a literal: the afisha runs
        on Moscow time and the captured bodies say so, so deriving it from the host
        clock would move the window on a machine in another zone."""
        a = str(date_from or date_to or "").strip()
        b = str(date_to or date_from or "").strip()
        if not a:
            raise TbankApiError("DATE_REQUIRED", "нужна дата: date_from (и date_to)")
        return {"from": f"{a}T00:00:00+03:00", "to": f"{b}T23:59:59+03:00"}

    def afisha_catalog(self, kind: str = "movie", city: str = "",
                       date_from: str = "", date_to: str = "",
                       city_id: int | str = 0, query: str = "",
                       count: int = 0, max_pages: int = 8) -> tuple[list[dict], int]:
        """Everything of one vertical playing in a date RANGE, as (events, amount).

        The app only asks for one day because its calendar picks one, but the
        server takes a range — an eight-day window in Moscow answers with 197
        unique films against 83 for a single day, and the extra titles are real
        one-off screenings. An event repeats across the days it runs, so results
        are deduplicated on eventId, first occurrence winning.

        Two shapes hide behind one call. movie ignores count/page and hands back
        the vertical whole, with EMPTY slots — the showings live in
        cinema_schedule. concert and spectacle paginate for real and do carry
        slots. Exhibitions have no catalogue at all; nothing in the captures posts
        to /api/events/exhibition, so this refuses rather than inventing a path.

        `query` is matched here, not by the server, which is why the pages are all
        read before filtering."""
        v = vertical(kind)
        if not v["catalog_key"]:
            raise TbankApiError(
                "NO_CATALOG",
                f"у вертикали «{kind}» нет каталога по датам; смотри "
                "search_app(screen=\"afisha\") или place_schedule()")
        cid = city_id_of(city, city_id)
        window = self._date_bounds(date_from, date_to)
        size = count if count > 0 else self.CATALOG_PAGE

        def fetch(page: int) -> tuple[list, int]:
            data = self._call_read(v["catalog_key"], body={
                "cityId": cid, "count": size, "page": page, "date": window})
            lst = [e for e in ((data or {}).get("list") or []) if isinstance(e, dict)]
            return lst, int((data or {}).get("amount") or 0)

        out, amount = fetch(1)
        if v["catalog_paged"] and len(out) < amount:
            pages = min(max(1, max_pages), -(-amount // size))
            if pages > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(4, pages - 1)) as pool:
                    for events, _ in pool.map(fetch, range(2, pages + 1)):
                        out.extend(events)
        seen, uniq = set(), []
        for e in out:
            key = str(e.get("eventId") or id(e))
            if key not in seen:
                seen.add(key)
                uniq.append(e)
        scanned = len(uniq)
        if query:
            q = _norm_city(query)
            uniq = [e for e in uniq if q in _norm_city(e.get("eventName") or "")]
        # `scanned` alongside `amount`, the way cinema_movies already reports it.
        # Without it the caller cannot tell «these are all of them» from «these are
        # the first max_pages×count of them» — and the `query` above filters the
        # SCANNED slice, so «ничего не найдено» was also being said about events the
        # scan never reached.
        return uniq, scanned, amount

    # ---- rail --------------------------------------------------------------

    def train_search(self, origin: str, destination: str, date: str,
                     adults: int = 1, children: int = 0) -> tuple[list[dict], str]:
        """Trains for one direction on one date, as (ways, trainSearchId).

        origin/destination are the bank's NUMERIC station codes (2000000 is
        Moscow). Nothing in the captures turns a city name into one, so the code
        is the caller's to supply — guessing it would send someone to the wrong
        city with a plausible-looking answer."""
        body = {"directions": [{"origin": str(origin),
                                "destination": str(destination),
                                "departureDate": date}],
                "adultsCount": adults, "childrenCount": children}
        data = self._call_read("train_search", body=body) or {}
        dirs = data.get("directions") or []
        ways = (dirs[0] or {}).get("ways") if dirs else []
        return ([w for w in (ways or []) if isinstance(w, dict)],
                str(data.get("trainSearchId") or ""))

    def train_calendar(self, origin: str, destination: str) -> list[dict]:
        """Which dates are on sale for a direction — a cheap way to check a pair
        of station codes is valid before searching a date that has no trains."""
        data = self._call_read("train_calendar", overrides={
            "origin": str(origin), "destination": str(destination)}) or {}
        return [d for d in (data.get("dates") or []) if isinstance(d, dict)]

    # ---- flights -----------------------------------------------------------

    def flight_history(self) -> list[dict]:
        """Past flight searches. Also the only place an airport code comes back
        WITH its name — nothing in the captures resolves a name to an IATA code,
        so this is where an agent can learn that Москва is MOW."""
        data = self._call_read("flight_history")
        return [h for h in (data or []) if isinstance(h, dict)]

    def flight_search(self, from_code: str, to_code: str, date: str,
                      adults: int = 1, children: int = 0, infants: int = 0,
                      cabin: str = "Y", only_bookable: bool = False,
                      max_batches: int = 8, deadline_s: float = 45.0) -> dict:
        """Flights, as {searchId, flights, offers, complete, batches}.

        The search STREAMS: the first call returns a batch and nextBatch blocks
        until the following one is ready, setting isOver on the last. Measured on
        one route: 4 batches, 757 flights, 4348 offers, the last batch alone
        adding 2836 — so an unbounded loop is a minute and a five-figure list.

        offers[].flights index the CONCATENATION of all batches, not the batch
        they arrived in (757 flights, highest index 756), so nothing can be
        resolved until the stream is stitched — and a caller that stops early
        must be told, which is what `complete` is for.

        only_bookable stops after the first batch. Only vendor == "Tinkoff" offers
        can be bought inside the bank, and on that route all 101 of them arrived
        in that first batch, so the other three round trips buy nothing but
        partner listings that lead out of the app."""
        body = {"segments": [{"from": from_code.upper(), "to": to_code.upper(),
                              "date": date}],
                "passengers": {"adults": adults, "children": children,
                               "infants": infants},
                "cabin": cabin, "composite": 0, "groupsLimit": 4000,
                "aviasales": True}
        first = self._call_read("flight_search_start", body=body) or {}
        search_id = str(first.get("searchId") or "")
        flights = list(first.get("flights") or [])
        offers = list(first.get("offers") or [])
        complete = bool(first.get("isOver")) or only_bookable
        batches = 1
        if not complete and search_id:
            started = time.monotonic()
            while batches < max(1, max_batches):
                if time.monotonic() - started > deadline_s:
                    break
                nxt = self._call_read("flight_search_next",
                                      body={"searchId": search_id}) or {}
                batches += 1
                flights.extend(nxt.get("flights") or [])
                offers.extend(nxt.get("offers") or [])
                if nxt.get("isOver"):
                    complete = True
                    break
        return {"searchId": search_id, "flights": flights, "offers": offers,
                "complete": complete, "batches": batches,
                "info": first.get("info") or {}}

    # ---- marketplace (Шопинг) ---------------------------------------------

    def shop_geo(self) -> dict:
        """The delivery address, memoised. Search wants its lat/lon, and asking
        for it per search would double the cost of every query."""
        memo = getattr(self, "_memo", None)
        if memo is None:
            memo = self._memo = {}
        if "shop_geo" not in memo:
            memo["shop_geo"] = self._call_read("shop_address") or {}
        return memo["shop_geo"]

    def shop_search(self, query: str, offset: int = 0,
                    size: int = 20) -> tuple[list[dict], list[dict], int]:
        """Marketplace products, as (products, partners, total_hits).

        Paging is the server's here — offset/size are real — unlike the afisha
        listings where a name has to be matched locally.

        The seller lives in a separate `partners` list, keyed by the product's
        dolyameShopId; the product itself only carries the id."""
        geo = self.shop_geo()
        ov = {"search": query, "offset": str(max(0, offset)), "size": str(size)}
        if geo.get("latitude") and geo.get("longitude"):
            ov["latitude"] = str(geo["latitude"])
            ov["longitude"] = str(geo["longitude"])
        data = self._call_read("shop_search", overrides=ov) or {}
        return ([p for p in (data.get("products") or []) if isinstance(p, dict)],
                [p for p in (data.get("partners") or []) if isinstance(p, dict)],
                int(data.get("totalHits") or 0))

    def shop_carts(self) -> list[dict]:
        """Carts, one per seller. body=None: the captured call sends
        Content-Length: 0, and body={} would put a literal `{}` on the wire."""
        data = self._call_read("shop_carts") or {}
        return [c for c in (data.get("carts") or []) if isinstance(c, dict)]

    def ticket_artifacts(self, order_id: str) -> dict:
        """What is actually presented at the door, for one order.

        It lives in the ORDERS FEED, in the order's `fields` — not in
        order_details, which carries the booking code and nothing else. The
        obvious-looking /api/tickets/get is dead: four calls, four code=228, so no
        template exists for it and none should be added.

        Coverage is partial and the caller has to say so: across 75 afisha orders
        every one carried a reservationCode but only 53 carried a `qr`, and the
        partners differ in what they hand out — Рамблер gives a pdfUrl, Ticketland
        gives neither. An unpaid reservation is not in this feed at all, so an
        empty answer means «no ticket yet», not «no such order»."""
        row = next((o for o in self.orders()
                    if str(o.get("orderId")) == str(order_id)), None)
        if row is None:
            return {}
        f = row.get("fields") or {}
        return {
            "found": True,
            "status": str(row.get("status") or ""),
            "event": str(f.get("eventName") or row.get("title") or ""),
            "venue": str(f.get("objectName") or f.get("objectForeignName") or ""),
            "hall": str(f.get("hallName") or ""),
            "partner": str(f.get("partnerName") or ""),
            "reservation_code": str(f.get("reservationCode") or ""),
            # A short payload string ("QQ1AB2C"), not an image: it is what the
            # scanner reads, and rendering it as a barcode is the client's job.
            "qr": str(f.get("qr") or ""),
            "barcode_type": str(f.get("barcodeType") or ""),
            "pdf_url": str(f.get("pdfUrl") or ""),
            "ticket_url": str(f.get("ticketUrl") or ""),
        }

    # ---- venues ----------------------------------------------------------

    PLACES_PAGE = 100              # the endpoint's ceiling: 116 answers 400

    def afisha_places(self, kind: str = "movie", city: str = "",
                      city_id: int | str = 0, query: str = "",
                      max_pages: int = 4) -> tuple[list[dict], int]:
        """Venues of one vertical in one city, as (matches, total).

        There is no server-side search here — no captured request carries a name
        parameter of any kind — so `query` is matched locally, which means every
        page has to be read before filtering, exactly as with the film listing.

        A 204 from this endpoint means the vertical is not serving, NOT that the
        city has no venues: live, cinema answers while concert, theatre and
        exhibition all return 204. The distinction matters — «нет площадок» and
        «раздел не отвечает» are different answers to the user.

        Pages after the first are fetched concurrently, same as cinema_movies()/
        afisha_catalog() and for the same reason: page 1 states the true total, so
        the page count is known after one request and the rest cost one round trip."""
        cid = city_id_of(city, city_id)
        service = vertical(kind)["service"]

        def fetch(page: int) -> tuple[list, int]:
            data = self._call_read("events_places", overrides={
                "service": service, "cityId": cid, "page": str(page),
                "count": str(self.PLACES_PAGE)})
            objs = [o for o in ((data or {}).get("objects") or [])
                    if isinstance(o, dict)]
            return objs, int(((data or {}).get("pagination") or {}).get("totalItems") or 0)

        out, total = fetch(1)
        pages = min(max(1, max_pages), -(-total // self.PLACES_PAGE) if total else 1)
        if pages > 1 and len(out) < total:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, pages - 1)) as pool:
                for objs, _ in pool.map(fetch, range(2, pages + 1)):
                    out.extend(objs)
        q = _norm_city(query) if query else ""
        if q:
            out = [o for o in out if q in _norm_city(o.get("name") or "")]
        return out, total

    def place_schedule(self, object_id: str, page: int = 1,
                       count: int = 50) -> tuple[list[dict], int]:
        """What is on at one venue, as (events, total).

        Concerts, theatre and exhibitions only — a cinema's repertoire lives in
        cinema_schedule(object_id=…, date=…) instead."""
        data = self._call_read("place_schedule", overrides={
            "objectId": str(object_id), "page": str(page), "count": str(count)})
        payload = data or {}
        lst = payload.get("list") or payload.get("events") or []
        return ([e for e in lst if isinstance(e, dict)],
                int(payload.get("amount") or 0))

    def place_info(self, object_id: str, with_halls: bool = False) -> dict:
        """Venue card. `geo.address` came back empty in every captured call, so
        with_halls stitches in the halls answer, which does carry addresses."""
        data = self._call_read("place_info", overrides={"objectId": str(object_id)})
        out = data if isinstance(data, dict) else {}
        if with_halls:
            try:
                halls = self._call_read("place_halls",
                                        overrides={"objectId": str(object_id)})
                out = dict(out)
                out["halls"] = (halls or {}).get("list") or (halls or {}).get("halls") or []
            except TbankApiError:
                pass
        return out

    # ---- ticket booking (cinema + concerts) ------------------------------

    def event_seats(self, event_id: str, slot_id: str, object_id: str,
                    kind: str = "movie", sector_id: str = "") -> list[dict]:
        """Hall layout for one showing: every seat with status and price.

        Cinemas key seats as "row:number". The other three verticals use a
        composite string ("Фанзона|5000§~§54093386|default") that must be passed
        back to order/create verbatim — it encodes sector, price and ticket id.

        sector_id narrows the answer to one sector; the app sends it when the
        buyer has already picked one off the hall map."""
        key = vertical(kind)["sectors_key"]
        params = {"eventId": str(event_id), "slotId": str(slot_id),
                  "objectId": str(object_id)}
        if sector_id:
            params["sectorId"] = str(sector_id)
        data = self._call_read(key, overrides=params)
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        return lst if isinstance(lst, list) else []

    def concert_hall(self, event_id: str, slot_id: str, object_id: str,
                     kind: str = "concert") -> dict:
        """Free-seating venues answer here instead of /sectors: sectors with
        `freeSeating: true` and a ticket count rather than a seat grid.

        Only concerts and theatre have this screen — the captures hold no
        /api/scheme/hall/exhibition at all, and cinemas number their seats.

        READ ONLY on purpose — the capture has no order/create example for this
        purchase screen, so the request body for it is unknown and this client
        will not invent one."""
        key = vertical(kind)["hall_key"]
        if not key:
            raise TbankApiError(
                "NO_FREE_SEATING",
                f"{kind}: у этой вертикали нет схемы со свободной рассадкой — "
                "места смотри в event_seats()")
        data = self._call_read(key, overrides={
            "eventId": str(event_id), "slotId": str(slot_id),
            "objectId": str(object_id)})
        return data if isinstance(data, dict) else {}

    def event_showings(self, event_id: str, kind: str = "concert",
                       object_id: str = "") -> list[dict]:
        """Showings of one concert, play or exhibition.

        Unlike movies these carry no date in the request and the answer covers
        everything scheduled ahead, so filtering by day is the caller's job.
        object_id narrows it to a single venue."""
        body: dict = {"eventId": str(event_id)}
        if object_id:
            body["objectId"] = str(object_id)
        data = self._call_read(vertical(kind)["schedule_key"], body=body)
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        return lst if isinstance(lst, list) else []

    def create_ticket_order(self, event_id: str, slot_id: str, object_id: str,
                            seats: list[dict], kind: str = "movie") -> dict:
        """Reserve seats. Creates an order and moves NO money — payment is a
        separate call. An order left unpaid expires by itself.

        seats: [{"id": "7:10", "type": "basic"}] for cinemas; concerts take the
        composite seatId and no type."""
        key = vertical(kind)["create_key"]
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

    def order_cancel_context(self, order_id: str) -> dict:
        """What is worth knowing BEFORE cancelling, from one order_details() call:
        {"available": bool|None, "status": str, "payment_id": str, "found": bool}.

        `available` is the bank's own isCancelAvailable and is the only field that
        predicted the outcome across every observed attempt: the one order flagged
        true cancelled, the seven flagged false were refused and stayed put. None
        means the field was absent — that is not a refusal, just no signal.

        Read `status`, not `paidFor`: an unpaid reservation carries paidFor=true as
        well, so that field says nothing about payment. Unpaid ones sit under the
        same `orderInfo` key and never appear in the orders() feed at all."""
        try:
            data = self.order_details(order_id)
        except TbankApiError:
            return {"available": None, "status": "", "payment_id": "", "found": False}
        info = data.get("orderInfo") if isinstance(data.get("orderInfo"), dict) else data
        cart = data.get("cartInfo") if isinstance(data.get("cartInfo"), dict) else {}
        avail = info.get("isCancelAvailable")
        return {
            "available": avail if isinstance(avail, bool) else None,
            "status": str(info.get("status") or ""),
            "payment_id": str(cart.get("paymentId") or ""),
            "found": bool(info),
        }

    def payment_id_for_order(self, order_id: str) -> str:
        """paymentId of one order, "" if it has none.

        The order's own cartInfo carries it and is one request; the orders() feed
        is the fallback, because a record there can hold a paymentId the card does
        not return. An unpaid reservation has neither."""
        try:
            cart = self.order_details(order_id).get("cartInfo") or {}
            if isinstance(cart, dict) and cart.get("paymentId"):
                return str(cart["paymentId"])
        except TbankApiError:
            pass
        return next((str(o.get("paymentId")) for o in self.orders()
                     if str(o.get("orderId")) == str(order_id) and o.get("paymentId")),
                    "")

    def cancel_ticket_order(self, order_id: str, kind: str = "movie",
                            payment_id: str = "") -> Any:
        """Cancel a ticket order. `orderId` rides in the query, `paymentId` next to
        it when the order has one, and the body stays empty.

        What decides the outcome is the order's own `isCancelAvailable`. The single
        captured success (delete-order.xml) cancelled a PAID order the bank had
        flagged cancelable: 200 {"status":"Success"}, and it moved to
        PARTIALLY_CANCELED — tickets refunded, service fee not. Seven live attempts
        on orders flagged `isCancelAvailable: false` answered 200 with a business
        refusal ({"status":"Failed","code":…}) and changed nothing; sending the same
        request as form-urlencoded made no difference.

        Read the verdict from the payload, not from the transport: this returns
        normally for a business refusal (the outer envelope is "Ok") and only raises
        when the request itself failed.

        payment_id is looked up from the order when the caller hasn't got it. An
        unpaid reservation has none, and does not need cancelling — it expires."""
        if not payment_id:
            payment_id = self.payment_id_for_order(order_id)
        key = vertical(kind)["cancel_key"]
        params = {"orderId": str(order_id)}
        if payment_id:
            params["paymentId"] = str(payment_id)
        # body=None, not {}: `json={}` puts a literal two-byte body on the wire and
        # both captured cancels send Content-Length: 0. The grocery flavour already
        # gets this right (grocery_order_cancel) — this one never got the same fix.
        return self._call_read(key, overrides=params)

    def cancel_grocery_order(self, order_id: str) -> dict:
        """Cancel a grocery (Город) order — paid or not. The app's request
        (cancel-grossary.xml) differs from the ticket flavour on both points that
        bit us there: ONLY orderId rides in the query — no paymentId — and the
        body is genuinely EMPTY (Content-Length: 0), still stamped
        Content-Type: application/json.

        The verdict is payload.{status,code}, NOT the outer envelope — the host
        wraps a refused cancellation in "status":"Ok" too. Observed: 605 = the
        order is already cancelled."""
        data = self._call_read("grocery_order_cancel",
                               overrides={"orderId": str(order_id)})
        return data if isinstance(data, dict) else {}

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
    # needs the phone. `statements` needs the account: both captured calls send
    # account + dateFrom + itemsOrder, and without them /v1/statements answers
    # INVALID_REQUEST_DATA — so the section was advertised in the tool description
    # and could not work, for anyone, ever. dateFrom/itemsOrder are supplied below.
    # Sections whose endpoint is a FILTER: without its argument it answers about
    # nothing at all. The last three were documented in FLOWS §7 and worked only by
    # falling through to the template name, which silently DROPPED `arg` — so
    # get_data("full_debt_amount", "<account>") asked the bank for the debt of no
    # account and the empty answer read as «долгов нет». Parameter names are the
    # app's own, taken from the captures: account_details?id=, full_debt_amount?
    # account=, statement_exist?account= (+ statementId, which this tool cannot
    # supply — see the docstring).
    _SECTION_ARG = {"providers": "ids", "requisites": "pointer",
                    "statements": "account", "account_details": "id",
                    "full_debt_amount": "account", "statement_exist": "account"}

    # Sections whose endpoint validates the SESSIONID, not just the Bearer. The
    # sessionid's CLIENT window is ~11 minutes, so these fail long before the token
    # expires — and they fail with a privileges/subscriber error that reads like an
    # empty result. Found live: a real unpaid bill was invisible through get_data
    # while the same call succeeded right after session_status() raised the level.
    # Measured, not guessed: with the session let lapse to ANONYMOUS, each endpoint
    # below was called directly and then re-called after a re-mint. Ten refused and
    # ten recovered — under FIVE different codes for one cause:
    #   REQUEST_RATE_LIMIT_EXCEEDED  get_requisites, list_regular_payments
    #   INSUFFICIENT_PRIVILEGES      payment_templates, invoices_to_pay,
    #                                autopayments, sbp_subscriptions,
    #                                manager_info, client_offers
    #   OPERATION_REJECTED           subscription_all, subscription_all_bills
    #   INTERNAL_ERROR               card_credentials
    # accounts_light, active_loans, operations, user_profile, contact_list,
    # bank_info and providers_compatible_page answered fine while ANONYMOUS — so
    # this is a real subset, not "everything", and the ping it costs stays targeted.
    _SECTION_NEEDS_CLIENT = {"invoices", "subscription_bills", "subscriptions",
                             "templates", "autopayments", "sbp",
                             "requisites", "manager", "offers"}

    def get_data(self, section: str, arg: str = "", days: int = 30) -> Any:
        """Unified getter for banking data.

        `arg` is required by the sections in _SECTION_ARG (providers → a
        comma-separated id list; requisites → a phone). Passing it for any other
        section is ignored. `days` sets the statements window — it used to be a
        buried literal 30, so older statements were unreachable through any
        argument while the answer read as complete."""
        _SECTIONS = {
            "subscriptions": "subscription_all",
            # NO subscriptionIds. An audit finding said the missing filter was why an
            # unpaid ГИБДД fine read as «счетов нет», and that the app's two-request
            # chain had to be copied. Implementing it returned nothing at all; the
            # live numbers say why:
            #     /v1/subscription/all_bills                   → 2 records, 4 bills
            #     /v1/subscription/all_bills?subscriptionIds=… → 0 records
            #     /v1/subscription/all                         → 0 subscriptions
            # The parameter NARROWS; the app sends it because it is rendering one
            # provider's screen. The bill really was invisible — but because this
            # section validates the sessionid, whose CLIENT window is ~11 minutes,
            # which _SECTION_NEEDS_CLIENT now raises first.
            "subscription_bills": "subscription_all_bills",
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
            "account_details": "account_details",
            "full_debt_amount": "full_debt_amount",
            "statement_exist": "statement_exist",
            # "qr_resolve" НЕ здесь: resolve_payment_qr — это POST, чей единственный
            # вход — тело {barcodeHash, qr, frontendFeatureFlag}, а get_data тела не
            # передаёт. Секция уходила запросом без QR и не могла ничего разрешить.
            # Рабочий путь — payment_qr(qr).
        }
        if section.lower() == "profile":
            # /userinfo/userinfo needs client_id=gorod-app + no mobile-BFF params —
            # route through _call_userinfo, not the generic _call_read (which 401s).
            return self._call_userinfo()
        if section.lower() in self._SECTION_NEEDS_CLIENT:
            # These validate the sessionid, not just the Bearer, and its CLIENT
            # window is ~11 minutes against the token's ~2h. Without this they
            # answer INSUFFICIENT_PRIVILEGES / «Невозможно определить подписчика»
            # once the window lapses — which reads to an agent as "you have no
            # bills", not as "the session needs raising". Costs one ping, and only
            # for the sections that actually check.
            self.ensure_client_session()
        if section.lower() not in _SECTIONS:
            # Раньше неизвестная секция уезжала В ИМЯ ШАБЛОНА: get_data("v1_pay")
            # выполняло POST /v1/pay, get_data("grocery_cart_set") — запись корзины,
            # get_data("order_cancel") — отмену заказа. Тул помечен
            # readOnlyHint=True/idempotentHint=True, то есть хост вправе выполнить
            # его без спроса. Перечень закрыт.
            raise TbankApiError("UNKNOWN_SECTION",
                f"get_data('{section}') — такой секции нет. Доступные: "
                + ", ".join(sorted(_SECTIONS))
                + ". Разобрать платёжный QR — payment_qr(qr).")
        key = _SECTIONS[section.lower()]
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
                       "Pass the account id from list_accounts()."
                       if arg_key in ("account", "id") else
                       "For a recipient lookup prefer transfer_sbp_resolve(phone), "
                       "which parses the same response; for YOUR OWN account details "
                       "use account_requisites(account_id) — a different endpoint."))
            ov = {arg_key: arg}
            if section.lower() == "statements":
                # The other two query params the app always sends with the account.
                start, _ = ms_for_period(days)
                ov.update({"dateFrom": str(start), "itemsOrder": "desc"})
            return self._call_read(key, overrides=ov)
        return self._call_read(key)


_SESSION_EXPIRED = {
    "NOT_AUTHORIZED", "SESSION_EXPIRED", "SESSION_NOT_FOUND", "NO_SESSION",
    "UNAUTHORIZED", "DEVICE_LINK_REMOVED", "REAUTH", "INVALID_SESSION",
    "invalid_grant", "invalid_token",
}


def ms_for_period(days: int = 30) -> tuple[int, int]:
    end = int(time.time() * 1000)
    return end - days * 86400 * 1000, end
