# Evening sheet — MON 2026-08-24, post-close window

> Pre-written 07:20–08:00 ET the same morning. Every "pinned" line below was measured
> today against the live box; every "forecast" line is a prediction and is labelled as one.

---

## 0. BANKED THIS MORNING — do not re-run these tonight

| check | result | how |
|---|---|---|
| signal-4 instrument | ✅ **CONTROL REPRODUCED `119\|19\|22`** at 07:25 ET | `bash ops/health/signal4_duplicate_legs.sh` |
| instrument present on box | ✅ both scripts, `trader:trader 664`, dated 08-23 13:15 | `ls -l ops/health/` |
| box HEAD | `253752a`, **0 CODE commits behind** (`-- src ops`); the 2 behind are docs #767/#768 | box reflog |
| log readability | ✅ **sudo-readable by CONTENT**: oms 196,983 lines · v2 87,800 across all rotations | `sudo zcat -f …\|wc -l` |
| ⛔ repo path trap | `/opt/project-mai-tai` **exists and is NOT a git repo** (stub, `src/` only). The real tree is **`/home/trader/project-mai-tai`**. A `cd A \|\| cd B` chain silently lands on the stub. | hit and pinned today |
| token store | `refresh_token_expires_at = 2026-08-25T20:46:01Z` = **Tue 08-25 16:46 ET** | read from the store |

---

## 1. ⛔⭐⭐ THE #739 GRADE — TODAY IS ITS **FIRST** TRADING SESSION

**Pinned, from the box reflog + `merge-base --is-ancestor`:**

| box HEAD | live for sessions | contains #739 (`844be295`)? |
|---|---|---|
| `f18132e` | 08-20 | ⛔ **NO** |
| `2a43b29` | **08-21 (Friday)** | ⛔ **NO** |
| `253752a` | 08-24 (today), since Sun 08-23 09:35 ET | ✅ **YES** |

`844be295` was committed to `main` **08-21 08:03 ET** — *after* the box's last pull before Friday.
The v2 service ran Friday on `2a43b29`.

⇒ **Friday is #739's baseline, not a grade of it** — which is exactly what the handoff header
says ("Friday's numbers are the PRE-FIX BASELINE"). **Tonight is data point #1, not a verdict.**

### The denominator forecast — say this BEFORE the number arrives

Signal-4 denominator (segments carrying a segment id), per ET day, `live:orb` filled fan-out
legs excluding `rth_resting_mirror` — **pinned today**:

```
08-10  23 segs (4 dup) | 08-17   2 segs (0 dup)
08-11   8 segs (4 dup) | 08-18   4 segs (1 dup)
08-12  14 segs (1 dup) | 08-19   4 segs (0 dup)
08-13   8 segs (0 dup) | 08-20   5 segs (1 dup)
08-14   7 segs (1 dup) | 08-21   2 segs (0 dup)
```

Median of the last five sessions = **4 segments/day**. Baseline is **19 of 119 = 16.0%**.

⇒ **FORECAST: tonight's signal 4 will land at a denominator of ~2–5 and will be a NON-RESULT
again.** At 4 segments/day it takes weeks to accumulate a population comparable to 119.
⛔ Do not let a small clean number tonight be written up as "#739 works".

### ⇒ THE RECOMMENDATION THIS CHANGES (operator's call)

The handoff holds #766 behind the #739 grade because **#766 moves signals 1 and 3**.
**#739's signal is 4.** Checked today: the two do not overlap —

- signal 4's population since 08-01 is **150 rows, 100% `side='buy'`, 0% exit-pair-marked**;
- **zero** `live:orb` rows have ever carried `webull_exit_only_pair` (consistent with §257: the
  constructor raised, so the success branch never recorded one).

⇒ **#766 cannot contaminate tonight's #739 reading.** ✅ **§262: the hold is RELEASED — #766
deploys tonight**, conditional on #769 landing first. Today's signal 4 is recorded as **data
point 1** of an accumulating post-#739 window, **explicitly not a verdict — say so on the line.**

### ✅ §262 — THE HARDENING IS DONE: **PR #769**, merge it FIRST tonight

