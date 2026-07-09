# schwab_1m_v2 ATR-Flip — Path-B decision (TICKET — deferred, do NOT touch in the re-arm PR)

**What Path-B is.** When a variant-B touch's hold-confirmation window has **thin tick coverage**
(`n_ticks < hold_confirm_min_ticks`, default 5), `_resolve_hold` takes the **`fallback_thin`** branch
(`schwab_1m_v2.py` L545–547) and **ENTERS anyway** at the bar-close-equivalent — instead of applying the
+5bps net_delta verdict. So hold-confirm is currently a **soft preference**, not a hard gate: on a thin
feed it falls back to "enter."

**The open question (deferred):** should hold-confirm be a **hard gate** (thin coverage → skip, like a
reject) or stay a **preference** (thin → enter)? This is arguably a **strategy question**, not a defect
— hence deferred, out of the re-arm PR (one change at a time; folding it in would confound the D3/D5
re-run).

---

## ⚠ LOAD-BEARING interaction with the re-arm bug (from the 10-day measurement, 2026-07-09)

The re-arm bug ("burn-the-fake, miss-the-real-flip",
`schwab-1m-v2-atr-flip-rearm-fix-design.md`) and Path-B **partially cancel**. Of 1,267 graze-first
flips (hold-confirm modeled on the first graze):

- **651 (51%)** reject → real flip **MISSED** (the re-arm bug).
- **390 (31%)** hit `fallback_thin` → **ENTER via Path-B** — i.e. Path-B has been **masking the re-arm
  bug 31% of the time.** Those grazes claimed the segment but still produced a fill, so they weren't
  counted as misses.

**Consequence — ordering is now load-bearing, not tidy:** if Path-B is ever closed (hold-confirm made a
hard gate) **before** the re-arm fix has landed, those 390 masked fills become misses too:

> **651 + 390 = 1,041 missed flips ≈ 20% of ALL ATR signal.**

**RULE: fix re-arm FIRST, then revisit Path-B. NEVER the reverse.** Closing Path-B without re-arm in
place would roughly *double* the miss rate.

---

## Semantics to make explicit NOW (in the re-arm PR, documentation only — no behavior change)

The guard `atr_fired_in_short_seg` — the thing the re-arm fix resets on rejection — means **"an entry
has been produced (a fill), or a hold is genuinely pending" for this short segment.** "Produced an
entry" MUST include **any path that fills**: touch-confirm, the flip-close backstop (new), AND
`fallback_thin`/Path-B. It must NOT mean only "the touch path fired." The re-arm PR documents this
definition and makes the guard consistent with it (a `fallback_thin` fill legitimately claims the
segment — it entered); it does **not** change Path-B's behavior. When Path-B is later revisited, this
definition is the anchor for what "already entered" means.

---

## When to pick this back up

After the re-arm fix is deployed AND D3/D5 have been re-run on the corrected entry. Then decide
hard-gate-vs-preference on its own, with its own before/after measurement (miss-rate change, fill-quality
of the thin fallbacks, net edge). Not before.
