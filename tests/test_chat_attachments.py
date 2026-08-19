"""Getting a document the bank sent into a chat.

The support chat is where the bank delivers what it cannot say in a message — a
statement, a broker report, a certificate. Until messenger_file() existed the
answer to «пришлите выписку» arrived and the agent could not even see it:
messenger_messages printed the bare word «[file]» and dropped content.fileId, the
only handle the download route takes.

The tool's job ends at the bytes: fetch them, put them on disk under a name that
opens, and say where. Reading them is the agent's job with the agent's own tools —
the file lands on the same machine it works on. These tests pin that contract: the
id reaching the listing, the error that arrives dressed as a file, the name that
must not collide or lose its extension, and the fact that the tool does not
paraphrase what it downloaded.

Everything runs against bytes built here; no live call, no network.

    python3 tests/test_chat_attachments.py
"""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the tool's default save directory at a temp dir BEFORE src.server reads it.
_TMP = tempfile.mkdtemp(prefix="tbank-attach-")
os.environ["TBANK_CHAT_FILES"] = os.path.join(_TMP, "chat-files")

# Every log the server writes resolves its path at IMPORT time. run_all.py
# redirects them per process; a STANDALONE run of this file did not, so its
# synthetic calls landed in the user's live ~/.local/share/tbank-mcp — the very
# files debug_report()/diagnostics() read back as real agent behaviour.
_LOGS = tempfile.mkdtemp(prefix="tbank-test-logs-")
os.environ.setdefault("TBANK_TRACE_FILE", os.path.join(_LOGS, "calls.jsonl"))
os.environ.setdefault("TBANK_EVENTS", os.path.join(_LOGS, "events.jsonl"))
os.environ.setdefault("TBANK_ATTEMPTS", os.path.join(_LOGS, "attempts.jsonl"))

from src import server  # noqa: E402
from src.client import MobileSession, TbankApiError, SessionExpired  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ---------------------------------------------------------------- fixtures

# A real .xlsx starts like this; the tool never looks further than the bytes it
# writes, so a plausible prefix is the whole fixture it needs.
XLSX = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 64
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"

AUTH_ENVELOPE = (b'[{"errorId":"deadbeef","errorCode":"AUTH_REQUIRED",'
                 b'"errorMessage":"Token inactive"}]')


class Resp:
    """The bits of a requests.Response the download path touches."""

    def __init__(self, content=b"", headers=None, status=200):
        self.content = content
        self.headers = headers or {}
        self.status_code = status
        self.text = ""


class FileSession(MobileSession):
    """A session whose messenger_file answers with canned bytes AND the name the
    server states for them — the pair the real client returns."""

    def __init__(self, blob, name=""):
        self.mobile_sessionid = "sid"
        self.access_token = "tok"
        self._blob = blob
        self._name = name

    def ensure_fresh(self, *a, **kw):
        return None

    def messenger_file(self, conversation_id, file_id):
        return self._blob, self._name


def run(tool, session, *args, **kw):
    saved = server._require
    server._require = lambda: session
    try:
        return tool(*args, **kw)
    finally:
        server._require = saved


def saved_path(out):
    line = [l for l in out.splitlines() if l.startswith("Сохранён:")]
    return line[0].split("Сохранён:", 1)[1].split("(0600)")[0].strip() if line else ""


# ---------------------------------------------------------------- the listing

def test_the_listing_shows_the_file_id_it_must_be_fetched_by():
    """«[file]» told the agent a document existed and nothing else. The fileId is
    an ARGUMENT, so it must survive whole — max_chars caps the prose, never it."""
    msgs = [{
        "id": "m1", "messageType": "file", "timestamp": "2026-08-04T13:13:00.350Z",
        "author": {"name": "Михаил", "role": "manager"},
        "content": {"fileId": "Ab000000000Cd000000000Ef000000000Gh",
                    "fileName": "Выписка_400000000001.xlsx", "fileSize": 68749},
    }]

    class S(FileSession):
        def messenger_messages(self, *a, **kw):
            return msgs

    out = run(server.messenger_messages, S(b""), "CONV1", max_chars=10)
    check("Ab000000000Cd000000000Ef000000000Gh" in out,
          f"fileId missing or cut from the message listing: {out!r}")
    check("Выписка_400000000001.xlsx" in out, f"fileName not shown: {out!r}")
    check("messenger_file" in out, f"listing does not name the tool that fetches it: {out!r}")
    check("[file]" not in out, f"still rendering the bare placeholder: {out!r}")


