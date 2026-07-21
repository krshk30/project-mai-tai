# STEP-1 gate runbook — Schwab native OCO bracket (attended, live, qty 1)

> **Read this end-to-end before flipping anything.** This is the go/no-go gate from
> [`oco-bracket-design.md`](oco-bracket-design.md). Nothing in the OCO work ships to live routing
> until every item below passes on this broker.
>
> **Broker:** Schwab (`live:schwab_1m_v2`). **Fork:** A (MARKET/LIMIT entry + OCO exits).
> **Code:** PR #498 (`claude/schwab-oco-combo`). **Date drafted:** 2026-07-21 (all times **ET**).

---

## ⚠️ TWO ORDERING FACTS THAT DRIVE THE WHOLE PLAN

**1. The restart window is the FLAT window, and it is EARLY — not 10:00.**
Both flags are read from the service env at process start, so turning them on needs an **OMS
restart**. The standing rule is no restart while a position is held, and **v2 can enter any time
from 07:00**. So the restart must happen **pre-open while the fleet is flat** — waiting until
10:00 risks v2 holding a position and blocking the restart outright.

**This is safe to do early because both flags are INERT in production:**
- `schwab_native_bracket_enabled` only changes behaviour for a request carrying `bracket`
  metadata, and **nothing in the code emits that yet** (wiring the emit is sequencing step 5,
  deliberately not built). Every ordinary v2 intent still takes the unchanged single-leg path.
- `oms_native_oco_stand_down_enabled` only makes the sync look for orders tagged
  `native_oco_bracket`. **None exist**, so the armed set stays empty and the exit ladder runs
  exactly as it does today.

⇒ **Deploy + restart + both flags ON, pre-open, while flat. Then STEP-1 at 10:00 needs no
restart at all** — it is a harness script placing one bracket through the adapter.

**2. Item 4 (the stand-down defer) CANNOT be fully proven today. Scope it honestly.**
The stand-down keys off `_managed_v2_symbols` — positions the OMS actually manages. A
harness-placed bracket on an unmanaged symbol will never appear there, so the stand-down will
correctly do nothing and we will have proven nothing. A full item-4 proof needs a **real
v2-managed position with a bracket attached**, which requires the emit wiring (step 5).

**Today's gate therefore covers items 0, 1, 2, 3, 5 — the broker-behaviour half, including the
E5 one-cancels-other proof.** Item 4 stays open with its unit + mutation coverage, and gets its
live proof when the emit is wired. **Do not let a green run today be recorded as "item 4 passed."**

---

## Pre-flight (pre-open, before any flag flip)

```bash
# 1. Fleet FLAT — mandatory before any restart. Expect zero open quantity.
curl -s https://project-mai-tai.live/api/positions | jq '.[] | select(.quantity != 0)'

# 2. Schwab token healthy (refresh_token expires 2026-07-27 16:09 ET).
ssh mai-tai-vps 'sudo python3 -c "import json;d=json.load(open(\"/var/lib/macd-webhook-server/data/schwab_tokens.json\"));print(d[\"refresh_token_expires_at\"], d[\"updated_at\"])"'

# 3. Record the EXACT current form of both flags — this is what the revert restores verbatim
#    (commented-out and =false are NOT the same thing; restore the form you found).
ssh mai-tai-vps 'sudo grep -nE "MAI_TAI_(SCHWAB_NATIVE_BRACKET_ENABLED|OMS_NATIVE_OCO_STAND_DOWN_ENABLED|OMS_NATIVE_OCO_CONFIRMATION_MAX_AGE_SECONDS)" /etc/project-mai-tai/project-mai-tai.env || echo "ABSENT (defaults apply: both False, dwell 30s)"'
```

**Abort the whole run if:** anything is holding, the token is stale, or the fleet is degraded.

## Deploy (pre-open, fleet flat)

1. **Merge PR #498** — ⚠️ irreversible-ish, needs explicit operator GO, operator attending.
2. Sync the VPS checkout to `main`.
3. Add the flags to `/etc/project-mai-tai/project-mai-tai.env`:
   ```
   MAI_TAI_SCHWAB_NATIVE_BRACKET_ENABLED=true
   MAI_TAI_OMS_NATIVE_OCO_STAND_DOWN_ENABLED=true
   ```
   Leave `MAI_TAI_OMS_NATIVE_OCO_CONFIRMATION_MAX_AGE_SECONDS` unset (30s default).
