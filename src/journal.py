"""Persistent checkout attempt journal (Phase 3, #10 — idempotency/reconciliation).

Append-only JSONL at ``~/.local/share/tbank-mcp/attempts.jsonl`` (env
``TBANK_ATTEMPTS`` overrides). One line = one event. An attempt's *state* is the
``status`` of its last event.

Why: ``order/create`` is a real-money POST with no backend idempotency key we can
rely on. If the call times out or returns no ``orderId`` we CANNOT prove the
backend didn't create the order — so an automatic retry could place a DUPLICATE.
This journal records each attempt's progress; after an UNKNOWN result we block the
auto-retry and point the user to ``grocery_attempts()`` for reconciliation.

Statuses (per event): ``started | delivery_ready | order_posting | order_posted | paid | failed | unknown``
  * ``paid``                       → done; block (already ordered + paid)
  * ``order_posting`` / ``order_posted`` / ``unknown``
                                   → an order MAY already exist; block auto-retry.
                                     ``order_posting`` is recorded the instant
                                     before we POST order/create — a crash/network
                                     drop from here on means the backend may have
                                     created the order without us seeing it.
  * ``started`` / ``delivery_ready`` → still pre-order; a generic runtime error here
                                     falls back to ``failed`` (safe to retry).
  * ``failed``                     → failed before any order POST (empty cart,
                                      delivery error, no payment account); safe to retry

What we store — store context (appId/pointId), a cart hash, the amount,
order/payment IDs, status, a short error code. NEVER tokens, cookies, the delivery
address, phone, email, or account/card numbers.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

from .observability import _redact_value as _redact

ATTEMPTS_FILE = os.environ.get(
    "TBANK_ATTEMPTS",
    os.path.expanduser("~/.local/share/tbank-mcp/attempts.jsonl"),
)

# statuses that mean "an order may already exist — do NOT auto-retry". ``order_posting``
# is the point-of-no-return (about to POST order/create); a crash there is UNKNOWN.
_BLOCKING = {"order_posting", "order_posted", "unknown", "paid"}
# public alias — the server reads this to classify a generic runtime exception.
BLOCKING_STATUSES = _BLOCKING


def _ts() -> float:
    return time.time()


def _append(rec: dict) -> None:
    """Append one redacted event with 0600 perms. Redaction (observability._redact_value)
    scrubs any secret/PII that a caller may have passed (e.g. a raw response dump)."""
    os.makedirs(os.path.dirname(ATTEMPTS_FILE), exist_ok=True)
    rec["ts"] = _ts()
    rec = _redact(rec)
    fd = os.open(ATTEMPTS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        os.chmod(ATTEMPTS_FILE, 0o600)  # enforce 0600 even if the file pre-existed
    except OSError:
        pass


def _events() -> list[dict]:
    if not os.path.exists(ATTEMPTS_FILE):
        return []
    out: list[dict] = []
    with open(ATTEMPTS_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def new_attempt(app_id: str, point_id: str, cart_hash: str, amount) -> str:
    """Start a new attempt. attempt_id is a uuid (unique even when two attempts
    for the same cart land in the same millisecond); recency comes from the ``ts``
    field + file order, not from the id itself."""
    aid = uuid.uuid4().hex[:12]
    _append({"attempt_id": aid, "step": "init", "status": "started",
             "app_id": app_id, "point_id": point_id,
             "cart_hash": cart_hash, "amount": amount})
    return aid


def record(attempt_id: str, step: str, status: str, **fields) -> None:
    """Append a progress event for an attempt."""
    rec = {"attempt_id": attempt_id, "step": step, "status": status}
    rec.update(fields)
    _append(rec)


def latest_for_cart(cart_hash: str) -> dict | None:
    """Last event of the most recent attempt for this cart_hash, or None."""
    events = _events()
    matching = [e for e in events if e.get("cart_hash") == cart_hash]
    if not matching:
        return None
    latest_aid = matching[-1].get("attempt_id")
    aid_events = [e for e in events if e.get("attempt_id") == latest_aid]
    return aid_events[-1] if aid_events else None


def is_retry_blocked(cart_hash: str) -> tuple[bool, dict | None]:
    """Should an auto-retry be blocked for this cart? Returns (blocked, last_event)."""
    last = latest_for_cart(cart_hash)
    if last and last.get("status") in _BLOCKING:
        return True, last
    return False, last


def last_status_of_attempt(attempt_id: str) -> str:
    """Last-recorded status for one attempt ('' if none). Used by the server to
    classify a GENERIC runtime exception (e.g. a Playwright crash) by how far the
    attempt got: a blocking last-status → UNKNOWN (order may exist), else FAILED."""
    if not attempt_id:
        return ""
    aid_events = [e for e in _events() if e.get("attempt_id") == attempt_id]
    return aid_events[-1].get("status", "") if aid_events else ""


def recent(limit: int = 20) -> list[dict]:
    """Last event of each of the most recent N attempts (for reconciliation UI).
    `limit <= 0` means every attempt on file — spelled out because `order[-0:]`
    happens to mean the same thing only by accident of slice syntax."""
    by_aid: dict[str, dict] = {}
    order: list[str] = []
    for e in _events():
        aid = e.get("attempt_id")
        if not aid:
            continue
        if aid not in by_aid:
            order.append(aid)
        by_aid[aid] = e  # last event wins
    return [by_aid[aid] for aid in (order if limit <= 0 else order[-limit:])]


def cart_hash_of(goods: list[dict]) -> str:
    """Stable, order-independent hash of cart item ids + counts."""
    pairs = sorted(
        (str(g.get("id") or g.get("goodId") or g.get("goodForeignId") or ""),
         str(g.get("count", 1)))
        for g in goods if isinstance(g, dict)
    )
    raw = "|".join(f"{i}:{c}" for i, c in pairs)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