def test_a_chat_time_is_the_time_the_app_shows():
    """`timestamp[:16]` kept the digits and dropped the `Z`. The bank sends UTC and
    the app renders MSK, so every chat line was three hours early — and a message
    sent between 00:00 and 03:00 Moscow was listed under the previous DAY. The repo
    already fixed exactly this for millisecond timestamps (_msk); the messenger's
    ISO strings never got it."""
    msgs = [{"id": "m1", "messageType": "text", "timestamp": "2026-08-04T23:30:00.000Z",
             "author": {"name": "Михаил", "role": "manager"},
             "content": {"text": "ночное"}},
            {"id": "m2", "messageType": "text", "timestamp": "2026-08-04T13:13:00.350Z",
             "author": {"name": "Михаил", "role": "manager"},
             "content": {"text": "дневное"}}]

    class S(FileSession):
        def messenger_messages(self, *a, **kw):
            return msgs

    out = run(server.messenger_messages, S(b""), "CONV1", max_chars=0)
    check("2026-08-05 02:30" in out,
          f"a 23:30 UTC message must be 02:30 next-day MSK: {out!r}")
    check("2026-08-04 16:13" in out, f"13:13 UTC must be 16:13 MSK: {out!r}")
    check("2026-08-04 23:30" not in out, "the UTC digits must not be shown as local")


def test_a_file_with_a_caption_keeps_both_the_caption_and_the_id():
    msgs = [{
        "id": "m1", "messageType": "file", "timestamp": "2026-08-04T13:13:00.350Z",
        "author": {"name": "Михаил", "role": "manager"},
        "content": {"fileId": "F1", "fileName": "a.pdf", "fileSize": 10,
                    "text": "Вот ваша выписка"},
    }]

    class S(FileSession):
        def messenger_messages(self, *a, **kw):
            return msgs

    out = run(server.messenger_messages, S(b""), "CONV1", max_chars=0)
    check("Вот ваша выписка" in out, f"caption lost: {out!r}")
    check("file_id=F1" in out, f"id lost when the message also had text: {out!r}")


def test_a_size_the_bank_sent_as_a_string_does_not_take_down_the_chat():
    """`"68749" / 1024` raises inside the f-string, and _err turns that into an
    error for the WHOLE listing — one attachment hiding every message."""
    msgs = [{"id": "m1", "messageType": "file", "timestamp": "2026-08-04T13:13:00.350Z",
             "author": {"name": "Михаил", "role": "manager"},
             "content": {"fileId": "F1", "fileName": "a.pdf", "fileSize": "68749"}},
            {"id": "m2", "messageType": "text", "timestamp": "2026-08-04T13:14:00.350Z",
             "author": {"name": "Михаил", "role": "manager"},
             "content": {"text": "второе сообщение"}}]

    class S(FileSession):
        def messenger_messages(self, *a, **kw):
            return msgs

    out = run(server.messenger_messages, S(b""), "CONV1", max_chars=0)
    check("второе сообщение" in out, f"the listing died on one attachment: {out!r}")
    check("file_id=F1" in out, f"the attachment row is missing: {out!r}")
    # …and a sub-kilobyte file is not «0 КБ», which reads as empty.
    msgs[0]["content"]["fileSize"] = 400
    out = run(server.messenger_messages, S(b""), "CONV1", max_chars=0)
    check("0 КБ" not in out, f"a 400-byte file printed as 0 КБ: {out!r}")


# ------------------------------------------------------- the download contract

def test_an_error_envelope_is_never_saved_as_a_file():
    """The messenger reports a dead token as HTTP 200 with a JSON body. Returned
    unchecked, those 119 bytes get written to disk and announced as the document —
    a file that opens as garbage, with nothing to retry."""
    calls = []

    class S(MobileSession):
        def __init__(self):
            self.mobile_sessionid = "sid"
            self.access_token = "tok"
            self.tmsg_session_id = "jwt"

        def _call_read(self, key, **kw):
            calls.append(kw.get("path_override"))
            return Resp(AUTH_ENVELOPE)

        def _ensure_tmsg(self):
            self.tmsg_session_id = "jwt2"

    try:
        S().messenger_file("CONV1", "FILE1")
        failures.append("an AUTH_REQUIRED envelope was returned as file bytes")
    except SessionExpired:
        pass
    except Exception as e:                      # noqa: BLE001 - report what it did raise
        failures.append(f"expected SessionExpired, got {type(e).__name__}: {e}")
    check(len(calls) == 2,
          f"an auth failure must earn exactly one re-mint and retry, got {len(calls)} calls")
    check(all("/files/FILE1" in (p or "") for p in calls),
          f"the download path is wrong: {calls}")


