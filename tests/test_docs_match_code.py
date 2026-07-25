"""The docs and the code must not drift apart — this is the check that catches it.

Every previous audit found the same shape of bug: a tool is renamed or deleted, and
the documents that teach an agent how to call it keep the old name. It is invisible
because nothing executes a document. `grocery_pick_lightest` survived in FLOWS.md
for a whole release after it was removed from server.py; an agent following that
line calls a tool that does not exist and has no way to recover.

The same rot hit the `flows` tool itself: it returned FLOWS.md[:6000] while the file
had grown to ~12 000 chars, so every flow from the messenger down — cards, orders,
nutrition, tickets — was silently unreachable through the one tool meant to serve it.
Truncation is invisible from the inside, so it is pinned here by section, not by
character count.

    python3 tests/test_docs_match_code.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import server  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def tool_names():
    """The @mcp.tool()-decorated function names in server.py."""
    src = open(os.path.join(ROOT, "src", "server.py"), encoding="utf-8").read().splitlines()
    names = set()
    for i, line in enumerate(src):
        if line.startswith("@mcp.tool"):
            for j in range(i + 1, min(i + 4, len(src))):
                m = re.match(r"(?:async )?def (\w+)", src[j])
                if m:
                    names.add(m.group(1))
                    break
    return names


# Names the documents mention on purpose while stating they are NOT tools: internal
# client methods and API steps that run inside a tool. FLOWS.md calls this out in its
# preamble; keeping the list here means adding a new internal reference is a conscious
# act, not an accident.
NOT_TOOLS = {
    "pay", "group_pay", "payment_gate_pay", "grocery_goods", "grocery_cart_set",
    "ensure_fresh", "ensure_client_session", "silent_relogin",
    "issueTokenBySSO", "grocery_order_create", "checkout_process_order",
    "login_cli", "nutrition", "python", "json", "getpass",
}

DOCS = ["FLOWS.md", "README.md", "AGENTS.md", "MOBILE_CHECKOUT.md"]


def doc_files():
    out = [os.path.join(ROOT, d) for d in DOCS if os.path.exists(os.path.join(ROOT, d))]
    skills = os.path.join(ROOT, "skills")
    if os.path.isdir(skills):
        for name in sorted(os.listdir(skills)):
            p = os.path.join(skills, name, "SKILL.md")
            if os.path.exists(p):
                out.append(p)
    return out


def test_documented_tools_exist():
    """Any `name(...)` in backticks in a doc is an instruction to call something."""
    tools = tool_names()
    for path in doc_files():
        text = open(path, encoding="utf-8").read()
        rel = os.path.relpath(path, ROOT)
        for line_no, line in enumerate(text.splitlines(), 1):
            for name in re.findall(r"`(\w+)\s*\(", line):
                if name in tools or name in NOT_TOOLS:
                    continue
                failures.append(
                    f"{rel}:{line_no} tells the agent to call `{name}(...)`, "
                    f"which is not an MCP tool")
    print(f"  {len(tools)} tools; every `name()` in {len(doc_files())} docs resolves")


def test_every_tool_is_documented():
    """A tool no document mentions is a tool no agent will find."""
    tools = tool_names()
    blob = "\n".join(open(p, encoding="utf-8").read() for p in doc_files())
    missing = sorted(t for t in tools if f"`{t}" not in blob)
    check(not missing,
          f"tools documented nowhere (invisible to an agent): {', '.join(missing)}")
    print(f"  {len(tools) - len(missing)}/{len(tools)} tools appear in a doc or skill")


def test_flows_serves_every_section():
    """flows() must reach the WHOLE file. It used to return the first 6000 chars,
    which silently cut everything from section 5 onward."""
    sections = server._flow_sections()
    check(len(sections) >= 10, f"FLOWS.md parsed into only {len(sections)} sections")

    toc = server.flows()
    for title, _ in sections:
        if title.lower().startswith("notes"):
            continue
        check(title in toc, f"flows() index does not list section {title!r}")

    # Each section must be reachable by a plausible request, and arrive whole.
    probes = {
        "Bootstrap": "логин", "Session": "сессия", "Read accounts": "операции",
        "Grocery cart": "продукты", "transfer": "перевод", "Messenger": "чат",
        "Invest": "инвестиции", "Credit": "кредит", "Cards": "реквизиты карты",
        "Orders": "заказы", "nutrition": "кбжу", "Tickets": "билеты",
        "Global search": "поиск",
    }
    by_title = {t: b for t, b in sections}
    for fragment, query in probes.items():
        hit = next((t for t in by_title if fragment.lower() in t.lower()), None)
        check(hit is not None, f"FLOWS.md has no section matching {fragment!r}")
        if hit is None:
            continue
        out = server.flows(query)
        check(hit in out, f"flows({query!r}) did not return section {hit!r}")
        body = by_title[hit]
        tail = [ln for ln in body.strip().splitlines() if ln.strip()]
        if tail:
            check(tail[-1].strip() in out,
                  f"flows({query!r}) truncated {hit!r} — last line missing")
    print(f"  flows(): {len(sections)} sections indexed, {len(probes)} probes returned whole")


def test_flows_unknown_topic_is_actionable():
    """A miss must hand the agent the valid topics, not a bare failure."""
    out = server.flows("совершенно посторонний запрос")
    check("не найден" in out.lower() or "not found" in out.lower(),
          "an unmatched topic must say so")
    check(sum(1 for t, _ in server._flow_sections() if t in out) >= 5,
          "an unmatched topic must list the sections that DO exist")
    print("  flows(): unknown topic answers with the list of real topics")


def test_money_tools_warn_in_their_own_docstring():
    """A skill may not be loaded. The tool's own docstring is the last line of
    defence before a real charge."""
    for name in ("transfer", "grocery_checkout", "ticket_pay"):
        fn = getattr(server, name, None)
        check(fn is not None, f"money tool {name} is missing entirely")
        if fn is None:
            continue
        doc = (fn.__doc__ or "").upper()
        check("РЕАЛЬН" in doc or "REAL" in doc,
              f"{name}'s docstring never says the money is real: {fn.__doc__!r}")
    print("  money tools: transfer / grocery_checkout / ticket_pay all warn in-docstring")


def main():
    print("docs vs code:")
    test_documented_tools_exist()
    test_every_tool_is_documented()
    test_flows_serves_every_section()
    test_flows_unknown_topic_is_actionable()
    test_money_tools_warn_in_their_own_docstring()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