`ops/health/signal4_duplicate_legs.sh` had **no side filter**. §186 already settled that an entry
is a filled BUY, so the filter was always the definition — it merely held by accident, because the
exit-pair success path was dead and recorded nothing.

Added `AND bo.side = 'buy'` **at both query sites** (`measure()` and the foot-of-script BLIND SPOT
count — a denominator and its caveat disagreeing is worse than either being wrong alone).

**Control re-run live against the box after the edit: `expected 119|19|22  got 119|19|22` ✅.**

⭐ **And it bites — mutation, not assertion.** One synthetic exit leg injected into a read-only
CTE (same symbol / segment / `cw_entry_n` as a real buy, `side='sell'`, `webull_exit_only_pair`
stamped — exactly the row #766 will record) scored over the §82 control window:

| variant | segments\|dup\|extra |
|---|---|
| **WITHOUT** the filter | **`119\|20\|23`** ← one exit row manufactures a duplicate *and* an extra leg |
| **WITH** the filter | **`119\|19\|22`** ← unchanged |

⛔ **#769 must merge BEFORE #766 deploys**, or every signal-4 reading from the first exit-pair
fill onward is contaminated.

---

## 1b. §263 — CAN THE GROUPING KEY MOVE TO `cw_entry_n`? **NO.** Here is why.

**P7 and #570 do not disagree — they answer different questions.** P7's rule is
*"first-vs-reclaim keys on `cw_entry_n`"*: that is a **per-fill LABEL** (which entry is this —
1 = first, 2 = reclaim), and for that question `cw_entry_n` is right and its 97% coverage is the
relevant number.

Signal 4 asks a **GROUPING** question: *did one entry slot fill more than once?* `cw_entry_n` is
the ordinal **inside** a segment, not the boundary **of** one. Two legs both stamped
`cw_entry_n=1` are duplicates only if they belong to the **same arm** — and without a segment id
you cannot tell "one arm filled twice" from "two arms each filled once."

### Measured, §82 control window, `live:orb` — the counterfactual

| grouping key | groups | dup groups | extra legs |
|---|---|---|---|
| **A — current `(symbol, cw_arm_bar_ts)`** | **119** | **19** | **22** ← §82's published truth |
| B — `(symbol, cw_entry_n)` | **65** | 45 | **158** |
| C — `(symbol, ET day, cw_entry_n)` | 69 | 47 | 154 |

⛔⭐⭐ **THE DENOMINATOR DOES NOT DOUBLE — IT HALVES (119 → 65).** Field coverage is not
grouping coverage. `cw_entry_n` is an **ordinal**, so a symbol's many arms all collapse onto
`n=1` and become *fewer* groups, not more.

And the numerator explodes: **158 extra legs against a ground truth of 22 — 136 manufactured
false duplicates**, on the exact window where §82's answer is known.

### Why it is worse than the blind spot it was meant to cure

On the resting path — precisely the leg with the coverage gap — P7 records that `cw_entry_n` is
*"stamped but never incremented"*. Measured today on `live:orb` (08-01→08-24, filled buys):

```
eh_resting    30 fills   ALL cw_entry_n=1    0 with a segment id
rth_resting  118 fills       cw_entry_n=1   52 with a segment id
```

**148 of 152 resting fills all claim to be "entry 1".** Grouping on that reads every symbol's
second resting fill of the window as a duplicate of its first. #570 has the live example
(EZRA/UPC at `entry_n=3` on 08-03).

⇒ Moving the key would replace a **declared blind spot** with a **silent wrong answer that
looks plausible** — the `cw_flip_level` failure mode #570 already ruled out, re-derived.

### ⇒ FIND A DIFFERENT QUESTION — and there is a good one

⛔⭐⭐ **#739 SHIPPED WITH NO MARKER.** Its diff adds a latch check and **no log line** —
`if self._dual_broker_fanout_enabled and not state.fanout_webull_claimed:` with no `else`. A
suppression is **completely silent**. That is exactly PR #763's thesis, and it is *why* signal 4
is the only instrument available: there is no direct observable to count.

#739's own commit decomposes the 19: **14 = `reactive` AFTER `rth_resting`**, 2 = same source
twice (not this fix), 3 = three legs. So the fix has a **specific, per-event signature** — and
every prevented duplicate is an event, not a rate.

**Proposal:** add the `else` branch. `[V2-FANOUT-REACTIVE-SUPPRESSED]` when the reactive path
finds `fanout_webull_claimed` already true, `[V2-FANOUT-REACTIVE-LATCHED]` when it claims.

- the **suppressed** count is a **direct measure of #739 working** — non-zero is GOOD NEWS,
  the same polarity as seed-gap 6a;
- it needs **no 119-segment denominator**; its denominator is arm windows, which is log-derived
  and ~100% covered (**0 of 1277 `[V2-CW-ARM]` lines carry `bar_ts=0`** — re-verified today);
- it produces a reading **the first time the path runs**, not in ~30 sessions.

⇒ This converts #739's grade from *unanswerable* to *answerable*, without weakening signal 4's
definition or touching its control. Signal 4 stays as the slow corroborating rate.

⚠ It touches the live v2 entry path, so it is a **queue item, not tonight**.

### The log-derived rekey, for completeness — viable but NOT a drop-in

`[V2-CW-ARM] → [V2-CW-DISARM]` gives real per-cross windows at ~100% coverage. But
**log retention is `daily, rotate 7` and the v2 stream starts 2026-08-17**, while the §82
control window is 08-01..08-19. ⇒ a log-derived signal 4 **cannot reproduce `119|19|22`** and
would therefore be a **new instrument with no reproducible control**, not a rekeying of this one.
It would need its own known-positive, validated on the 08-17..08-19 overlap where both keys
exist — and that overlap is only ~10 segments. Possible, but not cheap and not tonight.

---

---

## 2. CONFLICT CHECK — THE CONFLICT IS **NOT** WHERE THE SHEET SAID

**Pinned by real trial merges in a scratch worktree this morning.**

### ✅ #766 ↔ #758 (both `webull.py`) — **NO CONFLICT, EITHER ORDER**

Auto-merges. Verified by content that both fixes survive verbatim:
`fill_price=None` present ×1 · stale `price=None` gone · `_origin_from_exc` + `_status_reason`
defined · `origin=` on all 4 `_reject` sites · `LOCAL refusal` prefix present.
They touch disjoint regions (#766 at ~L258 exit-pair accept; #758 at L160-176, L682-710, L752, L1385+).
**98 tests pass** (`test_oms_risk_service` + `test_webull_attach_report_seam` + `test_broker_order_event_source`).

⇒ **Neither needs a rebase. Merge order is free.** Recommend **#766 first** on priority.

> 🔎 Follow-up, not a blocker: the report #766 resurrects passes no `origin=`, so it emits
> `origin="unknown"` on a path where the venue demonstrably accepted (a `combo_order_id` is in
> hand ⇒ `broker` by the `protocols.py` contract). #758 could not have caught this — before
> #766 that line **never produced an event**. Worth a one-line §-item after both land.

### ⛔ #760 ↔ #755 (both `oms/service.py`) — **THIS is the conflict**

`git merge-tree`: **CONFLICT in `src/project_mai_tai/oms/service.py` AND
`tests/unit/test_oms_risk_service.py`.** 4 hunks. **Symmetric — same 4 hunks in either order.**

All four are **both-added-at-a-shared-anchor**, no shared symbols:

| hunk | anchor | ours | theirs |
|---|---|---|---|
| src 1 | after `self._p0a_census_submitted` | `_broker_read_*` (4 counters) | `_order_event_*` (3 counters) |
| src 2 | after `self._maybe_emit_p0a_census()` | `_maybe_emit_broker_read_census()` | `_maybe_emit_order_event_census()` |
| src 3 | new methods appended | `_maybe_emit_broker_read_census` | `_append_order_event_isolated` + `_maybe_emit_order_event_census` |
| test 1 | new tests appended | 5 broker-read tests | 5 Q12 tests |

**RESOLUTION = keep both sides, plus two closers.** ⛔ A naive marker-strip does NOT work —
the trailing shared context (`except Exception: pass` in src, `    )` in the test) closes only
the SECOND side, orphaning the FIRST side's `try:` / `assert(`.

At **src hunk 3's `=======`** insert:

```
        except Exception:  # noqa: BLE001 - bookkeeping must never break the sync path
            pass

```

At the **test file's `=======`** insert:

```
    )

```

**Proven both directions this morning:** `#760→#755` and `#755→#760` both resolve to syntactically
valid, green code. **Full unit suite on all five PRs merged: `2336 passed in 283.82s`.**

⇒ **Recommend merge order: #755 first, #760 second** (#760 takes the resolution).
Rationale in §3.

---

## 3. ⚠ ATTRIBUTION MAP — ONE CORRECTION

The pre-stated map was "only #766 changes order behaviour; the other three are observability
and robustness." **#755 is not observability.**

| PR | what it changes | class |
|---|---|---|
| **#766** | one kwarg on the exit-pair success report | **order behaviour** — the attach stops being recorded as a refusal, and `_webull_protect_base` finally gets its handle |
| **#758** | reject `origin=`, status-poll `reason=`, `LOCAL refusal` prefix | observability / attribution |
| **#760** | `[BROKER-SYNC-OK]` + `consecutive=` + census | observability |
| **#755** | ⚠ **reorders `record_fill_if_needed` + `apply_fill_to_positions` to run BEFORE the audit write, and isolates the audit write in a SAVEPOINT** | **LEDGER behaviour** — it changes whether a FILL and a POSITION UPDATE get written when the audit row fails |

#755 does not change what we send the broker, but it changes **our books**. §183/#750 is titled
"the missed migration … drops FILLS". ⇒ if the evening runs short, **#755 is the last of the
three to drop, not the first.**

---

## 4. RUN ORDER

```
16:00  close
16:05  ── GRADE FIRST (nothing merged yet) ──
       bash ops/health/signal4_duplicate_legs.sh          # control must print 119|19|22
       bash ops/health/collect_deploy_evidence.sh POST --since boot
       → record signal 4 as post-#739 DATA POINT 1, with its denominator on the line.
       → signal 6 must read 0.  ⛔ It could not be graded intraday; the close is the first
         moment it is gradeable at all.
       → signal 4's denominator WILL be small — that is #765 truncating more, not decay.

16:20  ── #769 FIRST (§262) ──  the signal-4 BUY filter. Merge before ANY of the below.
       ⛔ It is ops-only and does not deploy, but it must be on main before #766 ships.

16:30  ── OMS ──   merge #766 → #758 → #755 → #760
       (#760 eats the 4-hunk resolution above; paste the two closers)
       gh workflow run deploy-service.yml --ref main -f service=oms -f run_migrations=false

17:00  ── v2 ──    merge #761
       gh workflow run deploy-service.yml --ref main -f service=schwab-1m-v2 -f run_migrations=false

17:30  #13 weekend-outage re-check (2nd weekend now retained)
```

⛔ **REVISED DROP ORDER if it runs long: #760 first, then #758. #766 and #755 STAY.**
#766 is a venue-proven order-behaviour fix; #755 changes whether a fill and a position get
written, so it is **protection, not telemetry**. #760 and #758 are observability and can wait.
Do not compress verification to fit five PRs into one evening before a re-auth morning.

⛔ **§262/#769 is not droppable** — it is a precondition of #766, not a peer of it.

### ⛔⭐⭐ DO NOT MERGE TONIGHT — the deploy health gate

A second workstream has built a **deploy health gate that alters `ops/systemd/deploy_service.sh`**.
It takes effect on the **NEXT DISPATCH — including tonight's own** — so merging it before this
window would change the deploy mechanism *while it is being used*, and any failure would be
ambiguous between the gate and the five PRs it was gating.

⇒ **It waits for a quiet window, alongside #756** (the preflight fences, already held for the
same only-change reason). Anyone picking up the PR queue tonight: it is not in the list above,
and that is deliberate.

### ⛔ §266 rides with #761 — ONE v2 restart, not two

The suppression counter (`[V2-FANOUT-REACTIVE-SUPPRESSED]`) touches the live v2 entry path, the
same file family as #761. Merge both, then **one** `schwab-1m-v2` dispatch. It cannot help
tonight's grade either way — it produces its first reading tomorrow — so nothing about it is
rushed.

### The dispatch name trap — pinned from `.github/workflows/deploy-service.yml`

`service:` is the only `required: true` with no default, and it is a `choice`. A missing or
misspelled value is **rejected 422 with no run created** — no trace but your own terminal.

**Valid options, verbatim:** `control` · `reconciler` · `strategy` · **`oms`** · `market-data` ·
**`schwab-1m-v2`**

⛔ **`schwab-1m-v2` has HYPHENS. The code slug `schwab_1m_v2` (underscores) is refused outright.**
(`oms` is plain `oms` — no trap there.)

**LANDED =** exit 0 / raw API **204 empty**, then within ~5 s:

```
gh run list --workflow=deploy-service.yml --limit 1     # must show branch=main
```

⛔ **Empty listing = NO RUN.** Re-read the error; do not wait. **Confirm the run before reporting it.**

### Market-hours gate — pinned from `ops/systemd/deploy_service.sh`

Window is `weekday && 07 <= ET hour < 16`. `oms` and `strategy` are `HIGH_RISK=1` and are
**refused inside it** without `allow_live_restart`. `schwab-1m-v2` is `HIGH_RISK=0`.
⇒ **After 16:00 ET no flag is needed for any of tonight's targets.**
⛔ The box also refuses any deploy if `git status --porcelain` is non-empty. It was clean at 07:25.

---

## 5. 🚪 §264 — THE SEAM UNDER CAUSE A: THE DOOR IS OPEN **AND THE HANDLES ARE NOT LOST**

**Does the Webull SDK expose a list-orders call? — YES.**
`OrderOperationV3` — *the class `webull.py` already imports* (L240, L549, L565) — exposes:

| method | signature |
|---|---|
| `get_order_open` | `(account_id, page_size=None, last_client_order_id=None)` → `GET /openapi/trade/order/open` |
| **`get_order_history`** | `(account_id, page_size=None, start_date=None, end_date=None, last_client_order_id=None)` |
| `get_order_detail` | `(account_id, client_order_id)` |

⛔⭐⭐ **CORRECTION TO MY OWN FIRST READ (same day).** I wrote *"we have never called any of
them"* off a grep of `src/` and stated it at repo scope. **It is false at repo scope, in two
different ways**, and a second agent found both from the other direction. *Name the population on
the line — the grep's scope was `src/`, the sentence's scope was "we".*

| capability | actual status |
|---|---|
| **per-ID detail** | ✅ **ALREADY WIRED IN THE ADAPTER.** `webull.py:336/338` and `:600/602` import `OrderDetailRequest` and use it in the status-poll path. My grep missed it because the adapter calls the low-level request class, not the `OrderOperationV3.get_order_detail` wrapper. |
| **enumeration** | ⛔ **never wired into the adapter or any service** — but **demonstrated**: `scripts/webull_oco_step1.py:114` calls `OrderOperationV3(client).get_order_open(account_id)` raw, as a shape-capture step. |

⇒ The corrected finding is **narrower and stronger**: the seam was not merely available, it was
**known, exercised, and left in a one-off script.** What is missing is *only* enumeration, and the
per-ID half the design needs is already in production code.

**What was correctly scoped and still stands:** every `list_open_orders` in our tree is
`self.store.list_open_orders` — `oms/service.py:4671, 5130, 6562`,
`maintenance/reset_active_state.py:26, 64`, defined at `oms/store.py:36`. **All five read our own
Postgres table.** No service path ever asks the venue.

⇒ **The reconciler's blindness is an UNWIRED METHOD, not a missing capability.**

### ⛔⭐⭐ CORRECTION TO THE HANDOFF — "no query of ours can confirm they are gone" is FALSE

The handoff records: *"Five broker-created pairs had their handle discarded. `broker_orders` never
held them by construction ⇒ no query of ours can confirm they are gone."*

**True of `broker_orders`. False of the LOG.** `_submit_exit_pair_blocking` logs the raw response
body *before* the `ExecutionReport` constructor raises — so **every combo_order_id was written to
disk.** Recovered today from `oms.log*`:

| ET (08-21) | symbol | `combo_order_id` | our coid |
|---|---|---|---|
| 09:50:38 | SUGP | `31IUL7OCV3K860JRGF0LLE4MI8` | `schwab_1m_v2-SUGP-protect-30cc147fa1d3` |
| 10:01:18 | JUNS | `NVHC4FQV179G0KKQS0GAPMA4EA` | `schwab_1m_v2-JUNS-protect-b25912cc84a9` |
| 12:42:47 | USDE | `JMH2DE9M85S48LBG3IU5HORI4B` | `schwab_1m_v2-USDE-protect-af78ca4317df` |
| 13:13:15 | EXYN | `6THU0AUEPQJG6J9ISV6I50GHA9` | `schwab_1m_v2-EXYN-protect-e9739b8edc08` |
| **15:40:45** | **USDE** | **`VHGU4AR1TEVN2QSSSDAEFAQP09`** | `schwab_1m_v2-USDE-protect-20f3108ce2ad` |

### ⭐⭐ THE LAST ROW IS THE KNOWN-POSITIVE, AND IT CROSS-VALIDATES ON THREE FIELDS

The operator's screen: **bought USDE 15:40:29 @ $7.99, cover live ~15 s after the fill, stopped
out 15:52:48 @ $7.58.** The log line at **15:40:45 ET** carries **`stop=7.5905`** — which is
**exactly −5.0% of 7.99**, and 7.58 is the fill just through it.

⇒ symbol, timestamp (±16 s) and stop price all agree **independently** of our order tables.
This is a genuine known-positive, not a self-referential one.

### The probe — read-only, designed against its failure mode

⚠ **`get_order_open` cannot serve here: the USDE pair is CLOSED** (it filled 15:52 Friday). Use
**`get_order_history`** over 08-21 against the five IDs above.

- **returns them** ⇒ enumeration is real, and the reconciler gap has a remedy with zero new deps;
- **control fails** (the venue-confirmed USDE pair absent) ⇒ the probe is **VOID, not negative** —
  it would mean history does not enumerate combo children, which is a different finding;
- ⛔ **`last_client_order_id` is a CURSOR. A truncated sweep reads exactly like a clean one.**
  Design against it from the first call: page until a short page, assert the returned count
  against `page_size`, and **print the page count beside every total**. Never report a history
  total without the number of pages that produced it.
- ⛔ it is `account_id`-scoped ⇒ **state the account with every count.**
- ⛔⭐⭐ **THE LISTINGS MAY LAG ⇒ A MISSING ORDER IS NOT PROOF OF ABSENCE.** This inverts the
  probe's logic: enumeration can only ever *confirm* presence. For a **known ID**, absence must be
  settled with the **detail call** — which the adapter already has (`OrderDetailRequest`). ⇒ the
  probe is `history` **to discover**, `detail` **to conclude**. Never conclude from a listing.
- ⛔⭐ **RATE LIMIT: 2 requests / 2 seconds**, on a venue that already refuses our status polls in
  bursts. A cursor walk plus a detail call per ID is a *sequence* of requests, so the sweep must be
  paced, not looped. ⇒ pace it, and **print the request count beside the page count** — an
  enumeration that got throttled mid-walk reads exactly like a short final page.

### ✅ THE PROBE EXISTS — `codex/webull-order-inventory-probe`, hardened by **#772**

Read-only by contract: **only `get_order_history` and `get_order_detail`**, paced at 2.1 s. The
five combo IDs above are **embedded as controls**, `VHGU4AR1TEVN2QSSSDAEFAQP09` carrying
`expected_stop=7.5905`.

⛔ **Its read-only guard was a DENYLIST enforcing an ALLOWLIST claim** and two mutants survived it
— `batch_place_order` (a real mutating method on the class the probe instantiates; `.place_order(`
does not match `.batch_place_order(`, the **B32 token-boundary blind spot** in the
highest-consequence guard in the repo) and a raw `api_client.post`. A third, `getattr(op,
"cancel_order")(...)`, survived my *first* fix. Now an AST allowlist + a dynamic-dispatch ban +
a string-constant ban: **5/5 killed with an unmutated control still green** (#772).

### ⭐⭐ THE THREE DESIGN IDEAS TO CARRY INTO ANY RECONCILIATION WORK

1. **CONTROLS ARE EMBEDDED AND CANNOT BE RESCUED BY A DETAIL CALL.** A complete sweep that omits
   or contradicts any of the five is **VOID**. ⇒ the probe *cannot return a comfortable answer on
   a broken instrument* — the usual failure mode of a validator that has only ever printed PASS.

2. **⛔⭐⭐ ADOPT THE WORDING: INCOMPLETE SWEEP = `COULD_NOT_TELL`, NOT `VOID`.**
   **VOID** = the control ran and *failed* — the instrument is broken, every number is unreadable.
   **COULD_NOT_TELL** = the control *never received a valid assay* — transport died, the cursor
   repeated, a page was unreachable. Nothing was measured, so nothing can be graded either way.
   ⇒ *We have muddled these repeatedly.* Both are "not a pass", and collapsing them loses whether
   the tool is broken or was simply never given a chance to speak.

3. **COUNT NESTED CHILD RECORDS, NOT WRAPPER OBJECTS.** Combo children can push a page **past**
   `page_size`, so the naive short-page terminal test fires early and **reads as a clean, complete
   sweep**. A truncation trap at the venue boundary that we had not anticipated. ⛔ Generalises:
   *a pagination terminal condition must count the unit the API paginates, not the unit you
   happen to be iterating.*

### The trust criteria a reconciliation design must satisfy

**coverage** · **freshness measured against `detail` across state transitions** · **combo children
with stable IDs** · **partial-fill quantities proven cumulative and monotonic** · **OCO sibling
resolution under one identity** · **cursor integrity**.

### ⛔⭐⭐ AND THE CEILING — state it beside every clean result

A clean sweep proves **a complete-looking population for one account, one window, one API version
and one cursor chain.** It **cannot prove present-moment absence during list lag.**
⇒ this is why the safety property is *enumerate to discover, **detail-call to conclude***, and why
`get_order_open` returning nothing is never an answer on its own.

### 🔴 RETENTION IS A CLOCK — CAPTURE THE IDS TODAY

`/etc/logrotate.d/project-mai-tai` is **`daily, rotate 7`** (+ `maxsize 200M`, which can rotate
*sooner*). The 08-21 lines live in `oms.log-20260822.gz`; today's oldest retained is
`oms.log-20260818.gz`.

⇒ **that file drops on or about 2026-08-29, sooner if volume spikes.** The five IDs are
transcribed into this document precisely so the probe is not gated on the log surviving.

## 6. INTRADAY — REPORT, DON'T GRADE

- **Signal 6 must read 0.** Today is #765's first real exercise. ⛔ A must-be-zero **cannot be
  graded intraday** — mid-window is *not yet failed*, never *passed*.
- **Signal 4's denominator will shrink** — more truncations ⇒ fewer symbols arm. Say so *before*
  the number arrives so it reads as #765 working, not signal 4 degrading.
- 07:25 ET read: signal 4 = `0 segments / 0 dup / 0 blind legs` ⇒ **UNMEASURED**, correctly
  self-reported by the script. Session was 25 minutes old.

---

## 7. P&L NOTE CARRIED FORWARD

The 08-21 USDE pair is venue-confirmed by the operator's screen: bought 15:40:29 @ $7.99,
stopped out 15:52:48 @ $7.58 — **−5.1% against a −5% stop**, cover live **15 s** after the fill,
held **12m19s**. First venue-side proof the attach *works*, not merely places.

⇒ ⚠ That exit is **not in our tables**, and it was a **LOSS**. The missing exits are therefore
**not neutral**: any P&L computed from our own tables for 08-21 on `live:orb` is **flattering**,
not merely incomplete. ⛔ Do not publish an 08-21 `live:orb` P&L number without this caveat
attached to it.
