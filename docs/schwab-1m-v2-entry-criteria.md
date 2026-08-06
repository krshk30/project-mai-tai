# schwab_1m_v2 — Entry Criteria (LIVE: CW-v2 ATR flip)

> ## ⛔ THIS DOCUMENT WAS REPLACED ON 2026-08-05
>
> Every prior version described **"MACD Momentum v1.32"** — `min_bars = 135`
> (`macd_slow + macd_signal + settling`), MACD/VWAP cross paths, stochastic, `rel_vol > 1.5×`,
> `abs-vol > 5000`. **None of it can fire in production.**
> `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_ONLY_MODE=true` forces `path_macd = path_vwap = False` at
> `schwab_1m_v2.py:2466`, and the comment there says so outright:
> *"with this flag on, the MACD/VWAP emit block below is unreachable."*
>
> That code still exists and still logs `[V2-MACD-PROBE]` lines — which is precisely why the old doc
> looked corroborated. **The probe is diagnostic-only and never gates behaviour** (`:1490`).
>
> **This cost a full day on 2026-08-05.** The file was memory-flagged canonical, so a live no-trade
> investigation was reasoned against a strategy we do not run and produced a wrong root cause
> (`min_bars = 135` "blocking" GTE) that had to be withdrawn. If you are here to answer *"why didn't
> we trade X"* — the answer is **not** in the MACD gates. Start at §3 and §6.

## Provenance

| | |
|---|---|
| **Code read against** | `786bbb6805caca6626c39f018cc4b468aac010d2` — deployed HEAD, branch `main` |
| **Live flags read from** | `/etc/project-mai-tai/project-mai-tai.env` on the VPS, 2026-08-05 |
| **Verification** | every gate cites `file:line`. Nothing here is inferred from naming. |

⛔ **Re-verify the flags before trusting this doc.** A gate's presence in code says nothing about
whether it is reachable — that is the exact failure this rewrite corrects.

```
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_ONLY_MODE=true                 <- makes MACD/VWAP unreachable
MAI_TAI_STRATEGY_SCHWAB_1M_V2_CONFIRMED_WINDOW_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_REACTIVE_ENTRY_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_RESTING_ENTRY_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_EH_RESTING_ENTRY_ENABLED=true
```

---

## 1. The live path, end to end

```
on_bar
  └─ _evaluate_completed_bar                                       :2142
       ├─ position_qty > 0 ......................... return None   :2464
       ├─ ATR_ONLY_MODE -> path_macd = path_vwap = False           :2466
       └─ _maybe_atr_emit                                          :1190
            ├─ not _atr_enabled or atr_signal is None ... return    :1202
            ├─ not bar_is_fresh ........................ return     :1204
            └─ _cw_enabled -> _cw_entry(...) OWNS the decision      :1211
                 └─ _cw_v2_enabled -> return None
                    "arm/trigger state is tracked in _cw_v2_track; entry is INTRABAR"
```

⭐ **The bar close does not enter — it ARMS.** `_cw_v2_track` sets the arm on a BUY flip; the entry
fires **intrabar**, either from `on_quote` (reactive) or from a resting buy-stop-limit filling at
the broker. This is why a resting fill can precede its own `[V2-CW-ARM]` line by 21s–706s, and why
entry composition must be read from the **ARM/DISARM log**, never reasoned from bar closes.

## 2. The gates that actually decide

| # | Gate | Where | Note |
|---|---|---|---|
| 1 | flat — `position_qty > 0` returns | `:2464` | conservative UNION (fills ∪ in-flight opens) |
| 2 | `_atr_enabled` and `atr_signal is not None` | `:1202` | `atr_signal` is None until the trail is defined |
| 3 | **`bar_is_fresh`** | `:1204` | *"never fire on a replayed historical touch"* — warmup bars can arm but cannot emit |
| 4 | **ATR flip == `BUY`** | `_cw_v2_track` | **edge-triggered** — an already-armed symbol does not re-arm |
| 5 | **volume floor** `cur.volume <= _atr_vol_floor` | `:1213`, `:1321`, `_last_bar_volume_ok:1513` | *"the only filter: bar volume > floor."* Settled at **10,000** |
| 6 | `cw_armed` + `cw_bars_waited` 3-bar wait | `_cw_v2_track` | reactive trigger is `state.cw_segment_high`, unconditional |
| 7 | entry window 07:00–18:00 ET | `_within_entry_window` (bot) | pre-market EH trading is live |
| 8 | **composition cap** ≤1 resting AND ≤1 reclaim per cross | #644 | ⛔ **not a count** — see §5 |
| 9 | watch-start: a flip predating watchlist join is declined | `_cap_reconstructed_segment` (bot) | *"a NEW stock needs to wait for a fresh flip"* |
| 10 | 04:00-ET session reset of the whole ATR trail | `_update_atr_state`, `_apply_session_anchor_reset` | **see §3 — this is the one that surprises people** |

