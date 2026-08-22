"""Run every test file and report one verdict.

The tests are standalone scripts (no pytest), which is fine — but running nine of
them by hand means, in practice, running the one you just touched. This runs them
all, isolates each in its own process, and points the journal/event logs at a temp
directory so a test run never appends to the user's real diagnostics files.

    python3 tests/run_all.py            # everything
    python3 tests/run_all.py transfer   # only files whose name contains "transfer"
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(f for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py")
                   and (not pattern or pattern in f))
    if not files:
        print(f"no test files match {pattern!r}")
        return 1

    tmp = tempfile.mkdtemp(prefix="tbank-tests-")
    env = dict(os.environ)
    # Never write into ~/.local/share/tbank-mcp/ from a test run.
    env["TBANK_ATTEMPTS"] = os.path.join(tmp, "attempts.jsonl")
    env["TBANK_EVENTS"] = os.path.join(tmp, "events.jsonl")
    # The call trace is on by default, and a test run drives hundreds of tools —
    # without this the suite would bury the user's real trace under its own noise.
    env["TBANK_TRACE_FILE"] = os.path.join(tmp, "calls.jsonl")
    # messenger_file writes the attachment it downloads. Same promise as above: a
    # test run must not drop files into the user's chat-files directory.
    env["TBANK_CHAT_FILES"] = os.path.join(tmp, "chat-files")

    width = max(len(f) for f in files)
    failed, results = [], []
    # A per-file wall-clock cap: without it one hung test (a real socket that never
    # answers, a deadlocked poll) hangs the WHOLE run with no output, and CI just
    # times out with nothing to point at. 300 s is far above the slowest file
    # (~4 s here) — it fires only on a genuine hang, and names the file that hung.
    PER_FILE_TIMEOUT = 300
    for name in files:
        started = time.monotonic()
        try:
            proc = subprocess.run([sys.executable, os.path.join(HERE, name)],
                                  cwd=ROOT, env=env, capture_output=True, text=True,
                                  timeout=PER_FILE_TIMEOUT)
            took = time.monotonic() - started
            ok = proc.returncode == 0
            results.append((name, ok, took, proc.stdout, proc.stderr))
        except subprocess.TimeoutExpired as e:
            took = time.monotonic() - started
            ok = False
            out = e.stdout or ""
            err = (e.stderr or "") + (
                f"\n*** TIMED OUT after {PER_FILE_TIMEOUT}s — the file hung and was "
                f"killed; the run did not wait on it forever. ***")
            out = out.decode() if isinstance(out, bytes) else out
            err = err.decode() if isinstance(err, bytes) else err
            results.append((name, ok, took, out, err))
        print(f"{name:{width}}  {'PASS' if ok else 'FAIL'}  {took:5.2f}s")
        if not ok:
            failed.append(name)

    if failed:
        print("\n" + "=" * 60)
        for name, ok, _, out, err in results:
            if ok:
                continue
            print(f"\n--- {name} ---")
            tail = (out or "").strip().splitlines()
            print("\n".join(tail[-25:]) if tail else "(no stdout)")
            if err.strip():
                print("stderr:", err.strip().splitlines()[-5:])
        print(f"\n{len(failed)} of {len(files)} FAILED: {', '.join(failed)}")
        return 1

    total = sum(r[2] for r in results)
    print(f"\nall {len(files)} passed in {total:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
