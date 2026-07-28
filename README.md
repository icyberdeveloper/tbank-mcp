# T-Bank MCP

**T-Bank (Т-Банк) MCP** — mobile banking API server for Claude Code, Codex, ChatGPT, and other MCP-capable agents.

## Features

- **66 tools**: accounts, cards, documents, operations, grocery ordering, cinema and
  concert tickets, orders, transfers, messenger, investments
- **10 skills**, entered through the `tbank` router skill: grocery order, tickets,
  transfer, bill pay, cards & documents, messenger, budget analysis, invest advisor,
  login
- **Pinned CA trust**: system store + the Russian Trusted Root CA (Минцифры), which no
  OS ships and every `*.t-bank-app.ru` host needs — that is most of the 22 hosts this
  MCP talks to. Shipped in `ca/roots/`, pinned by SHA-256. Leaf/intermediate rotation
  needs no action; a root rotation is a PEM drop into `ca/roots/` (or `TBANK_EXTRA_CA`).
  Certificates are never learned from the network — see the header of `src/tls.py`.
- **Grocery checkout**: search → cart → order → pay (proven end-to-end)
- **Secure login**: password/PIN stay OUT of the LLM context (local CLI or env var)

## Quick Install

### As a Claude Code plugin (server + all 10 skills in one step)

```bash
/plugin marketplace add icyberdeveloper/tbank-mcp
/plugin install tbank@tbank-mcp
/reload-plugins
```

There is no store to be admitted to — a marketplace is just a git repo with a
`.claude-plugin/marketplace.json`, and anyone can host one.

The venv and the Python dependencies are created on the server's first start by
`bin/tbank-mcp`: a plugin manifest cannot run install steps (`install` is not a
field in the schema), so the launcher does it once and every later start goes
straight to the server. Only the grocery checkout needs a browser, and 150 MB is
not something to download behind your back — install it yourself if you want that
flow:

```bash
~/.claude/plugins/*/tbank/.venv/bin/python -m playwright install chromium
```

### Manually (clone, no plugin)

```bash
git clone https://github.com/icyberdeveloper/tbank-mcp.git
cd tbank-mcp
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m playwright install chromium

# MCP server:
claude mcp add tbank -- ./.venv/bin/python -m src.server

# Skills — a COPY, so it does not follow the repo. Re-run after every pull, or
# the installed skills quietly describe an older version of these tools:
cp -r skills/* ~/.claude/skills/
```

## 🔒 Login — the password never reaches the agent

The password and the PIN are secrets, and they are **not put into the model's
context**. Logging in is done by a local script, or through an environment variable.

### Option 1 (recommended): the local CLI

The script asks for the password itself, via `getpass`, so it is never echoed to the
terminal and never passes through the agent. Its prompts are in Russian, as shown:

```bash
cd tbank-mcp

.venv/bin/python login_cli.py +7XXXXXXXXXX
# [1/3] login(+7XXXXXXXXXX) ...
#     SMS отправлена
# [2/3] SMS-код: ****                     ← the code from the SMS (hidden input)
# [3/3] Пароль (не отображается): ****    ← your password (hidden input)
#
# ✓ ГОТОВО! Сессия сохранена: ~/.local/share/tbank-mcp/session.json (права 0600).
#   MCP читает этот же файл — путь совпадает без ручной настройки.
```

Or with the password in the environment, for CI and scripts:

```bash
TBANK_PASSWORD="your-password" .venv/bin/python login_cli.py +7XXXXXXXXXX
```

Then **start Claude Code**. The agent picks up the saved session and works without
the password, which never enters the LLM context.

### Option 2: through the agent (convenient, but the LLM sees the password)

If you are content to hand the password to the agent:

```
> login(+7XXXXXXXXXX)
> [SMS code] 1234
> confirm_otp("1234")
> [bank asks password]
> confirm_password("YourPassword")
```

⚠️ **Note:** the password ends up in the model's context and in call logs. For an
account you care about, use Option 1.

