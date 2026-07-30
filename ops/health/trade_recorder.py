"""Append-only per-trade recorder: capture GROUND TRUTH as it happens, never reconstruct it.

⭐⭐ WHY THIS EXISTS (2026-07-29). Answering "what actually happened today?" from the DB after the
fact produced THREE different answers in one evening, two of them wrong:
  * FIFO pairing reached across ONE missing exit and manufactured a -8.40% AMIX trade that never
    existed (the real trade was +1.78%).
  * Pairing by client-order-id then exposed 5 exits dated BEFORE their own entry, because those exit
    rows had been written by a symbol-only matcher that attached any AMIX sell to any AMIX entry.
  * Every exit-rule study built on those pairs inherited the corruption.

The lesson is not "pair more carefully". It is that ATTRIBUTION MUST BE CAPTURED, NOT INFERRED.
This records each round trip WITH the broker's own order ids at the moment it closes, so no later
process has to guess which sell belongs to which buy.

WHAT IT WRITES (one JSON object per line, /home/trader/trade_records/YYYY-MM-DD.jsonl)
  identity   symbol - broker - entry+exit client_order_id AND broker_order_id
  execution  intended price (from intent metadata) - actual fill - slippage_pct
  path       reactive | rth_resting | eh_resting - cw_entry_n - cw_arm_bar_ts
  outcome    entry/exit time+price - held_secs - ret_pct
  bar path   mfe_pct / mae_pct after entry - touched_target - n_bars
  what-ifs   floor+2% with 2/3/5% trails - tiered stop (<$3:-5 / >=$3:-3) - 3-min time stop
             so exit rules can be judged on REAL trades without re-deriving anything

⛔ APPEND-ONLY and idempotent: keyed on the exit's broker_order_id, so a re-run never double-writes
and never rewrites history. Read-only against both the DB and the broker.
⛔ Records its own UNCERTAINTY: `intrabar_ambiguous` is true when a stop and a target share one
1-minute bar, because bars cannot order them. A what-if that hides its uncertainty is worse than none.

usage:  trade_recorder.py [--since-mins 1440] [--out DIR] [--stdout]
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
TARGET_PCT = 2.0
TIER_PRICE, TIER_TIGHT, TIER_WIDE = 3.0, 3.0, 5.0
TIME_STOP_SECS = 180
OUT_DIR = "/home/trader/trade_records"

# Each entry order joins to ITS OWN bracket exit via the `-ocoexit` coid -- the same ownership rule
# the adapter now enforces (#605). Never symbol-only, never FIFO.
PAIRS_SQL = """
SELECT e.symbol,
       ba.name                AS broker,
       e.client_order_id      AS entry_coid,
       e.broker_order_id      AS entry_boid,
       ef.price               AS entry_px,
       ef.filled_at           AS entry_at,
       ef.quantity            AS qty,
       x.client_order_id      AS exit_coid,
       x.broker_order_id      AS exit_boid,
       xf.price               AS exit_px,
       xf.filled_at           AS exit_at,
       i.payload              AS ipayload
FROM broker_orders e
JOIN fills ef             ON ef.order_id = e.id AND ef.side = 'buy'
JOIN broker_accounts ba   ON ba.id = e.broker_account_id
JOIN strategies st        ON st.id = e.strategy_id
LEFT JOIN trade_intents i ON i.id = e.intent_id
JOIN broker_orders x      ON x.client_order_id LIKE e.client_order_id || '-ocoexit%'
JOIN fills xf             ON xf.order_id = x.id
WHERE st.code = 'schwab_1m_v2'
  AND ef.filled_at >= now() - make_interval(mins => :mins)
  AND ef.quantity <= 5
ORDER BY ef.filled_at
"""

# ⭐ THE PAIRED FILE IS NOT THE WHOLE DAY. An entry that fills and never gets an exit fill recorded is
# a real transaction, and PAIRS_SQL cannot see it -- on 2026-07-29, 26 entries produced 23 pairs and
# the 3 unpaired ones were silently absent. Silently omitting a trade is the exact failure this whole
# tool exists to end, so they get captured too. ⛔ They are NOT necessarily open positions: all three
# on 07-29 were flat at BOTH brokers, i.e. the native-OCO exit-capture gap, not a naked position.
# Only the broker can tell those apart -- see [[project_mai_tai_oco_exit_fill_blackout]].
UNPAIRED_SQL = """
SELECT e.symbol,
       ba.name           AS broker,
       e.client_order_id AS entry_coid,
       e.broker_order_id AS entry_boid,
       ef.price          AS entry_px,
       ef.filled_at      AS entry_at,
       ef.quantity       AS qty,
       i.payload         AS ipayload
