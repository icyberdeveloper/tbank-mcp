"""A dependency floor is not a version policy — the ceiling is load-bearing here.

server.py's first import is `from mcp.server.fastmcp import FastMCP`. mcp 2.0.0
dropped that module, so a bare `mcp>=1.2` resolves to 2.0.0 on a fresh install and
dies with ModuleNotFoundError before a single line runs. This pins the fact so the
ceiling cannot quietly come off, and executes the two halves of the claim rather than
restating them: the module the server needs must exist in what is installed, and the
declared range must exclude the version that removed it.

    python3 tests/test_dependency_pins.py
"""
import importlib.util
import os
import sys
import tomllib

from packaging.requirements import Requirement

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def _mcp_requirement() -> Requirement:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    for spec in deps:
        req = Requirement(spec)
        if req.name == "mcp":
            return req
    raise AssertionError("mcp is not in pyproject dependencies at all")


def test_the_server_import_target_exists_in_the_installed_mcp():
    """The exact module server.py imports must be importable — a guard on the env the
    tests themselves run in."""
    check(importlib.util.find_spec("mcp.server.fastmcp") is not None,
          "mcp.server.fastmcp is missing from the installed mcp — server.py cannot "
          "import FastMCP; the installed mcp is too new (>=2.0) or broken")
    print("  installed: mcp.server.fastmcp resolves")


def test_the_declared_range_excludes_the_version_that_removed_fastmcp():
    """2.0.0 must NOT satisfy the requirement, and a known-good 1.x must."""
    spec = _mcp_requirement().specifier
    check("2.0.0" not in spec,
          f"pyproject allows mcp 2.0.0 ({spec}) — a fresh install would pull it and "
          f"fail at `from mcp.server.fastmcp import FastMCP`. Pin an upper bound <2.")
    check("2.1.0" not in spec and "3.0.0" not in spec,
          f"the ceiling must exclude the whole 2.x+ line, got {spec}")
    check("1.28.1" in spec,
          f"the range must still admit a working 1.x, got {spec}")
    print(f"  declared: mcp specifier {spec} excludes 2.x, admits 1.x")


def main():
    print("dependency pins:")
    test_the_server_import_target_exists_in_the_installed_mcp()
    test_the_declared_range_excludes_the_version_that_removed_fastmcp()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
