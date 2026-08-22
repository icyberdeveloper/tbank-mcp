"""Builtin T-Bank API endpoint shapes (static params only — no device/session/account secrets).
Generated from the API surface; the live sessionid/deviceId/access_token/cookies + per-call
args (account, start/end, ...) are added at runtime by MobileSession. NO user secrets here."""

# The app version string captured across every template below, plus the few
# spots in client.py/server.py/checkout.py that need it outside a template.
# One literal, not five copies of it.
#
# FROZEN at 7.31.6 ON PURPOSE — do NOT bump without a coordinated re-capture.
# This value feeds self.app_version (server.py builds the live session with it),
# and self.app_version is part of the SIGNED /v1/pay canonical body. The byte-exact
# signature reproduction in tests/test_transfer.py is pinned to the transfer.json
# capture, which was recorded at 7.31.6 — change this string and the reproduced
# HMAC no longer matches that capture, with no way to regenerate it short of a fresh
# signed-payment capture at the new version. Later read-only captures (recipient.json
# on 7.39.1) run fine against 7.31.6, so the bank accepts it; the freeze is about the
# signed money path, not about the reads. Bumping = re-capture /v1/pay + regenerate
# transfer.json, not a one-line edit.
APP_VERSION = "7.31.6"

BUILTIN_ENDPOINTS = {
 "accounts_light": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/accounts_light",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "ccc": "true",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "appName": "mobile",
   "platform": "ios",
   "connectionType": "WiFi"
  }
 },
 "operations": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/operations",
  "params": {
   # NO isSuspicious: it is a per-operation FIELD, not a client flag. Passing
   # isSuspicious=true narrows the result to fraud-flagged operations — verified
   # live: same request 283 operations without it, 0 with it. It was captured from
   # a one-off "suspicious operations" screen (item 105, which returned 0) and
   # mistakenly baked in as a default.
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": APP_VERSION,
   "connectionType": "WiFi",
   "ccc": "true",
   "cpswc": "true",
   "appName": "mobile",
   "inache": "drivetransitt"
  }
 },
 "operations_histogram": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/operations_histogram",
  "params": {
   "period": "day",
   "config": "allNotInner",
   "groupBy": "category",
   "timeZone": "+03:00",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "inache": "drivetransitt",
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "platform": "ios"
  }
 },
 "active_loans": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/active_loans",
  "params": {
   "appVersion": APP_VERSION,
   "inache": "drivetransitt",
   "connectionType": "WiFi",
   "appName": "mobile",
   "platform": "ios",
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true"
  }
 },
 "payments_credit_accounts": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/payments_credit_accounts",
  "params": {
   "appVersion": APP_VERSION,
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "ccc": "true",
   "appName": "mobile",
   "platform": "ios",
   "cpswc": "true"
  }
 },
 "bonuses_aggregated": {
  "method": "GET",
  "host": "https://ms-loyalty-api.tinkoff.ru",
  "path": "/api/bonusesAggregated",
  "params": {
   "ccc": "true",
   "cpswc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "inache": "drivetransitt"
  }
 },
 "investbox_accounts": {
  "method": "GET",
  "host": "https://api-invest.t-bank-app.ru",
  "path": "/investbox/api/account/all",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "cpswc": "true",
   "ccc": "true",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "connectionType": "WiFi",
   "appName": "mobile"
  }
 },
 "ca_portfolio_statistics": {
  "method": "GET",
  "host": "https://api-invest-gw.t-bank-app.ru",
  "path": "/ca-portfolio/api/v1/user/portfolio/statistics",
  "params": {
   "appName": "mobile",
   "connectionType": "WiFi",
   "cpswc": "true",
   "ccc": "true",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "platform": "ios",
   "appVersion": APP_VERSION
  }
 },
 "ca_operations": {
  "method": "GET",
  "host": "https://api-invest-gw.t-bank-app.ru",
  "path": "/ca-operations/api/v1/user/operations",
  "params": {
   "appName": "mobile",
   "connectionType": "WiFi",
   "cpswc": "true",
   "inache": "drivetransitt",
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "platform": "ios",
   "appVersion": APP_VERSION
  }
 },
 "purchased_securities": {
  "method": "GET",
  "host": "https://api-invest-gw.t-bank-app.ru",
  "path": "/invest-portfolio/portfolios/purchased-securities",
  "params": {
   "connectionType": "WiFi",
   "ccc": "true",
   "platform": "ios",
   "appName": "mobile",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appVersion": APP_VERSION
  }
 },
 "session_status": {
  "method": "GET",
  "host": "https://www.tbank.ru",
  "path": "/api/common/v1/session_status",
  "params": {
   "appName": "supreme",
   "appVersion": "webview-2.47.31-6136d0cf",
   "origin": "web,ib5,platform"
  }
 },
 "ping": {
  "method": "POST",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/ping",
  "params": {
   "ccc": "true",
   "cpswc": "true"
  }
 },
 "notification_count": {
  "method": "GET",
  "host": "https://social-api.t-bank-app.ru",
  "path": "/api-gateway/social/notification/v1/notification/count",
  "params": {
   "inache": "drivetransitt",
   "appVersion": APP_VERSION,
   "platform": "ios",
   "cpswc": "true",
   "ccc": "true",
   "appName": "mobile",
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "profile_own_lite": {
  "method": "GET",
  "host": "https://social-api.t-bank-app.ru",
  "path": "/api-gateway/social/profile/v1/profile/own/lite",
  "params": {
   "appVersion": APP_VERSION,
   "ccc": "true",
   "platform": "ios",
   "connectionType": "WiFi",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "inache": "drivetransitt"
  }
 },
 "get_requisites": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/get_requisites",
  "params": {
   "appName": "mobile",
   "cpswc": "true",
   "inache": "drivetransitt",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "subscription_all": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/subscription/all",
  "params": {
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appName": "mobile"
  }
 },
 "subscription_all_bills": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/subscription/all_bills",
  "params": {
   "ccc": "true",
   "cpswc": "true",
   "appName": "mobile",
   "inache": "drivetransitt",
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "subscription_bills": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/subscription/bills",
  "params": {
   "connectionType": "WiFi",
   "platform": "ios",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "ccc": "true",
   "inache": "drivetransitt"
  }
 },
 "account_details": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/account_details",
  "params": {
   "ccc": "true",
   "cpswc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": APP_VERSION,
   "inache": "drivetransitt"
  }
 },
 "full_debt_amount": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/full_debt_amount",
  "params": {
   "connectionType": "WiFi",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "platform": "ios",
   "ccc": "true"
  }
 },
 "payment_templates": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/templates",
  "params": {
   "inache": "drivetransitt",
   "platform": "ios",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "cpswc": "true"
  }
 },
 "invoices_to_pay": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/cm/invoices_to_pay",
  "params": {
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "platform": "ios",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "cpswc": "true",
   "ccc": "true"
  }
 },
 "available_cards": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/available_cards",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": APP_VERSION,
   "appName": "mobile",
   "platform": "ios",
   "ccc": "true",
   "cpswc": "true",
   "inache": "drivetransitt",
   "connectionType": "WiFi"
  }
 },
 "statements": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/statements",
  "params": {
   "platform": "ios",
   "cpswc": "true",
   "inache": "drivetransitt",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "appName": "mobile"
  }
 },
 "statement_exist": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/statement_exist",
  "params": {
   "ccc": "true",
   "platform": "ios",
   "inache": "drivetransitt",
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "connectionType": "WiFi"
  }
 },
 "credit_payment_schedule": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/credit/payment_schedule",
  "params": {
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "platform": "ios",
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "credit_rating": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/credit_rating",
  "params": {
   "connectionType": "WiFi",
   "cpswc": "true",
   "inache": "drivetransitt",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "manager_info": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/manager_info",
  "params": {
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appName": "mobile"
  }
 },
 "bank_info": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/bank_info",
  "params": {
   "appName": "mobile",
   "ccc": "true",
   "cpswc": "true",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "connectionType": "WiFi"
  }
 },
 "autopayments": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/autopayments",
  "params": {
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "appName": "mobile",
   "ccc": "true",
   "platform": "ios"
  }
 },
 "sbp_subscriptions": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/sbp/subscription/list",
  "params": {
   "cpswc": "true",
   "ccc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "inache": "drivetransitt"
  }
 },
 # ?pointer=<phone>&me2meOnly=true — the banks where THIS phone is registered for
 # SBP, i.e. where the client can pull their own money from. Not the same question
 # as transfer_sbp_resolve, which asks where to SEND money to someone else.
 "sbp_me2me": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/get_sbp_cache",
  "params": {
   "me2meOnly": "true",
   "cpswc": "true",
   "ccc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "inache": "drivetransitt"
  }
 },
 # POST with an EMPTY body — the client is identified by the session alone.
 "promocodes": {
  "method": "POST",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/promocodes",
  "body": {},
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "inache": "drivetransitt",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "providers_compatible": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/providers/compatible/filter",
  "params": {
   "platform": "ios",
   "ccc": "true",
   "inache": "drivetransitt",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "client_offers": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/client_offer_essences",
  "params": {
   "inache": "drivetransitt",
   "platform": "ios",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "cpswc": "true"
  }
 },
 "gift_for_recipient": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/gift/for_recipient",
  "params": {
   "appVersion": APP_VERSION,
   "ccc": "true",
   "platform": "ios",
   "connectionType": "WiFi",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "inache": "drivetransitt"
  }
 },
 "finhealth_balance_total": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/finhealth/v2/metric/balance/total",
  "params": {
   "appVersion": APP_VERSION,
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appName": "mobile",
   "inache": "drivetransitt",
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "finhealth_balance_turnover": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/finhealth/v2/metric/balance/turnover",
  "params": {
   "appVersion": APP_VERSION,
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appName": "mobile",
   "inache": "drivetransitt",
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "finhealth_invest_turnover": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/finhealth/v2/metric/invest/turnover",
  "params": {
   "appVersion": APP_VERSION,
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appName": "mobile",
   "inache": "drivetransitt",
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "services": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/services",
  "params": {
   "ccc": "true",
   "cpswc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": APP_VERSION,
   "inache": "drivetransitt"
  }
 },
 "invest_pension_profile": {
  "method": "GET",
  "host": "https://api-invest-gw.t-bank-app.ru",
  "path": "/pension/person/api/v2/client/profile",
  "params": {
   "appName": "mobile",
   "platform": "ios",
   "inache": "drivetransitt",
   "appVersion": APP_VERSION,
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "ccc": "true"
  }
 },
 "investbox_offers": {
  "method": "GET",
  "host": "https://api-invest-gw.t-bank-app.ru",
  "path": "/investbox/deposit/api/investdeposit/offers/info",
  "params": {
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "platform": "ios",
   "inache": "drivetransitt",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "appName": "mobile"
  }
 },
 "investbox_product_yield": {
  "method": "GET",
  "host": "https://api-invest.t-bank-app.ru",
  "path": "/investbox/api/product/yield",
  "params": {
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appName": "mobile",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "broker_margin": {
  "method": "GET",
  "host": "https://api-invest.t-bank-app.ru",
  "path": "/broker-api/portfolio/margin-attributes",
  "params": {
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "ccc": "true",
   "inache": "drivetransitt",
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION
  }
 },
 "bundles_all": {
  "method": "GET",
  "host": "https://api-common-gw.t-bank-app.ru",
  "path": "/bundles/api/v1/allBundles",
  "params": {
   "ccc": "true",
   "appName": "mobile",
   "appVersion": APP_VERSION,
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "inache": "drivetransitt",
   "cpswc": "true"
  }
 },
 "business_account_info": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/get_business_account_info",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appName": "mobile",
   "connectionType": "WiFi",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "ccc": "true",
   "platform": "ios"
  }
 },
 "shared_resources_owned": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/list_owner_shared_resources",
  "params": {
   "appName": "mobile",
   "inache": "drivetransitt",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "cpswc": "true",
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "shared_resources": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/list_shared_resources",
  "params": {
   "ccc": "true",
   "appVersion": APP_VERSION,
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "platform": "ios",
   "inache": "drivetransitt",
   "connectionType": "WiFi",
   "cpswc": "true"
  }
 },
 "contact_list": {
  "method": "POST",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/contact/list",
  "params": {
   "appName": "mobile",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appVersion": APP_VERSION,
   "platform": "ios",
   "connectionType": "WiFi",
   "ccc": "true"
  }
 },
 "providers_groups": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/providers/providers/groups/filter",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": APP_VERSION,
   "appName": "mobile",
   "platform": "ios",
   "ccc": "true",
   "cpswc": "true",
   "inache": "drivetransitt",
   "connectionType": "WiFi"
  }
 },
 # The provider CATALOGUE, and the only place the per-provider field schema lives:
 # each record carries fields[] with an id, a human name, a validating `regexp`, a
 # hint and usageTypes (which fields are required for `Pay` vs for a template).
 # `groups` (a group NAME, not an id), `page`, `pageSize` and `frontendFeatureFlag`
 # are sent by the app on every captured call — without them this returns a
 # differently-scoped page than the app's own screen.
 "providers_compatible_page": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/providers/compatible/page",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "cpswc": "true",
   "ccc": "true",
   "appName": "mobile",
   "connectionType": "WiFi",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "pageSize": "100",
   "page": "1",
   "frontendFeatureFlag": "SHAWithSubs"
  }
 },
 "appointment_deliveries": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/appointment/v1/deliveries/active",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appName": "mobile",
   "connectionType": "WiFi",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "cpswc": "true",
   "platform": "ios"
  }
 },
 "grocery_cart_get": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/cart",
  "params": {
   "inache": "drivetransitt",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "ccc": "true",
   "connectionType": "WiFi",
   "appName": "mobile"
  }
 },
 "grocery_cart_set": {
  "method": "POST",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/cart/set",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "inache": "drivetransitt",
   "platform": "ios",
   "ccc": "true",
   "cpswc": "true",
   "appVersion": APP_VERSION,
   "connectionType": "WiFi"
  }
 },
 "grocery_order_get": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/order",
  "params": {
   "platform": "ios",
   "inache": "drivetransitt",
   "appName": "mobile",
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform",
   "ccc": "true",
   "cpswc": "true",
   "appVersion": APP_VERSION
  }
 },
 "grocery_catalog": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/catalog",
  "params": {
   "inache": "drivetransitt",
   "appName": "mobile",
   "cpswc": "true",
   "platform": "ios",
   "appVersion": APP_VERSION,
   "ccc": "true",
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "grocery_client_info": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/client/info",
  "params": {
   "ccc": "true",
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "connectionType": "WiFi",
   "inache": "drivetransitt",
   "cpswc": "true",
   "appVersion": APP_VERSION
  }
 },
 "payment_commission": {
  "method": "POST",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/payment_commission",
  "form": True,
  "params": {
   "platform": "ios",
   "inache": "drivetransitt",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "ccc": "true",
   "cpswc": "true",
   "appVersion": APP_VERSION
  }
 },
 "grocery_goods": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/goods",
  "params": {
   "appVersion": APP_VERSION,
   "platform": "ios",
   "inache": "drivetransitt",
   "sortBy": "DEFAULT",
   "onlyDirectGoods": "false",
   "origin": "mobile,ib5,loyalty,platform",
   "ccc": "true",
   "cpswc": "true",
   "appName": "mobile",
   "connectionType": "WiFi"
  }
 },
 "v1_pay": {
  "method": "POST",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/pay",
  "params": {
   "platform": "ios",
   "ccc": "true",
   "inache": "drivetransitt",
   "appName": "mobile",
   "connectionType": "WiFi",
   "appVersion": APP_VERSION,
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 # Shared by conversations / messages / hints / faq (path per call).
 #
 # no_base_params: the app sends the messenger host NOTHING in the query — every
 # captured URL on tm.t-bank-app.ru carries at most messageId/direction. We were
 # appending sessionid, deviceId, oldDeviceId, appName… to all of them, and
 # sessionid is the HMAC key for /v1/pay: a payment credential in a URL, on a host
 # that never asks for it and logs it like any other. Verified live that dropping
 # them changes nothing: conversations, messages and unread answer identically with
 # and without (the messenger authorises on the tmsgSessionID cookie).
 #
 # no_bearer: for the same reason and with the same evidence. Across the captured
 # tm.t-bank-app.ru traffic the app sends an Authorization header exactly zero
 # times — the cookie IS the credential here. We were adding the access_token, the
 # Bearer for every other host in this file, to a host that never asks for it.
 # Verified live: conversations, messages, unread and the file download all answer
 # identically with no Authorization at all.
 #
 # messenger_send and messenger_mark_read keep theirs: the same captures say the
 # app sends none there either, but a send is a real message to a support agent and
 # cannot be probe-tested, so that one stays as it is until someone has a reason to
 # send a message anyway.
 "messenger_base": {
  "method": "GET",
  "host": "https://tm.t-bank-app.ru",
  "path": "/app/bank/messenger/conversations/unread",
  "params": {},
  "no_base_params": True, "no_bearer": True
 },
 "messenger_send": {
  "method": "POST",
  "host": "https://tm.t-bank-app.ru",
  "path": "/app/bank/messenger/conversations/{conversation_id}/messages",
  "params": {},
  "no_base_params": True,          # see messenger_base: no sessionid on this host
  "headers": {
   "Content-Type": "application/vnd.chats.chatapi.text.message.in.v1+json",
   "Accept": "application/vnd.chats.chatapi.text.message.out.v1+json"
   # Tmsg-User-Agent is NOT pinned here: it carries the app version, the iOS
   # version and the device model, all of which the session already knows.
   # Frozen as a literal it said iOS:17.5.1 — the exact stale value removed from
   # the main User-Agent — and omitted the `device:` segment every captured
   # request carries. Built in client._mobile_headers instead.
  }
 }
}

# 14 additional valuable endpoints found by the completeness audit.
BUILTIN_ENDPOINTS.update({
    "detected_merchant_subscriptions": {"method": "GET", "host": "https://api.t-bank-app.ru", "path": "/subscriptions/merchant/v2/subscriptions", "params": {}},
    "user_profile": {"method": "GET", "host": "https://id.t-bank-app.ru", "path": "/userinfo/userinfo", "params": {"ccc": "true", "cpswc": "true", "client_id": "gorod-app"}},
    "my_homes": {"method": "GET", "host": "https://my-home.tinkoff.ru", "path": "/api/v1/gw/homes", "params": {}},
    "my_cars": {"method": "GET", "host": "https://myauto.t-bank-app.ru", "path": "/api/my-auto/v2/cars/list-light", "params": {"inache": "drivetransitt"}},
    "payment_shortcuts": {"method": "GET", "host": "https://shortcuts.t-bank-app.ru", "path": "/v2/shortcuts", "params": {}},
    "resolve_payment_qr": {"method": "POST", "host": "https://api.t-bank-app.ru", "path": "/providers/providers/qr/resolve", "params": {}},
    "finhealth_account_presets": {"method": "GET", "host": "https://api.t-bank-app.ru", "path": "/finhealth/v2/settings/accounts/presets/default", "params": {}},
    "push_unread_count": {"method": "GET", "host": "https://push-history-api.t-bank-app.ru", "path": "/bank/v3/notifications/unseen/count", "params": {}},
})

# Endpoints for the scenarios added from captures2.xml (cards, documents, orders,
# grocery nutrition, cinema). Verified against the capture; item numbers in comments
# refer to captures2.xml.
#
# `session_param` overrides the query key the mobile sessionid is sent under. Most
# hosts take `sessionid`, but the prefill-profile and insurance hosts spell it
# `sessionId` and 401 on the lowercase form.
BUILTIN_ENDPOINTS.update({
    # ?ucid=<card ucid> — the card's ucid, NOT its id (account_cards gives both)
    "card_limits": {"method": "GET", "host": "https://api.t-bank-app.ru",
                    "path": "/v1/limits", "params": {}},
    # Returns the FULL pan + cvv2 + expireDate. The app sends a device-attributes
    # bundle alongside; card_requisites() fills those in from the session.
    "card_credentials": {"method": "GET", "host": "https://api.t-bank-app.ru",
                         "path": "/v1/card_credentials", "params": {}},
    # ?account=<id>;RUB (repeatable — pass a list to get every currency at once)
    "account_group_requisites": {"method": "GET", "host": "https://api.t-bank-app.ru",
                                 "path": "/v1/account_group_requisites", "params": {}},

    # ---- identity documents (items 8/14/15) -------------------------------
    "prefill_contact": {"method": "GET", "host": "https://api.t-bank-app.ru",
                        "path": "/api/prefill/profile/contact", "params": {},
                        "session_param": "sessionId"},
    # path is parameterized: /api/prefill/profile/contact/{contactId}/document/all
    "prefill_documents": {"method": "GET", "host": "https://api.t-bank-app.ru",
                          "path": "/api/prefill/profile/contact/_/document/all",
                          "params": {}, "session_param": "sessionId"},
    "prefill_userinfo_brief": {"method": "GET", "host": "https://api.t-bank-app.ru",
                               "path": "/api/prefill/profile/contact/_/userinfo/brief",
                               "params": {}, "session_param": "sessionId"},

    # ---- orders across every vertical (items 96/97/121) --------------------
    # /api/orders/list is the only endpoint that returns groceries, cinema,
    # concerts, flights, trains and hotels in ONE list (187 orders back to 2018).
    "orders_list": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                    "path": "/api/orders/list", "params": {}},
    # ?orderId= — full detail for entertainment orders (seats, hall, QR code)
    "order_get": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                  "path": "/api/order", "params": {}},

    # ---- grocery item detail incl. nutrition (item 1020) -------------------
    "grocery_good": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                     "path": "/api/grocery/good", "params": {"isExpress": "false"}},

    # ---- cinema (items 717/730/800) ---------------------------------------
    # POST with body {"genres": []}; the collectionCode selects the city listing
    "events_collection": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                          "path": "/api/events/collection",
                          "params": {"service": "cinema", "page": "1", "count": "30"}},
    # POST {"date","eventId","city","sort":{"by":"distance"},"location":{lat,lon}}
    "schedule_movie": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                       "path": "/api/schedule/movie", "params": {}},

    # ---- extras surfaced by captures2 -------------------------------------
    # bank-issued certificates (справки) — returns a BARE list, no envelope
    "bank_documents": {"method": "GET", "host": "https://cx-evolution-api.t-bank-app.ru",
                       "path": "/v3/cx-evolution-api/documents/get-document-list",
                       # X-Api-Version: v2 selects the record shape. Without it the
                       # endpoint answers in the v1 form, whose ids are negative ints
                       # in `tecmId` rather than the uuid in `tecmUuid` — which is why
                       # the tool used to print negative ids nothing else accepts.
                       # Capture-verified: captures2.xml #44.
                       "headers": {"X-Api-Version": "v2"},
                       "params": {}},
    "insurance_policies": {"method": "GET", "host": "https://api.tinsurance.ru",
                           "path": "/api/v2/policy/active_with_claims", "params": {},
                           "session_param": "sessionId"},
    # ?paymentId= — responds with application/pdf, not JSON (raw=True).
    # The BFF picks its serializer from Accept: with `application/json` (our
    # default) it answers 200 + {"resultCode":"INTERNAL_ERROR","errorMessage":
    # "Unsupported EndpointOutput: FixedStatusCode"}. Only the app's browser-ish
    # Accept yields the PDF — verified live across four paymentIds.
    "payment_receipt_pdf": {"method": "GET", "host": "https://api.t-bank-app.ru",
                            "path": "/v1/payment_receipt_pdf", "params": {},
                            "raw": True,
                            "headers": {"Accept": "text/html,application/xhtml+xml,"
                                                  "application/xml;q=0.9,*/*;q=0.8"}},
})

