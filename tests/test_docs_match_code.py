"""The docs and the code must not drift apart — this is the check that catches it.

Every previous audit found the same shape of bug: a tool is renamed or deleted, and
the documents that teach an agent how to call it keep the old name. It is invisible
because nothing executes a document. `grocery_pick_lightest` survived in docs/FLOWS.md
for a whole release after it was removed from server.py; an agent following that
line calls a tool that does not exist and has no way to recover.

The same rot hit the `flows` tool itself: it returned docs/FLOWS.md[:6000] while the file
had grown to ~12 000 chars, so every flow from the messenger down — cards, orders,
nutrition, tickets — was silently unreachable through the one tool meant to serve it.
Truncation is invisible from the inside, so it is pinned here by section, not by
character count.

    python3 tests/test_docs_match_code.py
"""
import os
import re
import sys
from collections import Counter

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
# client methods and API steps that run inside a tool. docs/FLOWS.md calls this out in its
# preamble; keeping the list here means adding a new internal reference is a conscious
# act, not an accident.
NOT_TOOLS = {
    "pay", "group_pay", "payment_gate_pay",
    "ensure_fresh", "ensure_client_session", "silent_relogin",
    "issueTokenBySSO", "grocery_order_create", "checkout_process_order",
    "login_cli", "nutrition", "python", "json", "getpass",
}

# AGENTS.md was deleted (nothing read it); doc_files() skips missing entries, so the
# name sat here harmlessly until someone recreated the file by accident.
DOCS = ["docs/FLOWS.md", "README.md", "docs/MOBILE_CHECKOUT.md"]


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


def test_evals_only_ask_for_tools_that_exist():
    """An eval is the spec for how the skill should behave, so a tool name in one is
    an instruction too — and nothing was checking them. `portfolio(days=90)` sat in
    the invest evals while the tool is `invest_portfolio(broker_account_id, days)`."""
    import glob
    tools = tool_names()
    # No space before the paren: eval prose says «без query (или …)», which is not
    # a call, while every real call is written name(...).
    call = re.compile(r"\b([a-z][a-z0-9_]{3,})\(")
    for path in sorted(glob.glob(os.path.join(ROOT, "skills", "*", "evals", "*.json"))):
        rel = os.path.relpath(path, ROOT)
        for name in set(call.findall(open(path, encoding="utf-8").read())):
            check(name in tools or name in NOT_TOOLS,
                  f"{rel} expects the agent to call {name}(...), which is not a tool")
    print("  evals name only real tools")


def test_the_marketplace_entry_matches_the_plugin():
    """Two manifests describing the same thing drift: the marketplace card advertised
    «6 skills» at version 0.1.0 while plugin.json shipped 10 at 0.1.1. Nobody counts
    them by hand twice and gets it right twice, so it is asserted instead.

    The skill count now comes from the skills/ DIRECTORY, not from a list inside the
    manifest: `skills` was dropped from plugin.json because Claude Code scans
    skills/ by default, and a hand-kept list was one more thing to forget."""
    import json as _json
    market = os.path.join(ROOT, ".claude-plugin", "marketplace.json")
    if not os.path.exists(market):
        print("  marketplace.json absent — nothing to keep in sync")
        return
    m = _json.load(open(market, encoding="utf-8"))
    # The manifest lives in .claude-plugin/. At the repo root it is not a manifest,
    # it is a file nobody reads — Claude Code loads only .claude-plugin/plugin.json.
    plugin_path = os.path.join(ROOT, ".claude-plugin", "plugin.json")
    check(os.path.exists(plugin_path),
          "plugin.json must live in .claude-plugin/ — anywhere else it is ignored")
    check(not os.path.exists(os.path.join(ROOT, "plugin.json")),
          "a plugin.json at the repo root is dead weight and drifts from the real one")
    if not os.path.exists(plugin_path):
        return
    p = _json.load(open(plugin_path, encoding="utf-8"))
    n = len([d for d in os.listdir(os.path.join(ROOT, "skills"))
             if os.path.exists(os.path.join(ROOT, "skills", d, "SKILL.md"))])
    check(m.get("metadata", {}).get("version") == p.get("version"),
          f"marketplace version {m.get('metadata', {}).get('version')!r} != "
          f"plugin version {p.get('version')!r}")
    # Third copy of the same number, and the one that was already out of step.
    pyproject = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    pv = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    check(pv and pv.group(1) == p.get("version"),
          f"pyproject version {pv.group(1) if pv else None!r} != "
          f"plugin version {p.get('version')!r}")
    for entry in m.get("plugins") or []:
        desc = entry.get("description", "")
        found = re.findall(r"(\d+)\s+skills", desc)
        check(found and int(found[0]) == n,
              f"marketplace advertises {found or ['no']} skills, skills/ holds {n}")
        check(entry.get("version") in (None, p.get("version")),
              f"marketplace entry version {entry.get('version')!r} != "
              f"plugin version {p.get('version')!r}")
    print(f"  marketplace.json agrees with plugin.json: v{p.get('version')}, {n} skills")


