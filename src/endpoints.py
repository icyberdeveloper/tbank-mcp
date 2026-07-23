"""Builtin T-Bank API endpoint shapes (static params only — no device/session/account secrets).
Generated from the API surface; the live sessionid/deviceId/access_token/cookies + per-call
args (account, start/end, ...) are added at runtime by MobileSession. NO user secrets here."""

BUILTIN_ENDPOINTS = {
 "accounts_light": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/accounts_light",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "ccc": "true",
   "appVersion": "7.31.6",
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
   "isSuspicious": "true",
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "cpswc": "true",
   "inache": "drivetransitt",
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "platform": "ios"
  }
 },
 "list_regular_payments": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/list_regular_payments_v2",
  "params": {
   "appVersion": "7.31.6",
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "connectionType": "WiFi",
   "ccc": "true",
   "cpswc": "true",
   "platform": "ios",
   "inache": "drivetransitt"
  }
 },
 "active_loans": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/active_loans",
  "params": {
   "appVersion": "7.31.6",
   "inache": "drivetransitt",
   "connectionType": "WiFi",
   "appName": "mobile",
   "platform": "ios",
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true"
  }
 },
 "credit_accounts_list": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/closing_accounts/credit_accounts_list",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "appName": "mobile",
   "connectionType": "WiFi",
   "appVersion": "7.31.6",
   "cpswc": "true",
   "ccc": "true",
   "platform": "ios"
  }
 },
 "payments_credit_accounts": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/payments_credit_accounts",
  "params": {
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6"
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
   "appVersion": "7.31.6"
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
   "appVersion": "7.31.6"
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "ccc": "true",
   "platform": "ios",
   "connectionType": "WiFi",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "inache": "drivetransitt"
  }
 },
 "shopping_favorites": {
  "method": "GET",
  "host": "https://shopping.t-bank-app.ru",
  "path": "/api/v1/favorites",
  "params": {
   "appVersion": "7.31.6",
   "appName": "mobile",
   "platform": "ios"
  }
 },
 "shopping_cart": {
  "method": "POST",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v1/carts/get-user-carts",
  "params": {
   "appName": "mobile",
   "appVersion": "7.31.6",
   "platform": "webview_ios"
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "platform": "ios",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "cpswc": "true",
   "ccc": "true"
  }
 },
 "get_invoices": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/get_invoices",
  "params": {
   "appName": "mobile",
   "appVersion": "7.31.6",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "platform": "ios",
   "connectionType": "WiFi",
   "cpswc": "true",
   "ccc": "true"
  }
 },
 "my_invoices": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/cm/my_invoices",
  "params": {
   "cpswc": "true",
   "appVersion": "7.31.6",
   "connectionType": "WiFi",
   "appName": "mobile",
   "platform": "ios",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "ccc": "true"
  }
 },
 "available_cards": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/available_cards",
  "params": {
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "ccc": "true",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "credit_recommendations": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/credit/recommendations",
  "params": {
   "connectionType": "WiFi",
   "cpswc": "true",
   "inache": "drivetransitt",
   "platform": "ios",
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile",
   "inache": "drivetransitt"
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appName": "mobile",
   "inache": "drivetransitt",
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "p2p_countries": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/p2panybank/countries",
  "params": {
   "appName": "mobile",
   "appVersion": "7.31.6",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "platform": "ios",
   "connectionType": "WiFi",
   "cpswc": "true",
   "ccc": "true"
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6"
  }
 },
 "invest_offers": {
  "method": "GET",
  "host": "https://api-invest-gw.t-bank-app.ru",
  "path": "/offer/api/v1/instance/virtual-stock",
  "params": {
   "connectionType": "WiFi",
   "ccc": "true",
   "platform": "ios",
   "appName": "mobile",
   "inache": "drivetransitt",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appVersion": "7.31.6"
  }
 },
 "bundles_all": {
  "method": "GET",
  "host": "https://api-common-gw.t-bank-app.ru",
  "path": "/bundles/api/v1/allBundles",
  "params": {
   "ccc": "true",
   "appName": "mobile",
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "appName": "mobile",
   "platform": "ios",
   "ccc": "true",
   "cpswc": "true",
   "inache": "drivetransitt",
   "connectionType": "WiFi"
  }
 },
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
   "appVersion": "7.31.6"
  }
 },
 "atm_withdrawal_qrs": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/atm_withdrawal_qrs",
  "params": {
   "appVersion": "7.31.6",
   "connectionType": "WiFi",
   "ccc": "true",
   "platform": "ios",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "cpswc": "true"
  }
 },
 "check_rating": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/check_rating",
  "params": {
   "appVersion": "7.31.6",
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "cpswc": "true",
   "appName": "mobile",
   "inache": "drivetransitt",
   "ccc": "true",
   "connectionType": "WiFi"
  }
 },
 "credit_collection_info": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/credit/collection_info",
  "params": {
   "ccc": "true",
   "cpswc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": "7.31.6",
   "inache": "drivetransitt"
  }
 },
 "active_account_options": {
  "method": "GET",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/active_account_options",
  "params": {
   "ccc": "true",
   "cpswc": "true",
   "connectionType": "WiFi",
   "platform": "ios",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": "7.31.6",
   "inache": "drivetransitt"
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
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
   "appVersion": "7.31.6",
   "connectionType": "WiFi"
  }
 },
 "grocery_cart_check": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/cart/check",
  "params": {
   "connectionType": "WiFi",
   "inache": "drivetransitt",
   "cpswc": "true",
   "appVersion": "7.31.6",
   "platform": "ios",
   "ccc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "appName": "mobile"
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
   "appVersion": "7.31.6"
  }
 },
 "grocery_order_create": {
  "method": "POST",
  "host": "https://www.tbank.ru",
  "path": "/api/supreme/lifestyle/api/grocery/order/create",
  "params": {
   "appName": "grocery_evo",
   "appVersion": "7.31.6",
   "platform": "webview_ios"
  }
 },
 "grocery_deliveries": {
  "method": "POST",
  "host": "https://www.tbank.ru",
  "path": "/api/supreme/lifestyle/api/grocery/deliveries",
  "params": {
   "appName": "grocery_evo",
   "appVersion": "7.31.6",
   "platform": "webview_ios"
  }
 },
 "grocery_address_set": {
  "method": "POST",
  "host": "https://www.tbank.ru",
  "path": "/api/supreme/lifestyle/api/grocery/address/set",
  "params": {
   "appName": "grocery_evo",
   "appVersion": "7.31.6",
   "platform": "webview_ios"
  }
 },
 "grocery_retailers": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/retailers",
  "params": {
   "appVersion": "7.31.6",
   "inache": "drivetransitt",
   "connectionType": "WiFi",
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "ccc": "true",
   "cpswc": "true",
   "appName": "mobile"
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
   "appVersion": "7.31.6",
   "ccc": "true",
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "grocery_categories": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/categories",
  "params": {
   "appName": "mobile",
   "connectionType": "WiFi",
   "ccc": "true",
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform",
   "appVersion": "7.31.6",
   "platform": "ios",
   "inache": "drivetransitt"
  }
 },
 "grocery_popular": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/popular",
  "params": {
   "ccc": "true",
   "platform": "ios",
   "appName": "mobile",
   "appVersion": "7.31.6",
   "connectionType": "WiFi",
   "origin": "mobile,ib5,loyalty,platform",
   "inache": "drivetransitt",
   "cpswc": "true"
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
   "appVersion": "7.31.6"
  }
 },
 "grocery_unseen_orders": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/orders/unseen/count",
  "params": {
   "platform": "ios",
   "origin": "mobile,ib5,loyalty,platform",
   "ccc": "true",
   "inache": "drivetransitt",
   "appVersion": "7.31.6",
   "cpswc": "true",
   "connectionType": "WiFi",
   "appName": "mobile"
  }
 },
 "payment_gate_pay": {
  "method": "POST",
  "host": "https://www.tbank.ru",
  "path": "/api/common/pg-api/v1/payment-gate/payments",
  "params": {
   "origin": "web,ib5,platform"
  }
 },
 "payment_commission": {
  "method": "POST",
  "host": "https://api.t-bank-app.ru",
  "path": "/v1/payment_commission",
  "params": {
   "platform": "ios",
   "inache": "drivetransitt",
   "appName": "mobile",
   "origin": "mobile,ib5,loyalty,platform",
   "connectionType": "WiFi",
   "ccc": "true",
   "cpswc": "true",
   "appVersion": "7.31.6"
  }
 },
 "shopping_change_qty": {
  "method": "POST",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v1/carts/change-items-quantity",
  "params": {
   "appVersion": "7.31.6",
   "appName": "mobile",
   "platform": "webview_ios"
  }
 },
 "shopping_cart_detail": {
  "method": "POST",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v1/carts/cart-detail-info",
  "params": {
   "appName": "mobile",
   "appVersion": "7.31.6",
   "platform": "webview_ios"
  }
 },
 "store_products": {
  "method": "GET",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v1/store-products",
  "params": {
   "appVersion": "7.31.6",
   "appName": "mobile",
   "platform": "webview_ios"
  }
 },
 "store_product": {
  "method": "GET",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v1/product",
  "params": {
   "appVersion": "7.31.6",
   "appName": "mobile",
   "platform": "webview_ios"
  }
 },
 "store_categories": {
  "method": "GET",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v4/store-categories",
  "params": {
   "appVersion": "7.31.6",
   "appName": "mobile",
   "platform": "webview_ios"
  }
 },
 "sphere_categories": {
  "method": "GET",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v5/sphere/categories",
  "params": {
   "appName": "mobile",
   "platform": "webview_ios",
   "appVersion": "7.31.6"
  }
 },
 "grocery_goods": {
  "method": "GET",
  "host": "https://lifestyle.t-bank-app.ru",
  "path": "/api/grocery/goods",
  "params": {
   "appVersion": "7.31.6",
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
 "payment_methods": {
  "method": "POST",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v6/payment-methods",
  "params": {
   "appVersion": "7.31.6",
   "appName": "mobile",
   "platform": "webview_ios"
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
   "appVersion": "7.31.6",
   "cpswc": "true",
   "origin": "mobile,ib5,loyalty,platform"
  }
 },
 "checkout_process_order": {
  "method": "POST",
  "host": "https://webview.t-bank-app.ru",
  "path": "/mybank/api/shopping/mobile/v1/checkout/process-order",
  "params": {
   "appName": "mobile",
   "appVersion": "7.31.6",
   "platform": "webview_ios"
  }
 },
 "messenger_base": {
  "method": "GET",
  "host": "https://tm.t-bank-app.ru",
  "path": "/app/bank/messenger/conversations/unread",
  "params": {}
 },
 "messenger_send": {
  "method": "POST",
  "host": "https://tm.t-bank-app.ru",
  "path": "/app/bank/messenger/conversations/AAKjxa24heaokKQBpfrl3vff_MGsAQIIYmFuawI/messages",
  "params": {},
  "headers": {
   "Content-Type": "application/vnd.chats.chatapi.text.message.in.v1+json",
   "Accept": "application/vnd.chats.chatapi.text.message.out.v1+json",
   "Tmsg-User-Agent": "com.idamob.tinkoff.android:7.31.6; tmsg-sdk-iOS:1.0.0; iOS:17.5.1"
  }
 }
}

# 14 additional valuable endpoints found by the completeness audit.
BUILTIN_ENDPOINTS.update({
    "detected_merchant_subscriptions": {"method": "GET", "host": "https://api.t-bank-app.ru", "path": "/subscriptions/merchant/v2/subscriptions", "params": {}},
    "user_profile": {"method": "GET", "host": "https://id.t-bank-app.ru", "path": "/userinfo/userinfo", "params": {}},
    "broker_portfolio_accounts": {"method": "POST", "host": "https://api-invest-gw.t-bank-app.ru", "path": "/invest-portfolio/portfolios/accounts/for-mb", "params": {"withClosingIis": "false", "currency": "RUB"}},
    "my_homes": {"method": "GET", "host": "https://my-home.tinkoff.ru", "path": "/api/v1/gw/homes", "params": {}},
    "my_home_activities": {"method": "GET", "host": "https://my-home.tinkoff.ru", "path": "/api/v1/gw/activities", "params": {}},
    "my_cars": {"method": "GET", "host": "https://myauto.t-bank-app.ru", "path": "/api/my-auto/v2/cars/list-light", "params": {}},
    "payment_shortcuts": {"method": "GET", "host": "https://shortcuts.t-bank-app.ru", "path": "/v2/shortcuts", "params": {}},
    "unread_support_requests": {"method": "GET", "host": "https://csc.tbank.ru", "path": "/app/bank/api/v1/tracker/userRequests/unread", "params": {}},
    "resolve_payment_qr": {"method": "POST", "host": "https://api.t-bank-app.ru", "path": "/providers/providers/qr/resolve", "params": {}},
    "merchant_brand": {"method": "GET", "host": "https://api.t-bank-app.ru", "path": "/v1/brand_by_merchant", "params": {}},
    "money_request_public_page": {"method": "GET", "host": "https://api.t-bank-app.ru", "path": "/v1/cm/public_page/money_request", "params": {}},
    "finhealth_account_presets": {"method": "GET", "host": "https://api.t-bank-app.ru", "path": "/finhealth/v2/settings/accounts/presets/default", "params": {}},
    "get_ip": {"method": "GET", "host": "https://api.tbank.ru", "path": "/v1/get_ip", "params": {}},
    "push_unread_count": {"method": "GET", "host": "https://push-history-api.t-bank-app.ru", "path": "/bank/v3/notifications/unseen/count", "params": {}},
})
