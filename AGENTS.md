# T-Bank MCP — agent guide

This is an MCP server (stdio) for the T-Bank mobile API. Connect it to your
agent (Claude/Codex/etc.), then call `flows` for the ordered tool sequences.

## Quick start (bootstrap once)

1. `login(phone)` — sends an SMS OTP. phone = full form, e.g. `+79991234567`.
2. `confirm_otp(otp)` — finishes login; mints the session. (If the bank returns a
   PIN step instead of OTP, call `confirm_pin(pin)`.)
3. The session persists; all reads + messenger now work headless. `ensure_fresh`
   silently re-logins (~every 2h) — no manual refresh needed.

## Key flows (call `flows` tool for the full text)

- **Read accounts + spending**: `list_accounts` → take `account.id` →
  `list_operations(account_id, days)` → `spending_categories(account_id, days)`.
- **Grocery cart → order → pay** (PROVEN): `grocery_goods(category_id=…)` →
  `grocery_cart_set(body)` (auto-fetches address.details) → web checkout via
  Playwright (portalSID+sessionID cookies sync mobile→web cart) → GET actual web
  cart sum → `deliveries` → `order/create` → `payment_gate_pay` immediately
  (currencyCode=643). Remove out-of-stock items first (they block order/create).
- **P2P transfer / bill pay** (SIGNED): `payment_commission` → `pay` (HMAC
  `x-api-signature`). Real money — confirm with the user before calling.
- **Messenger / support chat**: `messenger_conversations` → take
  `conversationId` → `messenger_messages` → `messenger_send_message`.
- **Invest**: `invest_accounts` → `invest_portfolio`/`invest_operations`/`invest_securities`.

## Safety

- **Money tools** (`pay`, `payment_gate_pay`, `grocery_order_create`,
  `checkout_process_order`) move REAL money. NEVER call without the user's
  explicit go-ahead; show the body first.
- `session.json` holds the secrets — keep it private. Canonical path:
  `~/.local/share/tbank-mcp/session.json` (override with `TBANK_SESSION`), 0600.
  Both `login_cli.py` and the MCP read this same file with no manual setup. At
  startup the MCP logs only path/size/mode — never tokens or cookies.
- On `SESSION EXPIRED`: call `refresh_session` — it tries `refresh_token` first,
  then silent re-login via SSO_SESSION (no OTP), and only returns
  `REAUTH_REQUIRED` if both fail (then the user must re-login). Retry the failed
  tool after a successful refresh.