Both options need the SMS code typed in either way, so there is no unattended
login: `TBANK_PASSWORD` / `TBANK_PHONE` are not read anywhere in this codebase,
and a section here used to claim otherwise.

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
The docstrings, the skills and everything the tools print are in Russian: the bank is
Russian and so is the person reading the answer.

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
| **Utility** | `flows`, `diagnostics`, `debug_report` |

`get_data(section)` covers 60+ endpoints: subscriptions, credit_schedule, statements, loans, invest_accounts, pension, etc. (`invest_portfolio` is a tool of its own, not a section — see the docstring for the full list.)

Grocery tools (`grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_set_cart`, `grocery_cart`, `grocery_checkout`) require `app_id` + `point_id` taken from `grocery_stores()` — there's no silent default store, so add/cart/checkout always operate on the same cart, instead of reporting an empty one right after something was added to a different store's.

## Skills

| Skill | What it does |
|---|---|
| `tbank` | **Entry point** — what the bank can do and which skill handles it |
| `tbank-grocery-order` | Recipe → search → cart → confirm → checkout |
| `tbank-tickets` | Cinema/concert: search → showtime → seats → book → pay |
| `tbank-bill-pay` | Topping up a phone (a P2P transfer). Service bills — utilities, taxes, fines — are **not** implemented; the skill says so and points at the app |
| `tbank-transfer-money` | P2P, SBP (СБП), account transfers |
| `tbank-cards-documents` | Cards, limits, requisites, passport and other documents |
| `tbank-messenger` | Bank chats and support |
| `tbank-budget-analyzer` | Spending analysis, subscription audit, savings tips |
| `tbank-invest-advisor` | Portfolio, P&L, rebalancing, tax optimization |
| `tbank-login` | Multi-step login, session management |

## Example requests

Ask in Russian — the tools answer in Russian. Everything below was run against the
live bank.

**Кино и афиша**
```
Что идёт в кино сегодня?
Купи два билета на «Майкла» на завтра в Каро 11 около 20:00 в центре зала
Отмени заказ
Покажи последние 5 моих заказов
```

**Деньги**
```
Покажи мои счета
Переведи 10 рублей Алёне на +79991234567
Какие последние 5 операций?
Покажи реквизиты счёта
```

**Продукты**
```
Хочу оливье, собери корзину с минимальным КБЖУ
Хочу оливье, собери корзину с минимальной ценой
Хочу оливье, собери корзину из премиум продуктов
Найди самый дешёвый картофель за килограмм
Отмени заказ
```

**Карты и документы**
```
Покажи реквизиты основной карты
Какие лимиты по основной карте?
Покажи реквизиты моего паспорта
Когда истекает мой загранпаспорт?
```

## Tests

No pytest — the tests are standalone scripts. Run them all:

```bash
.venv/bin/python tests/run_all.py            # every file, ~35 s, offline
.venv/bin/python tests/run_all.py transfer   # only files matching "transfer"
```

Each runs in its own process, and the runner redirects the attempt/event journals to
a temp directory so a test run never writes to `~/.local/share/tbank-mcp/`.

Everything needed is in the repo: request contracts are pinned against scrubbed
fixtures in `tests/fixtures/` (real structure and protocol values, synthetic personal
data), so the suite is meaningful on a clean clone. Where the original Burp capture is
present the tests additionally check the fixtures have not drifted from it.

## Security

- **`session.json`** — canonical path `~/.local/share/tbank-mcp/session.json`
  (override with `TBANK_SESSION`), mode 0600, owner-only. It holds tokens. Both
  `login_cli.py` and the MCP server read the same file, so there is nothing to
  configure. On start-up the MCP logs the path, size and permissions only — never a
  token or a cookie.
- **Password / PIN** — not in git, not in the code, and not in the LLM context if you
  use `login_cli.py`.
- **No secrets in the repo.** Two kinds of committed material look secret-adjacent and
  are not: `ca/roots/*.pem` are public CA root certificates, shipped on purpose and
  pinned by SHA-256 in `src/tls.py`; `tests/fixtures/*.json` are request contracts
  scrubbed from a real capture — real structure and protocol values, synthetic
  account, phone, address and device ids. The captures themselves are gitignored and
  never leave the machine.
