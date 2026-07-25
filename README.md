# T-Bank MCP

**T-Bank (Т-Банк) MCP** — mobile banking API server for Claude Code, Codex, ChatGPT, and other MCP-capable agents.

## Features

- **57 tools**: accounts, cards, documents, operations, grocery ordering, cinema and
  concert tickets, orders, transfers, messenger, investments
- **7 skills**: grocery order, tickets, bill pay, transfer, budget analysis,
  invest advisor, login
- **Pinned CA trust**: system store + the Russian Trusted Root CA (Минцифры), which
  no OS ships and 13 of the 18 bank hosts need. Shipped in `ca/roots/`, pinned by
  SHA-256. Leaf/intermediate rotation needs no action; a root rotation is a PEM drop
  into `ca/roots/` (or `TBANK_EXTRA_CA`). Certificates are never learned from the
  network — see the header of `src/tls.py`.
- **Grocery checkout**: search → cart → order → pay (proven end-to-end)
- **Secure login**: password/PIN stay OUT of the LLM context (local CLI or env var)

## Quick Install

```bash
git clone https://github.com/icyberdeveloper/tbank-mcp.git
cd tbank-mcp
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m playwright install chromium

# MCP server:
claude mcp add tbank -- ./.venv/bin/python -m src.server

# Skills:
cp -r skills/* ~/.claude/skills/
```

## 🔒 Login — БЕЗОПАСНО (пароль не попадает к агенту)

Пароль и PIN — это секреты. Они **НЕ передаются в контекст модели (LLM)**.
Логин выполняется локальным скриптом ИЛИ через env-переменную.

### Способ 1 (рекомендуемый): локальный CLI

```bash
cd tbank-mcp

# Пароль спросит скрипт (getpass — не отображается в терминале):
.venv/bin/python login_cli.py +7XXXXXXXXXX
# [1/3] login(+7XXXXXXXXXX) ... SMS отправлена
# [2/3] SMS-код: ****        ← вводишь код из SMS (скрытый ввод)
# [3/3] Пароль (не отображается): ****   ← вводишь пароль (скрытый ввод)
# ✓ ГОТОВО! session.json сохранён (права 0600).
#   Запусти Claude Code в этом репозитории.
#   Пароль НЕ передан агенту — он работает с сохранённой сессией.
```

Или с паролем в env (CI/скрипты):
```bash
TBANK_PASSWORD="пароль" .venv/bin/python login_cli.py +7XXXXXXXXXX
```

После логина — **запусти Claude Code**. Агент видит сохранённую сессию и работает
без пароля. Пароль никогда не попадает в контекст LLM.

### Способ 2: через агента (удобно, но пароль виден LLM)

Если тебе удобно передавать пароль агенту:

```
> login(+7XXXXXXXXXX)
> [SMS code] 1234
> confirm_otp("1234")
> [bank asks password]
> confirm_password("YourPassword")
```

⚠️ **Внимание:** пароль попадает в контекст модели и журналы вызовов.
Для чувствительных аккаунтов используй Способ 1.

### Способ 3: env-переменная (CI/автоматизация)

```bash
export TBANK_PASSWORD="пароль"
export TBANK_PHONE="+7XXXXXXXXXX"
# login() автоматически подхватит пароль из env после confirm_otp()
```

## Other agents (Codex, ChatGPT, Hermes, OpenClaw)

```jsonc
{
  "mcpServers": {
    "tbank": {
      "command": "/path/to/tbank-mcp/.venv/bin/python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/tbank-mcp"
    }
  }
}
```

## Tools

Each tool's docstring is the reference — this table is only a map of the surface.