# messenger/conversations/unread does its own content negotiation and 406s on the
# generic `application/json` that _mobile_headers injects. It needs the exact
# vendor type below (capture: captures.xml items 126/211) — hence its own template
# rather than a header on the shared `messenger_base`, which the conversations /
# messages / hints / faq / markRead paths reuse and which DO take application/json.
BUILTIN_ENDPOINTS.update({
    "messenger_unread": {
        "method": "GET", "host": "https://tm.t-bank-app.ru",
        "path": "/app/bank/messenger/conversations/unread", "params": {},
        "no_base_params": True, "no_bearer": True,   # see messenger_base
        "headers": {"Accept": "application/vnd.chats.chatapi.unread.out.v3+json"},
    },
    # markRead is a PUT with its own vendor types and an empty body. It used to be
    # sent through messenger_base, i.e. as a GET asking for application/json —
    # neither the method nor the content types the app uses. The path is supplied
    # per call via path_override.
    "messenger_mark_read": {
        "method": "PUT", "host": "https://tm.t-bank-app.ru",
        "path": "/app/bank/messenger/conversations/unread", "params": {},
        "no_base_params": True,          # see messenger_base: no sessionid here
        "headers": {
            "Content-Type": "application/vnd.chats.chatapi.markread.in.v1+json",
            "Accept": "application/vnd.chats.chatapi.markread.out.v1+json",
        },
    },
    # A chat attachment: the bytes behind content.fileId of a messageType="file"
    # record. GET .../conversations/{conversationId}/files/{fileId}, path supplied
    # per call. raw=True — the body is application/octet-stream, and _unwrap would
    # raise HTTP_200 on it. Verified live against a support-chat .xlsx.
    #
    # Three properties of this route, all found by probing it:
    #   * the tmsgSessionID cookie alone authorises it — with the cookie and NO
    #     Authorization header it still answers 200 with the file;
    #   * the file is scoped to its conversation. The same fileId under a different
    #     conversationId of the SAME user answers 401 NOT_AUTHORIZED, so the pair is
    #     the key, not the fileId;
    #   * no_base_params, because the app sends none here (nor on any other
    #     tm.t-bank-app.ru path — the captured messenger URLs carry only
    #     messageId/direction). Sending them costs a 200 nothing, but sessionid is
    #     the HMAC key for /v1/pay and does not belong in a URL this host will log.
    "messenger_file": {
        "method": "GET", "host": "https://tm.t-bank-app.ru",
        "path": "/app/bank/messenger/conversations/unread", "params": {},
        "raw": True, "no_base_params": True, "no_bearer": True,
        # The app's own Accept for this route. It is NOT load-bearing here — the
        # route also answers the `application/json` this template would otherwise
        # inherit — but two endpoints in this file were already caught choosing
        # their serializer off Accept (messenger_unread 406, payment_receipt_pdf
        # answering an error envelope), so the app's spelling is the one to send.
        "headers": {"Accept": "application/octet-stream"},
    },
})

