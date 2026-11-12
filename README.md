# T-Bank MCP

<!-- mcp-name: io.github.icyberdeveloper/tbank-mcp -->

**T-Bank (Т-Банк) MCP** — mobile banking API server for Claude Code, Codex, ChatGPT, and other MCP-capable agents.

[![PyPI](https://img.shields.io/pypi/v/tbank-mcp?style=flat-square)](https://pypi.org/project/tbank-mcp/)
[![Install MCP Server](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/install-mcp?name=tbank&config=eyJjb21tYW5kIjoidGJhbmstbWNwIn0%3D)
[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522tbank%2522%252C%2522command%2522%253A%2522tbank-mcp%2522%257D)
[![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522tbank%2522%252C%2522command%2522%253A%2522tbank-mcp%2522%257D)

The buttons register the `tbank-mcp` command — run `pip install tbank-mcp` first.

## Features

- **90 tools**: accounts, cards, documents, operations, grocery ordering, cinema and
  concert tickets, train and flight booking, hotel search, orders, transfers
  (including payment by bank requisites, from a scanned invoice QR), messenger,
  investments
- **A skill for every vertical**, entered through the `tbank` router skill: grocery
  order, tickets, travel, transfer, bill pay, cards & documents, messenger, budget
  analysis, invest advisor, login
- **Pinned CA trust**: system store + the Russian Trusted Root CA (Минцифры), which no
  OS ships and every `*.t-bank-app.ru` host needs — that is most of the hosts this
  MCP talks to. Shipped in `tbank_mcp/ca/roots/`, pinned by SHA-256. Leaf/intermediate rotation
  needs no action; a root rotation is a PEM drop into `tbank_mcp/ca/roots/` (or `TBANK_EXTRA_CA`).
  The verify bundle is (re)generated from that material into
  `~/.local/share/tbank-mcp/bundle.pem`; `TBANK_CA_BUNDLE` relocates it — it is a
  write target, not a curated input, so extra roots go in via `TBANK_EXTRA_CA`.
  Certificates are never learned from the network — see the header of `tbank_mcp/tls.py`.
- **Grocery checkout**: search → cart → order → pay (proven end-to-end)
- **Secure login**: password/PIN stay OUT of the LLM context (local CLI or env var)

## Quick Install

### As a Claude Code plugin (server + every skill in one step)

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
~/.claude/plugins/cache/tbank-mcp/tbank/*/.venv/bin/python -m playwright install chromium
```

### From PyPI

```bash
pip install tbank-mcp
tbank-mcp-login +7XXXXXXXXXX   # first login: SMS code + password in YOUR terminal, not the LLM
claude mcp add tbank -- tbank-mcp
tbank-mcp-skills               # optional: the Claude Code skills, into ~/.claude/skills
```

The `tbank-mcp` console script starts the stdio server; the pinned CA roots, the
flows reference and the skills all ship inside the wheel, so it runs from anywhere.
`tbank-mcp-login` is the same login CLI as the repo's `login_cli.py` — both write
the session to the same file the server reads. `tbank-mcp-skills` installs the
skills for Claude Code (default `~/.claude/skills`, `--target` for elsewhere) and,
unlike a plain `cp`, first removes stale copies under retired names — re-run it
after upgrades. The grocery checkout browser stays opt-in here too:
`python -m playwright install chromium`.

### Manually (clone, no plugin)

```bash
git clone https://github.com/icyberdeveloper/tbank-mcp.git
cd tbank-mcp
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m playwright install chromium

# MCP server:
claude mcp add tbank -- ./.venv/bin/python -m tbank_mcp.server

# Skills — a COPY, so it does not follow the repo. Re-run after every pull, or
# the installed skills quietly describe an older version of these tools. The
# installer also removes stale copies under RENAMED skill names, which a plain
# cp never does — the agent would keep loading the stale one:
.venv/bin/tbank-mcp-skills
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
login. `TBANK_PASSWORD` (and `TBANK_PIN`) are read **only** by the login CLI
(`tbank-mcp-login` / `login_cli.py`) — the
env example above — and never by the MCP server or the LLM. `TBANK_PHONE` is not
read anywhere: the phone is always a command-line argument.

> **Работа с MyT (рабочий календарь и парковка) переехала** в отдельный MCP:
> [tbank-myt](https://github.com/icyberdeveloper/tbank-myt). Другой аккаунт, другая
> сессия, свой `login_cli.py` — здесь их больше нет.

## Other agents (Codex, ChatGPT, Hermes, OpenClaw)

```jsonc
{
  "mcpServers": {
    "tbank": {
      "command": "/path/to/tbank-mcp/.venv/bin/python",
      "args": ["-m", "tbank_mcp.server"],
      "cwd": "/path/to/tbank-mcp"
    }
  }
}
```

With a `pip install tbank-mcp` the whole entry shrinks to `"command": "tbank-mcp"` —
no paths, no cwd.

### Use with Cursor

Click the **Install MCP Server** button above, or open the **Customize** page from
Cursor's sidebar to add the server, or create `~/.cursor/mcp.json` (global) /
`.cursor/mcp.json` (per-project):

```json
{
  "mcpServers": {
    "tbank": { "command": "tbank-mcp" }
  }
}
```

Cursor renders MCP elicitation, so the payment-confirmation buttons work in chat.

### Use with Cherry Studio

**Settings → MCP → MCP Servers → Add**: type `stdio`, command `tbank-mcp`, no
arguments. Save, enable the server, wait for the healthy status.

> Cherry Studio does not render MCP elicitation yet (open request
> [CherryHQ/cherry-studio#9145](https://github.com/CherryHQ/cherry-studio/issues/9145)),
> so reading works but the money tools refuse to execute there — by design, not by
> accident.

### Use with Goose

CLI: `goose configure` → **Add Extension** → **Command-line Extension**, name
`tbank`, command `tbank-mcp`. Desktop: sidebar → **Extensions** → **Add custom
extension**, same values.

Goose renders elicitation in both Desktop and CLI; its confirmation forms time out
after 5 minutes, so answer payment confirmations promptly.

Reading works in any MCP client. **Paying needs a client that renders MCP
elicitation** — the money tools confirm the sum with a button the user presses
(«Перевести/Отмена», «Оплатить …?»), and a client without that capability is refused
before anything is sent (`grocery_checkout` refuses there at any threshold; its
`dry_run=True` preview, which creates nothing, still works). Hermes/Telegram and
Claude Code (≥ 2.1.76) render it; Claude Desktop does not. See
`TBANK_CONFIRM_ABOVE` under Security.

### Use through an OpenAI Secure MCP Tunnel

The existing stdio server can run behind OpenAI's `tunnel-client`, including on a
GCP Compute Engine VM. No HTTP adapter or public MCP port is needed. See
[Secure MCP Tunnel setup](docs/SECURE_MCP_TUNNEL.md) for deployment prerequisites,
private access, validation, and the limitations of payment confirmations and
server-local files. Transport support alone does not guarantee that every tool
works in a particular hosted client.

## Tools

Each tool's docstring is the reference — this table is only a map of the surface.
The docstrings, the skills and everything the tools print are in Russian: the bank is
Russian and so is the person reading the answer.

| Group | Tools |
|---|---|
| **Login** | `login`, `confirm_otp`, `confirm_password`, `confirm_pin` |
| **Session** | `refresh_session`, `session_status`, `keepalive`, `push_unread_count` |
| **Reads** | `list_accounts`, `list_operations`, `spending_categories`, `operations_histogram`, `get_data` |
| **Cards & accounts** | `list_cards`, `card_limits`, `card_requisites`, `card_operations`, `account_requisites` |
| **Documents** | `documents`, `bank_documents`, `insurance_policies`, `payment_receipt` |
| **Grocery** | `grocery_stores`, `grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_set_cart`, `grocery_cart`, `grocery_checkout`, `payment_attempts`, `grocery_order_status`, `grocery_order_cancel` |
| **Nutrition** | `grocery_good_info`, `grocery_rank` |
| **Orders** | `orders`, `order_details`, `travel_order_details` |
| **Afisha** | `afisha_catalog`, `afisha_places`, `place_schedule`, `place_info` |
| **Tickets** | `cinema_search`, `cinema_schedule`, `cinema_seats`, `concert_schedule`, `concert_hall`, `cinema_book`, `ticket_pay`, `ticket_cancel`, `ticket_qr` |
| **Search** | `search_app` |
| **Travel search** | `train_search`, `train_calendar`, `flight_search`, `flight_offer`, `flight_history` |
| **Travel booking** | `train_seats`, `train_book`, `train_pay`, `train_refund`, `flight_seats`, `flight_book` |
| **Hotels** | `hotel_search`, `hotel_info` |
| **Trips** | `trips`, `travel_payment_options`, `travel_ticket_file` |
| **Marketplace** | `shop_search`, `shop_cart` |
| **Messenger** | `messenger_conversations`, `messenger_messages`, `messenger_file`, `messenger_send`, `messenger_unread` |
| **Money** | `transfer_sbp_resolve`, `transfer`, `payment_qr`, `transfer_requisites`, `payment_commission`, `pay_bill`, `payment_providers`, `confirm_payment`, `payment_status` |
| **Invest** | `invest_accounts`, `invest_portfolio`, `invest_operations`, `invest_securities` |
| **Utility** | `flows`, `diagnostics`, `debug_report` |

`get_data(section)` covers dozens of read sections: subscriptions, credit_schedule, statements, loans, invest_accounts, pension, etc. (`invest_portfolio` is a tool of its own, not a section — see the docstring for the full list.)

Grocery tools (`grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_set_cart`, `grocery_cart`, `grocery_checkout`) require `app_id` + `point_id` taken from `grocery_stores()` — there's no silent default store, so add/cart/checkout always operate on the same cart, instead of reporting an empty one right after something was added to a different store's.

## Skills

| Skill | What it does |
|---|---|
| `tbank` | **Entry point** — what the bank can do and which skill handles it |
| `tbank-grocery-order` | Recipe → search → cart → show it → checkout (the tool's own button confirms the sum) |
| `tbank-tickets` | Cinema/concert: search → showtime → seats → book → pay |
| `tbank-travel` | Trains and flights: search → seats → book → pay → refund; hotels and marketplace: search only |
| `tbank-bill-pay` | Service bills — utilities, taxes, fines: catalogue → provider fields → commission preview → pay |
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
.venv/bin/python tests/run_all.py            # every file, under a minute, offline
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
  the login CLI and the MCP server read the same file, so there is nothing to
  configure. On start-up the MCP logs the path, size and permissions only — never a
  token or a cookie.
- **Password / PIN** — not in git, not in the code, and not in the LLM context if you
  use the login CLI (`tbank-mcp-login` / `login_cli.py`).
- **No secrets in the repo.** Two kinds of committed material look secret-adjacent and
  are not: `tbank_mcp/ca/roots/*.pem` are public CA root certificates, shipped on purpose and
  pinned by SHA-256 in `tbank_mcp/tls.py`; `tests/fixtures/*.json` are request contracts
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
- **`TBANK_CONFIRM_ABOVE`** — the ruble threshold from which the paying tools
  that debit on the spot (`transfer`, `transfer_requisites`, `pay_bill`,
  `ticket_pay`, `grocery_checkout`, `train_pay`, `flight_book`)
  show the confirmation button — an MCP elicitation dialog («Перевести/Отмена»,
  «Оплатить …?», «Оформить заказ на N ₽?») rendered by the client (default `0`:
  every payment asks). It is a server-side setting, not a tool argument. Clients
  without elicitation are NOT waved through: at or above the threshold the tool
  refuses («ПЛАТЁЖ НЕ ВЫПОЛНЕН…») before anything is journalled or sent — no
  button, no payment. Hermes/Telegram and Claude Code (≥ 2.1.76) render
  elicitation; Claude Desktop does not (reads work there, paying does not).
  Below a positive threshold nothing is asked and the payment proceeds in any
  client — **except `grocery_checkout`, which refuses a client without elicitation
  at any threshold**: it is the one paying tool that must load the checkout page to
  learn its sum at all, and doing that means asking the store to hold a
  delivery slot, so it says no before doing that work rather than after.
  `grocery_checkout(dry_run=True)` — a preview that creates nothing — still works
  in any client.
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
- Money tools (`transfer`, `transfer_requisites`, `grocery_checkout`, `ticket_pay`,
  `pay_bill`, `train_pay`, `flight_book`, `confirm_payment`) require confirmation of a specific amount — "buy it"
  is not a confirmation. That confirmation is the button the tool shows itself
  (elicitation, see `TBANK_CONFIRM_ABOVE` above) with the real total — the agent
  shows the details beforehand (recipient, requisites, cart, seats + fee) and does
  not ask «да/нет» in text; `grocery_checkout` quotes the final sum itself and
  charges exactly what the button named — and if that quote comes back unpriced
  (empty cart, a preview the store refused, no finite positive total), it returns
  the preview and charges nothing. A `/v1/pay` the bank holds at `WAITING_CONFIRMATION` is
  resumed with `confirm_payment(attempt_id, otp)` and reconciled with
  `payment_status(attempt_id)` — never by repeating the transfer, which would create a
  second pending payment.
- **Tool annotations.** Every tool declares what it does, in one table —
  `TOOL_KINDS` in `tbank_mcp/server.py` — and a tool missing from it raises at import
  rather than defaulting to anything. Three kinds: 67 are `readOnlyHint: true` and
  may run without a prompt; 15 write something that costs nothing (a cart, a
  booking, a message, an OTP, a token, a local file) and are marked
  `destructiveHint: false`; 8 debit an account — `transfer`, `transfer_requisites`,
  `grocery_checkout`, `ticket_pay`, `pay_bill`, `train_pay`, `flight_book`,
  `confirm_payment` — and are the only
  ones carrying `destructiveHint`, which
  is what makes the host prompt before running them (the sum itself is then
  confirmed by the tool's own elicitation button, see above). The line is drawn at money on purpose: a
  booking expires by itself and a cart line is a rewrite away, so confirming those is
  friction that teaches people to click through the one dialog that matters.
  The 15 writers are not marked read-only, because they do modify things and that
  flag states the opposite — if your client still prompts on them, allow them once
  in the client rather than changing what the server claims.

## Disclaimer

For personal use with your own T-Bank account. Not affiliated with T-Bank.
