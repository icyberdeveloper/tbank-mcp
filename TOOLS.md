# T-Bank MCP — каталог тулов

**56 тулов.** Логин: login(phone) → confirm_otp/confirm_password. Дальше headless.

## `account_requisites`
Реквизиты счёта для перевода извне: получатель, счёт, БИК, корсчёт, ИНН/КПП.
account_id — из list_accounts(). currencies — через запятую (RUB,USD,EUR).

## `bank_documents`
Справки, заказанные в банке (о движении средств, о доходах и т.п.).

## `card_limits`
Лимиты по карте (на покупки, на снятие) и сколько уже израсходовано.
ucid — из list_cards().

## `card_operations`
Операции по КОНКРЕТНОЙ карте. card_id — поле id из list_cards().
Серверного фильтра по карте нет (API умеет только excludeCardIds), поэтому
берутся операции за период и фильтруются по полю card.

## `card_requisites`
Реквизиты карты: держатель, срок, номер. ucid — из list_cards().
По умолчанию номер маскируется, а CVV не выводится вообще — это полные
платёжные данные. reveal=True показывает номер и CVV целиком.

## `cinema_book`
ЗАБРОНИРОВАТЬ места. Создаёт заказ, но НЕ платит — деньги списывает
отдельный ticket_pay(). Неоплаченная бронь отваливается сама.

seats — через запятую: для кино "7:10,7:11" (ряд:место из cinema_seats),
для концертов — составные seatId из cinema_seats(kind="concert") как есть.

Покажи пользователю итоговую сумму со сбором ДО вызова ticket_pay.

## `cinema_schedule`
Сеансы фильма на дату. event_id — из cinema_search(), date — YYYY-MM-DD.
cinema — подстрока названия кинотеатра ("каро 11"), around — время "17:00",
window_min — допуск в минутах вокруг него.

## `cinema_search`
Найти фильм в прокате и его eventId (нужен для cinema_schedule).
query — часть названия; пусто = вся сегодняшняя афиша города.

## `cinema_seats`
Свободные места на сеансе, по рядам. Денег не двигает.
slot_id и object_id — из cinema_schedule()/concert_schedule().
row — показать только один ряд, max_price — потолок цены за место.
kind — "movie" или "concert".

## `concert_hall`
Секторы концерта со свободной рассадкой (входные билеты, фан-зоны).
Только чтение: примера создания заказа для такого экрана в захвате нет,
поэтому бронировать отсюда MCP не умеет — только смотреть наличие.

## `concert_schedule`
Показы концерта: площадка, дата, slotId и objectId для cinema_seats().
event_id — из search_app(query, screen="afisha").

## `confirm_otp`
Отправить SMS-код.

## `confirm_password`
Отправить пароль аккаунта (первый логин на новом устройстве).

## `confirm_pin`
Отправить PIN (re-auth).

## `diagnostics`
Недавние redacted-события (checkout delivery/order/payment + refresh сессии)
для диагностики — БЕЗ секретов. reconstruct попытку / найти последний
подтверждённый шаг. Источник: ~/.local/share/tbank-mcp/events.jsonl.

## `documents`
Документы клиента: паспорт, загранпаспорт, ВУ, СНИЛС, ИНН, ОСАГО/КАСКО, ПТС/СТС.
kind — фильтр по названию или коду (напр. "паспорт", "RusDriversLic"); пусто = все.
В хранилище лежат и документы РОДСТВЕННИКОВ, которые клиент когда-то вводил —
они отсеиваются по дате рождения; include_others=True покажет и их.

## `flows`
Гид по флоу (заказ продуктов, переводы, логин, мессенджер, инвест).

## `get_data`
Универсальный getter. section = subscriptions | credit_schedule | statements |
requisites | invoices | templates | contacts | providers | cards | loans | autopayments |
sbp | offers | gifts | services | bundles | manager | merchant_subs | profile | homes |
cars | shortcuts | finhealth_total | finhealth_turnover | invest_accounts |
pension | broker_margin | shared. (invest_portfolio/operations/securities — отдельные тулы.)

## `grocery_add_to_cart`
Добавить товары в корзину. items = JSON [{id, count}, ...].
app_id/point_id — из grocery_stores() (обязательны). Запомни их — тот же
магазин нужен для grocery_cart и grocery_checkout.