# Travel order detail. Only the hotel host is wired up here: it authorizes on the
# Bearer alone (verified live).
#
# Trains and flights are NOT unreachable — that earlier reading was wrong, and the
# captures-gorod.xml traffic shows why. `/v1/travel_link_auth_token` answers 200
# there (the INSUFFICIENT_PRIVILEGES seen before was a session that had lapsed to
# ANONYMOUS), and trains bootstrap from `trains-front/papi/auth/link-token`, not
# from the `tsocial…/auth/game` path that returned B002D965. Both hosts want their
# own cookie jar, which is why they are not templates yet rather than because the
# bank refuses us. See docs/FLOWS.md.
BUILTIN_ENDPOINTS.update({
    # path is parameterized: /api/v1/hotels/bookings/{bookingId}
    "hotel_booking": {"method": "GET", "host": "https://hotels.t-bank-app.ru",
                      "path": "/api/v1/hotels/bookings/_", "params": {}},
})

# Ticket booking: seat map → order/create → payment-gate → cancel.
# Verified against captures2.xml items 745/748/763/850/935/965/970.
BUILTIN_ENDPOINTS.update({
    # ?eventId&slotId&objectId — seats with status/price; seatId is "row:number"
    # for cinemas and a composite "Сектор|цена§~§id|type" string for concerts.
    "scheme_sectors_movie": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                             "path": "/api/scheme/sectors/movie", "params": {}},
    "scheme_sectors_concert": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                               "path": "/api/scheme/sectors/concert", "params": {}},
    # free-seating venues answer here instead, as sectors with availableTickets
    "scheme_hall_concert": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                            "path": "/api/scheme/hall/concert", "params": {}},
    # POST {"eventId"} — concert showings are not date-scoped like movies
    "schedule_concert": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                         "path": "/api/schedule/concert", "params": {}},
    # POST {slotId, objectId, eventId, seats:[{id,type}]} → order + nfsPaymentToken.
    # Creates a RESERVATION, moves no money; unpaid orders expire on their own.
    "order_create_movie": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                           "path": "/api/order/create/movie", "params": {}},
    "order_create_concert": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                             "path": "/api/order/create/concert", "params": {}},
    # POST ?orderId=[&paymentId=] with an EMPTY body. What decides the outcome is
    # the order's own isCancelAvailable, not the parameter set: the one captured
    # success (delete-order.xml, a PAID order the bank had flagged cancelable) came
    # back 200 {"status":"Success"} and the order moved to PARTIALLY_CANCELED. Seven
    # attempts on fresh UNPAID reservations answered 200 with a BUSINESS refusal
    # — {"status":"Failed","code":…}, codes 400/500/1002/1009 observed live — and
    # changed nothing. Content-Type and Accept do not move that needle: both forms
    # were tried on the same orders and answered identically. paymentId is sent
    # because the app sends it, not because its absence is a silent no-op.
    # Empty body with Content-Type: application/json — same as the grocery flavour
    # below. The client must pass body=None; body={} would put a literal `{}` on the
    # wire, which no captured cancel sends.
    "order_cancel_movie": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                           "path": "/api/order/cancel/movie", "params": {},
                           "headers": {"Content-Type": "application/json"}},
    "order_cancel": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                     "path": "/api/order/cancel", "params": {},
                     "headers": {"Content-Type": "application/json"}},
    # The GROCERY flavour of the same path (cancel-grossary.xml): ONLY orderId in
    # the query — no paymentId, unlike tickets above — and a genuinely EMPTY body
    # that the app still stamps Content-Type: application/json. The verdict is
    # payload.{status,code}; the outer "status":"Ok" is transport-level and reads
    # Ok even when nothing was cancelled (payload {"status":"Failed","code":"605"}
    # = the order is already cancelled).
    "grocery_order_cancel": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                             "path": "/api/order/cancel", "params": {},
                             "headers": {"Content-Type": "application/json"}},
    # MONEY. Note the host: the existing `payment_gate_pay` template points at the
    # WEB gate (www.tbank.ru, cookie-auth, used by the grocery Playwright
    # checkout). Marketplace orders from the mobile app pay through the MOBILE
    # gate below on a plain Bearer — no browser involved.
    # Pg-Api-System names the calling system and the gate sends it on EVERY call.
    # Both captured flavours carry it and they differ: the grocery web checkout says
    # "t-grocery-ib", the app's own marketplace payment (tickets) says
    # "t-entertainment-mb". Same path, same body shape, different caller — so
    # omitting it leaves the gate to guess which one a payment came from.
    "payment_gate_pay_mobile": {"method": "POST", "host": "https://api.t-bank-app.ru",
                                "path": "/pg-api/v1/payment-gate/payments",
                                "headers": {"Pg-Api-System": "t-entertainment-mb"},
                                "params": {}},
})


