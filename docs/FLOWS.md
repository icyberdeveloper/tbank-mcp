# T-Bank MCP — agent flows

Ordered tool-call sequences for common tasks. The session self-refreshes
(`ensure_fresh` → silent re-login, no OTP) on the first call of each flow, so you
don't call `refresh_session` manually unless a tool returns SESSION EXPIRED.

Served section-by-section by the `flows(topic)` tool — call it with no argument
for the list of topics. Reading the whole file is rarely what you want.

> **Tool names:** the **90 MCP tools** and their docstrings are the authoritative
> interface. Some sections below describe INTERNAL api steps — e.g. the web
> checkout + HMAC signing run INSIDE `grocery_checkout` / `transfer`. Call the MCP
> tools, not the internal methods named in the prose (`pay`, `payment_gate_pay`,
> `active_loans` are NOT MCP tools — and there is no raw `pay` to drop down to when
> a flow is unsupported; unsupported means the app).

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
2. `list_operations(account_id, days=30)` → recent purchases. `limit=0` shows
   every operation of the period; `desc_len=0` prints descriptions whole (the
   40-char column marks its cuts with «…»).
3. `spending_categories(account_id, days=30)` → spend grouped by category (+ share %).
   (or `operations_histogram(account_id, days, period, group_by)` for flexible
   breakdown by category/merchant/mcc.)

## 3. Grocery cart assembly → order → pay  (Город) — PROVEN end-to-end

> **Store context is mandatory.** Get `app_id`/`point_id` from
> `grocery_stores(sort_by)` — which also reports each store's nearest delivery
> window, its price and the minimum order, and sorts by `speed`/`price`/`min_sum`
> when the user names a criterion — and pass
> them to `grocery_search` / `grocery_plan_order` / `grocery_add_to_cart` /
> `grocery_set_cart` / `grocery_cart` /
> `grocery_checkout`. There is NO silent default store — without explicit context the tools
> return `NO_STORE_CONTEXT`, and mixing contexts makes the cart look empty. Keep app_id/pointId
> identical across the whole add → cart → checkout flow.

1. `grocery_search(query, app_id, point_id, limit)` → find goods by name and get
   their `id`, which every cart call addresses them by. The header separates
   shown / matched / what the store returned; `limit=0` shows every match.
   (`grocery_plan_order` does the
   same for a whole shopping list at once, and `grocery_rank` sorts the hits —
   see §10.)
2. `grocery_add_to_cart` (adds) / `grocery_set_cart` (absolute counts, `count: 0` removes, `clear=True` empties) → both go through cart/set on the
   mobile API, which REPLACES the whole cart — there is no delete endpoint, so
   removing an item means resending the list without it. The `delivery` block it builds
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

> **Confirmation is the button, not text.** Steps 3–7 run INSIDE
> `grocery_checkout(app_id, point_id)`. When not `dry_run`, the tool quotes the final
> sum itself (only the backend knows what weight goods reprice to), shows the user
> an elicitation button «Оформить заказ на N ₽?» with that sum, and locks the charge
> to it. Your job BEFORE the call is to show the cart contents (the button names only
> the total); do NOT ask «да/нет» in text — the button is the confirmation.
> `dry_run=True` is an optional read-only preview (works in any client) if you want to
> name the total and the delivery slot up front; `expected_sum` is an optional
> cross-check — if it differs from the tool's own quote by more than 0.01 ₽ the button
> shows both («… было M ₽ — банк пересчитал корзину») and the quoted sum is what gets
> charged. A client WITHOUT elicitation (no button to press) is refused at ANY
> threshold, and refused BEFORE the quote — «ПЛАТЁЖ НЕ ВЫПОЛНЕН…», no page loaded, no
> delivery slot asked for, no order, nothing sent; `dry_run=True` is then the only
> checkout call that still does anything. Hermes/Telegram and Claude Code (≥ 2.1.76)
> have elicitation, Claude Desktop does not. And a quote that comes back WITHOUT a
> finite positive sum is a refusal too — «ОПЛАТА НЕ ВЫПОЛНЕНА: предпросмотр вернул
> сумму …», with the preview text attached — never a charge: an unpriced total would
> put «0.00 ₽» on the button and switch the kopeck-exact guard off.