- **`events.jsonl` + `attempts.jsonl`** — redacted diagnostics in
  `~/.local/share/tbank-mcp/`. They carry step, http_status, blame, amount and order
  id, and never tokens, cookies, addresses, phone numbers, emails or account numbers.
  Safe to share while debugging; the `diagnostics` tool reads them.
- **`calls.jsonl`** — one line per tool call, so it can be seen how an agent uses
  this MCP: the tool, its arguments, the duration, and the FIRST LINE of the answer,
  which is what the agent actually read. Held to the same promise as the files above:
  arguments that are free text a person wrote (a chat message, a transfer note) or a
  credential are measured, never stored; long digit runs — account, card, order and
  payment ids — are replaced in the recorded line, both to keep them out and because
  the report groups by that line. The `debug_report` tool reads it. On by default;
  `TBANK_TRACE=0` disables it, `TBANK_TRACE_FILE` moves it, and it rotates at 5 MB.
- **Device profile.** Payments carry a 3DS/anti-fraud block whose device facts —
  screen size, locale, timezone, hardware model — default to the device the traffic
  was captured from. Override them with `TBANK_DEVICE_SCREEN_HEIGHT` / `_WIDTH` /
  `TBANK_DEVICE_LANGUAGE` / `TBANK_DEVICE_TIMEZONE` / `TBANK_DEVICE_MODEL` so your
  payments do not describe someone else's phone.
- **Request-shape switches.** Two divergences from the captured app are corrected
  behind env vars, so a rollback is one variable and no re-login (neither touches
  `session.json`):
  - `TBANK_QUERY_PROFILE=legacy` — restores sending `wuid` to every host and
    injecting `vendor`/`client_version` on every read. The app sends `wuid` only to
    `www.tbank.ru` under `/api/common/`, and the other two only on the OIDC
    authorize call, so the default is now the scoped form.
  - `TBANK_ACCEPT_PROFILE` — `json` (default, and today's behaviour byte-for-byte)
    | `auto` | a comma-separated host list. The app does not send
    `application/json` to its native hosts; that string is the Apple URL-loading
    default that appears when no Accept is set. The captured responses are
    `application/json` either way, so this is fidelity rather than a fix — but 63
    templates share the busiest host and there is no staging environment, so it is
    OFF until driven live. Roll it out one host class at a time, cheapest first:
    `webview`/`shortcuts`/`my-home` (unreachable or trivial reads) → `api-invest*`
    (`invest_accounts`, `invest_portfolio`) → `api.t-bank-app.ru` starting with
    `keepalive`, whose Content-Type demonstrably becomes `text/html` while its body
    stays JSON → `www.tbank.ru` → the three lifestyle shelf paths. A regression has
    one signature: `_unwrap` raising `HTTP_200` because the body no longer parses.
    Compare `debug_report()` before and after each step.
- Money tools (`transfer`, `grocery_checkout`, `ticket_pay`, `pay_bill`) require
  confirmation of a specific amount — "buy it" is not a confirmation.
- **Tool annotations.** Every tool declares what it does, in one table —
  `TOOL_KINDS` in `src/server.py` — and a tool missing from it raises at import
  rather than defaulting to anything. Three kinds: 50 are `readOnlyHint: true` and
  may run without a prompt; 12 write something that costs nothing (a cart, a
  booking, a message, an OTP, a token, a local file) and are marked
  `destructiveHint: false`; 4 debit an account — `transfer`, `grocery_checkout`,
  `ticket_pay`, `pay_bill` — and are the only ones carrying `destructiveHint`, which
  is what forces a confirmation dialog. The line is drawn at money on purpose: a
  booking expires by itself and a cart line is a rewrite away, so confirming those is
  friction that teaches people to click through the one dialog that matters.
  The 12 writers are not marked read-only, because they do modify things and that
  flag states the opposite — if your client still prompts on them, allow them once
  in the client rather than changing what the server claims.

## Disclaimer

For personal use with your own T-Bank account. Not affiliated with T-Bank.
