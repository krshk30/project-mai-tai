# B9 — §82 causes 2 and 3: routing the fan-out leg's OUTCOME back to the strategy

**Status: DESIGN, not built.** Written 2026-08-20. The build is deliberately not started — see
§7, which argues one part is small and safe and another part is neither.

**Cost of the defect:** 22 duplicate legs, every one filled worse, **median 4.58%**.

---

## 1. The one sentence

The strategy decides whether to place a second Webull leg using **two counters that can never see a
Webull fill**, so "we already have this leg" and "we have nothing" are the same reading — and the
code comment that says otherwise is wrong.

## 2. The blocker, stated as a constraint rather than a problem

> The strategy cannot distinguish *"blocked, never placed"* from *"placed and filled"*, because the
> Webull outcome never reaches it.

⛔ **A non-releasing counter trades a duplicate-fill defect for the FGI silent-no-order defect.**
That trade is not acceptable, and it is the reason this is a design note and not a patch. Any fix
that makes the latch *harder* to release must also make a stuck latch **loud**, or we have swapped a
defect that costs 4.58% for one that costs the entire trade and says nothing.

⇒ **Design rule for everything below: the latch may only be held by POSITIVE evidence that the leg
exists. Absence of evidence must continue to release it.** That keeps the current failure direction
(duplicate, visible, costed) and never introduces the silent one.

## 3. Why the two counters are blind — pinned, not asserted

`SymbolState` carries two quantities, both written by `update_position`:

| field | source | meaning |
|---|---|---|
| `position_qty` | union: `virtual_positions` ∪ in-flight open intents | "do we own this, conservatively" |
| `position_qty_held` | `virtual_positions` only | shares actually owned per filled orders |

Both come from `_fetch_position_maps`, which scopes every read to one broker account:

```python
account_name = self.settings.strategy_schwab_1m_v2_account_name
broker = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
...
select(VirtualPosition).where(VirtualPosition.broker_account_id == broker.id, ...)
```

That is the **Schwab** account. The fan-out leg fills on **`live:orb`**, a different
`broker_account_id`. ⇒ **A Webull-only fill moves neither counter.**

⛔ **And a load-bearing comment in `update_position` asserts the opposite:**

```
⭐ BOTH LEGS, for free: `SymbolState` is per SYMBOL, not per account, so the Webull
fan-out leg's fill lands here too. A fan-out-only cross -- Schwab rejected by the
API-open block, as UPC hit on 2026-08-03 -- therefore still consumes its slot...
```

`SymbolState` being per-symbol is true and irrelevant: the *query that feeds it* is per-account.
The slot accounting for fan-out-only crosses rests on this comment, so that accounting is wrong too
— a separate consequence worth its own check, not folded in here.

⭐ This is the fourth instance of [[project_mai_tai_broker_shaped_rule_needs_broker_scope]] in the
other direction: not a rule missing a broker scope, but a **broker scope that a comment forgot**.

## 4. Cause 2 — the claim expires *because* we look flat

```python
if state.cw_v2_emit_claimed and state.position_qty == 0:
    if now_ms - state.cw_v2_emit_ms >= self._atr_rearm_timeout_secs * 1000:
        state.cw_v2_emit_claimed = False
```

The release is gated on `position_qty == 0`. For a Webull-only fill that is **permanently true**, so
the timeout always matures and the claim always releases. The gate intended to mean *"nothing came
of that emit"* actually means *"we cannot see what came of that emit"*.

## 5. Cause 3 — a phantom close re-arms the latch

The close-detect block fires on the **union** falling to 0 and unconditionally re-arms:

```python
state.fanout_webull_claimed = False   # fan-out: allow a fresh Webull leg on the reclaim
state.fanout_claim_ms = 0
```

The union includes in-flight intents, so **one of our own resting intents going terminal drives it to
0** with no shares ever held. The code already computes exactly the right discriminator one line
earlier and uses it **only for a log string**:

```python
spurious = prev_held == 0 and state.position_qty_held == 0
```

⇒ Cause 3 is the cheaper half: the value needed is already computed and already correct.

## 6. What has to flow back, and from where

Both causes need one fact the strategy does not have: **does a fan-out leg exist for this symbol
right now?**

### Source options, ranked

