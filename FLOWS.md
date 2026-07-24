# T-Bank MCP — agent flows

Ordered tool-call sequences for common tasks. The session self-refreshes
(`ensure_fresh` → silent re-login, no OTP) on the first call of each flow, so you
don't call `refresh_session` manually unless a tool returns SESSION EXPIRED.

## 0. Bootstrap (first-time login)

**First-time login = phone → OTP (SMS) → password → session.**
Not just OTP — the bank requires the account password on the first login
on a new device. `login(phone)` returns which step is next (otp/password/pin).
Call the matching `confirm_*` tool.

1. `login(phone)` → SMS OTP sent (or password step).
2. `confirm_otp(otp)` → if bank returns `step: password`, continue.
3. `confirm_password(password)` → session minted. Persists `session.json`.

1. `login(phone)` → bank sends an SMS OTP. (phone = full form, e.g. `+79991234567`.)
2. `confirm_otp(otp)` → mints the session (access_token + sessionid + refresh_token
   + SSO_SESSION cookie). Persists `session.json`. After this, everything below
   works headless. Call this only once (or when the session is revoked).

## 1. Session / login (automatic, no OTP)

The MCP does this itself on first call or when the access_token nears expiry
(~2h). Documented so you understand why no phone is needed:

1. (internal) `auth/authorize` gorod-app + `SSO_SESSION` cookie → `{step:fingerprint, cid}`
2. (internal) `auth/step` `step=fingerprint` + static fingerprint blob → `{code}`
3. (internal) `auth/token/mobile` auth_code grant → `access_token` + `mobile.sessionid` + `refresh_token` (SSO-valid)
4. (messenger only) `issueTokenBySSO` {ssoToken} → `tmsgSessionID`

You normally just call a read tool; the above runs under the hood. Call
`session_status` / `keepalive` to check/extend.

## 2. Read accounts + recent purchases + spending

1. `list_accounts` → accounts + cards (take an `account.id`).
2. `list_operations(account_id, days=30)` → recent purchases.
3. `spending_categories(account_id, days=30)` → spend grouped by category (+ share %).
   (or `operations_histogram(account_id, days, period, group_by)` for flexible
   breakdown by category/merchant/mcc.)

## 3. Grocery cart assembly → order → pay  (Город) — PROVEN end-to-end

> **Store context is mandatory.** Get `app_id`/`point_id` from `grocery_stores()` and pass
> them to `grocery_search` / `grocery_plan_order` / `grocery_add_to_cart` / `grocery_cart` /
> `grocery_checkout`. There is NO silent default store — without explicit context the tools
> return `NO_STORE_CONTEXT`, and mixing contexts makes the cart look empty. Keep app_id/pointId
> identical across the whole add → cart → checkout flow.

1. `grocery_goods(category_id, app_id, point_id, page)` → search catalog.
   Requires: `sortBy=DEFAULT` (not `sort`), `onlyDirectGoods=false`, `categoryId`.
2. `grocery_cart_set(body)` → set cart on mobile API. Auto-fetches full address
   (with `details` — flat, houseType, doorphone) from GET cart; without `details`
   the backend crashes (code=100).
3. **Web cart sync** (checkout.py): set `portalSID` + `sessionID` + `deviceId` as
   cookies on .tbank.ru → links mobile cart → web checkout.
4. GET web cart → **actual sum** (weight-based items like potatoes may differ).
5. POST deliveries → init delivery slots.
6. POST order/create with ACTUAL web cart sum.
7. POST payment_gate_pay **immediately** (before auto-cancel) with
   `amount.currencyCode=643` (RUB). Returns `{paymentId, stage:{status:"SUCCESS"}}`.

> **Out-of-stock items** block order/create (code=211). Remove unavailable goods
> before ordering. Orders auto-cancel if not paid quickly — pay immediately.
> `grocery_order_create`, `checkout_process_order`, `payment_gate_pay` move real
> money — review the body before calling.

## 4. P2P transfer / bill pay  (signed)

1. `payment_commission(body)` → preview the fee (`payParameters` JSON).
2. `pay(body)` → **HMAC-signed** `v1/pay` (`x-api-signature` =
   base64(HMAC-SHA256(key=sessionid, msg=METHOD\n+path_tail\n+query\n+body))).
   `body` = form-encoded `payParameters=...` (provider p2p-anybank, moneyAmount,
   providerFields with the recipient). Moves real money.

> Only the `v1/pay`/`group_pay` paths are signed; grocery payment (`payment_gate_pay`)
> is cookie-only.

## 5. Messenger / support chat  (read + send)

1. `messenger_conversations()` → list chats (find the support chat
   `conversationId`, e.g. title "Поддержка").
2. `messenger_messages(conversation_id)` → read the chat history (newest first;
   `direction`/`message_id` to page).
3. `messenger_hints(conversation_id)` → quick-reply suggestions.
4. `messenger_faq(conversation_id)` → self-help FAQ.
5. `messenger_send_message(conversation_id, body)` → **send** a reply (real
   message, not money). `body` = JSON message body (or empty to replay).
6. `messenger_mark_read(conversation_id, message_id)` → mark read.
7. `messenger_unread()` → unread count across chats.

> Messenger needs a `tmsgSessionID` (JWT, ~1h), auto-minted via `issueTokenBySSO`
> from the silent-relogin access_token. No OTP — works as long as the long-lived
> `SSO_SESSION` cookie is obtained via login.

## 6. Invest browse

1. `invest_accounts()` → InvestBox/brokerage accounts (take `brokerAccountId`).
2. `invest_portfolio(broker_account_id, days)` → portfolio statistics.
3. `invest_operations(broker_account_id, operation_type, limit)` → broker ops.
4. `invest_securities(broker_account_id)` → purchased stocks/bonds/ETF.
5. `investbox_offers()` / `investbox_product_yield()` / `broker_margin()` /
   `invest_pension_profile()` → extras.

## 7. Credit / debt

1. `active_loans()` → active credits.
2. `credit_payment_schedule()` → payment schedule.
3. `credit_rating()` / `credit_recommendations()` → rating + advice.
4. `full_debt_amount()` / `account_details()` → debt + account detail.
5. `statements()` / `statement_exist()` → statements.

## Notes

- Every tool returns a short string (counts + summaries) or JSON; read its
  description in [TOOLS.md](TOOLS.md).
- On `SESSION EXPIRED`, call `refresh_session` (refresh_token → silent re-login,
  no OTP) and retry. If it returns `REAUTH_REQUIRED`, the user must re-login
  (login + OTP + password).
- `grocery_checkout` is safer now: post-delivery sum (cart re-read after deliveries),
  auto-selected payment account, and an attempt journal
  (`~/.local/share/tbank-mcp/attempts.jsonl`). After an UNKNOWN result (order may
  exist) the auto-retry is BLOCKED — reconcile via `grocery_attempts`, and only
  force a retry after the user confirms no order exists.
- Money tools (`pay`, `payment_gate_pay`, `grocery_order_create`,
  `checkout_process_order`) are REAL — confirm the body before running.
