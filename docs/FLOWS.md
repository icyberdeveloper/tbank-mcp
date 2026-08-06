# T-Bank MCP — agent flows

Ordered tool-call sequences for common tasks. The session self-refreshes
(`ensure_fresh` → silent re-login, no OTP) on the first call of each flow, so you
don't call `refresh_session` manually unless a tool returns SESSION EXPIRED.

Served section-by-section by the `flows(topic)` tool — call it with no argument
for the list of topics. Reading the whole file is rarely what you want.

> **Tool names:** the **85 MCP tools** and their docstrings are the authoritative
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

> **Out-of-stock items** block order/create (code=211). Remove unavailable goods
> before ordering. Orders auto-cancel if not paid quickly — pay immediately.
> `grocery_order_create`, `checkout_process_order`, `payment_gate_pay` move real
> money — review the body before calling.

8. `grocery_order_cancel(order_id, app_id)` → cancel a placed order, paid or not
   (refund goes back to the paying account). POST /api/order/cancel with ONLY
   `orderId` in the query — no paymentId, empty body — unlike the ticket flavour
   of the same path. The verdict is `payload.status` (`Success`/`Failed` + code,
   605 = already cancelled); the outer `"status":"Ok"` is transport-level. Pass
   `app_id` so the tool re-reads the order and reports the actual status.

## 4. P2P transfer / bill pay  (signed)

1. `transfer_sbp_resolve(phone)` → resolve a phone to its SBP recipient banks
   (`GET /v1/get_requisites`, read-only). Returns `bankMemberId`/`maskedFIO`/
   `pointerLinkId` per bank + `isDefaultBank`. **Required for a NEW (unsaved)
   recipient** before commission/transfer; if several banks and no default, ask the
   user which bank (never silently pick — wrong bank = money gone).
2. `payment_commission(body)` → preview the fee. `payParameters` with the resolved
   `providerFields`, `pointerType:"8276"`, `pointer:"+7…"` — plus
   **`paymentType:"Transfer"`, which commission REQUIRES and the transfer itself must
   NOT carry**: it appears in every captured commission body and in none of the three
   captured `/v1/pay` bodies. Do NOT use `pointerType:"ACCOUNT"`, the bank rejects it
   → INVALID_REQUEST_DATA.