def test_the_plugin_command_exists_and_is_runnable():
    """The manifest's server command is the whole plugin: if it is wrong, an install
    yields a plugin that loads dead. It used to point at ${REPO_DIR}, a variable
    Claude Code does not define, inside a .venv the manifest could not create —
    `install` is not a field in the schema, so those steps never ran."""
    import json as _json
    plugin_path = os.path.join(ROOT, ".claude-plugin", "plugin.json")
    if not os.path.exists(plugin_path):
        print("  plugin.json absent — nothing to check")
        return
    p = _json.load(open(plugin_path, encoding="utf-8"))
    servers = p.get("mcpServers") or {}
    check(isinstance(servers, dict) and servers,
          f"the plugin declares no MCP server: {servers!r}")
    for name, cfg in (servers.items() if isinstance(servers, dict) else []):
        cmd = cfg.get("command", "")
        check("${REPO_DIR}" not in cmd and "${REPO_DIR}" not in str(cfg.get("cwd", "")),
              f"{name}: ${{REPO_DIR}} is not a Claude Code variable — "
              f"use ${{CLAUDE_PLUGIN_ROOT}}")
        check("${CLAUDE_PLUGIN_ROOT}" in cmd,
              f"{name}: the command must be rooted at ${{CLAUDE_PLUGIN_ROOT}}, got {cmd!r}")
        rel = cmd.replace("${CLAUDE_PLUGIN_ROOT}/", "")
        target = os.path.join(ROOT, rel)
        check(os.path.exists(target), f"{name}: command {rel} does not exist in the repo")
        check(os.access(target, os.X_OK),
              f"{name}: command {rel} is not executable — git keeps the bit, chmod +x it")
    print("  plugin command: rooted at CLAUDE_PLUGIN_ROOT, present and executable")


