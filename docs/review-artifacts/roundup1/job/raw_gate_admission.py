"""Dated residual admission on original gate output, never a source/DB/env waiver."""
from decimal import Decimal
import re
from release_policy import Stop, digest, need
from strict_flat_readonly import ACCOUNTS, ALLOWANCES, fresh, ipdn_residual


def proof(snapshot, now):
    need(snapshot["rc"] == 0 and not snapshot["blockers"] and not snapshot["remaining_general_failures"],
         "fresh strict snapshot not admitted")
    need(not any(snapshot[key] for key in ("managed_rows", "virtual_rows", "working_orders", "inflight_intents")),
         "owned/dispatch activity present")
    residual = ipdn_residual(snapshot, snapshot["allowance_findings"], now)
    need(residual is not None and residual == snapshot["dated_operator_residual"], "exact residual proof missing/drift")
    need(all(row["symbol"] in {*ALLOWANCES, "IPDN"} for row in snapshot["allowance_findings"]),
         "third reconciliation finding cannot be admitted")
    for account in ACCOUNTS:
        fresh(snapshot["direct_read_started_at"][account], now, account + " raw gate direct proof")
    return residual


def only_ipdn(value):
    match = re.fullmatch(r"IPDN=([0-9]+(?:\.[0-9]+)?)", value)
    need(match is not None and Decimal(match[1]) == 1000, "raw gate position not sole exact IPDN1000")


def original_go(name, rc, raw, *, clock_reason=None):
    lines = [line.strip() for line in raw.splitlines()]
    need(rc == 0 and not any(token in raw for token in ("[BLOCK]", "[BLIND]", "COULD_NOT_TELL", "STALE", "NO-GO")),
         "zero gate rc conflicts with raw evidence")
    need(lines.count("[ok]    zero open managed rows") == 1, "zero managed evidence missing")
    if name == "v2":
        armed = [re.fullmatch(r"\[ok\]    zero armed segments \[published state, ([0-9.]+)s old\]", line) for line in lines]
        found = [match for match in armed if match]
        need(len(found) == 1 and 0 <= float(found[0][1]) <= 60, "zero-rc armed state not readable/fresh")
        overrides = [line for line in lines if "[OVERRIDE]" in line]
        need((not overrides and lines.count("[ok]    past 18:00 ET") == 1)
             or (overrides == ["[OVERRIDE] clock gate (<18:00 ET) overridden by OPERATOR"]
                 and clock_reason is not None and lines.count("reason: " + clock_reason) == 1), "clock/armed override differs")
        need(lines.count("[ok]    broker flat on both real-money accounts (operator manuals excluded)") == 1
             and lines.count("===> GO. Zero armed segments AND flat. Safe to restart v2.") == 1, "zero-rc v2 source/verdict missing")
    elif name == "oms":
        for account in ACCOUNTS:
            found = [re.fullmatch(r"\[ok\]    " + re.escape(account) + r" flat \[([0-9]+)s old\]", line) for line in lines]
            found = [match for match in found if match]
            need(len(found) == 1 and int(found[0][1]) <= 120, "zero-rc OMS account not fresh")
        need(lines.count("[ok]    database reachable") == 1
             and lines.count("[info]  strict all-account-position flatness enabled; no symbols are excluded") == 1
             and lines.count("===> GO. Flat on every real-money account, zero managed rows, all sources fresh.") == 1,
             "zero-rc OMS source/verdict missing")
    else:
        raise Stop("unknown original gate")


def admit(name, rc, raw, before, after, now, *, clock_reason=None):
    original = digest(raw.encode())
    a, b = proof(before, now), proof(after, now)
    need(a == b, "residual/fill identity changed across original gate")
    need(rc == 1 and not any(token in raw for token in ("[BLIND]", "COULD_NOT_TELL", "cannot read", "STALE", "override REFUSED")),
         "original gate unreadable/stale/other refusal")
    lines = [line.strip() for line in raw.splitlines()]
    blocks = [line for line in lines if "[BLOCK]" in line]
    need(len(blocks) == 1, "not exclusively one residual blocker")
    need(lines.count("[ok]    zero open managed rows") == 1, "raw managed-zero proof missing/ambiguous")
    if name == "v2":
        pattern = r"\[BLOCK\] broker not flat \(excluding operator manuals\): (.+)"
        match = re.fullmatch(pattern, blocks[0])
        need(match is not None, "v2 blocker not exact broker residual")
        only_ipdn(match[1])
        armed = [re.fullmatch(r"\[ok\]    zero armed segments \[published state, ([0-9.]+)s old\]", line) for line in lines]
        found = [match for match in armed if match]
        need(len(found) == 1 and 0 <= float(found[0][1]) <= 60, "v2 armed/freshness proof missing")
        overrides = [line for line in lines if "[OVERRIDE]" in line]
        if overrides:
            need(overrides == ["[OVERRIDE] clock gate (<18:00 ET) overridden by OPERATOR"]
                 and clock_reason is not None and lines.count("reason: " + clock_reason) == 1,
                 "non-clock/unnamed override")
        else:
            need(lines.count("[ok]    past 18:00 ET") == 1, "clock did not pass")
        need(lines.count("===> NO-GO. Do not restart v2. Re-run when the blocking lines clear.") == 1,
             "original v2 verdict missing")
    elif name == "oms":
        match = re.fullmatch(r"\[BLOCK\] live:schwab_1m_v2 NOT FLAT \[([0-9]+)s old\]: (.+)", blocks[0])
        need(match is not None and int(match[1]) <= 120, "OMS residual account/freshness differs")
        only_ipdn(match[2])
        flat = [re.fullmatch(r"\[ok\]    live:orb flat \[([0-9]+)s old\]", line) for line in lines]
        found = [match for match in flat if match]
        need(len(found) == 1 and int(found[0][1]) <= 120, "other real account not fresh flat")
        need(lines.count("[ok]    database reachable") == 1
             and lines.count("[info]  strict all-account-position flatness enabled; no symbols are excluded") == 1,
             "OMS strict/readability proof missing")
        need(not any("[OVERRIDE]" in line or "GO **BY OPERATOR OVERRIDE**" in line for line in lines), "OMS naked-position override not allowed")
        need(lines.count("===> NO-GO. A position is open; the OMS ladder is managing it.") == 1, "original OMS verdict missing")
    else:
        raise Stop("unknown raw gate")
    need(not any(re.match(r"===> GO\b", line) for line in lines), "mixed original gate verdict")
    return dict(policy="operator-20261006-exact-IPDN-install-admission", gate=name, original_rc=rc,
                original_sha256=original, original_verdict="NO-GO", admission="EXACT_DATED_RESIDUAL_ONLY",
                broker_flat=False, residual=b, pre_proof_sha256=digest(repr(before).encode()),
                post_proof_sha256=digest(repr(after).encode()), source_modified=False)
