# T-Bank MCP

**T-Bank (Т-Банк) MCP** — mobile banking API server for Claude Code, Codex, ChatGPT, and other MCP-capable agents.

## Features

- **25 tools**: accounts, operations, grocery ordering, transfers, messenger, investments
- **6 skills**: grocery order, bill pay, transfer, budget analysis, invest advisor, login
- **Self-bootstrap**: `login(phone)` → SMS OTP → password → session. No capture, no emulator, no frida.
- **Self-healing TLS**: handles Russian Trusted Root CA + HARICA cert rotation
- **Grocery checkout**: search → cart → order → pay (proven end-to-end)

## Quick Install

### Option A: Claude Plugin

```bash
# In Claude Code:
/plugin marketplace add neondelph/tbank-mcp
/plugin install tbank-mcp
```

Then:
```
> login(+79991234567)
> [SMS code]
> confirm_otp(1234)
> [if bank asks password]
> confirm_password(YourPassword)
```

Done. Ask: «покажи счета», «закажи продукты для борща», «сколько трачу на подписки».

### Option B: Manual

```bash
git clone https://github.com/neondelph/tbank-mcp.git
cd tbank-mcp
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m playwright install chromium

# MCP:
claude mcp add tbank -- ./.venv/bin/python -m src.server

# Skills:
cp -r skills/* ~/.claude/skills/
```

### Option C: Other agents (Codex, ChatGPT, Hermes, OpenClaw)

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

## Tools (25)

| Group | Tools |
|---|---|
| **Login** | `login`, `confirm_otp`, `confirm_password`, `confirm_pin` |
| **Session** | `refresh_session`, `session_status`, `keepalive` |
| **Reads** | `list_accounts`, `list_operations`, `spending_categories`, `operations_histogram`, `get_data` |
| **Grocery** | `grocery_stores`, `grocery_search`, `grocery_plan_order`, `grocery_add_to_cart`, `grocery_cart`, `grocery_checkout` |
| **Messenger** | `messenger_conversations`, `messenger_messages`, `messenger_send`, `messenger_unread` |
| **Money** | `transfer`, `payment_commission` |
| **Utility** | `flows` |

`get_data(section)` covers 60+ endpoints: subscriptions, credit_schedule, statements, requisites, invest_accounts, invest_portfolio, etc.

## Skills (6)

| Skill | What it does |
|---|---|
| `tbank-grocery-order` | Recipe → search → cart → confirm → checkout |
| `tbank-bill-pay` | Phone, internet, ЖКХ, taxes, fines |
| `tbank-transfer-money` | P2P, СБП, account transfers |
| `tbank-budget-analyzer` | Spending analysis, subscription audit, savings tips |
| `tbank-invest-advisor` | Portfolio, P&L, rebalancing, tax optimization |
| `tbank-login` | Multi-step login, session management |

## How it works

1. **Login** — phone → SMS OTP → password (first device) → session
2. **Headless** — silent re-login (~2h, no OTP), self-healing TLS
3. **Reads** — cookie + Bearer (no per-request signature)
4. **Grocery** — search/fulltext → cart/set (mobile API) → checkout (Playwright web cart sync)
5. **P2P pay** — HMAC-SHA256 `x-api-signature` (key = sessionid)

## Security

- `session.json` is **gitignored** — never commit
- No hardcoded secrets in code (verified by audit)
- Money tools require explicit confirmation
- `ca/bundle.pem` regenerated from public CAs

## Disclaimer

For personal use with your own T-Bank account. Not affiliated with T-Bank.
