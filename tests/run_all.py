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

    width = max(len(f) for f in files)
    failed, results = [], []
    for name in files:
        started = time.monotonic()
        proc = subprocess.run([sys.executable, os.path.join(HERE, name)],
                              cwd=ROOT, env=env, capture_output=True, text=True)
        took = time.monotonic() - started
        ok = proc.returncode == 0
        results.append((name, ok, took, proc.stdout, proc.stderr))
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