> **Out-of-stock items** block the order — remove unavailable goods before ordering.
> `code=211` has been seen on the deliveries step (HTTP 200 + the store's own code),
> but its meaning is NOT capture-verified: there is no error response for grocery in
> captures.xml at all. Treat it as "the store refused", report it as that, and do not
> tell the user what the code means. Orders auto-cancel if not paid quickly — pay immediately.
> `grocery_order_create`, `checkout_process_order`, `payment_gate_pay` move real
> money — review the body before calling.

8. `grocery_order_cancel(order_id, app_id)` → cancel a placed order, paid or not
   (refund goes back to the paying account). POST /api/order/cancel with ONLY
   `orderId` in the query — no paymentId, empty body — unlike the ticket flavour
   of the same path. The verdict is `payload.status` (`Success`/`Failed` + code,
   605 = already cancelled); the outer `"status":"Ok"` is transport-level. Pass
   `app_id` so the tool re-reads the order and reports the actual status.

## 4. P2P transfer / bill pay  (signed)

1. `transfer_sbp_resolve(phone)` → resolve a phone to everywhere it can be paid
   (`GET /v1/get_requisites`, read-only). **TWO calls, as the app makes them:**
   `pointerSource=internal` → the recipient's own T-Bank account, if they are a
   T-Bank client (`workflowType:"TinkoffInner"`, `maskedFIO` + `pointerLinkId`, and
   **no `bankMemberId`** — an internal transfer is not routed through SBP; that call
   carries neither `withTinkoff` nor `gapBanks`); `pointerSource=external&
   withTinkoff=true&gapBanks=true` → their SBP banks (`workflowType:"SBPTransfer"`,
   `bankMemberId`/`maskedFIO`/`pointerLinkId` + `isDefaultBank`). `withTinkoff=true`
   does NOT fold the internal answer into the external one — measured on the same
   phone: external = Sber + VTB, internal = the T-Bank account. Asking only the
   external one is why a recipient plainly visible in the app resolved to «no
   T-Bank». **Required for a NEW (unsaved) recipient** before commission/transfer;
   if several candidates and no default, the user picks which one — `transfer`
   shows a bank picker itself in a client with elicitation; otherwise ask in text
   (never silently pick — wrong account = money gone).
2. `payment_commission(body)` → preview the fee. `payParameters` with the resolved
   `providerFields`, `pointerType:"8276"`, `pointer:"+7…"` — plus
   **`paymentType:"Transfer"`, which commission REQUIRES and the transfer itself must
   NOT carry**: it appears in every captured commission body and in none of the three
   captured `/v1/pay` bodies. Do NOT use `pointerType:"ACCOUNT"`, the bank rejects it
   → INVALID_REQUEST_DATA. `"unfinishedFlag": true` in the reply means the preview is
   not a quote: the bank answers that to `moneyAmount:0` and to any body whose
   `providerFields` do not identify a recipient, and pairs it with «Комиссия не
   взимается» and a 3 000 000 ₽ ceiling for any phone at all. Only
   `unfinishedFlag:false` is a fee.
3. `transfer(amount, to_account, description, provider, bank_member_id, masked_fio,
   pointer_link_id, from_account, force)` → moves REAL money. **The confirmation is
   the tool's own button:** it shows the user «Перевести/Отмена» (elicitation, for
   sums ≥ `TBANK_CONFIRM_ABOVE`, default 0 = every transfer) BEFORE anything is
   journalled or sent. Show the recipient, sum and fee in text first, then call —
   do NOT ask «да/нет» in text; a client without elicitation is refused («ПЛАТЁЖ НЕ
   ВЫПОЛНЕН…», nothing sent). Two capture-verified
   flavours of the same `p2p-anybank` envelope, differing by ONE providerFields key:
   SBP carries `bankMemberId` (captures.xml #1477), a transfer to the recipient's own
   T-Bank account does NOT (captures-pay.xml — same `pointerType:"8276"` and phone
   pointer, 200 + `paymentId`). Choosing the internal one = pass its `pointer_link_id`
   with an empty `bank_member_id`. `provider=
   "transfer-legal"` raises `WRONG_METHOD` and names `transfer_requisites` instead:
   nine requisite fields do not fit this signature (see «By bank requisites» below).
   The HMAC
   `x-api-signature` over `/v1/pay` (base64(HMAC-SHA256(key=sessionid,
   msg=METHOD+path_tail+query+body))) is applied INSIDE `transfer`, over the query
   too — so the device/anti-fraud block it sends is part of what gets signed.
   `from_account` picks the debited account; omitted, it falls back to the first
   Current RUB, which is a guess. If the member fields are omitted, the recipient is
   AUTO-resolved (default bank, or single match; several-without-default →
   `RECIPIENT_MULTIPLE_BANKS`). `provider="transfer-inner"` (between own accounts)
   raises `NOT_SUPPORTED` — the body is plausible but has no captured `/v1/pay` to
   verify it against; transfer between own accounts in the app instead.
   Returns `paymentId` — the only handle for `payment_receipt()`, unavailable later.
   An unconfirmed outcome BLOCKS the next identical transfer; `force=True` retries
   with the same `userPaymentId` so the bank sees a repeat, not a second payment.

> Only the `v1/pay`/`group_pay` paths are signed; grocery payment (`payment_gate_pay`)
> is cookie-only.

### By bank requisites — paying a company or a sole trader (`transfer-legal`)

The invoice case: БИК + 20-digit account + ИНН, either typed or scanned off the QR
printed on the счёт. Whole flow capture-verified (`captures_payreq.xml`: QR →
resolve → commission → signed `/v1/pay`, 200 with a `paymentId`).

1. `payment_qr(qr)` → read-only. Takes the ГОСТ Р 56042-2014 string the QR encodes
   (`ST0001<enc>|Name=…|PersonalAcc=…|BankName=…|BIC=…|CorrespAcc=…|PayeeINN=…|
   KPP=…|Sum=…`) and prints the payee, the requisites, the sum and the fee — i.e.
   everything to show the user BEFORE the transfer call (the button that follows
   names only the sum and the payee). It also asks the bank what the QR means
   (`POST /providers/providers/qr/resolve`, with `barcodeHash` = **sha1 of the QR's
   utf-8 bytes** and the same three values in the query AND the JSON body). A QR
   that resolves to any provider OTHER than `transfer-legal` is a bill, not a
   transfer → `payment_providers(provider_id)` then `pay_bill`.
   **`Sum` is in KOPECKS.** `Sum=2360000` is 23 600 ₽.
2. `transfer_requisites(amount, qr, comment, account_number, bik, inn, name, kpp,
   corr_account, bank_name, nds, personal_account, from_account, force)` → REAL
   money. The tool itself shows the user the «Перевести N ₽ → <получатель>?»
   button (elicitation, sums ≥ `TBANK_CONFIRM_ABOVE`) before the signed POST — do
   NOT ask «да/нет» in text, show requisites + purpose and call; a client without
   elicitation is refused, nothing sent. `qr=` fills everything at once including
   the amount; explicit arguments
   always win over the QR, which is how a bad scan is corrected. `corr_account` and
   `bank_name` are looked up from the БИК (`GET /v1/bank_info?bik=`) when absent —
   the same call the app makes the moment a QR resolves.
   - `comment` (назначение платежа) is **required by the provider** and a QR often
     has none; ask the user. The commission preview accepts a body without it, so
     the refusal has to happen before that.
   - `nds` is the VAT mark on the payment order the recipient's accountant reads:
     `"322"` НДС не облагается (the provider's own default) or `"323"` НДС включен.
   - Values are checked against the provider's published `regexp` before anything is
     sent: `bankAcnt` `^[0-9]{20}$`, `bankBik` `(^(?![0]{9})[012]\d{8}$)`, `inn`
     `^(\d{10}|\d{12})$`, `kpp` `^[0-9]{9}$`.
   - The pay body differs from the p2p one: it KEEPS `isTransferStatus` /
     `isUrgentTransfer`, and adds `paidByPhoto:"QR"` — but only when the requisites
     really were scanned. `paymentType` stays out, as on every other `/v1/pay`; the
     commission call for this provider sends `paymentType:"Transfer"`, not
     `"Payment"`.
   - Unknown outcome blocks a repeat and reuses `userPaymentId`, exactly as
     `transfer` does. The duplicate key is (amount, recipient account, provider,
     payer account).

### Service bills (utilities, fines, taxes, internet)

4. `payment_providers()` → the 19 provider GROUPS. `payment_providers(group, query)`
   → providers inside one (102 571 exist in total across 1026 pages, so always
   filter; the header chains `page=N+1`). `payment_providers(provider_id, group)`
   → that provider's payment FIELD SCHEMA: field id, human name, required flag,
   hint and a validating `regexp`. That schema is the only source of the field
   names — they differ per provider. The id lookup scans up to `pages=` catalogue
   pages (default 5, 100 records each) and says so when a not-found is only a
   search boundary — with `group` the record lands on the first page.

   The group filter matches the provider's `groupId`, which does not always equal
   the name the groups list prints: «ЖКХ» is `Коммунальные платежи` (63 889
   providers) and «Интернет, ТВ и телефония» drops its comma. A mismatch is HTTP 200
   with an EMPTY payload, not an error. `GROUP_ALIASES` in `src/client.py` maps the
   two known cases.
5. `pay_bill(provider_id, fields, amount, group, from_account)` → REAL MONEY. It
   validates every field against the provider's `regexp` and refuses before sending,
   then prices the payment through `payment_commission` (which is also the bank
   validating the body) and enforces the provider's min/max — and only then shows
   the user the «Оплатить …?» button naming the real total WITH the commission
   (elicitation, sums ≥ `TBANK_CONFIRM_ABOVE`). Do NOT ask «да/нет» in text; a
   client without elicitation is refused before the commission round-trip («ПЛАТЁЖ
   НЕ ВЫПОЛНЕН…», nothing sent). Unknown outcome blocks a
   repeat and reuses `userPaymentId`, exactly as `transfer` does.

> ⚠️ The pay ENVELOPE for bill providers is not capture-verified: the one captured
> bill payment is on the WEB host (`www.tbank.ru/api/common/v1/pay`, with
> `delayAccepted`/`ucid` and a browser device block), while the mobile signed
> `/v1/pay` has only ever been captured carrying TRANSFER providers. Verified live
> that the mobile host accepts a bill provider on the commission endpoint — it
> resolves the provider, prices it and returns its limits. Keep the first real
> payment to a new provider small.

## 5. Messenger / support chat  (read + send)

1. `messenger_unread()` → how many unread, and in which chats (by name).
2. `messenger_conversations(archived, offset)` → one page of chats (find the
   support chat `conversationId`, e.g. title "Поддержка"). The header names the
   `offset` for the next page; `archived=True` lists archived chats.
3. `messenger_messages(conversation_id, limit, offset, max_chars)` → the chat
   history, oldest first, with author and time. The bank returns one page; the
   arguments window it LOCALLY: `limit` (0 = the whole page), `offset` skips the
   newest and walks older, `max_chars` caps one message's text (0 = whole text —
   use it to read a long bank message in full; a cut is always marked and names
   the full length).
4. `messenger_file(conversation_id, file_id, save_to, overwrite)` → download an
   attachment TO DISK and return the path. The listing marks one as
   `[файл: имя | 67 КБ | file_id=… → messenger_file()]`; both ids come from that
   one line, and the pair is the key (the same `file_id` under another chat
   answers 401). Saved 0600 under `~/.local/share/tbank-mcp/chat-files/`
   (`TBANK_CHAT_FILES`, or `save_to`) under the name the RESPONSE states —
   `x-amz-meta-filename-base64`, or the percent-encoded `Content-Disposition` —
   so no name travels through the agent, and the extension comes from there and
   from nowhere else.
   The tool does NOT parse or summarise the document: the file is on the machine
   the agent runs on, so the agent reads it with its own tools (Read for a PDF, an
   image or text; a script for a workbook).
5. `messenger_send(conversation_id, text)` → **send** a reply. Real message to a
   real support agent — not money, but not undoable either; say what you are
   about to send before sending it.

> This is where the bank delivers what a chat message cannot hold — a statement, a
> broker report, a certificate. `GET /app/bank/messenger/conversations/{cid}/files/
> {fileId}` authorises on the `tmsgSessionID` cookie alone and answers the raw
> bytes; an auth failure still arrives as HTTP **200** with a JSON error envelope,
> like every other messenger route, so the envelope is detected in the bytes rather
> than saved as the document.

> `messenger_hints`, `messenger_faq` and `messenger_mark_read` exist on the client
> but are NOT exposed as tools — quick replies and FAQ add nothing an agent cannot
> write itself, and marking a chat read is a side effect the user did not ask for.

> Messenger needs a `tmsgSessionID` (JWT, ~1h), auto-minted via `issueTokenBySSO`
> from the silent-relogin access_token. No OTP — works as long as the long-lived
> `SSO_SESSION` cookie is obtained via login.

## 6. Invest browse

1. `invest_accounts()` → InvestBox/brokerage accounts (take `brokerAccountId`).
2. `invest_portfolio(broker_account_id, days)` → portfolio statistics.
3. `invest_operations(broker_account_id, operation_type, limit)` → broker ops.
   When the bank holds more than `limit`, the header says so — raise `limit`
   (there is no cursor: its wire name is in no capture).
4. `invest_securities(broker_account_id)` → purchased stocks/bonds/ETF.

Extras have no tool of their own — reach them through `get_data(section)`:
`invest_offers`, `invest_yield`, `broker_margin`, `pension`.

## 7. Credit / debt

There are no dedicated tools here. Every one of these is a `get_data(section)`
call returning raw JSON:

1. `get_data("loans")` → active credits.
2. `get_data("credit_schedule")` → payment schedule.
3. `get_data("credit_rating")` → rating + advice.
4. `get_data("full_debt_amount", account_id)` / `get_data("account_details", account_id)`
   → debt + account detail. **Both are FILTER endpoints and need the account id** —
   they used to accept the call without one, drop the argument on the floor and
   answer about nothing, which read as «долгов нет». Omitting it now raises
   `ARG_REQUIRED`.
5. `get_data("statements", account_id, days)` → statements for the window.
   `get_data("statement_exist", account_id)` also takes an account; the app pairs it
   with a `statementId` this tool has no way to obtain, so treat its answer as
   «does this account have statements at all», not as a lookup of one.

## 8. Cards, account details, identity documents

> **Session LEVEL matters here.** These endpoints validate the mobile *sessionid*,
> not just the Bearer token, and refuse an ANONYMOUS-level session. The CLIENT
> window is only ~11 minutes (`/v1/ping` → `portalSessionExpiresInSeconds` ≈ 659)
> while `ensure_fresh` tracks the ~2h access_token — so between re-mints the
> session lapses and only these few tools notice. They call
> `ensure_client_session()`, which pings and re-mints when the window has closed.
> Both grants (refresh_token and authorization_code) mint an equally privileged
> session — the grant type is NOT the variable, the window is.

1. `list_cards()` → every card with **both** ids. `id` is what an operation's
   `card` field holds; `ucid` is what limits/credentials key off. Do not swap them.
2. `card_limits(ucid)` → monthly purchase + daily cash limits, and what is used up.
3. `card_requisites(ucid)` → holder, expiry, PAN. Masked by default; `reveal=True`
   returns the full number and CVV.
4. `card_operations(card_id, days)` → operations on ONE card. The API has no
   include-by-card filter (only `excludeCardIds`), so this filters client-side.
5. `account_requisites(account_id)` → recipient/account/BIC/corr/INN for inbound
   transfers. `currencies="RUB,USD"` returns one block per currency.
6. `documents(kind)` → passport, international passport, driver's licence, SNILS,
   INN, OSAGO/KASKO, PTS/STS. The store also holds RELATIVES' documents the client
   once entered; they are filtered out by birthDate unless `include_others=True`.

## 9. Orders across every vertical

`orders(kind)` is one call over `/api/orders/list` and covers groceries, cinema,
concerts, flights, trains and hotels together (188 orders back to 2018).
`kind` = "афиша" | "кино" | "путешествия" | "продукты" or a raw `objectType`.
`order_details(order_id)` adds hall/seats/booking code for entertainment orders;
groceries have their own `grocery_order_status`.

`travel_order_details(order_id)` covers all three travel verticals:
- **Hotels** — dates, city, hotel, room, guests, price.
- **Rail** — the car, the berths, what each ticket cost and its electronic-
  registration status, plus the `ticket_id` values `train_refund` takes. They
  exist nowhere else, which is why they are printed here.
- **Flights** — the route and the issued documents (one itinerary receipt per
  passenger, plus one for the order).

`travel_ticket_file(order_id)` saves the PDFs; `trips()` is the journey-shaped
view of the same thing, and is where a trip's route and insurance live.

An earlier version of this section said flights and trains could not be detailed
without a web session layered over the mobile one. That was wrong: probed live,
the flight endpoints answer under the plain mobile session, and the rail host's
own cookie — which `_ensure_trains` already mints — authorises its order API too.

## 10. Grocery nutrition / lowest-calorie shopping

1. `grocery_search(query, app_id, point_id)` → candidate goods.
2. `grocery_good_info(good_id, …)` → ingredients, storage, and КБЖУ per 100 g and
   per package. Nutrition comes in two shapes: some retailers fill the structured
   protein/fat/carb/energy fields, ВкусВилл leaves them empty and publishes only
   free text ("белки 3,3 г, жиры 3 г, углеводы 18,4 г; 113,8 ккал") — both parsed.
3. `grocery_rank(query, …, sort_by, order)` → the same search, ranked. `sort_by` ∈
   `price | weight | kcal | kcal_pack | protein | fat | carb`; empty = the store's
   own order. Nutrition keys auto-load the КБЖУ (one extra request per candidate),
   so pass them only when the user asked for a nutritional criterion.
   Goods whose nutrition the retailer does not publish sort LAST in BOTH
   directions — "not published" is not zero, and must never win a "most calories"
   query. The MCP ranks; WHICH ranking to use for a given phrase lives in the
   grocery skill, and applies only on an explicit request.

## 11. Tickets — cinema, concerts, theatre and exhibitions  (REAL money at step 5)

Full detail, including what to show the user before paying, lives in the
`tbank-tickets` skill. The order here is the part you must not improvise:

1. `cinema_search(query, city)` → `eventId` (city-independent). For concerts,
   theatre and exhibitions use `search_app(query, screen="afisha")` instead.
2. `cinema_schedule(event_id, date, cinema="каро 11", around="17:00", city)` →
   showtimes per venue, filtered by venue-name substring and a time window
   (`window_min`). Pass the SAME `city` as in step 1 — it also anchors the
   distance sort, and a Petersburg schedule ordered from the centre of Moscow
   looks plausible and is nonsense. Omitting both `city` and `object_id` is
   refused outright — `CITY_REQUIRED` — not silently defaulted to Moscow.
   Concerts: `concert_schedule(event_id)` — their showings are not date-keyed.
   Take **both** `slotId` and `objectId`; a `slotId` without its venue is useless.
3. `cinema_seats(event_id, slot_id, object_id, row, max_price, kind)` → free seats
   with prices. Empty for a concert usually means free seating — `concert_hall(…)`
   shows those sectors, but they are **read-only**: the capture has no
   order/create example for that screen, so the MCP will not invent one.
4. `cinema_book(…, seats="7:10,7:11")` → creates the order, moves NO money.
   Returns `orderId` and `nfsPaymentToken`. **The token is returned here and
   nowhere else** — `order_details()` does not carry it. Lose it and the booking
   can never be paid, only re-made.
5. `ticket_pay(order_id, amount, nfs_payment_token, account_id)` → **REAL money.**
   Show the seats and the total with the service fee (from `cinema_book`) in text
   first, then call — the tool itself shows the user the «Оплатить заказ …: N ₽?»
   button (elicitation, sums ≥ `TBANK_CONFIRM_ABOVE`); do NOT ask «да/нет» in text.
   A client without elicitation is refused («ПЛАТЁЖ НЕ ВЫПОЛНЕН…», nothing sent —
   the booking stays unpaid and expires by itself). The tool
   re-reads the order from the backend and refuses to pay a mismatched amount.
6. `order_details(order_id)` → booking code, hall, seats. The ticket itself —
   QR payload, PDF link — is in the orders feed instead, via `ticket_qr(order_id)`;
   `/api/tickets/get` is dead (four calls, four code=228). Coverage is partial by
   partner: of 75 afisha orders all carried a booking code, 53 a QR, and Ticketland
   hands out neither, so the tool prints what exists and names what does not.

> **A period, not a day.** `afisha_catalog(kind, city, date_from, date_to)` lists
> a vertical over a date RANGE. The app only ever asks for one day because its
> calendar picks one, but the server takes a window — eight days in Moscow answer
> with 197 films against 83 for one, and the extra titles are real one-off
> screenings. Cinema is the odd one: it ignores paging, returns the vertical whole
> and carries no showings, so times still come from `cinema_schedule`. Exhibitions
> have no catalogue of their own and the tool says so instead of guessing a path.

> **Venue first.** `afisha_places(kind, city)` is the only way to an objectId
> without going through some event that plays there — it lists cinemas, halls,
> theatres and museums with their ids. There is no server-side search on it, so a
> name is matched locally and every page is read first; a 204 means that vertical
> is not serving, which is a different answer from «no venues here». With an id in
> hand, `cinema_schedule(object_id=…, date=…)` returns a cinema's WHOLE day in one
> request, and `place_schedule(object_id)` does the same for concerts, theatre and
> exhibitions. `place_info` completes the card — its address field comes back empty,
> so pass `with_halls=True` to get one.

> **Which vertical.** `kind` selects it: `кино` | `концерт` | `театр` | `выставка`
> (the API's own `movie`/`concert`/`spectacle`/`exhibition` are accepted too). They
> differ in exactly two ways that matter here: cinema numbers its seats
> (`ряд:место`, the rest use a composite `seatId`) and scopes showings by date, so
> cinema listings come from `cinema_schedule` and everything else from
> `concert_schedule`. An unknown kind is refused rather than defaulted to cinema —
> defaulting is how a theatre booking would have been posted to /order/create/movie.

7. `ticket_cancel(order_id, kind, payment_id, force)` → cancels. It reads the
   order first and, when the bank has flagged it uncancelable, does not send the
   request at all — `force=True` sends it anyway. `orderId` goes in the
   query, `paymentId` next to it, resolved from the order when the caller omits
   it. What decides the outcome is the order's own `isCancelAvailable`, visible
   in `order_details()`: a flagged-cancelable order settles as
   **PARTIALLY_CANCELED** — tickets refunded, service fee kept — while an order
   flagged `false` answers `status=Failed` with a code (400/500/1002/1009 seen
   live) and changes nothing. Retrying that is pointless; the request form is not
   the reason, the same call as form-urlencoded behaves identically. An unpaid
   reservation has no `paymentId`, never reaches `orders()`, and expires by itself.

> On error the order status is UNKNOWN, not "still booked" — check
> `orders("афиша")` and the refund in `list_operations()` before doing anything
> else, and never retry blind.

## 12. Global search across the app

`search_app(query, screen, limit)` — one full-text search over whatever the given
screen indexes. `screen` is a strict enum and a wrong value is a 400, not an empty
result: `services` (banking + everything), `afisha` (cinema/concert/theatre/
exhibition), `movie_main` (films only), `concerts_main`, `spectacle_main`,
`exhibition_main`, `grocery`. Hits come back grouped by `objectType` with their
ids; for films the id IS the `eventId` that `cinema_schedule` wants, so search →
schedule needs no translation step.

**Venues are searchable too.** `cinema`, `concerthall`, `theatre` and `museum`
hits carry the venue's `objectId` — the same id `cinema_schedule(object_id=…)`
and the venue endpoints take. They used to be dropped by the parser, so a search
for a cinema by name answered with nothing; if a query that should match a venue
still returns none, that is the bank's index, not ours.

## 13. Marketplace — searching Шопинг

`shop_search(query, limit, offset)` and `shop_cart()`. Paging here is the
SERVER's — offset/size are real and totalHits is honest — unlike the afisha
listings, where a name is matched locally and every page has to be read first.
Products print the skuId/pointId/shopId triple a cart write needs, and the seller
comes from a separate `partners` list keyed by the product's dolyameShopId.

The host is not the native app: 179 captured requests, not one Authorization
header. It authorises on cookies carrying the access_token the mobile session
already holds, and wants none of the native query context — which is what the
`no_bearer` / `no_base_params` template flags are for. The search parameter is
`search`, not `query`; the sibling media endpoint uses `query`.

> **Placing an order is NOT supported.** `process-order` returns delivery options
> and a price and nothing in any capture goes further, so there is no confirmed
> step that places or pays for a marketplace order and none is invented. Search
> and read the cart here; finish in the app.

## 14. Flights — search, then buy  (REAL money at step 4)

Full detail lives in the `tbank-travel` skill. The order is the part you must not
improvise, and the ids are why:

1. `flight_search(from_code, to_code, date, only_bookable)` → rows carrying
   `offerId`. There is **no name→IATA resolver anywhere in the captures**;
   `flight_history()` is the one place a code comes back with its name, so take
   codes from there rather than guessing.
2. `flight_offer(offer_id)` → the fares. One search row expands into a FAMILY of
   fares — same flight and same seat, different baggage and refund rules — so the
   search price is only the cheapest of several. This step also re-prices, and it
   is not optional: it mints the internal id every later call needs.
3. `flight_seats(offer_id, fare)` — optional and paid. Skipping it is normal; a
   seat is then assigned at check-in.
4. `flight_book(offer_id, fare, passengers, seats)` — **books AND charges in one
   call.** Flights have no hold step, so there is nothing between the button and
   the ticket. The button carries the tool's OWN re-priced total, never a number
   the agent typed. Choosing seats also buys the check-in service, so the charge
   is fare + seats + check-in — three numbers.
5. `travel_order_details(order_id)` / `trips()` → the route and the PNR;
   `travel_ticket_file(order_id)` saves the itinerary receipts.

`passengers="me"` fills the passport from `documents()` — the same store the app
prefills from, Latin spellings included, so nothing is transliterated by guesswork.
Co-passengers are passed as JSON: the bank keeps documents for ONE contact, and
picking a relative's passport out of that store unasked would put a stranger on a
ticket.

> **There is no flight refund.** `grefund/calc` in the API is the «guaranteed
> refund» ADD-ON sold with the ticket, not a cancellation, and no capture shows a
> cancellation call. Say so rather than inventing a tool.

## 15. Rail — search, seats, book, pay, refund  (REAL money at step 4)

1. `train_calendar(origin, destination)` — which dates are on sale, and the cheap
   check that a pair of station codes is valid: a wrong pair comes back empty.
   origin/destination are the bank's NUMERIC codes (2000000 is Moscow) and nothing
   resolves a name to one.
2. `train_search(origin, destination, date)` → rows carrying `train_id`.
3. `train_seats(train_id)` → cars and free places, printed as `вагон/место`.
4. `train_book(train_id, seats="03/10,03/12", passengers)` → an order that **holds
   the seats for about fifteen minutes** and charges nothing.
5. `train_pay(order_id)` — call it FIRST WITHOUT a card: that lists the cards and
   accounts that can pay and charges nothing. Then `train_pay(order_id,
   card_id=…)`, where the button confirms. Payment runs through the T-Pay gateway
   headlessly; the webview the app opens is not needed.
6. `train_refund(order_id)` shows what would come back after fees and refunds
   NOTHING; `train_refund(order_id, confirm=True)` performs it. Show the
   calculation first — the fees are the whole point of looking.

`train_id` and the seat list are handles, not data: prices, availability and the
ids the order needs are re-read at the moment they are used, because all of them
go stale within minutes. A seat sold while the user was deciding fails by name.

## 16. Hotels — searching, and only searching

`hotel_search(query, checkin, checkout, adults)` → hotels with prices and
`hotel_id`; `hotel_info(hotel_id, checkin, checkout)` → the card, the generated
review summary, the ratings and the tariffs with their cancellation ladder.

`isLoadingCompleted: false` on a search means the answer is still filling in —
the tool says so, and «дешевле нет» must not be concluded from a partial list.

> **Booking is not supported.** The tariff carries a `bookHash`, and no capture
> anywhere shows the call that would consume it. Guessing that request shape is
> exactly the mistake this repo keeps paying for, so hotels are searched here and
> booked in the app.

## 17. Trips, and what a trip really costs

`trips()` — flights, trains and hotels in one feed; `trips(trip_id)` — the card
with its route and insurance. This is NOT `orders()`: that lists orders across
every vertical including groceries and cinema, this lists journeys.

`travel_payment_options(amount)` answers the question a price alone cannot: which
accounts may pay, how many loyalty bonuses can be burned, how much cashback comes
back, and what the installment plans are. It changes nothing.

## Notes

- Every tool returns a short string (counts + summaries) or JSON; its own
  docstring is the reference — they are the interface the MCP actually exposes.
- On `SESSION EXPIRED`, call `refresh_session` (refresh_token → silent re-login,
  no OTP) and retry. If it returns `REAUTH_REQUIRED`, the user must re-login
  (login + OTP + password).
- `grocery_checkout(dry_run=True)` quotes the order — it runs the cart + delivery
  steps and returns the sum that would actually be charged, creating nothing. It is
  OPTIONAL: the cart total is NOT the charge (weight goods are repriced by the
  backend during delivery), so a real `grocery_checkout` quotes the final sum ITSELF
  and puts it on the confirmation button — the agent never relays the number. Use
  `dry_run` when you want to name the total and the slot up front, or to surface a
  store that refuses delivery before the user is asked. It is also the ONLY checkout
  call that works in a client without elicitation: charging is refused there whatever
  the threshold (see «Elicitation» below). `expected_sum` is an optional
  cross-check (the number you already told the user): when it differs from the tool's
  quote by more than 0.01 ₽ the button says both and the quote wins. A quote writes no
  journal attempt (attempts are keyed by cart hash and the newest decides whether a
  retry is blocked), and a cart already held for reconciliation is not quoted either.
- A deliveries step the store refuses (HTTP 200 + its own code, a 5xx, or a dropped
  fetch) is retried inside `grocery_checkout` — nothing is posted at that point and
  the same call seconds later routinely works. A 4xx is not retried. The error names
  the blame and whether a retry can help.
- `grocery_checkout` contract is verified against captures.xml: agreement from
  `user/payment/account/last`, clientEmail from `get-customer-information`,
  post-delivery sum from deliveries `payload.cartPrice`, and no blind sleep (it polls
  the cart API until it answers). Every in-page request is bounded by an
  AbortController — `page.evaluate` has no timeout of its own, and a hung fetch
  between order/create and payment is the one place that must never stall.
  If the payment answer is lost, the order is read back once before the result is
  called unknown: a lost response is not an unpaid order. After a genuinely UNKNOWN
  result the auto-retry is BLOCKED — reconcile via `grocery_attempts` +
  `grocery_order_status(order_id)`, and force only after the user confirms no order
  exists.
- Diagnostics: checkout stages (delivery/order/payment) and session refresh emit
  redacted structured events to `~/.local/share/tbank-mcp/events.jsonl` (no
  secrets/PII). Call `diagnostics()` to reconstruct an attempt and find the last
  confirmed step.
- Money tools — all eight — are REAL: `transfer`, `transfer_requisites`,
  `pay_bill`, `grocery_checkout`, `ticket_pay`, `train_pay`, `flight_book`, and
  `confirm_payment` (which completes a payment the bank is holding for a second
  factor). The confirmation is the BUTTON the paying tools show themselves (see
  «Elicitation» below) — a concrete sum the user presses
  «Перевести/Оплатить/Оформить» on. Your part is BEFORE the call: show the
  recipient + sum + fee (transfer), the requisites + purpose
  (transfer_requisites), the cart contents (grocery_checkout), the seats + fee
  (ticket_pay), the car and berths (train_pay) or the fare, baggage and refund
  rules (flight_book) in text, then call.
  `train_book` and `train_refund` are NOT in this list and show no button:
  the first only holds seats that lapse on their own, the second is a
  cancellation. `train_refund` still refuses to act without `confirm=True`, and
  what it prints first is the fee — which is the reason to look. Do NOT ask «да/нет» in text on top — that is
  a double question in a client with buttons. A request to buy something is not a
  confirmation to pay for it; the button is.
- **WAITING_CONFIRMATION.** A large or risk-flagged `/v1/pay` comes back
  «ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ»: the bank accepted the payment but is holding it for a
  second factor. The money has NOT moved, but a pending payment now EXISTS on the
  backend keyed by its `userPaymentId` — so do NOT repeat `transfer` /
  `transfer_requisites` / `pay_bill` (a fresh call makes a SECOND pending payment).
  Take the SMS/push code from the user and call `confirm_payment(attempt_id, otp)`
  with the `attemptId` from the pending message; check whether it settled with
  `payment_status(attempt_id)` or `list_operations`. `confirm_otp` is the LOGIN
  code and cannot confirm a payment. Under the hood `confirm_payment` POSTs the code
  to `/v1/confirm` (cookie-authorised, the OTP rides as `secretValue`, the ticket as
  `initialOperationTicket`) — capture-verified, not a guessed endpoint.
- **Elicitation (payment confirmation only).** Money moves ONLY after the user
  presses a button the tool itself shows (MCP elicitation, zero-field: Accept is
  the action, Decline is «Отмена»). The live client (Telegram/Hermes) renders ONLY
  yes/no buttons — it has NO text-input field — so every dialog that asked the user
  to TYPE something (payment OTP, login SMS/PIN, a missing amount/purpose) is the
  text-through-agent flow, and the non-payment confirm buttons (messenger_send,
  ticket/grocery cancel, card reveal) were dropped. What the button covers:
  - `transfer` — picks the SBP bank when a phone maps to several (before any
    journal write), offers the debit account when more than one ruble account
    exists, then «Перевести/Отмена». `transfer_requisites` — offers the account,
    then «Перевести N ₽ → <получатель>?» (a missing amount/purpose is NOT elicited —
    the tool refuses on a missing amount and the bank on a missing purpose).
    `pay_bill` — «Оплатить …?» naming the real total WITH the commission.
    `ticket_pay` — «Оплатить заказ …: N ₽?» with the sum the backend holds for the
    order. `grocery_checkout` — quotes the final sum itself, shows «Оформить заказ на
    N ₽?» and locks the charge to that number (with `expected_sum` that differs the
    button says «… было M ₽ — банк пересчитал корзину»); `dry_run=True` is a
    read-only preview and works in any client.
  - Threshold: `TBANK_CONFIRM_ABOVE` (env on the server, default 0 = every
    payment). Below a positive threshold nothing is asked and the payment proceeds
    in any client — **except `grocery_checkout`, which refuses a client without
    elicitation at ANY threshold.** The other four have their amount before they do
    anything (an argument, or a QR string parsed locally), so below the threshold
    they can charge without a button; grocery cannot — the only way to learn its
    sum is a Playwright page load plus a `/grocery/deliveries` POST that asks the
    store to hold a delivery slot, and doing that work for a client that could never
    confirm the result is worse than saying no at once. Below the threshold in a
    client that DOES have elicitation, grocery asks nothing and still pins the
    charge to its own quote (`expected_sum` is set from it either way — a zero there
    would switch the kopeck-exact guard off).
  - The agent does NOT ask «да/нет» in text: its job is to show the details the
    button does not name (cart contents, seats + fee, requisites + purpose, fee)
    BEFORE the call. The button is the single confirmation.
  - **A client WITHOUT elicitation is REFUSED, not waved through:** the tool
    returns «ПЛАТЁЖ НЕ ВЫПОЛНЕН: этот клиент не поддерживает подтверждение
    кнопкой…» before any journal write and before any HTTP — nothing sent, money in
    place. That holds for all five, `grocery_checkout` included: its refusal comes
    BEFORE the quote, so no checkout page is loaded and no delivery slot is asked
    for. Hermes/Telegram and Claude Code (≥ 2.1.76) have elicitation; Claude
    Desktop does not — read-only tools work there, paying does not
    (`grocery_checkout(dry_run=True)` still does: it creates nothing).
  - **An unpriced quote is a refusal, not a charge** (`grocery_checkout` only):
    when the tool's own preview comes back without a finite positive sum, it returns
    «ОПЛАТА НЕ ВЫПОЛНЕНА: предпросмотр вернул сумму …» with that preview text
    attached, and no order is created. There is nothing to ask the user about and
    nothing to check the debit against — and a zero on the button would ALSO switch
    the kopeck-exact guard off, so whatever the bank recomputed would be charged.
    An empty cart or a failed preview is the same shape of answer: the preview text
    comes back as-is, and nothing is charged.
  - A decline («Отменено пользователем (кнопка «Отмена»)…»), a closed window or a
    timeout («Подтверждение не получено…») does NOTHING (no money moves) and leaves
    no journal trace. `transfer`, `pay_bill` and `ticket_pay` say «Ничего не
    сделано» in that refusal, because nothing ran before their button, and
    `transfer_requisites` words its own the same way («Платёж НЕ отправлен, деньги
    на месте»). `grocery_checkout` says «Заказ не создан» instead — its quote has
    already loaded the checkout page and asked the store for a delivery slot, so
    «ничего не сделано» would claim more than the code can keep. What every wording
    promises is the same: no order, no money moved.
    The SBP-bank and debit-account pickers are multi-option, not
    yes/no — they appear only when the client has elicitation; a payment below a
    positive threshold in a client without it falls back to the body's guess
    (default bank / first ruble account), as before. Above the threshold such a
    client never gets that far: the money refusal comes first.
  - `confirm_payment(attempt_id, otp)` — the WAITING_CONFIRMATION code is passed as
    text: take it from the user and call with `otp='<код>'`. It is never logged.
  - `login(phone)` — text flow: returns the next step, the agent collects the code
    and calls `confirm_otp(otp)` (and `confirm_pin`, if asked). The password is
    entered in the terminal via `login_cli.py`, never through the agent.