## `grocery_attempts`
Недавние попытки grocery checkout (read-only) — для reconciliation после
неопределённого результата (UNKNOWN). Показывает status/order_id/attempt_id/sum.

## `grocery_cart`
Содержимое корзины. app_id/point_id — из grocery_stores() (обязательны) и
должны совпадать с теми, что использовались в grocery_add_to_cart.

## `grocery_checkout`
Полный чекаут: доставка → заказ → оплата. РЕАЛЬНЫЕ ДЕНЬГИ.
app_id/point_id — из grocery_stores() (обязательны, тот же магазин что в корзине).
Счёт оплаты выбирается автоматически (первый Current RUB с балансом).
При неопределённом результате (заказ мог создаться) повтор БЛОКИРУЕТСЯ —
сначала grocery_attempts() и проверь заказ в приложении. force=True — только если
пользователь ЯВНО подтвердил, что прошлого заказа нет. Всегда показывай состав и
сумму и жди явного подтверждения перед вызовом.

Реализация: тул асинхронный и запускает браузер Playwright в отдельном worker-потоке
(asyncio.to_thread) — sync_playwright падает, если звать его внутри event-loop, а
FastMCP крутит sync-тулы именно в loop. Если тул падает с Playwright-ошибкой —
проверь `python -m playwright install chromium` (в окружении MCP).

## `grocery_good_info`
Карточка товара: состав, КБЖУ, вес, срок хранения, производитель.
good_id — из grocery_search()/grocery_plan_order(). КБЖУ приводится на 100 г
и на упаковку (у части сетей КБЖУ есть только текстом — он разбирается).

## `grocery_order_status`
Reconciliation: статус grocery-заказа по orderId (GET /api/grocery/order).
Read-only. Проверь после UNKNOWN checkout, создался/оплатился ли заказ на бэкенде.

## `grocery_plan_order`
Спланировать заказ: для каждого ингредиента ищет (custom_ordered → global).
ingredients = JSON массив, напр. ["свёкла","говядина","капуста"].
app_id/point_id — из grocery_stores() (обязательны).

## `grocery_rank`
Кандидаты по запросу с атрибутами, опционально отсортированные.

Это ИНСТРУМЕНТ, а не политика: сам по себе никакой стратегии выбора не
применяет. Стратегию задаёт вызывающий, и только когда пользователь её
попросил — иначе sort_by пустой и порядок остаётся магазинным.

sort_by: price | weight | kcal | kcal_pack | protein | fat | carb (пусто = без
сортировки). order: asc | desc. Питательные поля тянутся автоматически, если
по ним сортируем (это +1 запрос на кандидата), либо по with_nutrition=True.
Товары, у которых сеть не публикует нужное поле, всегда уходят в конец — и при
asc, и при desc: «нет данных» не равно нулю.

## `grocery_search`
Поиск товара по названию. app_id/point_id — из grocery_stores() (обязательны).
Возвращает товары с тегом likely_raw (сырой/готовый).

## `grocery_stores`
Список магазинов (название, appId, доставка, кешбэк).

## `insurance_policies`
Действующие страховые полисы (ОСАГО/КАСКО/путешествия) с суммами и сроками.

## `invest_accounts`
Инвест-счета (InvestBox/брокерские). Возьми brokerAccountId для следующих тулов.

## `invest_operations`
Брокерские операции. operation_type — фильтр (пусто = все).

## `invest_portfolio`
Статистика портфеля (P&L) за период. broker_account_id — из invest_accounts().

## `invest_securities`
Купленные бумаги (акции/облигации/ETF) по брокерскому счёту.

## `keepalive`
Пинг — продлить сессию.

## `list_accounts`
Счета + карты + балансы.

## `list_cards`
Все карты по всем счетам: id, ucid, баланс, тип.
id — для card_operations, ucid — для card_limits/card_requisites.

## `list_operations`
Операции за период.

## `login`
Начать логин. Отправляет SMS OTP. Возвращает какой шаг следующий (otp/password/pin).

## `messenger_conversations`
Список чатов.

## `messenger_messages`
История чата.

## `messenger_send`
Отправить сообщение.

