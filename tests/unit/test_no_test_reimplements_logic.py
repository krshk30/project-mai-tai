"""P5 — THE LINT: a test may never reimplement the logic it tests.

⛔⭐⭐ THE RULE, AND WHY IT IS WORTH A LINT (earned 2026-08-17/18).
Twice in one evening, on two different functions, a test computed its EXPECTED value by calling the
very code under test. Both passed against a broken implementation. One of the escapes would have
truncated **every weekend**. Mutants were the only thing that caught them — which means the rule was
being enforced by whoever happened to remember it.

    assert weekend_gap_bars(x) == weekend_gap_bars(x)      # passes for ANY implementation
    assert limit == _panic_limit_price(9.40, 0.5)          # passes for ANY buffer, including none

⇒ The expected side of an equality must be **PINNED** — a literal, or a chain that contains one.

## What this flags, and what it deliberately does NOT

FLAGGED: the expected side of `==` / `!=` calls a production FUNCTION (snake_case), or computes an
arithmetic expression from a production symbol, and the comparison chain contains **no literal**.

NOT flagged, on purpose — each of these was a false positive on the first pass over 178 files:

  1. `assert "no open positions" in _build_bot_position_rows(...)` — with `in`, the call is the
     SUBJECT being searched and the literal is the expectation. Inverting that reading would flag
     the correct pattern and let the broken one through.
  2. `assert x == WatchWindow(a, b)` — CamelCase is a value-object constructor, not logic. Building
     an expected dataclass is how you state an expectation precisely.
  3. `assert _exit_coid(A, b) == _exit_coid(A, b)` — both sides call it deliberately; the property
     under test IS determinism/uniqueness, and there is no "expected value" to pin.
  4. `assert m["limit_price"] == _panic_limit_price(9.40, 0.5) == "9.35"` — a chain containing a
     literal keeps the function exercised AND the value fixed. This is the pattern to copy.

⛔ THE HEURISTIC IS DELIBERATELY NARROW. A lint that fires on correct code gets suppressed, and a
suppressed lint is worse than none — it reads as enforcement while enforcing nothing.

⛔ THE ALLOWLIST MAY ONLY SHRINK. Every entry carries a REASON, and `test_allowlist_has_not_grown`
pins its size. An allowlist that can grow silently rebuilds the very ambiguity this removes.
"""

from __future__ import annotations

import ast
import pathlib

TESTS_ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCTION_PREFIXES = ("project_mai_tai", "scripts")

# (test file name, imported symbol) -> why calling it as the expected value is the LESSER evil.
# ⛔ Adding an entry needs a reason that survives being read aloud. "Hard to fix" is not one.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "test_oms_risk_service.py",
        "session_day_eastern_str",
    ): (
        "The field holds the CURRENT session day, and the helper implements the 04:00 ET session "
        "ROLL. Recomputing it in the test would duplicate that roll rule — a worse reimplementation "
        "than the one it replaces — and a hard-coded date would fail tomorrow. The honest fix is to "
        "inject the clock into the write path; until then this is a stated exception, not an oversight."
    ),
}


def _production_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(PRODUCTION_PREFIXES):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
    return names


def _chain_contains_literal(compare: ast.Compare) -> bool:
    """True if any element of the comparison chain is a literal constant.

    That is the pin. `a == f(x) == "9.35"` is fine; `a == f(x)` is not.
    """
    for node in [compare.left, *compare.comparators]:
        if isinstance(node, ast.Constant):
            return True
    return False


def _same_call_both_sides(compare: ast.Compare) -> bool:
    """`f(...) == f(...)` — a determinism/uniqueness property, not an expected value."""

    def root_fn(node: ast.AST) -> str | None:
        return (
            node.func.id if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) else None
        )

    fns = [root_fn(n) for n in [compare.left, *compare.comparators]]
    named = [f for f in fns if f]
    return len(named) >= 2 and len(set(named)) == 1


def find_violations() -> list[tuple[str, int, str, str]]:
    """(file, line, symbol, source) for every test that computes its expectation from production."""
    out: list[tuple[str, int, str, str]] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        produced = _production_imports(tree)
        if not produced:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
                continue
            compare = node.test
            # Only equality/inequality state an EXPECTED VALUE. `in`, `<`, `is` do not.
            if not all(isinstance(op, (ast.Eq, ast.NotEq)) for op in compare.ops):
                continue
            if _chain_contains_literal(compare) or _same_call_both_sides(compare):
                continue
            for side in compare.comparators:
                symbol = None
                for sub in ast.walk(side):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id in produced
                        and not sub.func.id[0].isupper()  # CamelCase => value object, not logic
                    ):
                        symbol = sub.func.id
                        break
                    if isinstance(sub, ast.BinOp):
                        hits = {n.id for n in ast.walk(sub) if isinstance(n, ast.Name)} & produced
                        hits = {h for h in hits if not h[0].isupper()}
                        if hits:
                            symbol = sorted(hits)[0]
                            break
                if symbol and (path.name, symbol) not in ALLOWED:
                    out.append((path.name, node.lineno, symbol, lines[node.lineno - 1].strip()))
                break
    return out


def test_no_test_computes_its_expected_value_from_production_code():
    violations = find_violations()
    assert violations == [], "\n".join(
        f"{f}:{ln} computes its expected value from `{sym}` — PIN THE LITERAL instead.\n    {src}"
        for f, ln, sym, src in violations
    )


def test_allowlist_has_not_grown():
    """⛔ The allowlist may only SHRINK. Pinned so an addition is a deliberate, reviewed edit."""
    assert len(ALLOWED) == 1
    assert all(len(reason) > 80 for reason in ALLOWED.values()), (
        "every exception states a real reason"
    )


def test_the_detector_actually_detects(tmp_path):
    """⛔⭐ A lint that has never gone red is not evidence. Drive it with a known-bad file.

    Without this, an over-narrowed heuristic would report a clean sweep forever.
    """
    # ⛔ The first attempt at this fixture was `assert '9.35' == _panic_limit_price(...)`, which the
    # lint correctly PASSED — the literal is right there, so the value IS pinned. That was a bad
    # known-bad tape, not a bug. The genuinely defective shape has NO literal anywhere in the chain,
    # so the assertion holds for whatever the function happens to return.
    bad = TESTS_ROOT / "unit" / "test_zz_lint_selfcheck_tmp.py"
    bad.write_text(
        "from project_mai_tai.oms.service import _panic_limit_price\n"
        "def test_bad():\n"
        "    m = {'limit_price': '9.35'}\n"
        "    assert m['limit_price'] == _panic_limit_price(9.40, 0.5)\n",
        encoding="utf-8",
    )
    try:
        found = [v for v in find_violations() if v[0] == bad.name]
        assert found, "the lint failed to flag a known reimplementation"
        assert found[0][2] == "_panic_limit_price"
    finally:
        bad.unlink()


def test_the_detector_does_not_flag_the_pinned_pattern(tmp_path):
    """The documented good pattern must stay legal, or the lint teaches the wrong habit."""
    good = TESTS_ROOT / "unit" / "test_zz_lint_selfcheck_ok_tmp.py"
    good.write_text(
        "from project_mai_tai.oms.service import _panic_limit_price\n"
        "def test_good():\n"
        "    assert _panic_limit_price(9.40, 0.5) == '9.35'\n",
        encoding="utf-8",
    )
    try:
        assert [v for v in find_violations() if v[0] == good.name] == []
    finally:
        good.unlink()