def test_every_tool_declares_what_it_does_to_the_world():
    """`readOnlyHint: true` is what lets a host run a tool without asking.

    Before these annotations every tool prompted alike, so confirming
    list_accounts looked exactly like confirming transfer — which is how a person
    learns to click «allow» without reading, on the one call that moves money. The
    annotations are also a pass/fail item in Anthropic's review criteria, along
    with a title on every tool and a name of 64 characters or less.

    Asserted against the LIVE registry, which is what a connected agent receives —
    not against the table in the source, which would just be checking that a dict
    equals itself."""
    tools = registered_tools()

    # Written out here as the SPEC, not derived from TOOL_KINDS — generated from
    # the same place, the test would prove nothing.
    #
    # MONEY debits an account. Only these three force a confirmation dialog: by the
    # repo owner's rule a booking that expires by itself, a cart line, a chat
    # message and an SMS are all recoverable, and a payment is not.
    MONEY = {"transfer", "transfer_requisites", "grocery_checkout", "ticket_pay",
             "pay_bill"}
    # WRITE changes something that costs nothing. They must NOT claim to be
    # read-only — `readOnlyHint: true` states that a tool does not modify its
    # environment, and every one of these does.
    WRITES = {
        "cinema_book", "ticket_cancel",                      # order, expires unpaid
        "grocery_order_cancel",                              # cancel, refund comes back
        "grocery_add_to_cart", "grocery_set_cart",           # cart contents
        "messenger_send",                                    # a message to a person
        "login", "confirm_otp", "confirm_password",          # sends an SMS / auth state
        "confirm_pin", "refresh_session",                    # rotates a live credential
        "payment_receipt",                                   # writes a local file
        "calendar_respond",                                  # answer everyone on the meeting sees
        "calendar_cancel",                                   # cancels a meeting for its participants
        "parking_book",                                      # takes a parking place for a day
    }

    for name, tool in sorted(tools.items()):
        ann = getattr(tool, "annotations", None)
        check(ann is not None, f"{name}: no annotations — the host must assume the worst")
        if ann is None:
            continue
        check(bool(ann.title), f"{name}: no title (review criteria require one)")
        check(len(name) <= 64, f"{name}: tool names must be 64 characters or fewer")
        check(ann.openWorldHint is True,
              f"{name}: every tool here talks to the bank — openWorldHint must be set")
        if name in MONEY:
            check(ann.readOnlyHint is not True,
                  f"{name} moves money and is marked read-only — it may run WITHOUT asking")
        elif name in WRITES:
            check(ann.readOnlyHint is not True,
                  f"{name} modifies something, so it must not claim readOnlyHint")
            check(ann.destructiveHint is False,
                  f"{name} costs nothing and must not carry the destructive flag — "
                  f"only payments confirm")
        else:
            check(ann.readOnlyHint is True,
                  f"{name} does not claim to be read-only, so a host will prompt for "
                  f"it — either it writes something (add it to WRITES) or its kind "
                  f"in TOOL_KINDS is wrong")

    # The three that confirm, and nothing else. A fourth would be friction on a
    # tool that costs nothing; a third missing would be a silent payment.
    confirming = {n for n, t in tools.items() if t.annotations.destructiveHint}
    check(confirming == MONEY,
          f"exactly the money tools must carry destructiveHint, got {sorted(confirming)}")

    # Money must be the loudest signal available, not merely "not read-only".
    for name in sorted(MONEY):
        check(tools[name].annotations.destructiveHint is True,
              f"{name} moves real money and must be marked destructive")
        check(tools[name].annotations.idempotentHint is False,
              f"{name} must not be advertised as safe to retry")

    missing = sorted(set(tools) - set(server.TOOL_KINDS))
    check(not missing, f"tools with no entry in TOOL_KINDS: {missing}")
    stale = sorted(set(server.TOOL_KINDS) - set(tools))
    check(not stale, f"TOOL_KINDS names tools that no longer exist: {stale}")

    auto = sum(1 for t in tools.values() if t.annotations.readOnlyHint)
    print(f"  annotations: {auto} read-only, {len(tools) - auto - len(MONEY)} write "
          f"nothing that costs money, {len(MONEY)} confirm")


def test_a_new_tool_cannot_ship_unclassified():
    """The failure this guards against is silent: a tool added without an entry
    would default to «prompts for everything», which looks harmless and trains the
    same click-through reflex the annotations exist to prevent. So it raises at
    import instead."""
    try:
        server._annotations_for("a_tool_nobody_classified")
        check(False, "an unclassified tool was annotated instead of refused")
    except RuntimeError as e:
        check("TOOL_KINDS" in str(e),
              f"the refusal must name the table to edit: {e}")
        check("READ" in str(e) and "MONEY" in str(e),
              f"the refusal must say what the choices mean: {e}")
    print("  annotations: an unclassified tool is refused at import, not defaulted")


