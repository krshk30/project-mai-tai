# Webull resting entry + exit protection — DESIGN (design-first; only the cancel-verify gate ships here)

**Operator decision 2026-08-12: option B — bracket and software ladder both live, first-wins.**
Rationale, in his words: on entries a no-fill is acceptable and a bad fill is not; **on exits that
inverts — you cannot decline to exit.** A position we hold must always have something trying to sell
it. B fails in the recoverable direction: a double-sell is recoverable, an unexited position is not.

⛔ **This PR ships ONE thing: cancel-and-verify** (`oms_cancel_verify_enabled`, default **OFF**).
Everything else below is design. The operator set cancel-verify as the explicit gate before any
Webull code, and it is also independently the cure for an open P0.

---

## 1. What was actually measured first (and what it overturned)

Four premises were checked against the broker and the tape before designing. All four inverted.

| premise | measured |
|---|---|
| "Webull accepts a STOP_LIMIT combo master" | ❌ **417** `invalid order_type, value: STOP_LOSS_LIMIT`. Probe W, CORE/RTH, 2026-08-12 |
| "Schwab's brackets never fill — ladder wins by ~3s, 0-for-94" | ❌ bracket wins **125 of 141** on Schwab, **166 of 174** on Webull |
| "Brackets stopped on 08-07 / lost the stop leg on 08-04" | ❌ 08-04 = 31 children / 31 stops / 31 targets; 08-07 the bot placed **no Schwab entries at all** |
| "Webull has no broker-side protection" | ❌ **174 bracketed entries** on `live:orb` in 14 days |

**And the ladder does not race the bracket.** All 8 exits the ladder won were at or after **16:00 ET**
— the RTH OCO expiring at the close and the EH software ladder taking over. Intraday the bracket
wins. So B's double-sell is a **16:00-boundary problem, not an intraday one.**

## 2. ⚠️ This diverges the brokers. It does not align them.

Stated at the top deliberately, so nobody later "aligns" them by copying the wrong side:

| | entry | protection attached at entry |
|---|---|---|
| **Schwab (today)** | STOP_LIMIT master — **rests at the ATR line** | ✅ OCO legs on the same order |
| **Webull (today)** | MARKET master — **chases** | ✅ OCO legs on the same order |
| **Webull (proposed)** | bare STOP_LIMIT — **rests** | ❌ **impossible — the broker refuses it** |

⛔ Webull can have a resting entry **or** attached protection, never both. That is the broker's
constraint (Probe W shape B), not a gap in our code. Choosing the resting entry means **giving up
protection-at-fill on that leg** in exchange for removing the chase. The chase is real — Webull's
market entries own the **+610 bps** and **+684 bps** outliers — but so is the exposure.

⛔ **DO NOT remove `webull.py:949`.** It refuses a non-LIMIT/MARKET combo master, and the probe proved
the broker refuses exactly that. **174 live Webull brackets depend on the shape it enforces.**
Removing it would emit a payload the broker rejects and break bracket placement on the fan-out leg.

## 3. The crux: does the software ladder still fire when a bracket is live?

**Under B: yes, by design — and that is the point.** The ladder is never stood down for Webull.

⛔ **Option C (stand down on bracket existence) is the trap.** It defers on a liveness signal Webull
does not reliably provide. A silently-absent bracket plus a deferred ladder means *nothing is trying
to exit* — the FRTT 08-11 shape and the gap #678 shipped to close. Do not rebuild it.

⛔ Option A (bracket as backstop only) was rejected on the premise that Schwab's bracket never fires.
That premise is false — it fires 89% of the time — so A is not "honest but pointless", it is simply
not what we want here.

Note Schwab already runs **C** (`_native_oco_stand_down_active`, `service.py:3554`, fail-open by
design). Webull under B will run differently on purpose.

## 4. The double-sell, designed rather than accepted

**Q: When one fills, how fast is the other cancelled?** Two mechanisms — only one is safe.

- **Bracket leg ↔ its sibling: atomic at the broker.** n=47 measured, median/p90/max **0.00s**, same
  close timestamp. The bracket's own two legs cannot both fill. Not a risk.
