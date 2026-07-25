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


def registered_tools():
    """The tools the MCP server actually serves, from its live registry.

    Not a scan of the source: this is the same list a connected agent receives, so a
    tool that fails to register (bad signature, decorator dropped, import error) is a
    miss here even though the `def` is still sitting in the file."""
    import asyncio
    import inspect
    listed = server.mcp._tool_manager.list_tools()
    if inspect.isawaitable(listed):
        listed = asyncio.run(listed)
    return {t.name: t for t in listed}


def tool_names():
    return set(registered_tools())


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


def test_every_tool_is_reachable_from_a_skill():
    """Docs are read only if the agent goes looking. A SKILL loads on its own, so a
    tool named in no skill is one the agent will not think to call — which is how
    cards, documents and the whole messenger went unreachable until the `tbank`
    router and the two new skills were added."""
    import glob
    tools = tool_names()
    skill_text = "\n".join(
        open(p, encoding="utf-8").read()
        for p in glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md")))
    missing = sorted(t for t in tools if f"`{t}(" not in skill_text
                     and f"`{t}`" not in skill_text)
    check(not missing,
          f"tools no skill mentions (an agent will never reach them): {', '.join(missing)}")

    # The router must actually route: every OTHER skill has to be named in it.
    router = os.path.join(ROOT, "skills", "tbank", "SKILL.md")
    check(os.path.exists(router), "the tbank router skill is missing")
    if os.path.exists(router):
        text = open(router, encoding="utf-8").read()
        others = [os.path.basename(os.path.dirname(p))
                  for p in glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))
                  if os.path.basename(os.path.dirname(p)) != "tbank"]
        unrouted = sorted(s for s in others if s not in text)
        check(not unrouted, f"the router does not mention: {', '.join(unrouted)}")
    print(f"  {len(tools) - len(missing)}/{len(tools)} tools reachable from a skill; "
          f"router names every other skill")


def test_plugin_ships_every_skill():
    """A skill on disk but absent from plugin.json ships to nobody — that is how the
    tickets skill was invisible to plugin installs."""
    import glob
    import json as _json
    manifest = os.path.join(ROOT, "plugin.json")
    check(os.path.exists(manifest), "plugin.json is missing")
    if not os.path.exists(manifest):
        return
    listed = set(_json.load(open(manifest, encoding="utf-8")).get("skills") or [])
    on_disk = {"skills/" + os.path.basename(os.path.dirname(p))
               for p in glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))}
    check(on_disk - listed == set(),
          f"skills on disk but not shipped: {sorted(on_disk - listed)}")
    check(listed - on_disk == set(),
          f"plugin.json lists skills that do not exist: {sorted(listed - on_disk)}")
    print(f"  plugin.json ships all {len(on_disk)} skills")


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


def test_money_tools_warn_in_the_description_the_agent_receives():
    """A skill may not be loaded. The description the MCP actually ships with the
    tool is the last line of defence before a real charge — so assert on that, not
    on the docstring in the file."""
    tools = registered_tools()
    for name in ("transfer", "grocery_checkout", "ticket_pay"):
        t = tools.get(name)
        check(t is not None, f"money tool {name} is not registered with the server")
        if t is None:
            continue
        desc = (t.description or "").upper()
        check("РЕАЛЬН" in desc or "REAL" in desc,
              f"{name}'s shipped description never says the money is real: {t.description!r}")
    print("  money tools: the descriptions shipped to the agent all warn about real money")


def main():
    print("docs vs code:")
    test_documented_tools_exist()
    test_every_tool_is_documented()
    test_every_tool_is_reachable_from_a_skill()
    test_plugin_ships_every_skill()
    test_flows_serves_every_section()
    test_flows_unknown_topic_is_actionable()
    test_money_tools_warn_in_the_description_the_agent_receives()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