def test_a_non_auth_error_envelope_raises_instead_of_being_read_as_a_document():
    class S(MobileSession):
        def __init__(self):
            self.mobile_sessionid = "sid"
            self.access_token = "tok"

        def _call_read(self, key, **kw):
            return Resp(b'[{"errorCode":"CONVERSATION_NOT_FOUND",'
                        b'"errorMessage":"no such chat"}]')

    try:
        S().messenger_file("CONV1", "FILE1")
        failures.append("a CONVERSATION_NOT_FOUND envelope was returned as file bytes")
    except TbankApiError as e:
        check("CONVERSATION_NOT_FOUND" in str(e), f"error code lost: {e}")


def test_a_path_breaking_id_never_reaches_a_url():
    class S(MobileSession):
        def __init__(self):
            self.mobile_sessionid = "sid"
            self.access_token = "tok"

        def _call_read(self, key, **kw):
            failures.append(f"a rejected id still built a request: {kw}")
            return Resp()

    for bad in ("../../etc/passwd", "a/b", "a?x=1", "a#b", ""):
        try:
            S().messenger_file("CONV1", bad)
            failures.append(f"file_id {bad!r} was accepted")
        except TbankApiError as e:
            check("BAD_ID" in str(e) or "недопустимые" in str(e),
                  f"file_id {bad!r} rejected for the wrong reason: {e}")


def test_a_real_id_with_base64_padding_is_accepted():
    seen = []

    class S(MobileSession):
        def __init__(self):
            self.mobile_sessionid = "sid"
            self.access_token = "tok"

        def _call_read(self, key, **kw):
            seen.append(kw.get("path_override"))
            return Resp(XLSX)

    S().messenger_file("CONV1", "A00000000000000000=")
    check(seen and seen[0].endswith("/files/A00000000000000000="),
          f"a padded fileId did not reach the path: {seen}")


def test_the_name_comes_from_the_response_not_from_the_caller():
    """The message record has a fileName, but making the agent copy it back in is a
    round trip through the model for a value the server states itself — and states
    twice. Verified against the live headers: percent-encoded in
    Content-Disposition, and exact bytes in x-amz-meta-filename-base64."""
    from src.client import _response_filename as fn
    live = {"content-disposition":
            'attachment; filename="Otchet_%D0%BE%D1%82%D1%87%D1%91%D1%82_400000000001.xlsx"',
            "x-amz-meta-filename-base64": "0JLRi9C/0LjRgdC60LAueGxzeA=="}
    check(fn(live) == "Выписка.xlsx", f"the base64 header must win: {fn(live)!r}")
    check(fn({k: v for k, v in live.items() if k == "content-disposition"})
          == "Otchet_отчёт_400000000001.xlsx",
          "a percent-encoded Content-Disposition must be decoded")
    check(fn({"Content-Disposition": "attachment; filename*=UTF-8''%D0%90.pdf"}) == "А.pdf",
          "RFC 5987 filename* must be read")
    check(fn({"Content-Disposition": 'attachment; filename="plain name.pdf"'})
          == "plain name.pdf", "an unencoded name must survive unchanged")
    check(fn({}) == "" and fn(None) == "", "no header ⇒ no name, not a crash")
    check(fn({"x-amz-meta-filename-base64": "not base64 at all!!"}) != "!!",
          "undecodable base64 must fall through, not explode")


def test_a_server_supplied_name_is_scrubbed_like_any_other():
    """The name is now the server's, which makes it bank text — not trusted text.
    A Content-Disposition of «../../.ssh/authorized_keys» must land in chat-files
    like anything else."""
    out = run(server.messenger_file, FileSession(XLSX, "../../../../tmp/evil.xlsx"),
              "CONV1", "FILEEVIL")
    path = saved_path(out)
    root = os.path.realpath(os.environ["TBANK_CHAT_FILES"])
    check(os.path.realpath(path).startswith(root + os.sep),
          f"a server-supplied name escaped the directory: {path}")
    check(not os.path.exists("/tmp/evil.xlsx"), "traversal actually wrote outside")