4. **Restart choreography (OMS change, fleet flat):**
   `stop strategy` → `restart oms` → `start strategy` → restart `schwab-1m-v2`.
5. **Post-restart verification — the deploy is a no-op or it is not correct:**
   ```bash
   ssh mai-tai-vps 'sudo tail -200 /var/log/project-mai-tai/oms.log | grep -E "OCO-STAND-DOWN|ERROR|Traceback"'
   ```
   **Expect: nothing.** No stand-down markers (no brackets exist), no new errors. v2 continues
   trading normally on the unchanged single-leg path. **If v2 behaves differently in any way,
   revert the flags and stop** — inertness is the claim, and a violated claim is a no-go.

---

## STEP-1 gate — run at 10:00 ET, attended, qty 1

**Why 10:00:** ORB has window-flattened, the open's noise is past, and nothing else is competing
for the OMS. **No restart is needed here** — the flags are already live.

Pick a liquid, cheap symbol. Prices below assume a ~$10 stock; scale to the real quote.

### Item 0 — preview accepts the shape ✅ (already passed 2026-07-21 07:00)
`POST /previewOrder` returned HTTP 200 `status: "ACCEPTED"`, zero rejects,
`advancedOrderType: "OTOCO"`. Re-run `scripts/schwab_oco_preview.py` if anything changed.

### Item 1 — the combo RESTS far from market
Place the bracket with the entry buy-stop **far above** market so it cannot trigger.

- **PASS:** broker accepts; the parent shows open/working; querying open orders shows the
  bracket present.
- **Expect:** the two exit children are **not yet working** — they arm on the parent's fill.
- **FAIL / ABORT:** any rejection, or exit legs live before the entry filled (that would mean
  unattached sells against shares we do not own).

### Item 2 — ATOMIC AT FILL
Re-place with the entry trigger **just above** market so it fills on a qty-1 lot.

- **PASS:** the instant the entry fills, **BOTH** exit legs are live at the broker. Query open
  orders and see the SELL LIMIT and the SELL STOP together.
- **FAIL / ABORT:** any window where the position exists with fewer than two working exits.
  That is the naked-position hole this structure exists to close. **Flatten by hand immediately.**

### Item 3 — ONE-CANCELS-OTHER (★ the E5 proof)
Make one exit leg marketable (e.g. move the target to just above the bid).

- **PASS:** one leg fills, **the broker auto-cancels the sibling**, the position goes flat, and
  there is **no oversell rejection anywhere.** This is the whole point: no second uncoordinated
  sell exists to be rejected.
- **FAIL / ABORT:** sibling still working after the fill, or any oversold/overbought message.

### Item 4 — software exit DEFERS ⛔ OUT OF SCOPE TODAY
See the ordering note above. Needs a real v2-managed position with an attached bracket, i.e.
the emit wiring. **Record as "not tested," never as passed.**

### Item 5 — cancel + flat
Cancel the un-triggered combo from item 1.

- **PASS:** the whole combo cancels (no orphan child left working) and the account is flat.
- **FAIL:** any leg still working after the cancel → **cancel it by hand and stop.**

---

## Watch during the run

```bash
# OMS live tail
ssh mai-tai-vps 'sudo tail -f /var/log/project-mai-tai/oms.log | grep -E "OCO|OVERSOLD|REJECT|HARD-STOP"'
```
Markers that mean something: `[OMS-OCO-STAND-DOWN-EXPIRED]` (a confirmation aged out — expected
only if the sync stalls), `[OMS-OCO-STAND-DOWN-CLEARED]` (bracket resolved, ladder resumed).

## Abort / rollback at any point

1. **Cancel every working order** for the test symbol; confirm the account is **flat by hand**.
2. Restore both flags to the **exact form recorded in pre-flight** (absent vs `=false`).
3. Restart choreography, fleet flat.
4. With the flags off the ladder and the single-leg path are unchanged — the fleet is back to
   the pre-run configuration.

**Standing rule for the whole run: a live position must never exist without a working exit.**
If that is ever true for more than a moment, flatten by hand first and diagnose afterwards.

## After the run

- Record **per item**: pass / fail / not-tested, with the broker order IDs.
- If items 1/2/3/5 pass: Schwab's broker-behaviour half is proven. **Live routing still does not
  ship** — the emit wiring plus item 4's live proof come first.
- Update [`session-handoff.md`](session-handoff.md) and the OCO memory the same day.
