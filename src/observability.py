"""Redacted structured event log for safe diagnostics (Phase 4, #16).

Append-only JSONL at ``~/.local/share/tbank-mcp/events.jsonl`` (env ``TBANK_EVENTS``
overrides). Purpose: reconstruct one checkout/session attempt AFTER the fact — which
step ran, its HTTP/app status, who is to blame, how long it took — WITHOUT ever
exposing secrets or PII.

Emitted fields (per #16): ``attempt_id, step, appId, pointId, cart_hash, item_count,
amount, http_status, app_code, blame, order_id_present, payment_id_present, duration_ms``.

NEVER logged: tokens, cookies, the delivery address, phone, email, account/card
numbers. Redaction is defense-in-depth — applied by KEY NAME and by VALUE PATTERN —
and every string value is truncated, so even an accidental full response dump cannot
leak a secret. The events file is safe to share for debugging.
"""
from __future__ import annotations

import json
import os
import re
import time

EVENTS_FILE = os.environ.get(
    "TBANK_EVENTS",
    os.path.expanduser("~/.local/share/tbank-mcp/events.jsonl"),
)

_MAX_VAL = 300  # truncate any string value longer than this

# key fragments (case-insensitive substring) that mark a secret / PII field → redacted
_REDACT_KEY = (
    "token", "cookie", "authoriz", "password", "passwd", "pin", "otp", "sms",
    "address", "phone", "tel", "email", "e-mail", "account", "cardnum", "card",
    "pan", "cvv", "cvc", "cipher", "sessionid", "session_id", "sso", "secret",
    "bearer", "apikey", "api_key", "fingerprint", "deviceid", "device_id",
    "passport", "inn", "login", "credential",
)

# value patterns that look like a secret regardless of the key name
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]+){0,2}")
_RE_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# any 40+ char base64/hex run — refresh_token (86), access_token (88), cipher_key (86),
# fingerprint blob (1333) — standalone OR embedded in a string. Short values
# (cart_hash=16, uuid=32, order_id=12 digits) are NOT matched.
_RE_BLOB = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")


def _is_sensitive_key(k: str) -> bool:
    kl = str(k).lower()
    return any(frag in kl for frag in _REDACT_KEY)


def _redact_value(v):
    """Recursively redact secrets/PII in a value and truncate long strings."""
    if isinstance(v, str):
        v = _RE_JWT.sub("<jwt>", v)
        v = _RE_CARD.sub("<card>", v)
        v = _RE_BLOB.sub("<redacted-blob>", v)
        if len(v) > _MAX_VAL:
            v = v[:_MAX_VAL] + "…<trunc>"
        return v
    if isinstance(v, dict):
        return {k: ("<redacted>" if _is_sensitive_key(k) else _redact_value(val))
                for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(x) for x in v][:50]
    return v


def _append(rec: dict) -> None:
    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    with open(EVENTS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def emit(step: str, **fields) -> None:
    """Emit one redacted structured event. Any secret/PII field (by key or value
    pattern) is scrubbed before writing. Never raises — observability must not break
    the flow it is observing."""
    try:
        rec: dict = {"ts": time.time(), "step": step}
        rec.update(fields)
        rec = _redact_value(rec)
        _append(rec)
    except Exception:
        pass


def blame_of(http_status, app_code=None) -> str:
    """Classify who is at fault for a failed call: backend / client / network / app."""
    try:
        s = int(http_status)
    except (TypeError, ValueError):
        s = -1
    if s == 0 or s < 0:
        return "network"
    if s >= 500:
        return "backend"
    if s >= 400:
        return "client"
    if app_code and str(app_code) not in ("OK", "0", "200", "None", ""):
        return "app"
    return "ok"


def recent(limit: int = 40, step: str | None = None) -> list[dict]:
    """Last N events (optionally filtered by step)."""
    if not os.path.exists(EVENTS_FILE):
        return []
    out: list[dict] = []
    with open(EVENTS_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if step and rec.get("step") != step:
                continue
            out.append(rec)
    return out[-limit:]


def for_attempt(attempt_id: str) -> list[dict]:
    """Reconstruct every event for one attempt (chronological)."""
    if not attempt_id or not os.path.exists(EVENTS_FILE):
        return []
    out: list[dict] = []
    with open(EVENTS_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("attempt_id") == attempt_id:
                out.append(rec)
    return out
