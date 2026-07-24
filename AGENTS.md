# T-Bank MCP — agent guide

This is an MCP server (stdio) for the T-Bank mobile API. Connect it to your
agent (Claude/Codex/etc.), then call `flows` for the ordered tool sequences.

## Quick start (bootstrap once)

1. `login(phone)` — sends an SMS OTP. phone = full form, e.g. `+7XXXXXXXXXX`.
2. `confirm_otp(otp)` — finishes login; mints the session. (If the bank returns a
   PIN step instead of OTP, call `confirm_pin(pin)`.)
3. The session persists; all reads + messenger now work headless. `ensure_fresh`
   silently re-logins (~every 2h) — no manual refresh needed.

## Key flows (call `flows` tool for the full text)

- **Read accounts + spending**: `list_accounts` → take `account.id` →
  `list_operations(account_id, days)` → `spending_categories(account_id, days)`.
- **Grocery order** (store context required): `grocery_stores()` → take `app_id`/`point_id` →
  `grocery_search` / `grocery_plan_order` (pass app_id/point_id) → `grocery_add_to_cart` →
  `grocery_cart` (SAME app_id/point_id) → show the user → `grocery_checkout` (REAL money).
  The web delivery→order→pay flow runs inside `grocery_checkout` (verified against captures.xml).
- **P2P transfer** (SIGNED, REAL money): `payment_commission` (preview) → `transfer`
  (HMAC x-api-signature inside). phone/СБП needs recipient member fields
  (bank_member_id/masked_fio/pointer_link_id); between own accounts use
  `provider="transfer-inner"`. Confirm with the user before calling.
- **Messenger / support chat**: `messenger_conversations` → take `conversationId` →
  `messenger_messages` → `messenger_send(conversation_id, text)`.
- **Invest**: `invest_accounts` → take `brokerAccountId` →
  `invest_portfolio` / `invest_operations` / `invest_securities`.

## Safety

- **Money tools** (`transfer`, `grocery_checkout`) move REAL money. NEVER call without
  the user's explicit go-ahead; show the amount/recipient (transfer) and store + sum
  (grocery_checkout) first.
- `session.json` holds the secrets — keep it private. Canonical path:
  `~/.local/share/tbank-mcp/session.json` (override with `TBANK_SESSION`), 0600.
  Both `login_cli.py` and the MCP read this same file with no manual setup. At
  startup the MCP logs only path/size/mode — never tokens or cookies.
- On `SESSION EXPIRED`: call `refresh_session` — it tries `refresh_token` first,
  then silent re-login via SSO_SESSION (no OTP), and only returns
  `REAUTH_REQUIRED` if both fail (then the user must re-login). Retry the failed
  tool after a successful refresh.
- `grocery_checkout` records an attempt journal
  (`~/.local/share/tbank-mcp/attempts.jsonl`); after an UNKNOWN result (order may
  have been created) an automatic retry is BLOCKED to prevent duplicate orders.
  Reconcile via `grocery_attempts` + `grocery_order_status(order_id)`, and pass
  `force=True` only after the user confirms no order exists. The checkout contract
  (agreement from `user/payment/account/last`, email from `get-customer-information`,
  post-delivery `cartPrice`) is verified against captures.xml.
- Checkout stages and session refresh emit redacted structured events to
  `~/.local/share/tbank-mcp/events.jsonl` — step, http_status, app_code, blame,
  duration, order/payment id presence. NEVER tokens, cookies, address, phone,
  email, or account numbers. Call `diagnostics()` to reconstruct an attempt / find
  the last confirmed step.