FROM broker_orders e
JOIN fills ef             ON ef.order_id = e.id AND ef.side = 'buy'
JOIN broker_accounts ba   ON ba.id = e.broker_account_id
JOIN strategies st        ON st.id = e.strategy_id
LEFT JOIN trade_intents i ON i.id = e.intent_id
WHERE st.code = 'schwab_1m_v2'
  AND ef.filled_at >= now() - make_interval(mins => :mins)
  AND ef.quantity <= 5
  AND NOT EXISTS (
        SELECT 1 FROM broker_orders x JOIN fills xf ON xf.order_id = x.id
        WHERE x.client_order_id LIKE e.client_order_id || '-ocoexit%')
ORDER BY ef.filled_at
"""

# ⛔⭐ THE SECOND EXIT ROUTE, AND WHY IT IS NOT PAIRED HERE.
# v2 exits leave by two coids: `<entry>-ocoexit-*` (native OCO, shares the entry's id) and
# `<symbol>-close-*` (OMS-managed: flip, hard stop, EH ladder, EOD transition). The `-close-` coid
# carries a FRESH random suffix and its own single-order intent, so **nothing in the DB links it back
# to its entry.** Over 30 days: 74 `-close-` vs 36 `-ocoexit-` fills -- historically the majority.
# ⛔ Attributing them by symbol+time is exactly the heuristic that manufactured a -8.40% trade and
# booked the operator's manual 1000-share sell as ours. So this does NOT pair them. It reports a
# same-symbol close fill as an unproven CANDIDATE, and says so in the record.
# ⇒ The real fix is at the WRITE site: stamp the entry's broker_order_id onto the close order when the
#   OMS submits it. Design-first, not built. Until then this route is visible but unattributed.
CLOSE_CANDIDATE_SQL = """
SELECT f.price AS px, f.filled_at AS at, f.quantity AS qty, o.client_order_id AS coid
FROM fills f
JOIN broker_orders o ON o.id = f.order_id
JOIN broker_accounts ba ON ba.id = o.broker_account_id
WHERE o.symbol = :sym AND ba.name = :broker AND f.side = 'sell'
  AND o.client_order_id LIKE '%-close-%'
  AND f.filled_at >= :after
ORDER BY f.filled_at
LIMIT 1
"""

BARS_SQL = """
SELECT bar_time, high_price, low_price
FROM strategy_bar_history
WHERE symbol = :sym AND strategy_code = 'schwab_1m_v2' AND interval_secs = 60
  AND bar_time >= :lo AND bar_time <= :hi
ORDER BY bar_time
"""


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def analyse(entry_px: float, bars: list, actual_ret: float) -> dict:
    """Bar-path facts + what-if exits, derived ONLY from the bars the live bot saw -- so a what-if
    can never be better-informed than the bot was."""
    tgt = entry_px * (1 + TARGET_PCT / 100.0)
    mfe, mae = 0.0, 0.0
    touched = False
    tier = TIER_TIGHT if entry_px >= TIER_PRICE else TIER_WIDE
    tier_stop_px = entry_px * (1 - tier / 100.0)
    tier_hit = tgt_hit = None
    ambiguous = False

    for b in bars:
        hi, lo = _f(b.high_price), _f(b.low_price)
        mfe = max(mfe, (hi / entry_px - 1.0) * 100.0)
        mae = min(mae, (lo / entry_px - 1.0) * 100.0)
        s_hit, t_hit = lo <= tier_stop_px, hi >= tgt
        if t_hit:
            touched = True
        if tier_hit is None and tgt_hit is None and s_hit and t_hit:
            ambiguous = True          # one bar, both levels -> order unknowable
        if tier_hit is None and s_hit:
            tier_hit = b.bar_time
        if tgt_hit is None and t_hit:
            tgt_hit = b.bar_time

    out = {
        "mfe_pct": round(mfe, 3),
        "mae_pct": round(mae, 3),
        "touched_target": touched,
        "n_bars": len(bars),
        "intrabar_ambiguous": ambiguous,
        "tier_used_pct": tier,
        "whatif_tier_stop_pct": round(
            -tier if (tier_hit and (not tgt_hit or tier_hit <= tgt_hit)) else actual_ret, 3
        ),
    }
    for w in (2.0, 3.0, 5.0):
        # floor rule: once +2% is touched, never book less than +2%; trail from the running high
        out["whatif_floor2_trail%d_pct" % int(w)] = round(
            max(TARGET_PCT, mfe - w) if touched else actual_ret, 3
        )
    return out


def write_unpaired(path: str, recs: list[dict]) -> int:
    """⛔ OVERWRITE, never append -- and atomically.

    This file answers "which entries have no exit RIGHT NOW", which is a *state*, not a history: an
    entry unpaired at 10:05 is normally paired by 10:06. Appending would turn one open trade into a
    growing pile of stale duplicates and read as dozens of naked positions. Same two-verb split as
    the handoff docs: the paired JSONL is the append-only log, this is the overwritten state.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
    os.replace(tmp, path)          # atomic: a reader never sees a half-written file
    return len(recs)