def test_the_documented_counts_match_the_registry():
    """Every count printed in a doc is derived here from the LIVE registry.

    These numbers were hand-maintained and drifted three times: README said 59 tools
    and «3 debit an account» while the table held 61 and four, and FLOWS said 62. A
    reader has no way to tell a stale count from a real one, and `pay_bill` was
    missing from the money list — the one enumeration where an omission matters."""
    kinds = server.TOOL_KINDS
    counts = Counter(kind for _, kind in kinds.values())
    live = len(tool_names())

    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    flows = open(os.path.join(ROOT, "docs", "FLOWS.md"), encoding="utf-8").read()

    claims = [
        ("README **N tools**", readme, r"\*\*(\d+) tools\*\*", live),
        ("FLOWS **N MCP tools**", flows, r"\*\*(\d+) MCP tools\*\*", live),
        ("README readOnlyHint count", readme,
         r"(\d+) are `readOnlyHint: true`", counts[server.READ]),
        ("README write count", readme, r"(\d+) write something", counts[server.WRITE]),
        ("README money count", readme, r"(\d+) debit an account", counts[server.MONEY]),
    ]
    for label, blob, pattern, expected in claims:
        found = re.search(pattern, blob)
        if not found:
            check(False, f"{label}: the claim is gone — the ratchet cannot check it")
            continue
        check(int(found.group(1)) == expected,
              f"{label}: doc says {found.group(1)}, registry says {expected}")

    # The money enumeration is the one place an omission is dangerous: a reader
    # takes the list as exhaustive and assumes anything unlisted cannot spend.
    #
    # Checked in THREE places, not one. README was ratcheted; FLOWS and the router
    # skill were not — and both drifted to naming three of the five, which is worse
    # than naming none: `flows()` with no argument is what an agent reads to orient
    # itself, and its list is the one it will trust. `pay_bill` and
    # `transfer_requisites` were the two missing, and both debit an account.
    money = sorted(n for n, (_, k) in kinds.items() if k == server.MONEY)
    #
    # For FLOWS the check is against what `flows("")` RETURNS, not against the file.
    # A whole-file search passes as soon as the tool is named anywhere — and it was,
    # in its own section — while the Notes list that flows() actually serves still
    # named three of five. The artifact an agent reads is the one to assert on.
    router = os.path.join(ROOT, "skills", "tbank", "SKILL.md")
    router_text = open(router, encoding="utf-8").read() if os.path.exists(router) else ""
    served = server.flows("")
    for label, blob in (("README", readme),
                        ("what flows() serves with no argument", served),
                        ("the tbank router skill", router_text)):
        unlisted = [n for n in money if f"`{n}`" not in blob]
        check(not unlisted,
              f"money tools missing from the enumeration in {label}: "
              f"{', '.join(unlisted)}")
    print(f"  counts: {live} tools = {counts[server.READ]} read / "
          f"{counts[server.WRITE]} write / {counts[server.MONEY]} money, "
          f"all {len(money)} money tools named")


# Claims the captures disprove. Each one was stated as fact in several files at
# once, which is how a wrong reading survives: you fix the docstring, the skill
# still teaches it. Regexes are narrow on purpose — the underlying error codes are
# real and may be quoted as history; what is banned is asserting them as the reason.
RETIRED_CLAIMS = [
    (r"orderId alone still answers",
     "«orderId alone answers Success and does nothing» — no such response exists in "
     "any capture; a refusal comes back as status=Failed with a code"),
    (r'answers 200 \{"status":"Success"\} and\s+does nothing',
     "same claim in the client docstring"),
    (r"хост отвечает 200 «Success»",
     "same claim in the ticket_cancel docstring"),
    (r"Без `paymentId` хост отвечает",
     "same claim in the tickets skill"),
    (r"With it missing the host still answers",
     "same claim in FLOWS"),
    (r"500s in `?captures\.xml",
     "captures.xml holds no cancel requests at all — the 500s were in the other two"),
    (r"тот отвечает INSUFFICIENT_PRIVILEGES\s+даже на CLIENT",
     "travel_link_auth_token answers 200 in captures-gorod.xml; the earlier refusal "
     "was a lapsed session, not a permission wall"),
    (r"INSUFFICIENT_PRIVILEGES`?,? even on a\s+CLIENT",
     "the English half of the same claim, which lived on in FLOWS §9 for a month "
     "after the Russian one was retired — the tool's own message says the endpoint "
     "is simply not wired up yet, not that the bank refuses"),
    (r"link-token minted outside this host",
     "same paragraph, same wrong diagnosis: the flight order needs a web session "
     "layered over the mobile one (the bridge IS in the capture), not a link-token"),
]


