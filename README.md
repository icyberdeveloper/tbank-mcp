# T-Bank MCP

**T-Bank (Т-Банк) MCP** — mobile banking API server for Claude Code, Codex, ChatGPT, and other MCP-capable agents.

## Features

- **28 tools**: accounts, operations, grocery ordering, transfers, messenger, investments
- **6 skills**: grocery order, bill pay, transfer, budget analysis, invest advisor, login
- **Self-healing TLS**: handles Russian Trusted Root CA + HARICA cert rotation
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
.venv/bin/python login_cli.py +79991234567
# [1/3] login(+79991234567) ... SMS отправлена
# [2/3] SMS-код: ****        ← вводишь код из SMS (скрытый ввод)
# [3/3] Пароль (не отображается): ****   ← вводишь пароль (скрытый ввод)
# ✓ ГОТОВО! session.json сохранён (права 0600).
#   Запусти Claude Code в этом репозитории.
#   Пароль НЕ передан агенту — он работает с сохранённой сессией.
```

Или с паролем в env (CI/скрипты):
```bash
TBANK_PASSWORD="пароль" .venv/bin/python login_cli.py +79991234567
```

После логина — **запусти Claude Code**. Агент видит сохранённую сессию и работает
без пароля. Пароль никогда не попадает в контекст LLM.

### Способ 2: через агента (удобно, но пароль виден LLM)

Если тебе удобно передавать пароль агенту:

```
> login(+79991234567)
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
export TBANK_PHONE="+79991234567"
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

## Tools (28)

| Group | Tools |
|---|---|
| **Login** | `login`, `confirm_otp`, `confirm_password`, `confirm_pin` |
| **Session** | `refresh_session`, `session_status`, `keepalive` |
| **Reads** | `list_accounts`, `list_operations`, `spending_categories`, `operations_histogram`, `get_data` |
| **Grocery** | `grocery_stores`, `grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_cart`, `grocery_checkout`, `grocery_attempts`, `grocery_order_status` |
| **Messenger** | `messenger_conversations`, `messenger_messages`, `messenger_send`, `messenger_unread` |
| **Money** | `transfer`, `payment_commission` |
| **Utility** | `flows`, `diagnostics` |

`get_data(section)` covers 60+ endpoints: subscriptions, credit_schedule, statements, requisites, invest_accounts, invest_portfolio, etc.

Grocery tools (`grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_cart`, `grocery_checkout`) require `app_id` + `point_id` taken from `grocery_stores()` — there's no silent default store, so add/cart/checkout always operate on the same cart (no more "Корзина пуста" after adding).

## Skills (6)

| Skill | What it does |
|---|---|
| `tbank-grocery-order` | Recipe → search → cart → confirm → checkout |
| `tbank-bill-pay` | Phone, internet, ЖКХ, taxes, fines |
| `tbank-transfer-money` | P2P, СБП, account transfers |
| `tbank-budget-analyzer` | Spending analysis, subscription audit, savings tips |
| `tbank-invest-advisor` | Portfolio, P&L, rebalancing, tax optimization |
| `tbank-login` | Multi-step login, session management |

## Security

- **`session.json`** — канонический путь `~/.local/share/tbank-mcp/session.json`
  (переопределяется env `TBANK_SESSION`), права 0600 (owner-only). Содержит токены.
  Один и тот же файл читают и `login_cli.py`, и MCP-сервер — без ручной настройки.
  При старте MCP логирует только путь/размер/права доступа, без токенов и cookies.
- **Пароль/PIN** — НЕ в git, НЕ в коде, НЕ в контексте LLM (если используешь login_cli.py).
- **0 hardcoded secrets** in code (verified by audit).
- **`events.jsonl` + `attempts.jsonl`** — redacted diagnostics-логи (`~/.local/share/tbank-mcp/`).
  Содержат только step / http_status / blame / сумму / order id — никогда токены, cookies,
  адрес, телефон, email, номера счетов. Безопасны для расшаривания при дебаге (читаются тулом `diagnostics`).
- Money tools (`transfer`, `grocery_checkout`) требуют подтверждения.

## Disclaimer

For personal use with your own T-Bank account. Not affiliated with T-Bank.