# Theatre and exhibitions ride the same four shapes as cinema and concerts —
# only the path segment differs. Counted in captures-gorod.xml, all 200:
# schedule/spectacle 9, schedule/exhibition 1, scheme/sectors/spectacle 3,
# scheme/sectors/exhibition 1, scheme/hall/spectacle 5, order/create/spectacle 2,
# order/create/exhibition 1. There is no scheme/hall/exhibition anywhere.
BUILTIN_ENDPOINTS.update({
    "schedule_spectacle": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                           "path": "/api/schedule/spectacle", "params": {}},
    "schedule_exhibition": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                            "path": "/api/schedule/exhibition", "params": {}},
    "scheme_sectors_spectacle": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                                 "path": "/api/scheme/sectors/spectacle", "params": {}},
    "scheme_sectors_exhibition": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                                  "path": "/api/scheme/sectors/exhibition", "params": {}},
    "scheme_hall_spectacle": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                              "path": "/api/scheme/hall/spectacle", "params": {}},
    "order_create_spectacle": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                               "path": "/api/order/create/spectacle", "params": {}},
    "order_create_exhibition": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                                "path": "/api/order/create/exhibition", "params": {}},
    # ?service=&cityId= — the vertical's landing shelves. Its collections[].code
    # holds the REAL collectionCode ("Segodnya-v_kino_Moskva"), which used to be
    # guessed by transliterating the city name. The guess only ever worked for the
    # cities whose code happens to match: the server's own codes spell Moscow three
    # different ways (Moskva, moscow, msk) depending on the shelf.
    "events_by_service": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                          "path": "/api/events/by/service", "params": {}},

    # ---- vertical catalogues ----------------------------------------------
    # POST {"cityId":"1","count":20,"page":1,
    #       "date":{"from":"…T00:00:00+03:00","to":"…T23:59:59+03:00"}}
    #
    # The app only ever sends a single day because its calendar picks one, but the
    # server takes a RANGE — probed live: one day in Moscow is 83 films, eight days
    # is 197 unique ones, and the extra titles are real (TheatreHD and Globe
    # screenings that run on specific later dates). The time inside the bounds is
    # ignored; an evening window returns the whole day.
    #
    # movie behaves differently from the other two and the difference is not
    # cosmetic: it ignores count/page and returns the vertical whole, and its slots
    # come back EMPTY — the showings are in schedule/movie. concert and spectacle
    # paginate server-side and do carry slots. There is no /api/events/exhibition
    # in any capture, so exhibitions have no catalogue of their own.
    "events_movie": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                     "path": "/api/events/movie", "params": {}},
    "events_concert": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                       "path": "/api/events/concert", "params": {}},
    "events_spectacle": {"method": "POST", "host": "https://lifestyle.t-bank-app.ru",
                         "path": "/api/events/spectacle", "params": {}},

    # ---- venues -----------------------------------------------------------
    # ?service=&cityId=&page=&count=&include=all — the venue directory, and the
    # only way to learn an objectId without going through some event that plays
    # there. count tops out at 100: 116 answers 400, so Moscow's 116 cinemas are
    # two pages. There is NO text search — no q/query/name/title in any captured
    # request — so matching a name is the client's job and every page has to be
    # read before filtering. service is a validated enum: cinema/concert/theatre/
    # exhibition are accepted (spectacle and museum answer 400), but only cinema
    # returns data right now; the other three answer 204, which is «this vertical
    # is not serving» and must not be printed as «no venues here».
    "events_places": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                      "path": "/api/events/objects",
                      "params": {"include": "all", "page": "1", "count": "100"}},
    # ?objectId&page&count — what is on at one venue. Covers concert, theatre and
    # exhibition; cinemas are NOT here, their repertoire comes from schedule/movie
    # with an objectId and a date.
    "place_schedule": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                       "path": "/api/events/place/schedule",
                       "params": {"page": "1", "count": "50"}},
    # ?objectId — the venue card. geo.address came back EMPTY in all seven captured
    # calls, so the address has to be read from place_halls instead.
    "place_info": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                   "path": "/api/events/place/info", "params": {}},
    "place_halls": {"method": "GET", "host": "https://lifestyle.t-bank-app.ru",
                    "path": "/api/events/place/halls", "params": {}},
})


