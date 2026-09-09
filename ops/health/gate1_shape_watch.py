#!/usr/bin/env python
"""GATE1 — page the moment a qualifying #647 Gate 1 shape exists.

⭐ WHY THIS EXISTS. The operator ruled 2026-09-09 that Gate 1 must NOT be waived: the RTH-edge
bracket flag (`oms_v2_rth_edge_bracket_enabled`) stays off until an unreserved-share preview proves
the shape. He took that branch knowing the cost — the pre-market bracket hole stays open meanwhile
(47 positions measured to 2026-08-12, plus 60 more pre-market entries filled since).

⛔⭐⭐ THE QUALIFYING SHAPE *IS* THE DEFECT. Gate 1 needs a v2-held SCHWAB long during RTH whose
shares are NOT already reserved by an open exit. An ordinary RTH entry can NEVER qualify: it is
bracketed on entry, so its shares are reserved — that is exactly why SUNE failed to qualify. The
only way to hold unreserved Schwab shares in RTH is to have entered PRE-MARKET and still be holding
at 09:30, which is precisely the hole Part 1 exists to fix.
⇒ So the wait is bounded by the defect's own frequency (18 Schwab pre-market fills since 08-12),
  and the window is LIVE only while the position is open. Miss it and we wait for the next one.

⛔ READ-ONLY. This places nothing and previews nothing. It reports that the shape exists so the
`previewOrder` step can be taken by hand while it is live. Per the rollout, Gate 1 is a
`previewOrder`, NEVER a live probe.

Exit codes: 0 = no qualifying shape · 1 = SHAPE PRESENT (page) · 2 = CANNOT SEE (refused)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
V2_ACCOUNT = "live:schwab_1m_v2"

# ⛔ THE EXACT SET oms/store.py uses for "reserved". Do NOT re-derive it here: `reserved` must mean
# what the OMS means by it, or this watch qualifies a position the real gate would refuse.
# oms/store.py:35 OPEN_ORDER_STATUSES = ("pending","submitted","accepted","partially_filled")
OPEN_ORDER_STATUSES = ("pending", "submitted", "accepted", "partially_filled")


def _psql(sql: str) -> list[str]:
    """One read. ⛔ RAISES on failure — a query that could not run is UNKNOWN, never 'no shape'."""
    out = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", "project_mai_tai", "-F", "|", "-tAc", sql],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql rc={out.returncode}: {(out.stderr or '').strip()[:200]}")
    return [ln for ln in (out.stdout or "").splitlines() if ln.strip()]


def qualifying_rows(rows: list[dict], now_et: datetime) -> tuple[list[dict], list[dict]]:
    """(qualifying, disqualified_with_reason).

    ⛔ EVERY row is classified and returned. A row dropped silently is a denominator nobody named.
    """
    qualifying, rejected = [], []
    for row in rows:
        entry_et = row["entry_at"].astimezone(ET)
        if row["account"] != V2_ACCOUNT:
            rejected.append({**row, "why": f"wrong account ({row['account']}) — Gate 1 needs Schwab"})
        elif row["quantity"] <= 0:
            rejected.append({**row, "why": "not a long"})
        elif entry_et.time() >= RTH_OPEN:
            # ⛔ Say only what was CHECKED. An RTH entry is USUALLY bracketed on entry, but the
            # reserved count is read separately and may legitimately be 0 (measured live on SUNE).
            # Asserting "shares reserved" here would state a fact this branch never verified.
            rejected.append({**row, "why": f"entered {entry_et:%H:%M} ET — not a pre-market entry"})
        elif now_et.time() < RTH_OPEN:
            rejected.append({**row, "why": "regular hours have not started yet"})
        elif row["reserved"] > 0:
            rejected.append({**row, "why": f"{row['reserved']} share(s) reserved by an open exit — "
                                            "Gate 1 needs UNRESERVED shares"})
        else:
            qualifying.append(row)
    return qualifying, rejected


def fetch_rows() -> list[dict]:
    statuses = ",".join(f"'{s}'" for s in OPEN_ORDER_STATUSES)
    # ⛔⭐⭐ ENTRY TIME COMES FROM THE MANAGED ROW, AND ONLY FROM IT (codex-2, #931).
    # I briefly took `least(m.entry_time, min(any buy fill in the prior hour))` as belt-and-braces.
    # That fill is NOT bound to this managed row, order or episode, so an EARLIER position's fill
    # can be attached to the CURRENT one: a 09:20 pre-market fill that later closed, plus a 09:38
    # RTH re-entry, yields least(...)=09:20 and the watch reports the RTH position as a qualifying
    # pre-market shape. Today's SUNE is exactly that pattern.
    # ⛔ And "erring early is safe" was wrong reasoning: this page ASKS THE OPERATOR TO ACT — to
    # take the Gate 1 preview against the claimed shape. A false positive is not a wasted cycle,
    # it is a false instruction. The managed row stamps the FILL and is correct; use it.
    sql = (
        "select b.name, p.symbol, p.quantity, m.entry_time, "
        "coalesce((select sum(o.quantity) from broker_orders o "
        "  where o.broker_account_id=p.broker_account_id and o.symbol=p.symbol "
        f"    and o.side='sell' and o.status in ({statuses})), 0) as reserved "
        "from account_positions p "
        "join broker_accounts b on b.id=p.broker_account_id "
        "left join oms_managed_positions m on m.symbol=p.symbol "
        "     and m.broker_account_name=b.name and m.status='open' "
        "where p.quantity <> 0"
    )
    rows = []
    for line in _psql(sql):
        name, symbol, qty, entry_time, reserved = (line.split("|") + [""] * 5)[:5]
        if not entry_time.strip():
            # ⛔ No managed row => we cannot say WHEN it was entered. UNKNOWN, not pre-market.
            rows.append({"account": name, "symbol": symbol, "quantity": float(qty or 0),
                         "entry_at": None, "reserved": float(reserved or 0), "unknown_entry": True})
            continue
        rows.append({
            "account": name, "symbol": symbol, "quantity": float(qty or 0),
            "entry_at": datetime.fromisoformat(entry_time).replace(
                tzinfo=timezone.utc) if "+" not in entry_time else datetime.fromisoformat(entry_time),
            "reserved": float(reserved or 0), "unknown_entry": False,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="/home/trader/gate1_watch/STATUS.txt")
    ap.add_argument("--now", default=None, help="ISO instant, for tests only")
    args = ap.parse_args()

    now_et = (datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)).astimezone(ET)
    try:
        rows = fetch_rows()
    except Exception as exc:  # noqa: BLE001 — a failed read is never "no shape"
        line = (f"[GATE1-WATCH] verdict=CANNOT_SEE as_of={now_et:%F %H:%M:%S %Z} "
                f"detail={type(exc).__name__}: {exc}")
        _write(args.status, line + "\n⛔ A failed read is UNKNOWN, not an absence of the shape.\n")
        print(line)
        return 2

    # Rows with no managed row cannot be dated; report them rather than dropping them.
    undatable = [r for r in rows if r["unknown_entry"]]
    datable = [r for r in rows if not r["unknown_entry"]]
    qualifying, rejected = qualifying_rows(datable, now_et)

    body = [
        f"[GATE1-WATCH] as_of={now_et:%F %H:%M:%S %Z} positions_evaluated={len(rows)} "
        f"qualifying={len(qualifying)} rejected={len(rejected)} undatable={len(undatable)}",
    ]
    for r in qualifying:
        body.append(f"  ⭐ QUALIFIES  {r['symbol']:6} {r['account']} qty={r['quantity']:.0f} "
                    f"entered {r['entry_at'].astimezone(ET):%H:%M} ET  reserved=0")
    for r in rejected:
        body.append(f"  -  {r['symbol']:6} {r['account']}: {r['why']}")
    for r in undatable:
        body.append(f"  ?  {r['symbol']:6} {r['account']}: UNKNOWN entry time (no open managed row)")
    text = "\n".join(body)
    _write(args.status, text + "\n")
    print(text)
    return 1 if qualifying else 0


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


if __name__ == "__main__":
    sys.exit(main())
