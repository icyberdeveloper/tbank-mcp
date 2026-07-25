# T-Bank MCP — agent flows

Ordered tool-call sequences for common tasks. The session self-refreshes
(`ensure_fresh` → silent re-login, no OTP) on the first call of each flow, so you
don't call `refresh_session` manually unless a tool returns SESSION EXPIRED.

> **Tool names:** the **32 MCP tools** are the authoritative interface (see TOOLS.md
> and the skills). Some sections below describe INTERNAL api steps — e.g. the web
> checkout + HMAC signing run INSIDE `grocery_checkout` / `transfer`. Call the MCP
> tools, not the internal methods named in the prose (`pay`, `payment_gate_pay`,
> `grocery_goods`, `grocery_cart_set`, `active_loans` are NOT MCP tools).

## 0. Bootstrap (first-time login)

**First-time login = phone → OTP (SMS) → password → session.**
Not just OTP — the bank requires the account password on the first login
on a new device. `login(phone)` returns which step is next (otp/password/pin).
Call the matching `confirm_*` tool.

1. `login(phone)` → SMS OTP sent (or password step). phone = full form, e.g. `+7XXXXXXXXXX`.
2. `confirm_otp(otp)` → if bank returns `step: password`, continue.
3. `confirm_password(password)` → session minted. Persists `session.json`.

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
2. `grocery_cart_set(body)` → set cart on mobile API. The `delivery` block it builds
   has three non-obvious requirements, all capture-verified — get any of them wrong
   and cart/set answers HTTP 200 while storing nothing, so the next GET reads empty:
   - **`address.details`** (flat, houseType, doorphone, …) must be complete, and
     `details.streetWithType` is client-side — no GET returns it, copy it from `street`.
   - **`address` cannot come from the store's own cart.** A store the user has never
     ordered from HAS no cart, so there is no address to copy and the write is
     rejected — which means no cart is ever created and the next attempt fails the
     same way. Seed from `GET /api/grocery/client/info` → `payload.deliveryInfo.address`.
   - **`areaId`** is per-retailer and REQUIRED by the retailers that publish one
     (ВкусВилл appId=204, Лента appId=246). Azbuka (578) has none and its real bodies
     omit the key. The ONLY source is `GET /api/grocery/retailers` →
     `payload.categories[].retailers[].delivery.areaId`.
   `pointId` goes in the BODY under `delivery`, never in the query — only `appId`
   scopes the cart. And cart/set REPLACES the whole cart, so an "add" must resend the
   existing goods merged with the new ones.
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

1. `transfer_sbp_resolve(phone)` → resolve a phone to its SBP recipient banks
   (`GET /v1/get_requisites`, read-only). Returns `bankMemberId`/`maskedFIO`/
   `pointerLinkId` per bank + `isDefaultBank`. **Required for a NEW (unsaved)
   recipient** before commission/transfer; if several banks and no default, ask the
   user which bank (never silently pick — wrong bank = money gone).
2. `payment_commission(body)` → preview the fee (`payParameters`, same shape as
   transfer — with the resolved `providerFields`, `paymentType:"Transfer"`,
   `pointerType:"8276"`, `pointer:"+7…"`. Do NOT use the old `pointerType:"ACCOUNT"`,
   the bank rejects it → INVALID_REQUEST_DATA).
3. `transfer(amount, to_account, description, provider, bank_member_id, masked_fio,
   pointer_link_id)` → moves REAL money. The HMAC `x-api-signature` over `/v1/pay`
   (base64(HMAC-SHA256(key=sessionid, msg=METHOD+path_tail+query+body))) is applied
   INSIDE `transfer`. If the member fields are omitted, the recipient is AUTO-resolved
   (default bank, or single match; several-without-default → `RECIPIENT_MULTIPLE_BANKS`).
   `provider="transfer-inner"` for between-own-accounts.

> Only the `v1/pay`/`group_pay` paths are signed; grocery payment (`payment_gate_pay`)
> is cookie-only.

## 5. Messenger / support chat  (read + send)

1. `messenger_conversations()` → list chats (find the support chat
   `conversationId`, e.g. title "Поддержка").
2. `messenger_messages(conversation_id)` → read the chat history (newest first;
   `direction`/`message_id` to page).
3. `messenger_hints(conversation_id)` → quick-reply suggestions.
4. `messenger_faq(conversation_id)` → self-help FAQ.
5. `messenger_send(conversation_id, text)` → **send** a reply (real
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
- `grocery_checkout` contract is verified against captures.xml: agreement from
  `user/payment/account/last`, clientEmail from `get-customer-information`,
  post-delivery sum from deliveries `payload.cartPrice`, and no blind sleep (it polls
  the cart API instead). After an UNKNOWN result the auto-retry is BLOCKED — reconcile
  via `grocery_attempts` + `grocery_order_status(order_id)`, and force only after the
  user confirms no order exists.
- Diagnostics: checkout stages (delivery/order/payment) and session refresh emit
  redacted structured events to `~/.local/share/tbank-mcp/events.jsonl` (no
  secrets/PII). Call `diagnostics()` to reconstruct an attempt and find the last
  confirmed step.
- Money tools (`transfer`, `grocery_checkout`) are REAL — confirm the amount/recipient
  (transfer) and store+sum (grocery_checkout) with the user before running.
