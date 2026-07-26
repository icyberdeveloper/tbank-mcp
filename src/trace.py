"""Every tool call, recorded — so it can be seen HOW an agent uses this MCP.

`observability.py` already logs the checkout/payment path step by step. That answers
"what did this payment do". It cannot answer the question this module exists for:
*which tools does an agent reach for, in what order, what does it get back, and where
does it get stuck* — because the tools it never reached, the arguments it guessed
wrong and the refusals it read and retried leave no trace anywhere.

WHAT IS RECORDED, per call: the tool name, its arguments (redacted, see below), how
long it took, whether the error path was taken, how big the answer was, and the FIRST
LINE of that answer. That last field is the important one: the tools return strings,
and a refusal — «NO_STORE_CONTEXT», «Неизвестное поле сортировки», «Перевод НЕ
выполнен» — is a perfectly ordinary return value. The first line is what the agent
actually read, and grouping by it later shows which messages agents run into.

WHAT IS **NOT** CLASSIFIED HERE. It would be easy to label each call ok / empty /
refused by matching the answer against a table of strings. That table would rot the
first time a message is reworded, and it would rot silently, turning a real failure
into a green count. So this module records faithfully and `report()` groups at read
time — the messages come out of the data, not out of a list somebody maintained.

SAFETY. Values go through observability._redact_value (by key name and by value
pattern), then are truncated. Arguments that are free text a person wrote or a
secret — a messenger message, a transfer description, a password, an OTP — are never
stored at all, only their length. The full argument tuple is hashed so repeated
identical calls can be detected without keeping what was in them.

ON BY DEFAULT, because a debugging aid that has to be switched on before the
interesting thing happens is not one. `TBANK_TRACE=0` turns it off; the file is
capped and rotated so it cannot grow without bound.

    ~/.local/share/tbank-mcp/calls.jsonl     (TBANK_TRACE_FILE overrides)
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import re
import time
import uuid

from .observability import _is_sensitive_key, _redact_value, redact_text

TRACE_FILE = os.environ.get(
    "TBANK_TRACE_FILE",
    os.path.expanduser("~/.local/share/tbank-mcp/calls.jsonl"),
)
MAX_BYTES = int(os.environ.get("TBANK_TRACE_MAX_BYTES", 5 * 1024 * 1024))

# Arguments never stored, only measured. Free text a human wrote (a chat message, a
# transfer note) is theirs, and a credential is a credential — neither tells us
# anything about how the agent USES the tool, which is the whole point here.
_OPAQUE_ARGS = {"text", "description", "password", "pin", "otp", "code", "body",
                "save_to"}

# Tools whose ANSWER contains text a person wrote — the message just sent, the chat
# history, the preview of the last message in each conversation. Blanking the
# argument is not enough when the tool echoes it straight back: messenger_send
# returns «Отправлено в чат …: «<the whole message>»». For these the first line is
# replaced by its length — unless the call FAILED, in which case the first line is an
# error string from _err(), which is already redacted and is the thing worth keeping.
_ECHOES_USER_TEXT = {"messenger_send", "messenger_messages", "messenger_conversations"}

_MAX_ARG = 64
_HEAD = 160

# Long digit runs — account, card, order and payment ids — are replaced in the stored
# first line. Two reasons, and they point the same way: this file is meant to be as
# shareable as events.jsonl, which promises never to carry account numbers; and the
# report GROUPS by that line, so a per-account id turns one recurring message into a
# crowd of singletons. Short numbers («229 всего, показано 50») survive.
_RE_LONG_ID = re.compile(r"\d{4,}")

# One id per server process. An MCP server is started per agent session, so this is
# the closest thing to "one agent's run" that exists without inventing a protocol.
RUN_ID = uuid.uuid4().hex[:8]
_seq = 0

# Set by server._err() on the way out, so "this call failed" is a FACT reported by
# the error path itself rather than a guess made by matching the returned string.
_last_error: dict = {}


def enabled() -> bool:
    return os.environ.get("TBANK_TRACE", "1") not in ("0", "false", "no", "")


def note_error(exc: BaseException) -> None:
    """Called from the one place every tool funnels its failures through."""
    _last_error["cls"] = type(exc).__name__


def _short_args(args: dict) -> tuple[dict, str]:
    """(what is safe to store, a hash of what was really passed)."""
    canon = json.dumps({k: str(v) for k, v in sorted(args.items())},
                       ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]
    out: dict = {}
    for k, v in args.items():
        if k in _OPAQUE_ARGS:
            out[k] = f"<{len(str(v))} chars>" if v not in ("", None) else ""
            continue
        # The key decides, not just the value. _redact_value only consults
        # _is_sensitive_key when it is handed a DICT, and this loop unpacks the
        # arguments before calling it — so the name never reached the blocklist and
        # every phone, account and card id passed as an argument was stored verbatim,
        # in the one file this module promises is safe to share (see the header).
        # The answer's first line was scrubbed by _RE_LONG_ID all along; the
        # arguments were not.
        if _is_sensitive_key(k):
            out[k] = "<redacted>"
            continue
        red = _redact_value(v)
        s = red if isinstance(red, (int, float, bool)) or red is None else str(red)
        if isinstance(s, str) and len(s) > _MAX_ARG:
            s = s[:_MAX_ARG] + "…"
        out[k] = s
    return out, digest


def _rotate() -> None:
    try:
        if os.path.exists(TRACE_FILE) and os.path.getsize(TRACE_FILE) > MAX_BYTES:
            os.replace(TRACE_FILE, TRACE_FILE + ".1")
    except OSError:
        pass


def _append(rec: dict) -> None:
    _rotate()
    os.makedirs(os.path.dirname(TRACE_FILE), exist_ok=True)
    fd = os.open(TRACE_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        os.chmod(TRACE_FILE, 0o600)
    except OSError:
        pass


def record(tool: str, args: dict, started: float, result, error: str | None) -> None:
    """Never raises: a tracer that can break the thing it traces is worse than none."""
    global _seq
    try:
        if not enabled():
            return
        _seq += 1
        text = result if isinstance(result, str) else ""
        head = ""
        if text.strip():
            if tool in _ECHOES_USER_TEXT and not error:
                head = f"<{len(text)} chars, содержимое не записывается>"
            else:
                first = redact_text(text.strip().splitlines()[0])
                head = _RE_LONG_ID.sub("#", first)[:_HEAD]
        safe, digest = _short_args(args)
        _append({
            "ts": round(started, 3), "run": RUN_ID, "seq": _seq, "tool": tool,
            "args": safe, "args_hash": digest,
            "ms": int((time.time() - started) * 1000),
            "err": error, "chars": len(text), "head": head,
        })
    except Exception:                                        # noqa: BLE001
        pass


def wrap(fn):
    """Record one call of `fn`. functools.wraps keeps __wrapped__, which is what
    inspect.signature follows — so FastMCP builds the SAME schema and description it
    would have built for the undecorated function. Pinned by a test, because a
    silently changed schema would change what every agent sees."""
    name = fn.__name__

    def _bind(a, kw) -> dict:
        try:
            import inspect
            bound = inspect.signature(fn).bind_partial(*a, **kw)
            return dict(bound.arguments)
        except (TypeError, ValueError):
            return dict(kw)

    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*a, **kw):
            started = time.time()
            _last_error.pop("cls", None)
            try:
                out = await fn(*a, **kw)
            except BaseException as e:                       # noqa: BLE001
                record(name, _bind(a, kw), started, "", type(e).__name__)
                raise
            record(name, _bind(a, kw), started, out, _last_error.pop("cls", None))
            return out
        return awrapper

    @functools.wraps(fn)
    def swrapper(*a, **kw):
        started = time.time()
        _last_error.pop("cls", None)
        try:
            out = fn(*a, **kw)
        except BaseException as e:                           # noqa: BLE001
            record(name, _bind(a, kw), started, "", type(e).__name__)
            raise
        record(name, _bind(a, kw), started, out, _last_error.pop("cls", None))
        return out

    return swrapper


# ── reading the trace back ───────────────────────────────────────────────────

def load(path: str | None = None, runs: int = 0) -> list[dict]:
    """Every recorded call, oldest first. `runs=N` keeps only the last N runs —
    a run being one server process, i.e. roughly one agent session."""
    path = path or TRACE_FILE
    rows: list[dict] = []
    for p in (path + ".1", path):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    if runs > 0:
        order, seen = [], set()
        for r in rows:                       # first-seen order, not set order
            if r.get("run") not in seen:
                seen.add(r.get("run"))
                order.append(r.get("run"))
        keep = set(order[-runs:])
        rows = [r for r in rows if r.get("run") in keep]
    return rows


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def report(rows: list[dict], top: int = 6) -> dict:
    """Turn raw calls into the four questions worth asking of them.

    Deliberately NOT a health score. Every number here points at specific rows a
    person can go and read; a single «87% healthy» would hide exactly the rare call
    that broke a checkout."""
    per: dict = {}
    heads: dict = {}
    transitions: dict = {}
    repeats: list[dict] = []
    starts: dict = {}
    by_run: dict = {}

    for r in rows:
        by_run.setdefault(r.get("run"), []).append(r)

    for run, calls in by_run.items():
        calls.sort(key=lambda x: x.get("seq", 0))
        if calls:
            starts[calls[0].get("tool")] = starts.get(calls[0].get("tool"), 0) + 1
        run_len = 1
        for i, c in enumerate(calls):
            tool = c.get("tool", "?")
            st = per.setdefault(tool, {"n": 0, "err": 0, "ms": [], "chars": 0})
            st["n"] += 1
            st["ms"].append(int(c.get("ms") or 0))
            st["chars"] += int(c.get("chars") or 0)
            if c.get("err"):
                st["err"] += 1
            if c.get("head"):
                heads.setdefault(tool, {})
                heads[tool][c["head"]] = heads[tool].get(c["head"], 0) + 1
            if i + 1 < len(calls):
                key = (tool, calls[i + 1].get("tool", "?"))
                transitions[key] = transitions.get(key, 0) + 1
            # The same tool called with the same arguments, back to back, is an
            # agent that did not understand the answer it got.
            same = (i + 1 < len(calls)
                    and calls[i + 1].get("tool") == tool
                    and calls[i + 1].get("args_hash") == c.get("args_hash"))
            if same:
                run_len += 1
            else:
                if run_len > 1:
                    repeats.append({"run": run, "tool": tool, "times": run_len,
                                    "head": c.get("head", "")})
                run_len = 1

    tools = []
    for tool, st in sorted(per.items(), key=lambda kv: -kv[1]["n"]):
        tools.append({
            "tool": tool, "n": st["n"], "err": st["err"],
            "p50_ms": _pct(st["ms"], 0.5), "p95_ms": _pct(st["ms"], 0.95),
            "avg_chars": st["chars"] // max(1, st["n"]),
            "answers": sorted(heads.get(tool, {}).items(), key=lambda kv: -kv[1])[:top],
        })
    return {
        "runs": len(by_run), "calls": len(rows), "tools": tools,
        "repeats": sorted(repeats, key=lambda x: -x["times"])[:top * 2],
        "transitions": sorted(transitions.items(), key=lambda kv: -kv[1])[:top * 2],
        "starts": sorted(starts.items(), key=lambda kv: -kv[1]),
    }
