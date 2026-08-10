# Monday dual-broker FAN-OUT validation checklist (attended, real money)

**Goal:** prove the fan-out fires **both broker legs in parallel** at the cross — Schwab (TOS) + Webull —
with correct order shapes, both fills, per-account exits, and no naked/double leg. Two checkpoints, both
brokers each: **pre-market (~07:00 ET, EH)** and **RTH open (09:30 ET)**. This is real money at **qty 1**.

- Live state going in: fan-out is **deployed flag-OFF** (mirror-on-fill runs). Full-exempt resting refresh is
  live (#547). VPS = `origin/main`. See [[project-mai-tai-dual-broker-fanout-build]].
- KILL SWITCH (any time): drop `MAI_TAI_STRATEGY_SCHWAB_1M_V2_DUAL_BROKER_FANOUT_ENABLED` from the env +
  restart oms & schwab-1m-v2 → back to mirror-on-fill (byte-identical).

> ### ⛔⭐⭐ READ BEFORE USING THAT KILL SWITCH — IT NO LONGER LANDS WHERE THE LINE ABOVE SAYS
> *(found 2026-08-10; that line was written when the mirror was expected to get its own account.)*
>
> Production now runs **BOTH** flags on:
> ```
> MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_MIRROR_ENABLED=true
> MAI_TAI_STRATEGY_SCHWAB_1M_V2_DUAL_BROKER_FANOUT_ENABLED=true
> ```
> Mirror-on-fill is inert **only because the fan-out flag suppresses it** — the queue predicate is
> `mirror_enabled AND NOT fanout_enabled` (`oms/service.py`). The two are mutually exclusive by
> design, and fan-out currently wins. §A.3/B.3 below test that mutual exclusion while fan-out is ON;
> they say nothing about what happens when it goes OFF.
>
> ⛔ **So dropping the fan-out flag does not return to a dormant state — it ACTIVATES mirror-on-fill,
> pointed at `live:orb`.** That is the one thing the mirror path's own code forbids:
> *"Provision a dedicated account (e.g. `live:v2_webull`); **do NOT point this at `live:orb`**."*
> `live:orb` is the fan-out account, and the shared Webull account.
>
> ⇒ **A rollback that makes things worse, at the exact moment nobody re-reads the second flag.**
> **Drop `..._WEBULL_MIRROR_ENABLED` in the SAME edit**, then verify BOTH are absent/false before
> restarting oms. Otherwise the kill switch trades one live path for an unintended one.
>
> ⚠️ Not a live defect today — a **rollback hazard**. Recorded here *and* in
> [`dual-broker-v2-design.md`](dual-broker-v2-design.md) because the hazard is the INTERACTION, so
> whichever flag you arrive at, you need to know about the other one.

Shell prelude for the SQL blocks (run once per ssh session):
```bash
URL=$(sudo grep -E '^MAI_TAI_DATABASE_URL=' /etc/project-mai-tai/project-mai-tai.env | head -1 | cut -d= -f2-)
export PGPASSWORD=$(echo "$URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')
PGUSER=$(echo "$URL" | sed -E 's|^[^:]+://([^:]+):.*|\1|')
DSN="dbname=project_mai_tai user=$PGUSER host=localhost"
psql "$DSN" -c 'select 1' >/dev/null && echo DB-OK
```

---

## 0. Pre-open setup (attended, before ~07:00 ET)

**0.1 — fleet-flat pre-flight (MANDATORY before any restart):**
```bash
psql "$DSN" -tAc "SELECT count(*) FROM virtual_positions WHERE quantity<>0"      # want 0
psql "$DSN" -tAc "SELECT count(*) FROM oms_managed_positions WHERE current_quantity>0 OR status='open'"  # want 0
psql "$DSN" -tAc "SELECT count(*) FROM oms_armed_stops"                          # want 0
```
All three `0` = FLAT. If not flat, STOP — do not restart.

**0.2 — set the flag + Webull leg (edit `/etc/project-mai-tai/project-mai-tai.env`, sudo):**
```
MAI_TAI_STRATEGY_SCHWAB_1M_V2_DUAL_BROKER_FANOUT_ENABLED=true
# confirm the Webull leg account is set (fan-out routes the Webull leg here):
MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ACCOUNT_NAME=live:orb
# day-1 size the Webull leg small (honest but cheap); omit => same qty as the Schwab leg:
MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_FANOUT_QUANTITY=1
```
Back up the env first: `sudo cp /etc/project-mai-tai/project-mai-tai.env{,.bak.pre-fanout.$(date +%Y%m%dT%H%M%SZ)}`.

**0.3 — restart (choreography: stop v2 → restart oms → start v2):**
```bash
sudo systemctl stop project-mai-tai-schwab-1m-v2
sudo systemctl restart project-mai-tai-oms
sudo systemctl start project-mai-tai-schwab-1m-v2
```

**0.4 — confirm the flag resolved ON + services healthy:**
```bash
sudo journalctl -u project-mai-tai-schwab-1m-v2 --since '2 min ago' | grep -i 'V2-FANOUT.*ENABLED'   # want: dual-broker fan-out ENABLED -> Webull leg -> account live:orb
for s in oms schwab-1m-v2; do systemctl show project-mai-tai-$s -p ActiveState -p NRestarts | tr '\n' ' '; echo " $s"; done
```
Want: `[V2-FANOUT] dual-broker fan-out ENABLED — Webull leg -> account live:orb`, both `active NRestarts=0`.

---

## 1. CHECKPOINT A — pre-market (~07:00 ET, extended hours)

Trigger a controlled entry the usual way (manual harness / a real setup at qty 1). Then verify **all** of:

**A.1 — the cross fired both legs (bot):**
```bash
sudo journalctl -u project-mai-tai-schwab-1m-v2 --since '10 min ago' \
  | grep -iE 'V2-RESTING-EH-CROSS|V2-FANOUT-RTH-RESTING|V2-CW\b'
```
Expect the Schwab-side cross line AND that a Webull leg was queued (the fan-out draft). In EH the resting
leg is `[V2-RESTING-EH-CROSS]`; the Webull leg is emitted right after through the 2nd emitter.

**A.2 — TWO intents, one per account, ~same instant:**
```bash
psql "$DSN" -c "
SELECT ba.name AS account, ti.symbol, ti.side, ti.quantity, ti.reason,
       to_char(ti.created_at,'HH24:MI:SS.MS') AS at
FROM trade_intents ti JOIN broker_accounts ba ON ba.id=ti.broker_account_id
WHERE ti.created_at > now() - interval '15 min' AND ti.intent_type='open'
ORDER BY ti.created_at DESC LIMIT 10;"
```
PASS = one row on the **Schwab** account + one on **live:orb** for the same symbol, `created_at` within ~1s.
The Webull row's `reason` contains `fan-out webull`.

**A.3 — the on-fill MIRROR did NOT also fire (mutual exclusion):**
```bash
sudo journalctl -u project-mai-tai-oms --since '15 min ago' | grep -i 'OMS-V2-MIRROR'   # want: NOTHING
```
Any `[OMS-V2-MIRROR]` line here = FAIL (fan-out + mirror both fired → would double the Webull leg).

**A.4 — Webull leg shape = marketable EH-LIMIT (EH is limit-only on Webull):**
```bash
psql "$DSN" -c "
SELECT ba.name AS account, bo.symbol, bo.side, bo.order_type, bo.status,
       bo.payload->>'session' AS session, bo.payload->>'extended_hours' AS eh
FROM broker_orders bo JOIN broker_accounts ba ON ba.id=bo.broker_account_id
WHERE bo.created_at > now() - interval '15 min' AND bo.side='buy'
ORDER BY bo.created_at DESC LIMIT 10;"
```
PASS = Webull (`live:orb`) leg `order_type=limit`, `session=am`, `extended_hours=true`; Schwab leg is its
EH-limit too. (No MARKET / no native OCO in EH — both brokers 417 those pre-market.)

**A.5 — BOTH legs FILLED, both accounts (~2x capital):**
```bash
psql "$DSN" -c "
SELECT ba.name AS account, f.symbol, f.side, f.quantity, f.price,
       to_char(f.filled_at,'HH24:MI:SS') AS at
FROM fills f JOIN broker_accounts ba ON ba.id=f.broker_account_id
WHERE f.filled_at > now() - interval '20 min'
ORDER BY f.filled_at DESC LIMIT 12;"
```
PASS = a buy fill on the Schwab account AND on `live:orb` for the same symbol.

**A.6 — BOTH legs EXIT / flatten clean (per-account software EH ladder in EH):**
Watch the exit fire on each account (sell fill in `fills` on both), then re-run 0.1 → **FLAT**.
`sudo journalctl -u project-mai-tai-oms --since '25 min ago' | grep -iE 'OMS-V2-MANAGED|V2-CW-.*EXIT|FLATTEN'`

**A.7 — per-broker reject isolates (if one broker rejects — opportunistic, not forced):** if either leg
rejects, the OTHER still trades. Reject reason in `broker_orders.payload->>'reason'`; a Webull *not-tradable*
reject writes `webull_ineligible_today` (a 429/transient does NOT — verify it never marks a name ineligible).

**CHECKPOINT A verdict:** A.2 (both intents) + A.3 (no mirror) + A.5 (both fills) + A.6 (both flat) all PASS
on **TOS/Schwab and Webull** → EH fan-out validated. Book cost ≈ pennies at qty 1.

---

## 2. CHECKPOINT B — RTH open (09:30 ET, regular session)

Repeat the same controlled entry in regular hours. The differences vs A: the Schwab leg is its native mode
(reactive MARKET / resting stop-limit) and the Webull leg is **MARKET + native OCO** (not EH-limit).

**B.1 — cross fired both legs:**
```bash
sudo journalctl -u project-mai-tai-schwab-1m-v2 --since '10 min ago' \
  | grep -iE 'V2-CW\b|V2-FANOUT-RTH-RESTING'
```
Reactive cross = `[V2-CW] ... INTRABAR ENTER`; RTH-resting cross = `[V2-FANOUT-RTH-RESTING] ... >= resting_level`.

**B.2 — two intents, one per account, ~same instant:** same query as A.2. PASS = Schwab + `live:orb`, ~1s apart.

**B.3 — no mirror double-fire:** same as A.3 → want NOTHING from `[OMS-V2-MIRROR]`.

**B.4 — Webull leg shape = MARKET + native OCO (RTH):**
```bash
psql "$DSN" -c "
SELECT ba.name AS account, bo.symbol, bo.order_type, bo.status,
       bo.payload->>'bracket' AS bracket, bo.payload->>'bracket_entry_type' AS entry_type,
       bo.payload->>'bracket_target_price' AS tgt, bo.payload->>'bracket_stop_price' AS stop
FROM broker_orders bo JOIN broker_accounts ba ON ba.id=bo.broker_account_id
WHERE bo.created_at > now() - interval '15 min' AND bo.side='buy'
ORDER BY bo.created_at DESC LIMIT 10;"
```
PASS = Webull (`live:orb`) leg has `bracket=true`, `bracket_entry_type=MARKET`, target/stop set (~+2%/−5%
off the cross px). Also `[V2-OCO-EMIT]` in the oms log for the entry.

**B.5 — both fills, both accounts:** same as A.5. PASS = buy fill on Schwab AND `live:orb`.

**B.6 — the OCO one-cancels-other resolves + both flat:** on each account the target or stop fills and the
OTHER leg auto-cancels (no oversell — the E5 proof, per account). Watch `fills` for the sell on each account,
then re-run 0.1 → **FLAT**. `sudo journalctl -u project-mai-tai-oms --since '25 min ago' | grep -iE 'OCO|one-cancel|MANAGED-CLOSE'`

**CHECKPOINT B verdict:** B.2 + B.3 + B.4 + B.5 + B.6 PASS on **both brokers** → RTH fan-out validated.

---

## 3. Go / no-go

- **GO (leave fan-out ON):** both checkpoints PASS on both brokers — parallel dual submit, correct shapes,
  both fills, per-account exits, no naked/double leg, no oversell. Scale the Webull leg qty when ready
  (`MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_FANOUT_QUANTITY`, or drop it to match the Schwab leg).
- **NO-GO (revert):** any naked leg, double Webull fire (`[OMS-V2-MIRROR]` appeared), oversell reject, or a
  leg that fires at the wrong instant. KILL: drop the fan-out env line + choreographed restart → mirror-on-fill.

## 4. What each result tells you (honest reading)

- Both legs fire but Webull slips more than Schwab on a resting entry → **expected** (Webull can't buy-STOP,
  so its leg is a MARKET-at-cross; the spike slippage is the accepted asymmetry).
- A Schwab-rejected foreign name still trades on Webull → **the whole point** (the reject is free under
  fan-out; the name stays a Webull-only leg).
- `webull_ineligible_today` gains a row only on a *clear not-tradable* Webull reject, NEVER on a 429/transient.