def test_the_endpoint_template_matches_what_the_host_answers():
    """Probed live: the route is a GET on the messenger host that returns raw bytes,
    authorised by the tmsgSessionID cookie alone, and it takes none of the base
    query params (the app sends none there — sessionid is the HMAC key for /v1/pay
    and has no business in this URL)."""
    from src.endpoints import BUILTIN_ENDPOINTS
    tpl = BUILTIN_ENDPOINTS.get("messenger_file") or {}
    check(tpl.get("method") == "GET", f"method: {tpl.get('method')}")
    check(tpl.get("host") == "https://tm.t-bank-app.ru", f"host: {tpl.get('host')}")
    check(tpl.get("raw") is True, "raw=True is what keeps _unwrap off the bytes")
    check(tpl.get("no_base_params") is True,
          "the messenger gets no sessionid/deviceId in the query")
    check(tpl.get("no_bearer") is True, "the app sends no Bearer to this host")


# ------------------------------------------------------------- the tool

def test_the_tool_hands_over_a_file_and_does_not_paraphrase_it():
    """The whole point of the redesign: the bytes land on disk untouched and the
    agent reads them with its own tools. A tool that also renders the document
    duplicates the reader, and that duplicate is where the bugs were."""
    out = run(server.messenger_file, FileSession(XLSX, "выписка.xlsx"), "CONV1", "FILE1")
    path = saved_path(out)
    check(path.endswith("выписка.xlsx"), f"saved under the wrong name: {out!r}")
    check(os.path.exists(path), f"file not saved at {path}")
    if os.path.exists(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check(mode == 0o600, f"saved with mode {oct(mode)}, must be 0600")
        check(open(path, "rb").read() == XLSX, "saved bytes differ from what was downloaded")
    check(str(len(XLSX)) in out, f"the size is not reported: {out!r}")
    # No rendering, no fence, no excerpt of the document.
    check("СОДЕРЖИМОЕ" not in out and "===" not in out,
          f"the tool is rendering the document again: {out!r}")
    check(len(out.splitlines()) <= 3, f"the answer grew a body: {out!r}")


def test_the_first_line_carries_no_filename():
    """trace.py stores the first line of every answer. A bank-chosen filename can
    carry a surname or an account number, so the path belongs on line two."""
    out = run(server.messenger_file,
              FileSession(XLSX, "Выписка_Иванов_0000000000.xlsx"),
              "CONV1", "FILE2")
    check("Иванов" not in out.splitlines()[0],
          f"the traced first line names the file: {out.splitlines()[0]!r}")
    check("Иванов" in out, "the path must still be reported, just not first")


def test_a_hostile_file_name_cannot_choose_the_path():
    out = run(server.messenger_file, FileSession(XLSX, "../../../../tmp/pwned.xlsx"),
              "CONV1", "FILE3")
    path = saved_path(out)
    check(bool(path), f"nothing was saved: {out!r}")
    if path:
        root = os.path.realpath(os.environ["TBANK_CHAT_FILES"])
        check(os.path.realpath(path).startswith(root + os.sep),
              f"a file name escaped the chat-files directory: {path}")
    check(not os.path.exists("/tmp/pwned.xlsx"), "traversal actually wrote outside")


def test_a_long_bank_name_keeps_its_extension_and_says_it_was_shortened():
    """«Справка … 04 августа 2026 года.pdf» cut at 100 CHARACTERS became a `.pd`
    file — a document the user is pointed at and cannot open — and the cut was
    never mentioned. ext4 measures the limit in BYTES, and Cyrillic costs two."""
    long_name = ("Справка о состоянии вклада Иванова Ивана Ивановича по договору "
                 "0000000000 на 04 августа 2026 года, сформирована по обращению "
                 "клиента в чате поддержки, страница первая.pdf")
    check(len(long_name.encode()) > 200, "fixture must exceed the byte budget")
    out = run(server.messenger_file, FileSession(PDF, long_name), "CONV1", "FILE6")
    path = saved_path(out)
    check(path.endswith(".pdf"), f"the extension was cut off: {path}")
    check(len(os.path.basename(path).encode()) <= 255,
          f"the name exceeds the filesystem limit: {len(os.path.basename(path).encode())} байт")
    check("укорочено" in out or "длиннее" in out,
          f"a shortened name must be announced: {out!r}")
    check(long_name in out, "the answer must still carry the bank's full name")
    check(os.path.exists(path), f"the announced path does not exist: {path}")


def test_two_documents_whose_names_differ_late_do_not_share_one_file():
    """«…часть 1 из 3.pdf» and «…часть 2 из 3.pdf» differ past the cut. Both used
    to land on ONE path: part 2 was refused as «уже существует», naming a file that
    holds part 1 — or with overwrite=True it destroyed it."""
    base = ("Справка по счёту 0000000000 Иванова Ивана Ивановича сформирована "
            "04 августа 2026 года по запросу клиента, часть ")
    paths = []
    for n, fid in ((1, "FILEP1"), (2, "FILEP2")):
        out = run(server.messenger_file,
                  FileSession(PDF + f"часть {n}".encode(), f"{base}{n} из 3.pdf"),
                  "CONV1", fid)
        path = saved_path(out)
        check(bool(path), f"часть {n} not saved: {out!r}")
        if path:
            paths.append(path)
    check(len(set(paths)) == 2, f"two documents collided onto one path: {paths}")
    for n, p in enumerate(paths, 1):
        check(open(p, "rb").read().endswith(f"часть {n}".encode()),
              f"часть {n} holds the wrong bytes: {p}")


def test_the_fallback_name_cannot_collide_either():
    """Without file_name the leaf came from file_id cut to 32 characters — and a
    real id is 40 characters of near-constant filler, so two attachments from one
    chat landed on one path."""
    paths = []
    for fid, blob in (("A" * 38 + "1A", XLSX), ("A" * 38 + "2A", PDF)):
        out = run(server.messenger_file, FileSession(blob), "CONV1", fid)
        path = saved_path(out)
        check(bool(path), f"{fid[:6]}… not saved: {out!r}")
        if path:
            paths.append(path)
    check(len(set(paths)) == 2, f"two file ids collided onto one path: {paths}")


def test_an_existing_file_is_not_silently_overwritten():
    args = ("CONV1", "FILE4")
    first = run(server.messenger_file, FileSession(XLSX, "dup.xlsx"), *args)
    path = saved_path(first)
    second = run(server.messenger_file, FileSession(PDF, "dup.xlsx"), *args)
    check("НЕ сохранён" in second, f"second write did not stop: {second!r}")
    check("Сохранён:" not in second,
          f"a refused write must not report a path as saved: {second!r}")
    check(open(path, "rb").read() == XLSX, "the first file was clobbered anyway")
    third = run(server.messenger_file, FileSession(PDF, "dup.xlsx"), *args,
                overwrite=True)
    check(saved_path(third) == path, f"overwrite=True did not save: {third!r}")
    check(open(path, "rb").read() == PDF, "overwrite=True did not replace the bytes")


def test_any_format_is_just_a_file():
    """No sniffing, no per-format branch: an image, a zip and an unknown blob all
    take the same path through the tool."""
    for name, blob in (("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40),
                       ("arch.zip", b"PK\x03\x04zip"),
                       ("weird.bin", b"\x00\x01\x02\x03")):
        out = run(server.messenger_file, FileSession(blob, name), "CONV1", f"FID{name}")
        path = saved_path(out)
        check(path.endswith(name), f"{name}: saved as {path}")
        check(os.path.exists(path) and open(path, "rb").read() == blob,
              f"{name}: bytes differ on disk")
        check("СОДЕРЖИМОЕ" not in out, f"{name}: the tool tried to render it")


def main():
    test_the_listing_shows_the_file_id_it_must_be_fetched_by()
    test_a_chat_time_is_the_time_the_app_shows()
    test_a_file_with_a_caption_keeps_both_the_caption_and_the_id()
    test_a_size_the_bank_sent_as_a_string_does_not_take_down_the_chat()
    test_an_error_envelope_is_never_saved_as_a_file()
    test_a_non_auth_error_envelope_raises_instead_of_being_read_as_a_document()
    test_a_path_breaking_id_never_reaches_a_url()
    test_a_real_id_with_base64_padding_is_accepted()
    test_the_name_comes_from_the_response_not_from_the_caller()
    test_a_server_supplied_name_is_scrubbed_like_any_other()
    test_the_endpoint_template_matches_what_the_host_answers()
    test_the_tool_hands_over_a_file_and_does_not_paraphrase_it()
    test_the_first_line_carries_no_filename()
    test_a_hostile_file_name_cannot_choose_the_path()
    test_a_long_bank_name_keeps_its_extension_and_says_it_was_shortened()
    test_two_documents_whose_names_differ_late_do_not_share_one_file()
    test_the_fallback_name_cannot_collide_either()
    test_an_existing_file_is_not_silently_overwritten()
    test_any_format_is_just_a_file()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
