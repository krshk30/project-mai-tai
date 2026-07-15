# Design — the FALSE-FLAT reconcile: never delete protection on absence of evidence

> **Status: DESIGN ONLY, no code.** Written 2026-07-15 after a live naked position (ERNA, real
> money). Review before any PR. **This is a live-money stop path — the fix must be behaviour-
> identical except where the change is the point.**
>
> **Severity: this is the bug that turned a broken entry into an unprotected position.** It is
> **not** ORB-specific and **not** resting-entry-specific: the same helper backs the **v2 CW
> managed-exit reconcile**, so it is armed on v2 right now with the resting-entry flag off.

---

## 1. The incident (2026-07-15, ORB/Webull, real money, qty 2)

| Time (ET) | Event |
|---|---|
| 09:33:00 | ORB rests a buy `STOP_LIMIT` on ERNA (first live day of `orb_resting_entry_enabled`) |
| **09:33:17** | **FILLED — buy 2 ERNA @ 9.47**, real Webull fill `9KGME18JSJK753VLVQ780EBGSB:2` |
| 09:33:18–47 | native sell-`STOP` guard armed / re-armed → `cancelled` ×3 |
| 09:34:00 | sell `STOP` → **REJECTED `ORDER_NOT_SUPPORT_REVERSE_OPTION` (http 417)** |
| 09:34:16–18 | bid 9.13 → 9.09 through the 9.196 trail ⇒ `[HARD-STOP TRIGGERED]` ×7, **all 3 closes fail** |
| **09:34:18** | **`[HARD-STOP RECONCILE-FLAT] orb ERNA broker flat after 3 failed closes -> clearing phantom armed stop`** |
| 09:34:18 → | **NAKED.** `oms_armed_stops` empty, `virtual_positions` 0. Nothing owns the position. |
| ~09:37 | **Operator closes it by hand in Webull at ≈ −17.5% (−$3.32).** |

**The OMS could not have closed it.** Once the managed/virtual row is gone, the sell quantity clamps
to a `virtual_position` of 0 (the scoping invariant) — a position it does not believe in is one it is
structurally incapable of selling. Manual close was the only path. **The bot deleted its own ability
to protect the trade.**

**Ground truth:** a `buy` fill exists, **no `sell` fill exists**. We held 2 shares the entire time.
The broker read that said "flat" was **wrong**.

---

## 2. Root cause — `oms/service.py:2936 _broker_symbol_is_flat`

```python
try:
    positions = await self.broker_adapter.list_account_positions(broker_account_name)
except Exception:
    return False                      # <-- honours the promise ONLY for raised errors
target = str(symbol).upper()
for position in positions or []:
    if str(getattr(position, "symbol", "")).upper() != target:
        continue
    try:
        if Decimal(str(getattr(position, "quantity", 0))) != 0:
            return False
    except (TypeError, ValueError, ArithmeticError):
        return False
return True                           # <-- symbol ABSENT *or* list EMPTY *or* list None
```

Its own docstring states: *"A read failure returns False — **NEVER** clear protection / a managed
row on an unconfirmed read (that could strip a genuinely-held position)."* **The code only keeps
that promise for exceptions.** Three non-exception paths fall through to `return True` = FLAT:

| Broker returns | Truth | Function says |
|---|---|---|
| raises | unknown | `False` ✅ |
| `[{ERNA, qty 2}]` | held | `False` ✅ |
| `[{ERNA, qty 0}]` | flat | `True` ✅ |
| `[{OTHER, qty 5}]` — ERNA **absent** | **unknown** | `True` ❌ |
| `[]` — **empty** | **unknown** | `True` ❌ |
| `None` | **unknown** | `True` ❌ |

**Absence of evidence is being treated as evidence of absence.** An empty list is
indistinguishable from a silent read failure, and marks **every symbol on the account as flat**.

### Why it fired here (mechanism INFERRED, not pinned)

The read is **not logged**, so we cannot say from the record whether Webull returned `[]` or a list
omitting ERNA. The leading hypothesis is **positions-endpoint lag on a fresh fill**: bought
09:33:17, asked 09:34:18 — **61s later**. If that endpoint does not reflect a fill within ~a minute,
then **any stop triggering shortly after entry sees "flat" and deletes itself** — i.e. protection
vanishes exactly when a fast post-entry reversal needs it. That is precisely ERNA's shape.
**Fix 0 below exists to turn this inference into evidence.**

### The compounding assumption (#436 Bug C)

`service.py:2916` reasons: *"the close neither placed nor named a no-position reason (e.g. Webull
`ORDER_NOT_SUPPORT_REVERSE_OPTION` **after the shares were flattened out-of-band**)"* — i.e. it
treats repeated close failures as *probably already flat*, then asks the broker to confirm.

**Today that assumption was false** (shares were held; the rejection meant something else), and the
confirmation step **rubber-stamped it**. Two independent "probably flat" signals, neither sound,
combined to delete a real stop. A wrong prior is survivable; a wrong prior plus a confirmation that
cannot say "I don't know" is not.

---

## 3. Blast radius (both live today)

| Caller | Line | Deletes | Live now? |
|---|---|---|---|
| ORB hard-stop reconcile (`_broker_position_is_flat`) | 2922 | `_armed_hard_stops` + the F2 durable mirror row | **YES** |
| v2 CW managed-exit reconcile (`_v2_close_reconcile_flat`) | 1988 | `oms_managed_positions` row + quote-eval disarm | **YES** |