def test_disproven_claims_do_not_come_back():
    """A wrong reading that lived in five files is easy to reintroduce in one.

    This is the one test in the suite that searches SOURCE TEXT, and it stays that
    way deliberately. The thing under test is not behaviour — it is a sentence
    asserting something the captures disprove, sitting in a file an agent will read
    and act on. There is no code path to execute; the defect is that the words
    exist. Everywhere else in this repo, asserting on text would be a substitute for
    running the code, and is banned.

    doc_files() ALREADY returns every skills/*/SKILL.md, and a second loop appended
    them again: the list held 34 entries for 23 distinct files, every skill scanned
    twice and the success line overstating its coverage by 48%. Deduped, and the
    count now reports distinct files."""
    scanned = set(doc_files())
    scanned |= {os.path.join(ROOT, "src", f)
                for f in os.listdir(os.path.join(ROOT, "src")) if f.endswith(".py")}
    scanned = sorted(scanned)
    for pattern, why in RETIRED_CLAIMS:
        hits = [os.path.relpath(p, ROOT) for p in scanned
                if re.search(pattern, open(p, encoding="utf-8").read())]
        check(not hits, f"retired claim is back in {', '.join(hits)}: {why}")

    # The scan is only worth its exemption if it covers what it claims to. A skill
    # or a doc that stopped being scanned would silently narrow it.
    import glob
    for required in (glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))
                     + [os.path.join(ROOT, "docs", "FLOWS.md"),
                        os.path.join(ROOT, "README.md"),
                        os.path.join(ROOT, "src", "client.py"),
                        os.path.join(ROOT, "src", "server.py")]):
        check(required in scanned,
              f"{os.path.relpath(required, ROOT)} is no longer scanned for retired "
              f"claims — the exemption this test has is for full coverage")
    print(f"  {len(RETIRED_CLAIMS)} disproven claims stay retired across "
          f"{len(scanned)} distinct files")


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
    """A skill on disk but absent from the plugin ships to nobody — that is how the
    tickets skill was invisible to plugin installs.

    The manifest no longer lists skills: Claude Code scans skills/ by default, so
    every directory with a SKILL.md ships automatically and the hand-kept list was
    only a chance to forget one. What has to hold now is that nothing REPLACES that
    default — a `skills` override narrows the scan back down to whatever it names."""
    import glob
    import json as _json
    manifest = os.path.join(ROOT, ".claude-plugin", "plugin.json")
    check(os.path.exists(manifest), ".claude-plugin/plugin.json is missing")
    if not os.path.exists(manifest):
        return
    on_disk = {"skills/" + os.path.basename(os.path.dirname(p))
               for p in glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))}
    listed = _json.load(open(manifest, encoding="utf-8")).get("skills")
    if listed is None:
        print(f"  plugin ships all {len(on_disk)} skills (default skills/ scan)")
        return
    names = {listed} if isinstance(listed, str) else set(listed)
    normalised = {n.strip("./").rstrip("/") for n in names}
    check(on_disk - normalised == set(),
          f"skills on disk but not shipped: {sorted(on_disk - normalised)}")
    print(f"  plugin ships all {len(on_disk)} skills (explicit list)")


def test_flows_serves_every_section():
    """flows() must reach the WHOLE file. It used to return the first 6000 chars,
    which silently cut everything from section 5 onward."""
    sections = server._flow_sections()
    check(len(sections) >= 10,
          f"docs/FLOWS.md parsed into only {len(sections)} sections")

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
        check(hit is not None, f"docs/FLOWS.md has no section matching {fragment!r}")
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
    test_every_tool_declares_what_it_does_to_the_world()
    test_a_new_tool_cannot_ship_unclassified()
    test_the_documented_counts_match_the_registry()
    test_disproven_claims_do_not_come_back()
    test_every_tool_is_documented()
    test_every_tool_is_reachable_from_a_skill()
    test_plugin_ships_every_skill()
    test_evals_only_ask_for_tools_that_exist()
    test_the_marketplace_entry_matches_the_plugin()
    test_the_plugin_command_exists_and_is_runnable()
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
