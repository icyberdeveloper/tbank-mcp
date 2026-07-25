# Mobile checkout migration (design / future phase)

**Status:** not started — design only. Current grocery checkout uses the **web flow
(Playwright)**, which is capture-verified end-to-end (including `payment_gate_pay`).
This doc captures the alternative **native mobile checkout** found in `captures.xml`
as a future migration to **drop the Playwright/chromium dependency**.

## Why
- `grocery_checkout` currently drives a headless chromium (`src/checkout.py`:
  `sync_playwright` → `chromium.launch`). That requires `python -m playwright install
  chromium` and adds a heavy browser dependency + a page-load wait.
- The real T-Bank app places grocery orders via **native mobile JSON endpoints**
  (cookie + Bearer, no browser). Adopting them removes Playwright entirely.

## Capture-verified mobile checkout flow (captures.xml, 2026-07-22, host www.tbank.ru)
Sequence observed (all HTTP 200, mobile session):

1. `GET /mybank/api/shopping/mobile/v1/addresses/get` — delivery address.
2. `POST /mybank/api/shopping/mobile/v1/carts/get-user-carts` (no body) → `{carts:[]}`.
3. Browse/search: `sphere/gallery`, `sphere/categories`, `store-products`,
   `store-categories`, `mobile/v1/product`, `recs-items`, `get-partners`.
4. `POST /mybank/api/shopping/mobile/v1/carts/change-items-quantity` — add/modify:
   `{latitude, longitude, shoppingMerchantId, shouldUpdateCartsOrdering,
     items:[{skuId, quantity, pointOfSaleId, quantityUnit, hasAttributesScreen}]}` →
   `{changedItems:[{skuId, currentQuantity, availableQuantity, totalPriceInKopecks,…}], cartId}`.
5. `POST /mybank/api/shopping/mobile/v1/carts/cart-detail-info` — `{cartId, latitude, longitude}`
   → full cart: `items[{skuId, productCardId, categoryId, pointOfSaleId, quantity,
   quantityUnit, price, priceInKopecks, name, weightInGrams, totalPriceInKopecks,
   oldPrice,…}], outOfStockItems, currentCartPriceInKopecks, merchantInn, merchantLegalName`.
6. `POST /mybank/api/shopping/mobile/v1/checkout/get-cashback` —
   `{priceInKopecks, dolyameShopId, accountNumbers}` → `{cashbackByAccounts:[{accountId,
   cashbackPercent, cashbackSum,…}]}`.
7. `GET /mybank/api/shopping/mobile/v1/checkout/get-customer-information` →
   `{fullName, name, surname, email, phone, patronymic}`.
8. `POST /mybank/api/shopping/mobile/v1/checkout/process-order` — order PREPARE:
   `{cartId, deliveryAddress:{fullAddress, latitude, longitude, city, country, street,
   postalCode, flat, floor, entrance, flatCode, house, isPrivateHouse}, onlySelectedItems,
   deliveryMethods:[code]}` → `{items[], outOfStockItems, currentCartPriceInKopecks,
   courierDelivery:{courierShipmentSplits[], offers[{deliveryDate, deliveryPriceMinInKopecks,…}]},
   notAvailableForDelivery, replacementOptions,…}`.
9. (after) `GET /mybank/api/shopping/mobile/v1/bnpl/check-availability` (Dolyame/BNPL),
   `get-validated-promocodes-with-text-by-partner-id`.

## ⛔ Blocker — final order+pay step is NOT captured
The capture **ends** (last shopping call) right after `process-order` + `bnpl` +
`cart-detail-info` — the user **did not complete payment** in this session. So:
- `process-order` is the **prepare/validate** step (returns delivery options + availability),
  NOT the final order creation + payment.
- The actual **confirm/create-order + pay** mobile call is **unobserved**.
- ⇒ The migration **cannot be completed** from the current capture. It needs a **new
  capture of a finished mobile grocery order** (incl. the confirm/pay call + its response).

## New data model (vs current)
- Current (`grocery_cart_set` / `/api/grocery/cart`): goods `{id, count}` + `appId/pointId`.
- Mobile: items keyed by **`skuId`** + `pointOfSaleId` + `quantity`/`quantityUnit`, cart
  keyed by **`cartId`** (uuid). Money in **kopecks** (`priceInKopecks`, `currentCartPriceInKopecks`).
- ⇒ search/goods must return `skuId` (currently returns `goodForeignId`); cart ops move to
  the `carts/*` endpoints; the whole `grocery_cart_*` + `checkout.py` (Playwright) layer is
  replaced.

## Migration scope (when unblocked)
1. New client methods: `shopping_get_user_carts`, `shopping_change_items_quantity`,
   `shopping_cart_detail_info`, `shopping_process_order`, `shopping_get_cashback`,
   `shopping_get_customer_information` (+ the confirm/pay call once captured).
2. Switch `grocery_search`/`grocery_goods` to return `skuId` + `pointOfSaleId`.
3. Rebuild `grocery_add_to_cart` / `grocery_cart` / `grocery_checkout` on the mobile model.
4. **Delete `src/checkout.py` (Playwright) + drop the `playwright` dependency** from
   `plugin.json` / `pyproject.toml` / README install step.
5. Keep the attempt journal + observability (Phase 3/4) — they're flow-agnostic.

## Benefits
- No Playwright / chromium (lighter install, no browser, no page-load wait / `sleep`).
- Native JSON, faster, fewer moving parts, no cookie-sync to web.
- Money in kopecks (precise); richer item fields (weightInGrams, outOfStockItems).

## Decision points / next steps
- **Need:** one clean capture of a **completed** mobile grocery order (the confirm/pay call).
- Until then: keep the web flow (verified) — do NOT migrate half a flow.
- Optional interim: adopt the mobile cart ops for **read/preview** (richer fields) while
  keeping web checkout for the actual pay — but that mixes two cart models (not recommended).

## Open questions
- Does the mobile flow's final pay reuse `payment_gate_pay` (like the web flow) or a
  dedicated shopping pay endpoint? (Must be answered by the missing capture.)
- Are `process-order`'s `deliveryMethods` codes stable/enum or session-specific?