# ---- afisha verticals ------------------------------------------------------
# One row per vertical, because the bank spells each one four different ways and
# picking the wrong spelling fails differently every time. The path segment is
# `movie`, the ?service= for the same thing is `cinema`; theatre is `spectacle`
# in a path and `theatre` in a service. Nothing in the responses declares the
# mapping — it was read off objects that carry both at once (an event with
# eventType=spectacle arriving under service=theatre).
#
# `order_types` is a DIFFERENT axis: it is the type of the VENUE, not of the
# vertical, and the two disagree — the orders feed holds objectType=cinema rows
# whose eventType is concert. It belongs here only because the feed filter has to
# be built from somewhere, and having one table beats four hand-kept tuples.
#
# Confirmed live except where noted.
VERTICALS = {
    "movie": {
        "ru": "кино",
        "segment": "movie",          # /api/{schedule,scheme/sectors,order/create}/…
        "service": "cinema",         # ?service= on events/objects, events/info, by/service
        "screen": "movie_main",      # search_app screen
        "order_types": ("cinema",),
        "sectors_key": "scheme_sectors_movie",
        "hall_key": "",              # cinemas have numbered seats, no free seating
        "schedule_key": "schedule_movie",
        "create_key": "order_create_movie",
        "cancel_key": "order_cancel_movie",   # the only cancel segment in any capture
        "seat_type": "basic",        # seats[].type — ONLY movie sends it
        "seat_render": "grid",       # numbered rows vs a flat list
        "catalog_key": "events_movie", "catalog_paged": False,
    },
    "concert": {
        "ru": "концерт",
        "segment": "concert",
        "service": "concert",
        "screen": "concerts_main",   # plural, unlike the others
        "order_types": ("concerthall", "club", "sports", "other"),
        "sectors_key": "scheme_sectors_concert",
        "hall_key": "scheme_hall_concert",
        "schedule_key": "schedule_concert",
        "create_key": "order_create_concert",
        "cancel_key": "order_cancel",
        "seat_type": "",
        "seat_render": "list",
        "catalog_key": "events_concert", "catalog_paged": True,
    },
    "spectacle": {
        "ru": "театр",
        "segment": "spectacle",
        "service": "theatre",
        "screen": "spectacle_main",
        # ASSUMED, not observed: no spectacle order appeared in any capture, so
        # this is the venue type such an order would plausibly carry. An extra
        # type in the feed filter is harmless; a missing one loses orders.
        "order_types": ("theatre",),
        "sectors_key": "scheme_sectors_spectacle",
        "hall_key": "scheme_hall_spectacle",
        "schedule_key": "schedule_spectacle",
        "create_key": "order_create_spectacle",
        "cancel_key": "order_cancel",
        "seat_type": "",
        "seat_render": "list",
        "catalog_key": "events_spectacle", "catalog_paged": True,
    },
    "exhibition": {
        "ru": "выставка",
        "segment": "exhibition",
        "service": "exhibition",
        "screen": "exhibition_main",
        "order_types": ("museum",),
        "sectors_key": "scheme_sectors_exhibition",
        "hall_key": "",              # no /api/scheme/hall/exhibition in any capture
        "schedule_key": "schedule_exhibition",
        "create_key": "order_create_exhibition",
        "cancel_key": "order_cancel",
        "seat_type": "",
        "seat_render": "list",
        "catalog_key": "", "catalog_paged": False,
    },
}