| # | source | verdict |
|---|---|---|
| **A** | `fills` rows for the fan-out account | ⭐ **preferred** — append-only; a fill is never un-written |
| B | `virtual_positions` on the `live:orb` account | ⛔ **inherits a known defect** |
| C | a Redis event published by the OMS on fan-out fill | most faithful, largest build |

⛔ **B is the trap.** [[project_mai_tai_virtual_positions_false_zero]] — `[VIRTUAL-CLEAR]` zeroes a
live row 0.7 s after the fill and never restores it; the root cause and four consumers are still
untouched. Reading the latch's evidence from a table with a known false-zero means the latch
releases spuriously — i.e. the duplicate comes back, and now it comes back *through the fix*.
⭐ Note the direction is at least SAFE (false-zero → release → duplicate → visible, costed), which is
why B is a degraded option rather than a dangerous one. But it would make the fix look broken while
behaving exactly as designed, which is its own cost.

⇒ **Take A.** `fills` is the same table the P&L work already trusts
([[project_mai_tai_fill_price_lives_in_the_fills_table]]) and it cannot read false-zero.

### The shape

1. `_fetch_position_maps` returns a **third** map, `fanout_held`, scoped to the fan-out account and
   sourced from `fills`.
2. `update_position(..., fanout_qty=...)` writes a **new** `SymbolState.fanout_qty`.
3. Cause 2: release the emit claim only when `position_qty == 0 **and** fanout_qty == 0`.
4. Cause 3: release the fan-out latch only when **not** `spurious` — i.e. gate on
   `position_qty_held`, the value already computed and discarded.

⛔⛔ **`fanout_qty` MUST NOT feed any other entry gate.** The moment it is folded into
`position_qty`, v2 believes it holds a Schwab position when only the Webull leg is open — the exact
defect `_fetch_reportable_state` was split out to prevent. It is an input to **two latch releases**
and to nothing else. A test should pin that, not a comment.

5. **A held latch LOGS.** `[V2-FANOUT-LATCH-HELD] SYM held by fanout_qty=N age=Ms` every time it
   suppresses a leg. This is the FGI insurance from §2: a latch that goes quiet is the failure mode
   we are trading *away from*, so it must be the loudest thing in the change.

## 7. Size, and the recommendation

**Cause 3 is small and safe. Cause 2 is neither.**

- **Cause 3** changes one condition to use a variable computed on the line above. No new query, no
  new field, no new source of truth. Its blast radius is the latch it already writes.
  ⇒ **Recommend building it now, on its own**, with a test that a spurious union→0 does not release
  the latch and a control that a REAL close still does.

- **Cause 2** needs a new cross-account read on the position-poll path, a new `SymbolState` field,
  and a new failure mode to insure against. That is the part that deserves the deploy-window
  attention, and it should not ride in on cause 3's coat-tails.
  ⇒ **Recommend a separate change**, after cause 3 is live and its effect on the 22 is measured.

⭐ Splitting them also gives a **measurement we cannot otherwise get**: #739 fixes the reactive latch
(14 of 19). If cause 3 lands alone, the residual duplicates tell us how much of the remainder cause 2
actually owns — instead of shipping both and being unable to attribute the improvement to either.
That is [[feedback_check_which_parts_already_work]] applied before the build rather than after.

## 8. What this design does NOT resolve

- The **slot accounting** for fan-out-only crosses, which rests on the same wrong comment (§3). It
  is a consequence of the same blindness but a different code path, and it is not fixed here.
- The `virtual_positions` false-zero **root cause** and its four remaining consumers.
- Whether the 22 duplicates are fully explained by causes 2 and 3. §82 was believed to have two
  causes and turned out to have three; the residual after cause 3 is the honest test of that.

## 9. Open question for the operator

Cause 2's release condition has a third possible reading. Should the emit claim release when:

1. `position_qty == 0 and fanout_qty == 0` — strictest, and the one above; or
2. the fan-out leg reaches a **terminal** broker state (filled-and-exited, cancelled, rejected),
   read from `broker_order_events`?

(2) is more truthful but sits on a table that
[[project_mai_tai_broker_order_events_conflates_client_aborts]] shows conflates our own aborts with
broker rejects — so a client abort would read as "leg is done, release" and reproduce the duplicate.
⇒ Recommend (1) until Q1 lands the `source` column, then revisit.