3. `transfer(amount, to_account, description, provider, bank_member_id, masked_fio,
   pointer_link_id, from_account, force)` → moves REAL money. `provider=
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
   everything the user has to confirm. It also asks the bank what the QR means
   (`POST /providers/providers/qr/resolve`, with `barcodeHash` = **sha1 of the QR's
   utf-8 bytes** and the same three values in the query AND the JSON body). A QR
   that resolves to any provider OTHER than `transfer-legal` is a bill, not a
   transfer → `payment_providers(provider_id)` then `pay_bill`.
   **`Sum` is in KOPECKS.** `Sum=2360000` is 23 600 ₽.
2. `transfer_requisites(amount, qr, comment, account_number, bik, inn, name, kpp,
   corr_account, bank_name, nds, personal_account, from_account, force)` → REAL
   money. `qr=` fills everything at once including the amount; explicit arguments
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
   validating the body) and enforces the provider's min/max. Unknown outcome blocks a
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
3. `get_data("credit_rating")` / `get_data("credit_recommendations")` → rating + advice.
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

Travel is split by vertical, because each one authorizes differently:
- **Hotels** — `travel_order_details(order_id)` works: `hotels.t-bank-app.ru`
  accepts the plain Bearer, and returns dates, city, hotel, room, guests, price.
- **Flights and trains** return only the `orders()` summary — and NOT because the
  bank refuses. Each detail endpoint wants a session this MCP does not yet build:
  the flight order lives on `www.tbank.ru/api/travel/flight/order`, which needs a
  web session layered over the mobile one (the bridge IS in the capture —
  `session/webview/get_by_token` hands back a portal session — it is simply not
  wired up), and the train order lives on `trains.t-bank-app.ru/api/orders/{id}`,
  which authorises by a cookie that host sets itself in response to a Bearer
  request. `travel_order_details` says exactly that, naming the host and what is
  missing, instead of retrying.

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

Full detail, including the confirmation wording, lives in the `tbank-tickets`
skill. The order here is the part you must not improvise:

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
   Only after the user confirms a concrete sum and concrete seats. The tool
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

## 14. Flights — searching, and only searching

`flight_search(from_code, to_code, date, only_bookable)` plus `flight_history()`.

The captured traffic runs this under a web session behind a multi-step bridge,
which reads as unreachable from a mobile one. It is not: probed live, the same
endpoints answer under the plain mobile Bearer with `X-Travel-Context: mb` and
the session in `sessionId`. No bridge is built, because none is needed.

The search STREAMS. `startStreaming` returns the first batch; `nextBatch` blocks
for the next and sets `isOver` on the last. Measured on one route: 4 batches, 757
flights, 4348 offers, the final batch alone adding 2836. `offers[].flights` index
the CONCATENATION of every batch — 757 flights, highest index 756 — so nothing
resolves until the stream is stitched, and a caller that stops early is told.

`only_bookable` (the default) stops after the first batch: only `vendor ==
"Tinkoff"` offers are buyable inside the bank, and all 101 of them arrived in
that first batch, so the other three round trips buy partner listings that lead
out of the app.

> **There is no name→IATA resolver anywhere in the captures.** `flight_history()`
> is the one place a code comes back with its name; take codes from there rather
> than guessing. And buying is not supported — no confirmed booking or payment
> step exists, so this searches and compares, nothing more.

## 15. Rail — searching trains

`train_search(origin, destination, date)` and `train_calendar(origin, destination)`.

This host keeps its own session, and getting it is one request: GET
https://trains.t-bank-app.ru/ with the ordinary mobile Bearer answers with
Set-Cookie, and the search API accepts those cookies. No redirect chain, no
browser — the older note calling rail unreachable was reading a different
bootstrap path. The mint happens in an ISOLATED jar, because that same response
also clears the cookie for the tbank.ru domain and doing it in the shared jar
would race every other host mid-flight.

origin/destination are the bank's NUMERIC station codes (2000000 is Moscow) and
nothing in the captures resolves a name to one, so the tools take codes.
`train_calendar` doubles as the cheap check that a pair is valid: a wrong pair
comes back empty.

> **Buying is not supported.** `orders/pay` hands back a tpay webview URL that
> cannot be completed headlessly, and creating an order needs passenger passport
> data. This searches and compares.

## 16. Work calendar — MyT (встречи, приглашения)

**This is not the bank.** MyT is T-Bank's corporate app; the calendar lives behind
`kairos.tbank.ru` with its own login. A bank session gives no access to it and
`refresh_session()` cannot fix it. Check with `myt_status()`; if there is no
session, the user runs `.venv/bin/python login_cli.py --myt <login>` in their own
shell — the corporate password never goes through the agent, and there is
deliberately no tool that takes it.

The token exchange itself IS exposed. `POST magentbep.tcsbank.ru/v3/auth/token` with
`grantType: refresh_token` is what keeps the session alive, and it runs by itself
before every request (120 s before expiry) — so a zero countdown in `myt_status()`
means «an hour passed», not «dead». `myt_refresh_session()` forces that exchange now:
useful to test the REFRESH token specifically (an access token can be alive while the
refresh one has been revoked) or to renew before a long chain of calls. Unlike the
bank's, this refresh token does NOT rotate on use — the server returns the same one,
so calling it repeatedly burns nothing. Only a dead refresh token needs the CLI.

1. `calendar_schedule(date_from, date_to)` — one request per day (that is how the
   app itself reads it), max 14 days per call. Prints the full appointment id,
   because every other call here needs it.
2. `calendar_event(id)` — participants with their answers, the meeting URL, the
   agenda (the Outlook HTML is stripped to text), recurrence rule.
3. `calendar_respond(id, "пойду" | "не пойду" | "может быть")` — the answer is
   visible to the organiser and overwrites the previous one, so confirm the
   MEETING TITLE with the user first. Kairos throttles to one answer per 5
   seconds; the tool waits and retries once by itself.
4. `calendar_cancel(id, occurrence_start)` — organiser only, notifies everyone.

Two traps, both from the capture:

- **Times are true UTC** — confirmed against the live calendar, where an event
  labelled `15:00+00:00` shows as 18:00 in the Moscow app. The tools convert to the
  employee's timezone and name it in the header. The zone is resolved, not assumed:
  workplacer lists 66 buildings across EIGHT offsets (+02:00…+10:00), so it comes
  from the employee's own building, overridable with `TBANK_MYT_TZ`
  (`+05:00` or `Asia/Yekaterinburg`), and falls back to Moscow only while saying so.
  `occurrence_start` is the exception: it is an occurrence KEY, so it stays in the
  original UTC form kairos returned.
- **A recurring meeting resolves to the series master.** `calendar_event()` on an
  occurrence in 2026 returns `start` in 2020 — the first meeting of the series. The
  app cancels using that master start, gets HTTP 200, and the occurrence stays on
  the schedule. So 200 is not proof here: `calendar_cancel` re-reads the day and
  says what actually happened, and it refuses a recurring meeting without an
  explicit `occurrence_start`.

## 17. Office parking — MyT (бронь машиноместа)

Same corporate session as the calendar, different host (`workplacer.tbank.ru`).

1. `parking_places(date)` — empty date means TOMORROW, because booking opens ahead
   and today is usually already gone. One call answers the whole question: the
   booking window (`availableParkingPeriodDays`, 2 days in the capture), the hour
   it opens (`openParkingAccessTime`), the buildings, the car from the last
   booking, and the free places with their `place_id`.
2. `parking_book(date, place_id)` — car number, model and building default to the
   previous booking. The server answers **200 with an empty body**, so the tool
   re-reads the bookings and prints the row that actually got saved. The car number
   comes back transliterated (А000АА000 → A000AA000); that is how workplacer stores
   it, not a bug.
3. `office_bookings(date)` — parking, desk, fixed desk and lockers from that date
   onward, not just on it.

> **Cancelling a parking booking is not supported.** No such request appears in the
> capture, and guessing a method and path against a live corporate service is
> exactly how this repo has broken things before. Cancel in the MyT app.

## Notes

- Every tool returns a short string (counts + summaries) or JSON; its own
  docstring is the reference — they are the interface the MCP actually exposes.
- On `SESSION EXPIRED`, call `refresh_session` (refresh_token → silent re-login,
  no OTP) and retry. If it returns `REAUTH_REQUIRED`, the user must re-login
  (login + OTP + password).
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
- Money tools — all five — are REAL: `transfer`, `transfer_requisites`,
  `pay_bill`, `grocery_checkout`, `ticket_pay`. Confirm the
  amount/recipient (transfer), store+sum (grocery_checkout) or sum+seats
  (ticket_pay) with the user before running. A request to buy something is not a
  confirmation to pay for it; the confirmation is an answer to a concrete sum.