- **Ladder fill → the bracket leg: our cancel, and it is fire-and-forget.** FRTT 2026-08-11: cancel
  emitted, died on the network, order **WORKING and unowned for 136 minutes**. **Unbounded.**

⇒ **That is B's entire exposure, and it is why cancel-verify is the gate.**

**Q: If both fill, do we detect it?** Yes — `classify_oversell` (#679) plus the position sync. And
no short has ever been recorded: zero rows with `quantity < 0` in `account_positions` or
`virtual_positions`, ever.

**Q: What reverses it?** Nothing automatic. Manual. ⚠️ Detection ≠ reversal; do not conflate them.

**Q: Can the ladder gate on a confirmed FILL rather than existence?** Yes, and it is the cheap win —
most of B's safety with most of C's cleanliness. The plumbing already exists
(`oms_native_oco_exit_poll_enabled=true` feeding `-ocoexit-` fill rows). The ladder emit checks
*"has a bracket fill been RECORDED for this symbol in the last N seconds"* — a fact, not a signal.
Absence still lets the ladder fire, so it is not C. **Phase 2.**

## 5. What ships in this PR — cancel-and-verify

`oms_cancel_verify_enabled` (**default False ⇒ byte-identical**), plus `_attempts` (3),
`_interval_seconds` (2.0), `_resubmits` (1).

After a cancel is submitted, `_verify_cancel_landed` **reads the order back from the broker** until
it is settled; if the reads say it is still working it **re-submits the cancel**; and if it still
cannot be confirmed it emits `[OMS-CANCEL-UNCONFIRMED]` carrying the coid and broker id.

Three deliberate choices:

1. **A raised cancel is an UNKNOWN, not a failure.** The exception is swallowed *only when the flag
   is on*, and the verifier resolves it by reading. That is the FRTT case exactly.
2. **`accepted` / `PENDING_CANCEL` is NOT proof.** `_CANCEL_TARGET_SETTLED_STATUSES` deliberately
   excludes it — Schwab answers a just-issued DELETE with PENDING_CANCEL, and believing it is the
   assumption that cost 136 minutes.
3. **Backgrounded, not inline.** Verification sleeps; doing it inline would stall
   `process_trade_intent`, which also carries **exits**. Delaying an exit to confirm a cancel trades a
   rare unowned order for a common late stop — the wrong direction.

**Never silent:** every exit path logs. An unverifiable cancel is a WARNING, not an absence.

### Validation
- 13 new tests, **1978 passed / 0 failed**, ruff clean.
- **5 mutations, each caught by the test that should catch it:** `accepted` added to the settled set;
  resubmit removed; unwired from `_process_cancel_intent`; raise swallowed with the flag OFF; the
  UNCONFIRMED warning renamed. All red, then reverted green.
- ⛔ **Broker calls are exercised only against a scripted adapter.** The real Schwab/Webull
  `fetch_order_update` behaviour under this flag is **UNEXERCISED until it runs live.**

## 6. Sequence — not to be reordered

1. **cancel-verify** (this PR) — flag OFF. Enable and watch for `[OMS-CANCEL-CONFIRMED]` /
   `[OMS-CANCEL-UNCONFIRMED]` on the existing cancel traffic before anything depends on it.
2. **confirmed-fill gate** on the ladder emit (§4).
3. **Webull resting entry** — bare STOP_LIMIT via the single-order path at the same ATR line and band
   Schwab uses, RTH only, protection attached on fill. Only after 1 and 2 are proven live.

⚠️ **Not to be rushed into a deploy window.** This is the stop-loss path — the thing that bounds the
cost of a bad trade. Deployed when proven, not when the clock says so.

## 7. Still open, deliberately not answered here

- **Level movement / reprice.** The exit legs are computed once at entry off the **decided** price and
  never repriced; the CW_FLOOR ratchet lives only in OMS memory. Two numbers, no reconciliation path.
- **The 16:00 boundary** — where all 8 real ladder-won exits live. Webull's combo is RTH-only too.
- **Penny rounding.** At ~$1.40 one cent is ~70 bps, so the configured +2%/−5% actually ran
  **+2.11…+2.47% / −4.38…−5.11%** on 08-11, and the 0.5% entry band collapses to zero on sub-$2 names.
  Any level-setting work should fix the unit, not the number.