# What an agent may type. The Russian words are what the tools document; the
# segments are accepted too because the API uses them and they leak into
# conversations through ids and paths.
VERTICAL_ALIASES = {
    "фильм": "movie", "кино": "movie", "movie": "movie", "cinema": "movie",
    "концерт": "concert", "concert": "concert", "concerthall": "concert",
    "театр": "spectacle", "спектакль": "spectacle",
    "spectacle": "spectacle", "theatre": "spectacle", "theater": "spectacle",
    "выставка": "exhibition", "музей": "exhibition",
    "exhibition": "exhibition", "museum": "exhibition",
}


# ---- marketplace (Шопинг) --------------------------------------------------
# webview.t-bank-app.ru serves the shopping webview, and it is not the native
# app: across 179 captured requests there is not one Authorization header. It
# authorises on cookies whose sessionID and sso_api_session both carry the very
# access_token the mobile session already holds — hence no_bearer, and the cookie
# is assembled in MobileSession._cookie_for. It also wants none of the native
# query context: appName, appVersion and platform=webview_ios, nothing else.
#
# The search parameter is `search`, NOT `query` — the sibling media endpoint uses
# `query`, which is exactly the kind of near-miss worth writing down.
_SHOP = {"appName": "mobile", "appVersion": APP_VERSION, "platform": "webview_ios"}
# The nine legacy webview templates above carry no_base_params/no_bearer for the
# reason _SHOP_LEAN exists: webview.t-bank-app.ru authorises on its own cookies, and
# 1177 of 1177 captured requests to that host carry NEITHER an Authorization header
# NOR the native query block (sessionid, deviceId, oldDeviceId, ccc, cpswc, origin,
# inache, connectionType). Sending what the app does not send is the same class of
# divergence that once broke the grocery cart — and it was sending the Bearer to a
# host that never asks for one.
_SHOP_LEAN = {"no_base_params": True, "no_bearer": True}

BUILTIN_ENDPOINTS.update({
    # The delivery address, and the only source of the lat/lon that search wants.
    "shop_address": {"method": "GET", "host": "https://webview.t-bank-app.ru",
                     "path": "/mybank/api/shopping/mobile/v1/addresses/get",
                     "params": dict(_SHOP), **_SHOP_LEAN},
    # ?search=&size=&offset=&latitude=&longitude= — server-side paging, real
    # totalHits. Products carry skuId/pointId/dolyameShopId, which is the triple a
    # cart write needs.
    "shop_search": {"method": "GET", "host": "https://webview.t-bank-app.ru",
                    "path": "/mybank/api/shopping/mobile/v5/search/multi-search",
                    "params": {**_SHOP, "size": "20", "offset": "0",
                               "withFacets": "true", "showRating": "false",
                               "withCorrection": "true", "showUnavailable": "true",
                               "addUtm": "true", "useAutoFilters": "true"},
                    **_SHOP_LEAN},
    # Empty body with Content-Length: 0 — body=None, not {}.
    "shop_carts": {"method": "POST", "host": "https://webview.t-bank-app.ru",
                   "path": "/mybank/api/shopping/mobile/v1/carts/get-user-carts",
                   "params": dict(_SHOP),
                   "headers": {"Content-Type": "application/json"}, **_SHOP_LEAN},
})


# ---- flights (www.tbank.ru /api/travel) ------------------------------------
# The captured traffic runs these under a WEB session (a psid cookie from a
# multi-step bridge), which read as «unreachable from a mobile session». It is
# not: probed live, the same endpoints answer under the plain mobile Bearer with
# the session in `sessionId` and X-Travel-Context: mb. No bridge, no new
# credential — so the whole psid apparatus the plan budgeted for is not built.
#
# The search streams. startStreaming opens it and returns the first batch;
# nextBatch blocks until the next one is ready and sets isOver on the last.
# offers[].flights are indices into the CONCATENATION of every batch, not into
# the batch they arrived in — measured: 757 flights, highest offer index 756 — so
# a flight can only be resolved after the whole stream is stitched.
_TRAVEL_MB = {"session_param": "sessionId",
              "headers": {"X-Travel-Context": "mb"}}
_TRAVEL_MB_POST = {"session_param": "sessionId",
                   "headers": {"Content-Type": "application/json",
                               "X-Travel-Context": "mb"}}

BUILTIN_ENDPOINTS.update({
    "flight_search_start": {"method": "POST", "host": "https://www.tbank.ru",
                            "path": "/api/travel/flight/search/startStreaming",
                            "params": {}, **_TRAVEL_MB_POST},
    "flight_search_next": {"method": "POST", "host": "https://www.tbank.ru",
                           "path": "/api/travel/flight/search/nextBatch",
                           "params": {}, **_TRAVEL_MB_POST},
    # Past searches, and the only place codes come back WITH their names — there
    # is no name→IATA resolver anywhere in the captures.
    "flight_history": {"method": "GET", "host": "https://www.tbank.ru",
                       "path": "/api/travel/flight/history/getSearchHistoryBySession",
                       "params": {}, **_TRAVEL_MB},
})


