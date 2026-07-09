# schwab_1m_v2 — remove the legacy `atr_fired_in_short_seg` bool (TICKET — GATED follow-up)

**Owner:** open · **Priority:** medium · **Filed:** 2026-07-09 · **Gate:** the ATR re-arm flag
(`strategy_schwab_1m_v2_atr_flip_rearm_enabled`) is ON in production and proven over multiple sessions.

## Why this exists

The re-arm fix layers a new guard (`atr_guard`: UNCLAIMED/PROVISIONAL/CLAIMED + `atr_emit_ts_ms`)
**alongside** the shipped `atr_fired_in_short_seg` bool. That was a deliberate transitional choice —
byte-identical-off is worth more than elegance for the first touch of a live-money strategy: with the flag
off, the bool path runs unchanged.

**But two state fields modelling one concept ("has this short segment been entered?") is exactly the
drift condition that caused the original bug** — the live strategy and the backtest each had their own
copy of the entry logic and parity certified them into agreement on a shared defect. Leaving the bool and
the guard both live, gated by a flag, is the same smell at a smaller scale. It is acceptable **only** as a
transition.

## The change (once gated)

Once the re-arm flag is ON in production and proven:
1. Delete `atr_fired_in_short_seg` and every `if not self._atr_rearm_enabled: … bool …` branch.
2. Make the guard the **single** unconditional path (remove the flag too, or keep it as a kill-switch for
   one more cycle then remove).
3. `_set_atr_guard` stays as the one centralized guard write.
4. Re-run the byte-identical / golden suite against the **flag-on** behavior (now the only behavior).

## Guardrails

- **Do NOT do this while the flag is off or unproven** — the bool is still the shipped path until then.
- Characterization-first: green on the flag-on behavior → remove the bool → prove identical.
- Compose with the **shared `atr_flip_entry.py` extraction** (the other logged follow-up, from the re-arm
  design §3): removing the bool and extracting the shared entry module are the two halves of retiring the
  drift class permanently. Sequence them, don't bundle.

## When to pick up

After the attended flag flip + D3/D5 re-run + a few clean sessions with the flag on. This is cleanup that
locks in the fix; it is not itself a behavior change.
