# v2 Decision Tape — stamping the decision fields in the persist path

**Status:** SCOPED, not built. Operator-selected 2026-08-03 (option 1 of two).
**Gate:** live-persist-path change on the real-money bot ⇒ **flag-gated, default OFF, attended enable.**
**Priority:** not urgent. Queued behind the P0a live watch, alongside #366 in a quiet window.

---

## The problem

`schwab_1m_v2` — the only real-money bot — **has never written a decision row.** Across its entire
history:

| `decision_status` | rows | span |
|---|---|---|
| `(empty)` | 208,297 | 2026-05-21 → 2026-08-03 |
| `rest_backfill` | 855 | 2026-07-30 → 2026-08-03 |

`decision_status`, `decision_path` and `decision_reason` are empty on every bar the bot persists.
The only non-empty rows in the table are written by the **bar-gap auto-repair**, not by the bot.

Two consequences, one cosmetic and one that matters:

1. The bot page renders **STALE** — *"Bot has symbols, but no fresh decision rows are being
   recorded"* (`control_plane.py:4943`, fires when `decision_age > 120s`). It is only ever GREEN in
   the two minutes after an unrelated backfill lands. **This has been true for ten weeks.**
2. ⭐ **v2 has no staleness signal at all.** The badge cannot distinguish a healthy bot from a dead
   one, so during the P0a validation window the one bot trading real money is the one bot whose
   health indicator is meaningless.

## ⛔ Why option 2 (re-key the check to bar freshness) is a DEAD END — recorded so it is not re-proposed

The cheaper alternative was to leave the fields empty and re-key the staleness check onto bar
freshness. Rejected:

- it **silences the badge without restoring any signal** — the Decision Tape stays permanently
  empty on the real-money bot, which is the actual gap
- the natural backstop does not exist: `indicator_snapshots` is **also empty** for v2, so the
  `indicator_age_seconds <= 120` escape hatch at `control_plane.py:4944` can never fire either
- it treats a *reporting* defect as a *display* defect. Bars are already known-fresh from two
  independent sources (`[V2-MACD-PROBE]` per minute, and the bar-gap watch); a third bar-freshness
  view adds no information

⇒ **Option 1 is the only one that converts the false alarm into a real signal.**

## ⭐ The structural finding — this is an ORDERING problem, not a missing INSERT

`services/schwab_1m_v2_bot.py`:

```
1672:  await asyncio.to_thread(self._persist_bar, symbol, bar)   # <-- persist
1675:  draft = self.strategy.on_bar(symbol, bar)                 # <-- decide
```

**The bar is persisted BEFORE it is evaluated.** At persist time the decision does not exist, so
the fields are not "forgotten" — they are unknowable at that point. Any fix that only edits the
INSERT statement cannot work.

Note also `_persist_bar` writes `position_state="flat"` as a **hardcoded literal** and
`indicators_json={}` as a literal — the same always-empty-field shape as
[[project_mai_tai_v2_snapshot_hardcoded_empty_fields]]. `position_state` is therefore wrong
whenever v2 is holding. Fixing it belongs to this workstream but is a **separate commit**.

## Design — a follow-up stamp, not a reorder

Rejected: **reordering** persist after evaluate. It changes the sequencing of the live real-money
path, and it makes the bar row conditional on `on_bar` not raising — today the row lands even if
evaluation fails, which is the safer property and worth keeping.

**Chosen:** leave `_persist_bar` exactly where it is; add a second, targeted `UPDATE` after
`on_bar` returns, stamping the decision for that `(symbol, bar_time)`.

This composes with the existing upsert by design: `_persist_bar`'s `on_conflict_do_update` already
refreshes **OHLCV only** and deliberately leaves `decision_*` / `position_*` untouched, so a later
writer owning those columns does not fight it.

**Sketch:**
1. `strategy_core/schwab_1m_v2.py` — stash the evaluation outcome on the existing per-symbol
   `SymbolState` (the object `_macd_probe` already holds when it logs every input, ~line 2181).
   No new computation: **every value is already computed and logged today.**
2. `services/schwab_1m_v2_bot.py` — after `on_bar`, read that outcome and `UPDATE` the row's
   `decision_status` / `decision_path` / `decision_reason` (+ `indicators_json`).
3. Field values must mirror the strategy-engine vocabulary so the shared dashboard query treats v2
   bars identically (`_persist_bar`'s docstring already states this as the intent).

**Cost:** one small UPDATE per symbol per bar (~2–15 rows/min at current watchlist sizes).

## Gate

| | |
|---|---|
| flag | `MAI_TAI_STRATEGY_SCHWAB_1M_V2_DECISION_TAPE_ENABLED`, **default `false`** |
| off | byte-identical behaviour — no second write issued at all |
| enable | **attended**, outside the entry window, one symbol observed before leaving it on |
| kill | drop the flag + restart `schwab-1m-v2` |

⛔ The write must be **fail-soft**: a decision-stamp failure must never propagate into `on_bar` or
the emit path. It is observability; it may never cost a trade. Wrap and log, never raise.

⛔ **Do not "fix" this by making the tape a display-only construct.** The value is a durable,
queryable record of what the bot decided per bar — the same record every exit study has lacked.

## Tests

- flag OFF ⇒ **no** second write occurs (assert on the session/mock, not just on output)
- flag ON ⇒ `decision_status` non-empty for an evaluated bar
- a raising stamp does **not** propagate — `on_bar`/emit still complete
- the stamp targets exactly one `(strategy_code, symbol, interval_secs, bar_time)` row
- ⭐ **mutation-test the gate**: force the flag on with the writer stubbed to raise, and prove the
  emit path is unaffected — per [[feedback_mutate_the_code_pin_the_threshold]], a green suite is
  not evidence until a deliberate break turns it red

## Rollout

1. merge behind the flag (OFF) — zero live change
2. enable attended, after close, watch one symbol for a full bar cycle
3. confirm the bot page leaves STALE **and** that the badge can still go STALE for the right
   reason: stop feeding bars and verify it reports stale rather than staying green
4. only then treat the badge as a health signal during the P0a window

⚠️ Step 3 is the one that is easy to skip and the one that matters — a badge that has gone from
always-red to always-green is not fixed, it is inverted.

## Related

[[project_mai_tai_v2_snapshot_hardcoded_empty_fields]] · [[feedback_a_watch_that_fails_to_a_false_clean]] ·
[[project_mai_tai_restart_bar_gap_checklist]] · `docs/schwab-1m-v2-entry-criteria.md`