# ---- flight booking (www.tbank.ru) -----------------------------------------
# The capture runs this leg under a linked WEB session minted through
# travel_link_auth_token → session/link/authorize → check_auth. Probed live, it is
# not needed: every call below answers 200 under the plain mobile session, the
# same way search does. The bridge is not built.
#
# TWO different ids are in play and they are not interchangeable:
#   offerId  "{searchId}.{n}"  — what flight_search prints
#   uuid     a bare UUID       — what preliminary RETURNS, and the only id
#                                fareRules/getBaggage/getSeatMaps/travel_pay take
# So preliminary is a mandatory step, not an optional preview: it re-prices the
# offer and hands out the id everything downstream needs. The tools keep the uuid
# internal so an agent can never send the wrong one.
BUILTIN_ENDPOINTS.update({
    # ?uuid={searchId}.{n} — body is an EMPTY object, not the offer.
    "flight_preliminary": {"method": "POST", "host": "https://www.tbank.ru",
                           "path": "/api/travel/flight/booking/v2/preliminary",
                           "params": {"context": "travel"}, **_TRAVEL_MB_POST},
    "flight_fare_rules": {"method": "GET", "host": "https://www.tbank.ru",
                          "path": "/api/travel/flight/booking/fareRules",
                          "params": {"context": "travel"}, **_TRAVEL_MB},
    "flight_baggage": {"method": "GET", "host": "https://www.tbank.ru",
                       "path": "/api/travel/flight/getBaggage",
                       "params": {"context": "travel"}, **_TRAVEL_MB},
    "flight_seatmaps": {"method": "GET", "host": "https://www.tbank.ru",
                        "path": "/api/travel/flight/getSeatMaps",
                        "params": {"context": "travel", "isNative": "true"},
                        **_TRAVEL_MB},
    # {"offerId"} -> the price of the check-in service. Seats are sold as part of
    # it: the captured purchase carries BOTH a `seats` block and a `checkin` block,
    # and the charge is fare + seats + this — three numbers, not two.
    "flight_checkin_calc": {"method": "POST", "host": "https://www.tbank.ru",
                            "path": "/api/travel/checkin/calcPrice",
                            "params": {"context": "travel"}, **_TRAVEL_MB_POST},
    # The money call. It BOOKS AND PAYS in one POST — there is no separate hold
    # step for flights — and answers asynchronously: status "Working" plus a
    # detachKey that IS the orderId. The result is polled from pay/result.
    "flight_pay": {"method": "POST", "host": "https://www.tbank.ru",
                   "path": "/api/prefill/proxy/travel_pay",
                   "params": {"context": "travel"}, **_TRAVEL_MB_POST},
    # Polled until status leaves "Working": Ok carries bookingInfo.bookingNumber
    # (the PNR). A 400 here means no payment is in flight, not an auth failure.
    "flight_pay_result": {"method": "GET", "host": "https://www.tbank.ru",
                          "path": "/api/travel/flight/booking/pay/result",
                          "params": {"context": "travel"}, **_TRAVEL_MB},
    # {"orderId"} -> the order's documents (itinerary receipts), each with a
    # document_id fetched separately as PDF bytes.
    "flight_documents": {"method": "POST", "host": "https://www.tbank.ru",
                         "path": "/api/travel/flight/v2/documents",
                         "params": {"context": "travel"}, **_TRAVEL_MB_POST},
    # PDF bytes. Accept is spelled out: some routes choose their serializer from
    # it and the session default is application/json.
    "flight_document": {"method": "GET", "host": "https://www.tbank.ru",
                        "path": "/api/travel/flight/v1/document",
                        "params": {}, "raw": True,
                        "session_param": "sessionId",
                        "headers": {"X-Travel-Context": "mb", "Accept": "*/*"}},
})


# ---- trips: one feed across flights, rail and hotels ------------------------
# The cross-vertical answer to «where am I going». Distinct from orders(): that
# lists ORDERS (including groceries and cinema), this lists TRIPS with their
# timeline, and a rail order reaches it through train_trip_id.
BUILTIN_ENDPOINTS.update({
    "trips": {"method": "GET", "host": "https://www.tbank.ru",
              "path": "/api/travel/v1/trips/get-trips", "params": {}, **_TRAVEL_MB},
    "trip": {"method": "GET", "host": "https://www.tbank.ru",
             "path": "/api/travel/v1/trips/get-trip", "params": {}, **_TRAVEL_MB},
    "trip_insurance": {"method": "GET", "host": "https://www.tbank.ru",
                       "path": "/api/travel/v1/trips/get-insurance",
                       "params": {}, **_TRAVEL_MB},
})


# ---- what a travel purchase actually costs ---------------------------------
# Four different services answer four different halves of «how should I pay»:
# which accounts are eligible, how many loyalty bonuses may be burned, how much
# cashback comes back, and what the installment plans are.
BUILTIN_ENDPOINTS.update({
    "travel_accounts": {"method": "GET", "host": "https://www.tbank.ru",
                        "path": "/api/common/v1/travel/checkout/accounts",
                        "params": {}, **_TRAVEL_MB},
    # This one is NOT under /api/travel and takes no travel context header.
    "travel_usable_bonuses": {
        "method": "POST", "host": "https://www.tbank.ru",
        "path": "/api/loyalty/compensation/api/mother-api/v1/get_usable_bonuses",
        "params": {}, "session_param": "sessionId",
        "headers": {"Content-Type": "application/json"}},
    "travel_predict_bonuses": {"method": "POST", "host": "https://www.tbank.ru",
                               "path": "/api/travel/miles/predictBonusesForOrder",
                               "params": {"context": "travel"}, **_TRAVEL_MB_POST},
    "travel_installment": {"method": "GET", "host": "https://www.tbank.ru",
                           "path": "/api/travel/flight/loan/calcInstallment",
                           "params": {"context": "travel"}, **_TRAVEL_MB},
    "travel_loan_allowance": {"method": "GET", "host": "https://www.tbank.ru",
                              "path": "/api/travel/flight/loan/checkAllowance",
                              "params": {"context": "travel"}, **_TRAVEL_MB},
})


# ---- hotels (hotels.t-bank-app.ru) -----------------------------------------
# Plain Bearer, no travel context header, no cookie of its own — the simplest of
# the three verticals to reach and the only one that cannot be BOUGHT: no capture
# covers the booking POST, so `bookHash` from the rates call has nowhere to go and
# guessing that shape is exactly the mistake this repo keeps paying for.
_HOTELS = "https://hotels.t-bank-app.ru"
_HOTELS_POST = {"headers": {"Content-Type": "application/json"}}

BUILTIN_ENDPOINTS.update({
    # {"input": "Сочи"} -> locations[] (locationId for search) + hotels[] (direct hits)
    "hotel_autocomplete": {"method": "POST", "host": _HOTELS,
                           "path": "/search-api/search/autocomplete",
                           "params": {}, **_HOTELS_POST},
    # The listing. isLoadingCompleted=false means the answer is still filling in —
    # the same «this is not the whole result» honesty the flight stream needs.
    "hotel_search": {"method": "POST", "host": _HOTELS,
                     "path": "/search-api/v2/hotels/map/searchHotelPoints",
                     "params": {}, **_HOTELS_POST},
    # {"hotelIds":[…]} -> name, stars, location for a BATCH. The listing carries
    # only ids, so this is what turns a search result into something readable —
    # one call for the whole page, not one per hotel.
    "hotel_static_info": {"method": "POST", "host": _HOTELS,
                          "path": "/search-api/v1/hotels/getHotelStaticInfo",
                          "params": {}, **_HOTELS_POST},
    # path parameterized: /api/v1/hotels/{hotelId}
    "hotel_card": {"method": "GET", "host": _HOTELS,
                   "path": "/api/v1/hotels/_", "params": {}},
    # path parameterized: /api/v3/hotels/{hotelId}/rates — tariffs, meal, the
    # cancellation ladder and bookHash.
    "hotel_rates": {"method": "POST", "host": _HOTELS,
                    "path": "/api/v3/hotels/_/rates", "params": {}, **_HOTELS_POST},
    # path parameterized: /api/v1/review/{hotelId}/summary — one sentence of
    # generated prose, and it is often empty.
    "hotel_review_summary": {"method": "GET", "host": _HOTELS,
                             "path": "/api/v1/review/_/summary", "params": {}},
    "hotel_review_ratings": {"method": "GET", "host": _HOTELS,
                             "path": "/api/v1/review/_/ratings", "params": {}},
})