def trade_day(entry_at) -> str:
    """⛔⭐ A trade belongs to the ET day its ENTRY FILLED on -- never to the day the recorder
    happened to run.

    This used to be `datetime.now(ET)`, applied to the filename AND stamped into every record's
    `day` field. Because the cron passes `--since-mins 1440`, the first run of each day reaches back
    24h and swept the PREVIOUS day's tail into today's file under today's date. Proven 2026-07-30:
    a run at 06:42 ET wrote a `2026-07-30.jsonl` containing 23 round trips whose `entry_at_et` were
    every one of them `2026-07-29`.

    That is the same class of error the whole tool exists to end -- a plausible-looking answer that
    silently misattributes real trades. The 1440-minute lookback is deliberate (a missed run must
    self-heal, and a late EH exit must still be caught), so the window stays wide and the DAY is
    derived from the data instead.
    """
    return entry_at.astimezone(ET).strftime("%Y-%m-%d")


def load_seen(path: str) -> set[str]:
    """Exit broker-order-ids already captured in a day-file, so re-runs never duplicate a trade."""
    seen: set[str] = set()
    if not os.path.exists(path):
        return seen
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                seen.add(json.loads(line)["exit_boid"])
            except Exception:  # noqa: BLE001 - a torn last line must not block today's writes
                continue
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-mins", type=int, default=1440)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()

    sf = build_session_factory(get_settings())
    with sf() as s:
        pairs = list(s.execute(text(PAIRS_SQL), {"mins": a.since_mins}).all())
        orphans = list(s.execute(text(UNPAIRED_SQL), {"mins": a.since_mins}).all())

    os.makedirs(a.out, exist_ok=True)
    run_day = datetime.now(ET).strftime("%Y-%m-%d")

    # one seen-set and one counter per TRADE day the window happens to span
    seen_by_day: dict[str, set[str]] = {}
    written_by_day: dict[str, int] = {}

    for p in pairs:
        exit_boid = str(p.exit_boid or "")
        if not exit_boid:
            continue
        day = trade_day(p.entry_at)
        path = os.path.join(a.out, day + ".jsonl")
        seen = seen_by_day.setdefault(day, load_seen(path))
        if exit_boid in seen:
            continue
        with sf() as s:
            bars = list(s.execute(
                text(BARS_SQL), {"sym": p.symbol, "lo": p.entry_at, "hi": p.exit_at}
            ).all())
        ep, xp = _f(p.entry_px), _f(p.exit_px)
        ret = (xp / ep - 1.0) * 100.0 if ep else 0.0
        held = (p.exit_at - p.entry_at).total_seconds()
        meta = ((p.ipayload or {}).get("metadata") or {}) if isinstance(p.ipayload, dict) else {}
        want = _f(meta.get("stop_price") or meta.get("entry_price"), 0.0)
        rec = {
            "day": day,
            "symbol": p.symbol,
            "broker": p.broker,
            "qty": int(_f(p.qty)),
            "entry_coid": p.entry_coid,
            "entry_boid": str(p.entry_boid or ""),
            "exit_coid": p.exit_coid,
            "exit_boid": exit_boid,
            "entry_at_et": p.entry_at.astimezone(ET).isoformat(timespec="seconds"),
            "exit_at_et": p.exit_at.astimezone(ET).isoformat(timespec="seconds"),
            "entry_px": ep,
            "exit_px": xp,
            "intended_px": want or None,
            "slippage_pct": round((ep / want - 1.0) * 100.0, 3) if want else None,
            "held_secs": int(held),
            "ret_pct": round(ret, 3),
            "path": meta.get("fanout_source") or meta.get("atr_variant"),
            "cw_entry_n": meta.get("cw_entry_n"),
            "cw_arm_bar_ts": meta.get("cw_arm_bar_ts"),
            "order_type": meta.get("order_type"),
            "whatif_timestop_would_fire": held > TIME_STOP_SECS,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        rec.update(analyse(ep, bars, round(ret, 3)))
        line = json.dumps(rec, separators=(",", ":"), default=str)
        if a.stdout:
            print(line)
        else:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        seen.add(exit_boid)
        written_by_day[day] = written_by_day.get(day, 0) + 1

    orphan_recs: list[dict] = []
    for p in orphans:
        meta = ((p.ipayload or {}).get("metadata") or {}) if isinstance(p.ipayload, dict) else {}
        want = _f(meta.get("stop_price") or meta.get("entry_price"), 0.0)
        ep = _f(p.entry_px)
        with sf() as s:
            cand = s.execute(text(CLOSE_CANDIDATE_SQL), {
                "sym": p.symbol, "broker": p.broker, "after": p.entry_at
            }).first()
        # ⛔ CANDIDATE, NOT A PAIRING. Reported so the route is never invisible; deliberately NOT
        # folded into ret_pct, because symbol+time attribution is the bug this tool exists to end.
        cand_block = {
            "exit_route": "close_unattributed" if cand else None,
            "close_candidate_px": _f(cand.px) if cand else None,
            "close_candidate_at_et": (
                cand.at.astimezone(ET).isoformat(timespec="seconds") if cand else None),
            "close_candidate_ret_pct": (
                round((_f(cand.px) / ep - 1.0) * 100.0, 3) if cand and ep else None),
        }
        orphan_recs.append({
            "day": trade_day(p.entry_at),
            "status": "ENTRY_WITH_NO_EXIT_FILL",
            "symbol": p.symbol,
            "broker": p.broker,
            "qty": int(_f(p.qty)),
            "entry_coid": p.entry_coid,
            "entry_boid": str(p.entry_boid or ""),
            "entry_at_et": p.entry_at.astimezone(ET).isoformat(timespec="seconds"),
            "entry_px": ep,
            "intended_px": want or None,
            "slippage_pct": round((ep / want - 1.0) * 100.0, 3) if want else None,
            "path": meta.get("fanout_source") or meta.get("atr_variant"),
            "cw_entry_n": meta.get("cw_entry_n"),
            # ⛔ Do NOT read this as a naked position. It means only that no exit FILL is recorded.
            # Ask the broker (list_account_positions) before ever calling one of these open.
            "verify": "ask the broker; an unrecorded exit is not an open position",
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **cand_block,
        })
    unpaired_path = os.path.join(a.out, run_day + ".unpaired.jsonl")
    # ⛔ The unpaired file is OVERWRITTEN (it is state, not history), so only the RUN day's file may
    # be rewritten. An earlier day's unpaired state was already finalised by that day's own runs;
    # rewriting it from a window that only partially covers that day would DELETE real entries and
    # read as "those trades never happened". Earlier-day orphans are reported, never written.
    run_day_orphans = [r for r in orphan_recs if r["day"] == run_day]
    older_orphans = [r for r in orphan_recs if r["day"] != run_day]

    if a.stdout:
        for r in orphan_recs:
            print(json.dumps(r, separators=(",", ":"), default=str))
        n_unpaired = len(orphan_recs)
    else:
        n_unpaired = write_unpaired(unpaired_path, run_day_orphans)

    for day in sorted(set(written_by_day) | {run_day}):
        path = os.path.join(a.out, day + ".jsonl")
        n_pairs = sum(1 for p in pairs if trade_day(p.entry_at) == day)
        print("[trade-recorder] %s: %d paired round trips seen, %d newly recorded -> %s"
              % (day, n_pairs, written_by_day.get(day, 0), path))
    n_cand = sum(1 for r in run_day_orphans if r.get("exit_route") == "close_unattributed")
    print("[trade-recorder] %s: %d entries with NO exit fill (state, overwritten) -> %s"
          % (run_day, n_unpaired, unpaired_path))
    if n_cand:
        print("[trade-recorder] %s: of those, %d have an UNATTRIBUTED -close- exit candidate "
              "(route visible, pairing unprovable -- see CLOSE_CANDIDATE_SQL)" % (run_day, n_cand))
    if older_orphans:
        by_day: dict[str, int] = {}
        for r in older_orphans:
            by_day[r["day"]] = by_day.get(r["day"], 0) + 1
        print("[trade-recorder] %d unpaired entries from EARLIER days seen but not rewritten "
              "(%s) -- their day's own state file is authoritative"
              % (len(older_orphans), ", ".join("%s:%d" % kv for kv in sorted(by_day.items()))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