Turning `orb_resting_entry_enabled` off (done 2026-07-15 10:37 ET, ORB PID→177630) removes the
*trigger* seen today but **not this bug**. Any path that produces ≥3 failed closes reaches the same
false flat — on v2 too.

---

## 4. Proposed fix

**Principle: `is_flat` must be able to answer "I don't know", and "I don't know" must never delete
protection.** Return a tri-state, not a bool.

### Fix 0 — log the read (prerequisite; do this first)
Log what the broker actually returned (count, whether the symbol was present, raw qty). Today's root
cause is *inferred* because nothing recorded it. Cheap, zero-risk, and makes the next occurrence
diagnosable. **Worth shipping even alone.**

### Fix 1 — empty/None ⇒ UNKNOWN, never flat
`positions` empty or `None` ⇒ `UNKNOWN`. Indistinguishable from a silent failure, and an empty list
would otherwise declare *every* symbol flat.

### Fix 2 — flat requires POSITIVE confirmation
`FLAT` only when the symbol is **present with qty 0**, or absent from a **non-empty** read. Absent
from an empty/degenerate read ⇒ `UNKNOWN`.

### Fix 3 — contradiction check against our own ledger
We already know what we did: a `buy` fill and no offsetting `sell` fill ⇒ **we are long**. If our own
fills say long and the broker says flat, that is a **contradiction**: return `UNKNOWN`, keep the stop,
and log **loudly** (candidate: ntfy — this is the naked-position precursor). Never silently side with
the broker against our own execution record.

### Fix 4 — fresh-fill grace
Do not honour a flat read within `N` seconds of our own fill for that symbol (start `N=120`,
configurable). Directly targets the settlement-lag hypothesis. **Gate on Fix 0's evidence** — if the
logs show Webull is prompt, this is unnecessary complexity; if they show lag, this is the fix.

### Explicitly NOT proposed
- **Do not** stop reconciling. The phantom-churn it prevents (07-13 AGEN, 181× loop) is real. The
  bug is the *inference*, not the existence of the check.
- **Do not** trust our ledger alone — the broker read is what catches genuine out-of-band closes
  (exactly what the operator did today). Both inputs stay; only their disagreement handling changes.

### Failure-mode trade
Today: a wrong "flat" ⇒ **naked position, unbounded loss** (ERNA ran to −17.5% unwatched).
After: a wrong "held" ⇒ the stop keeps retrying closes on a phantom ⇒ **bounded, noisy, visible**
churn. **Strictly the better direction to be wrong in.**

---

## 5. Test plan (the verdict is the tests, not a clean run)

Each must **fail before the fix**:
1. `list_account_positions` → `[]` ⇒ stop **retained** (today: deleted).
2. → `None` ⇒ retained.
3. → `[OTHER]`, target absent ⇒ retained (degenerate/short read).
4. → `[TARGET qty 0]` ⇒ **cleared** (the genuine out-of-band close must still work — no regression).
5. → raises ⇒ retained (existing behaviour preserved).
6. **Contradiction:** own fills say long, broker says flat ⇒ retained + loud log.
7. **Fresh-fill grace:** flat read < N s after our fill ⇒ retained; > N s ⇒ honoured.
8. **Both callers** covered — ORB armed-stop *and* v2 managed-row. A fix to one only is a half fix.
9. **ERNA replay:** the 2026-07-15 sequence (fill → reverse-conflict rejects → 3 failed closes →
   empty read) ⇒ stop **retained**. This is the regression anchor.

---

## 6. Rollout

- Flag `oms_reconcile_require_positive_flat` (default **true** — the safe direction is the default;
  `false` restores today's behaviour as the rollback lever).
- Attended, fleet-flat, OMS choreography (stop strategy → restart oms → start strategy).
- **Not** market hours: touches the live stop path.
- Watch: `[RECONCILE]` read logs, any contradiction warnings, no return of the AGEN-class churn.

---

## 7. Out of scope (tracked separately)

- **`INTENT_MAX_AGE` (30s) kills resting stop-entries** — the resting order lives ~30s, burning ORB's
  2 attempts in ~60s ⇒ suppressed for the day. Needs a per-intent exemption; do **not** raise the
  guard globally. Moot while the flag is off.
- **`ORDER_NOT_SUPPORT_REVERSE_OPTION` on the protective stop** — mechanism **NOT pinned**; the buy
  order shows `filled`, not working, so the "resting buy reserves the shares" theory does not fit.
  Needs its own investigation. See `service.py:4306` ("a resting sell still reserves the …").
- **Whether the resting entry is worth keeping at all** — it collided with two OMS guards on day one.
  Operator decision.
- **Pre-market validation was structurally blind:** `validate_buy_stop.py` places orders *directly
  through the adapter*, bypassing the OMS intent lifecycle — so it could not exercise
  `INTENT_MAX_AGE`, never produced a real fill to arm a stop against, and never hit reverse-conflict.
  **Any future entry-mechanism gate must run through the real intent path**, or it proves only that
  the broker accepts the order shape.

[[project_mai_tai_oms_orb_exit_fixes]] [[project_mai_tai_oms_scoping_invariant]]
[[project_mai_tai_orb_rnd_2026_07_13]] [[project_mai_tai_v2_entry_warmup_gate]]