| Group | Tools |
|---|---|
| **Login** | `login`, `confirm_otp`, `confirm_password`, `confirm_pin` |
| **Session** | `refresh_session`, `session_status`, `keepalive` |
| **Reads** | `list_accounts`, `list_operations`, `spending_categories`, `operations_histogram`, `get_data` |
| **Cards & accounts** | `list_cards`, `card_limits`, `card_requisites`, `card_operations`, `account_requisites` |
| **Documents** | `documents`, `bank_documents`, `insurance_policies`, `payment_receipt` |
| **Grocery** | `grocery_stores`, `grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_set_cart`, `grocery_cart`, `grocery_checkout`, `grocery_attempts`, `grocery_order_status` |
| **Nutrition** | `grocery_good_info`, `grocery_rank` |
| **Orders** | `orders`, `order_details`, `travel_order_details` |
| **Tickets** | `cinema_search`, `cinema_schedule`, `cinema_seats`, `concert_schedule`, `concert_hall`, `cinema_book`, `ticket_pay`, `ticket_cancel` |
| **Search** | `search_app` |
| **Messenger** | `messenger_conversations`, `messenger_messages`, `messenger_send`, `messenger_unread` |
| **Money** | `transfer_sbp_resolve`, `transfer`, `payment_commission` |
| **Invest** | `invest_accounts`, `invest_portfolio`, `invest_operations`, `invest_securities` |
| **Utility** | `flows`, `diagnostics` |

`get_data(section)` covers 60+ endpoints: subscriptions, credit_schedule, statements, loans, invest_accounts, pension, etc. (`invest_portfolio` is a tool of its own, not a section — see the docstring for the full list.)

Grocery tools (`grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_set_cart`, `grocery_cart`, `grocery_checkout`) require `app_id` + `point_id` taken from `grocery_stores()` — there's no silent default store, so add/cart/checkout always operate on the same cart (no more "Корзина пуста" after adding).

## Skills

| Skill | What it does |
|---|---|
| `tbank-grocery-order` | Recipe → search → cart → confirm → checkout |
| `tbank-tickets` | Cinema/concert: search → showtime → seats → book → pay |
| `tbank-bill-pay` | Topping up a phone (a P2P transfer). Service bills — ЖКХ, taxes, fines — are **not** implemented; the skill says so and points at the app |
| `tbank-transfer-money` | P2P, СБП, account transfers |
| `tbank-budget-analyzer` | Spending analysis, subscription audit, savings tips |
| `tbank-invest-advisor` | Portfolio, P&L, rebalancing, tax optimization |
| `tbank-login` | Multi-step login, session management |

## Tests

No pytest — the tests are standalone scripts. Run them all:

```bash
.venv/bin/python tests/run_all.py            # 11 files, ~35 s, offline
.venv/bin/python tests/run_all.py transfer   # only files matching "transfer"
```

Each runs in its own process, and the runner redirects the attempt/event journals to
a temp directory so a test run never writes to `~/.local/share/tbank-mcp/`.

Everything needed is in the repo: request contracts are pinned against scrubbed
fixtures in `tests/fixtures/` (real structure and protocol values, synthetic personal
data), so the suite is meaningful on a clean clone. Where the original Burp capture is
present the tests additionally check the fixtures have not drifted from it.

## Security

- **`session.json`** — канонический путь `~/.local/share/tbank-mcp/session.json`
  (переопределяется env `TBANK_SESSION`), права 0600 (owner-only). Содержит токены.
  Один и тот же файл читают и `login_cli.py`, и MCP-сервер — без ручной настройки.
  При старте MCP логирует только путь/размер/права доступа, без токенов и cookies.
- **Пароль/PIN** — НЕ в git, НЕ в коде, НЕ в контексте LLM (если используешь login_cli.py).
- **No secrets in the repo.** Two kinds of committed material look secret-adjacent and
  are not: `ca/roots/*.pem` are public CA root certificates, shipped on purpose and
  pinned by SHA-256 in `src/tls.py`; `tests/fixtures/*.json` are request contracts
  scrubbed from a real capture — real structure and protocol values, synthetic
  account, phone, address and device ids. The captures themselves are gitignored and
  never leave the machine.
- **`events.jsonl` + `attempts.jsonl`** — redacted diagnostics-логи (`~/.local/share/tbank-mcp/`).
  Содержат только step / http_status / blame / сумму / order id — никогда токены, cookies,
  адрес, телефон, email, номера счетов. Безопасны для расшаривания при дебаге (читаются тулом `diagnostics`).
- Money tools (`transfer`, `grocery_checkout`, `ticket_pay`) требуют подтверждения
  конкретной суммы — просьба «купи» подтверждением не считается.

## Disclaimer

For personal use with your own T-Bank account. Not affiliated with T-Bank.
