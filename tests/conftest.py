"""Make a bare `pytest` honest about this suite.

These tests are standalone scripts. A test_* function reports a problem by appending
to a module-level `failures` list that ONLY each file's main() reads — main() is what
turns a non-empty list into a non-zero exit code. Run under `pytest`, main() never
runs: pytest calls the test_* functions directly, they append to `failures` instead
of raising, pytest sees no exception, and every test reports PASS no matter what it
found. That green proves nothing, and it hides exactly the regressions the suite
exists to catch.

The sanctioned runner is tests/run_all.py, which drives each file's main() in its own
process and reads the list. The hook below only closes the trap for anyone who
reaches for pytest out of habit: around each test it reads the module's `failures`
list, and if the test appended to it without otherwise raising, it turns that test
into a real FAILURE. Files that signal failure another way (a local list, a raise)
are unaffected — the hook is a no-op when the module has no `failures` list.
"""
import pytest


def pytest_collection_modifyitems(items):
    """Refuse a multi-file pytest run of this suite — it cannot be correct.

    Each test file is a standalone script that mutates process-global state (env
    vars, module singletons, the session client) and never cleans up: main() gets
    away with it because run_all.py gives every file its OWN process. Collect two of
    them into one pytest session and they pollute each other — a test that passes
    alone fails only because an unrelated file ran first. That is a false RED, just
    as misleading as the false green this suite used to give.

    So: one file under pytest is allowed (handy for the file you just touched, and
    honest thanks to the hook below). Two or more — send the reader to run_all.py,
    which isolates each file the way they were written to be run.
    """
    modules = {item.module.__name__ for item in items
               if isinstance(getattr(item.module, "failures", None), list)}
    if len(modules) > 1:
        raise pytest.UsageError(
            "these tests are process-isolated standalone scripts and share global "
            "state — running more than one file under a single pytest process gives "
            "unreliable results (a test can fail only because another file ran "
            "first). Run the whole suite with the sanctioned runner instead:\n"
            "    python tests/run_all.py\n"
            "or point pytest at a single file, e.g. pytest tests/test_transfer.py")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    failures = getattr(item.module, "failures", None)
    before = len(failures) if isinstance(failures, list) else None
    outcome = yield
    if before is None:
        return
    recorded = failures[before:]
    if recorded and outcome.excinfo is None:
        outcome.force_exception(
            AssertionError(
                f"{item.name} recorded {len(recorded)} failure(s) that a bare pytest "
                f"would have reported as PASS:\n  - "
                + "\n  - ".join(str(f) for f in recorded)))
