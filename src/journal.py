"""Persistent checkout attempt journal (Phase 3, #10 — idempotency/reconciliation).

Append-only JSONL at ``~/.local/share/tbank-mcp/attempts.jsonl`` (env
``TBANK_ATTEMPTS`` overrides). One line = one event. An attempt's *state* is the
``status`` of its last event.

Why: ``order/create`` is a real-money POST with no backend idempotency key we can
rely on. If the call times out or returns no ``orderId`` we CANNOT prove the
backend didn't create the order — so an automatic retry could place a DUPLICATE.
This journal records each attempt's progress; after an UNKNOWN result we block the
auto-retry and point the user to ``grocery_attempts()`` for reconciliation.

Statuses (per event): ``started | delivery_ready | order_posted | paid | failed | unknown``
  * ``paid``                       → done; block (already ordered + paid)
  * ``order_posted`` / ``unknown`` → an order MAY already exist; block auto-retry
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

ATTEMPTS_FILE = os.environ.get(
    "TBANK_ATTEMPTS",
    os.path.expanduser("~/.local/share/tbank-mcp/attempts.jsonl"),
)

# statuses that mean "an order may already exist — do NOT auto-retry"
_BLOCKING = {"order_posted", "unknown", "paid"}


def _ts() -> float:
    return time.time()


def _append(rec: dict) -> None:
    os.makedirs(os.path.dirname(ATTEMPTS_FILE), exist_ok=True)
    rec["ts"] = _ts()
    with open(ATTEMPTS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


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


def recent(limit: int = 20) -> list[dict]:
    """Last event of each of the most recent N attempts (for reconciliation UI)."""
    by_aid: dict[str, dict] = {}
    order: list[str] = []
    for e in _events():
        aid = e.get("attempt_id")
        if not aid:
            continue
        if aid not in by_aid:
            order.append(aid)
        by_aid[aid] = e  # last event wins
    return [by_aid[aid] for aid in order[-limit:]]


def cart_hash_of(goods: list[dict]) -> str:
    """Stable, order-independent hash of cart item ids + counts."""
    pairs = sorted(
        (str(g.get("id") or g.get("goodId") or g.get("goodForeignId") or ""),
         str(g.get("count", 1)))
        for g in goods if isinstance(g, dict)
    )
    raw = "|".join(f"{i}:{c}" for i, c in pairs)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