## 3. ⭐ The 04:00 ET session slice — read this before reporting a missed setup

**The ATR trail is rebuilt from scratch at 04:00 ET every day.** `_update_atr_state` resets the
entire indicator (`atr_hl`, `atr_wilders`, `atr_trail`, `atr_state`) plus the CW setup whenever a
bar's `session_start_ts_ms()` anchor differs from the stored one — the same anchor VWAP uses,
*"so the live series matches the validated session-sliced backtest."*

⛔ **This is why a charting platform can show a flip the bot never saw.** TOS computes the ATR
trailing stop **continuously across sessions**; the bot slices at 04:00 ET. Same tape, same
ATRPeriod=5 / ATRFactor=3.5 / Wilders — **different answer**, because the indicator is
path-dependent.

Measured 2026-08-05. Same symbols, same bars; only the slice differs:

| Symbol | unsliced multi-day run | live 04:00-ET slice | bot's actual arm |
|---|---|---|---|
| GTE | flip 07:00 ET (+2 more) | **BUY 09:01 ET** | `[V2-CW-ARM]` 09:02:02 ✅ |
| BJDX | flip 07:30 ET (+3 more) | **BUY 08:49 ET** | `[V2-CW-ARM]` 08:50:02 ✅ |

The bot armed on the sliced flip **both times, within ~90 seconds**. Nothing was missed. **Live and
the backtest agree; the chart is the outlier.**

⛔ **Never judge a "missed setup" against an unsliced oracle run.** Two wrong conclusions were drawn
from exactly that error on 2026-08-05, including an acceptance test for a live entry-path change
that could never have gone green. This likely explains a whole class of "I saw a setup and we
didn't take it" reports.

> Whether daily re-seeding is right for **pre-market** — where the first hours have little history
> to seed from — is a **strategy** question on the parked list. Flagged, not built.

## 4. What is NOT a gate

`macd_slow` · `macd_signal` · `min_bars = 135` · MACD cross · VWAP cross · stochastic · `ema9` ·
`rel_vol` / `pretrigger_min_bar_rel_vol` · `avg_vol_20` · `abs-vol > 5000`.

These live in `strategy_core/entry.py`, `strategy_core/indicators.py`, `exit_logic/config.py` and
the `polygon_30s` / `schwab_native_30s` strategies. **All are real, live modules — for other
strategies.** `_maybe_atr_emit` and `_cw_v2_track` contain **zero** references to any of them.

`min_bars = 135` additionally carries an **explicit ATR carve-out** below it, added after it
*"blinded fresh-scanned/post-restart symbols like QTEX."*

## 5. ⛔ `cw_entries_this_flip` is a LABEL, not the cap

Declared at `:214`, incremented at `:1588` — both annotated *"kept for labelling/back-compat;
**NOT the cap**."* The #644 cap is **composition**: ≤1 resting AND ≤1 reclaim per cross.

`cw_armed_segments()` nevertheless derives `capped` and `dangerous` from that retired counter, so
**neither is a safety verdict**. FUSE read `3/2 capped=true dangerous=false` on 2026-08-05 only
because a DB-seed replay incremented the label three times **while emitting no order**; at 0 or 1
the identical state would have read `dangerous=true`. The P1.3 boot-hold release depends on this
and is therefore **unvalidated**. Tracked separately; not fixed here.

`arm_age_secs` and `stale_session` were added to the same snapshot in the session-roll change —
both derive from the session anchor, not the counter, so replay cannot inflate them.

## 6. How to check a specific no-trade

1. `grep '\[V2-CW-ARM\] <SYM>' /var/log/project-mai-tai/schwab-1m-v2.log`
   ⛔ the **log file**, not `journalctl -u schwab-1m-v2` — that unit does not exist, so it returns a
   header line and reads as a **false zero**. The unit is `project-mai-tai-schwab-1m-v2`. Assert the
   source is non-empty with a known-present control probe before trusting any zero.
2. Compute the flip on the **04:00-ET slice** (§3) — never on a multi-day series.
3. Check the volume floor on the **flip bar**, not the session median.
4. Replay it: `python -m project_mai_tai.backtest --strategy v2 --eh <SYM> <DATE>`. This runs the
   live code. It needs the service env sourced or it fails on DB auth.

## 7. What this doc does not cover

Exits (OMS-owned), the OCO bracket, fan-out leg behaviour, and the resting-order reprice/cancel
ladder. It enumerates the gates verified by direct read on `786bbb6`; it is **not** a complete
transcription of `_cw_v2_track`. When in doubt read the code and update this file — a stale spec
here has now cost a day once.