## `messenger_unread`
Чаты с непрочитанными сообщениями (по названиям, а не по сырым id).

## `operations_histogram`
Траты по периодам/категориям/мерчантам.

## `order_details`
Детали одного заказа (места, зал, код брони, состав корзины).
Работает для развлекательных заказов (кино/концерты); для продуктов —
grocery_order_status, для поездок деталей в этом API нет, только orders().

## `orders`
Все заказы клиента: продукты, кино, концерты, авиабилеты, ж/д, отели.
kind — "афиша" | "кино" | "путешествия" | "продукты" | код objectType; пусто = все.
Отсортировано по дате создания, новые сверху.

## `payment_commission`
Предпросмотр комиссии (без денег).

## `payment_receipt`
Скачать PDF-чек по операции. payment_id — поле paymentId из orders()
или из истории операций. save_to — путь файла (по умолчанию /tmp).

## `refresh_session`
Обновить сессию. Сначала пробует refresh_token, при invalid_grant —
silent re-login через SSO_SESSION (без OTP). Если оба пути не работают — REAUTH_REQUIRED.

## `search_app`
Полнотекстовый поиск по разделу приложения.

screen — СТРОГИЙ enum, угадывать бесполезно (всё остальное → 400):
  afisha     — кино, концерты, театр, выставки, спектакли (по умолчанию);
               отдаёт eventId, готовый для cinema_schedule/concert_schedule
  movie_main — только фильмы
  services   — самый широкий: та же афиша плюс контакты из телефонной книги
               и сервисные блоки; id приходится доставать из диплинка
  grocery    — каталог магазина, но для него есть grocery_search/grocery_rank
               (там нужны app_id/point_id и фильтр «в наличии»)

## `session_status`
Проверить жива ли сессия.

## `spending_categories`
Траты по категориям.

## `ticket_cancel`
Отменить заказ билета. kind — "movie" или "concert".

⚠️ Надёжность не подтверждена: в захвате оба пути отмены отвечали 500. Если
тул вернёт ошибку, считай статус НЕИЗВЕСТНЫМ (не «всё ещё забронировано») —
проверь orders() и при необходимости отменяй через приложение.

## `ticket_pay`
ОПЛАТИТЬ бронь билета. РЕАЛЬНЫЕ ДЕНЬГИ — вызывай ТОЛЬКО после того, как
пользователь подтвердил конкретную сумму и заказ. Сам по себе запрос
пользователя «купи билет» подтверждением НЕ является.

Все три первых аргумента бери из ответа cinema_book(): order_id, итоговую
сумму и nfs_payment_token. Токен живёт только в ответе на создание заказа —
order_details() его не отдаёт, поэтому переспросить потом будет негде.
account_id — счёт списания (по умолчанию первый рублёвый Current).

## `transfer`
Перевод (РЕАЛЬНЫЕ ДЕНЬГИ — подтверди с пользователем). Контракт сверен с захватом.
phone/СБП (по умолчанию): to_account=телефон. Если bank_member_id/masked_fio/
pointer_link_id не переданы — получатель резолвится АВТОМАТИЧЕСКИ (transfer_sbp_resolve):
выберется дефолтный банк; при нескольких банках без дефолта вернётся RECIPIENT_MULTIPLE_BANKS
со списком (тогда передай выбранные поля явно). Между своими счетами: provider='transfer-inner',
to_account=счёт-получатель. По реквизитам юр.лица — низкоуровневый pay() с явными providerFields.

## `transfer_sbp_resolve`
Резолвинг получателя СБП по номеру (read-only, БЕЗ денег). Возвращает банки
получателя (маскированное имя + банк + isDefaultBank) и готовый provider_fields.
Используй ПЕРЕД transfer()/payment_commission() для НОВОГО (несохранённого)
получателя. provider_fields вставь в payParameters.providerFields комиссии — не
пиши 8276 руками. Для transfer() передай bank_member_id+pointer_link_id (или
ничего — выберется дефолт); при нескольких банках без дефолта нужен явный выбор.

## `travel_order_details`
Детали поездки по orderId из orders("путешествия").

Полная карточка есть только для ОТЕЛЕЙ: даты, отель, номер, питание, гости.
Для авиа и ж/д API отдаёт только сводку из orders() — почему, тул объяснит.