# ---- rail (trains.t-bank-app.ru) -------------------------------------------
# This host keeps its own session: GET https://trains.t-bank-app.ru/ with the
# ordinary mobile Bearer answers with Set-Cookie, and the search API accepts
# those cookies. One request, no redirect chain, no browser — the earlier note
# calling it unreachable was reading the wrong bootstrap path.
#
# It takes none of the native query context and no Bearer on the API calls; the
# cookie is what authorises, and MobileSession._ensure_trains mints it in an
# ISOLATED jar because the bootstrap response also clears the tbank.ru cookie.
#
# That cookie also authorises the ORDER endpoints, not just search: probed live,
# `/api/info/contactInfo` answers with the real phone and e-mail and
# `/api/orders?status[0]=Booked` answers 200. The SSO redirect chain the browser
# front runs (authorize → hidden-auth-html → session/get_by_token) buys nothing
# we do not already have.
#
# `trains.tbank.ru` is the same host under a second name — both resolve and both
# serve the bootstrap — so the alias is matched in _cookie_for rather than
# duplicated here.
RAIL_HOST = "https://trains.t-bank-app.ru"
# The rail front's own version, NOT the mobile app's APP_VERSION: these calls go
# out as trains-front, and that is the pair the host is sent.
RAIL_APP_NAME = "trains-front"
RAIL_APP_VERSION = "1.66.0"


def _rail(api_method: str, *, post: bool = False, accept: str = "",
          form: bool = False) -> dict:
    """Rail request profile. `api_method` is X-Api-Method-Name, which the front
    sends on EVERY call with a per-endpoint value (orderApiCreateOrder,
    searchApiSearchTrains, …). It is not decoration — this repo's bug history is
    largely dropped headers answering 406 — so it is required per template rather
    than defaulted, and a new rail endpoint cannot be added without naming it.

    Travelsessionid is the third header the front always sends; its value is the
    `_T_travel_session_id` cookie, so it is injected at call time from the minted
    cookie (see MobileSession._call_read) instead of being frozen here."""
    headers = {"X-App-Name": RAIL_APP_NAME, "X-App-Version": RAIL_APP_VERSION,
               "X-Api-Method-Name": api_method,
               "Origin": RAIL_HOST, "Referer": RAIL_HOST + "/"}
    profile = {}
    if form:
        # An empty-body POST the app sends as a FORM, not JSON: blank-status carries
        # Content-Length 0 with application/x-www-form-urlencoded. Posting {} as
        # JSON sends the two bytes "{}" with application/json — a different request.
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        profile["form"] = True
    elif post:
        headers["Content-Type"] = "application/json"
    if accept:
        # Some routes pick their response serializer from Accept, and the
        # session-wide default is application/json — which is not what a PDF
        # route should be asked for.
        headers["Accept"] = accept
    return {"no_base_params": True, "no_bearer": True, "headers": headers, **profile}


BUILTIN_ENDPOINTS.update({
    # {"directions":[{"origin","destination","departureDate"}],
    #  "adultsCount","childrenCount"} — origin/destination are NUMERIC station
    # codes (2000000 = Moscow), and nothing in the captures resolves a name to
    # one, so the tools take codes and say where to get them.
    "train_search": {"method": "POST", "host": RAIL_HOST,
                     "path": "/api/search/trains", "params": {},
                     **_rail("searchApiSearchTrains", post=True)},
    # ?origin=&destination= — which dates are on sale at all.
    "train_calendar": {"method": "GET", "host": RAIL_HOST,
                       "path": "/api/search/sale-calendar", "params": {},
                       **_rail("searchApiSaleCalendar")},
    # {"origin","destination","trainNumber","departureDate","trainSearchId"} —
    # cars, their places and prices. trainSearchId comes from the ROOT of the
    # train_search response, not from a segment.
    "train_cars": {"method": "POST", "host": RAIL_HOST,
                   "path": "/api/search/train/cars", "params": {},
                   **_rail("searchApiSearchTrainCars", post=True)},

    # --- orders -------------------------------------------------------------
    # create takes ways[][] with the segment's own id AND carSearchId — which
    # comes from the train_cars response, not from the search. Confusing the two
    # is the single easiest way to get a rejected booking.
    "train_order_create": {"method": "POST", "host": RAIL_HOST,
                           "path": "/api/orders/create", "params": {},
                           **_rail("orderApiCreateOrder", post=True)},
    # {"orderId"} -> {"paymentUrl"} — a tpay webview URL; the money leg lives in
    # MobileSession.tpay_pay.
    "train_order_pay": {"method": "POST", "host": RAIL_HOST,
                        "path": "/api/orders/pay", "params": {},
                        **_rail("orderApiPayOrder", post=True)},
    # path is parameterized: /api/orders/{orderId}
    "train_order": {"method": "GET", "host": RAIL_HOST,
                    "path": "/api/orders/_", "params": {},
                    **_rail("orderApiGetOrder")},
    # /api/orders/{orderId}/status -> {"status","paymentDueInSeconds"}
    "train_order_status": {"method": "GET", "host": RAIL_HOST,
                           "path": "/api/orders/_/status", "params": {},
                           **_rail("orderApiOrderStatus")},
    # /api/orders/{orderId}/blank-status -> per-ticket erStatus + isRefundPossible.
    # A POST with an EMPTY body — same shape as grocery_order_cancel.
    "train_blank_status": {"method": "POST", "host": RAIL_HOST,
                           "path": "/api/orders/_/blank-status", "params": {},
                           **_rail("orderApiBlankStatus", form=True)},
    # ?orderId= -> application/pdf. raw: the bytes are the answer.
    "train_blank": {"method": "GET", "host": RAIL_HOST,
                    "path": "/api/orders/documents/blank", "params": {},
                    "raw": True, **_rail("orderApiGetBlank", accept="*/*")},

    # --- refund -------------------------------------------------------------
    # The two refund calls DISAGREE on the case of the ticket-id key: calculate
    # wants "TicketIds", refund wants "ticketIds". Verified in the capture; both
    # spellings are pinned by tests/test_travel_booking_bodies.py.
    "train_refund_calc": {"method": "POST", "host": RAIL_HOST,
                          "path": "/api/orders/refund/calculate", "params": {},
                          **_rail("orderApiCalculateRefund", post=True)},
    "train_refund": {"method": "POST", "host": RAIL_HOST,
                     "path": "/api/orders/refund", "params": {},
                     **_rail("orderApiRefund", post=True)},
    # /api/orders/refund/{operationId} -> NotStarted → Succeed, and the ids it
    # returns in refundedTicketsIds are NEW ones, not the ids that were sent.
    "train_refund_status": {"method": "GET", "host": RAIL_HOST,
                            "path": "/api/orders/refund/_", "params": {},
                            **_rail("orderApiRefundStatus")},
    "train_contact_info": {"method": "GET", "host": RAIL_HOST,
                           "path": "/api/info/contactInfo", "params": {},
                           **_rail("infoContactInfo")},
})
