# T-Bank MCP — каталог тулов

**25 тулов.** Логин: login(phone) → confirm_otp/confirm_password. Дальше headless.

## `confirm_otp`
Отправить SMS-код.

## `confirm_password`
Отправить пароль аккаунта (первый логин на новом устройстве).

## `confirm_pin`
Отправить PIN (re-auth).

## `flows`
Гид по флоу (заказ продуктов, переводы, логин, мессенджер, инвест).

## `get_data`
Универсальный getter. section = subscriptions | credit_schedule | statements |
requisites | invoices | templates | contacts | providers | cards | loans | autopayments |
sbp | offers | gifts | services | bundles | manager | merchant_subs | profile | homes |
cars | shortcuts | finhealth_total | finhealth_turnover | invest_accounts |
invest_portfolio | invest_operations | invest_securities | pension | broker_margin | shared.

## `grocery_add_to_cart`
Добавить товары в корзину. items = JSON [{id, count}, ...].

## `grocery_cart`
Содержимое корзины.

## `grocery_checkout`
Полный чекаут: корзина → доставка → заказ → оплата. РЕАЛЬНЫЕ ДЕНЬГИ.

## `grocery_plan_order`
Спланировать заказ: для каждого ингредиента ищет (custom_ordered → global).
ingredients = JSON массив, напр. ["свёкла","говядина","капуста"].

## `grocery_search`
Поиск товара по названию. Возвращает товары с тегом likely_raw (сырой/готовый).

## `grocery_stores`
Список магазинов (название, appId, доставка, кешбэк).

## `keepalive`
Пинг — продлить сессию.

## `list_accounts`
Счета + карты + балансы.

## `list_operations`
Операции за период.

## `login`
Начать логин. Отправляет SMS OTP. Возвращает какой шаг следующий.

## `messenger_conversations`
Список чатов.

## `messenger_messages`
История чата.

## `messenger_send`
Отправить сообщение.

## `messenger_unread`
Непрочитанные.

## `operations_histogram`
Траты по периодам/категориям/мерчантам.

## `payment_commission`
Предпросмотр комиссии (без денег).

## `refresh_session`
Обновить сессию без OTP: сначала refresh_token, при `invalid_grant` — silent
re-login через SSO_SESSION. Если оба пути не работают — возвращает
`REAUTH_REQUIRED` (нужен полный логин login+OTP+password). Вызови при SESSION EXPIRED.

## `session_status`
Проверить жива ли сессия.

## `spending_categories`
Траты по категориям.

## `transfer`
Перевод (P2P/СБП). РЕАЛЬНЫЕ ДЕНЬГИ — подтверди с пользователем.
