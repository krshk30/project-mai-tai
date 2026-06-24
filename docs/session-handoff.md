# Session Handoff — ACTIVE (read this first)

> **This is the single entry point.** It stays small and current. Full dated history lives in
> [`handoff-archive/`](handoff-archive/) by month. To onboard an agent: *"Read `docs/session-handoff.md`."*
>
> **Structure:** status + open items + live ops state + recent activity here → deep history in the archives →
> design/reference docs linked throughout. The v2-isolated bot's deep history is
> [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md).
>
> **Maintenance rule:** new work appends to **Recent activity** below; monthly, roll entries older than ~2
> weeks into [`handoff-archive/<YYYY-MM>.md`](handoff-archive/). Keep this file under ~400 lines.

---

## 🚦 STATUS — v2 IS LIVE (2026-06-17, ATR-only, real Schwab account)

v2 went **live-credentialed** on **2026-06-17** as a **reasoned, operator-accepted risk** (profitability-after-spread
was/is still accumulating — see open items). Running config, ground-truthed from `/proc/<pid>/environ` + DB on deploy:

- **`broker_provider=schwab`, `account_name=live:schwab_1m_v2`**, real shared hash bound (the only `live:` Schwab key);
  `go_live_enabled=true`, `atr_only_mode=true` (P1/P2 disabled at two layers), qty 10, ATR fresh-flip qualifier on (age<5).
- **CYN is PROTECTED** — `MAI_TAI_PROTECTED_SYMBOLS=CYN` → `protected_symbol_set={CYN}` in the running config; the real
  account **holds 8000 sh CYN @ $2.57** (operator's manual position). 3-layer block + watchlist exclusion + #326. v2
  has never emitted/ordered/filled CYN (verified). `oms_managed_positions` CYN rows = 0 (bot does not manage it).
- **Rollback (tested):** `systemctl stop project-mai-tai-schwab-1m-v2.service` halts new entries instantly (OMS +
  market-data keep managing exits). Re-isolate to paper = `GO_LIVE_ENABLED=false` + `BROKER_PROVIDER=simulated` + restart.
  Env backup: `/etc/project-mai-tai/project-mai-tai.env.bak.pre-golive.20260617T003247Z`.

**What "live" has and hasn't proven yet:** the execution path is proven **to Schwab acceptance** (06-17: LNAI order
accepted by Schwab, working order, broker_order_id assigned). It is **NOT yet proven to a real FILL** — see open items.

---

## 🔴 OPEN ITEMS — DO NOT LOSE (future-you: read these)

**🆕 2026-06-24 (today's threads):**
- **Webull real ORB account — flip "Enable 2FA Verification" OFF** (operator; portal change rate-limited to ~next day) → re-probe (read-only) → build the real Webull adapter (response shapes from the probe) → wire `live:orb`→webull → **attended go-live**. Until then ORB reclaim runs SHADOW (no posting). Keys + IP whitelist + Trading perm already set. **PR #364** (design + probe).
- **#366 snapshot-persist throttle — ATTENDED close-deploy + validate** (enable `snapshot_persist_throttle_secs`, re-arm the #350 py-spy capture at a 16:00 ET close, confirm snapshot gaps <50s). Then decide #350 **piece #2 (offload) / #3 (encode-once)** from the re-capture. **PRs #365 (design) / #366 (throttle)**.
- **strategy-engine restart drift** — box disk (`e76d8b5`, #362+#363) is AHEAD of the running strategy-engine (PID 2415361, not restarted). Next restart deploys #362's byte-identical leaf import — attend it.
- **ORB trail width** — regime-classifier rejected; default a FIXED trail (3% leading on 1 week, idealized fills). Confirm on more days / realistic fills before committing. Also reroute the backtest decider off `market_trade_ticks` onto validated `market_capture_trades`.

1. **RESTART-WHILE-HOLDING is UNTESTED.** The 06-17 mid-session restart recovery test (below) was run **FLAT**. We have
   **not** verified how v2 recovers a restart **while holding an open position** — i.e. position reconciliation across
   restart, exit-ladder continuity, and whether the held position's exit metadata/floor/stops survive. **Test this before
   relying on restart safety during a live trade.** (Recovery-while-flat = ~17s, proven; while-holding = unknown.)
2. **✅ RESOLVED (confirmed 2026-06-17) — CI `validate` is GREEN again and can gate.** The JSONB-on-SQLite harness
   incompatibility (`market_trade_ticks`/`market_quote_ticks.raw` → JSON variant) + stale assertions that made every
   push red are fixed on main. **Proof: PR #333's `validate` ran fully green** (unit + integration/replay + ruff, 1m24s)
   — a branch off main could not pass if the ~150 JSONB CompileErrors were still present. Merges no longer *need*
   `--admin` to bypass red CI (admin-merge stays available). Keep running the targeted test file + ruff locally anyway.
3. **First real ATR FILL still PENDING.** 06-17 fired 4 live qty-10 ATR orders (08:05–08:17 ET): **NIVF/YMAT/EHGO REJECTED**
   by Schwab — *"Opening transactions for this security must be placed with a broker. Contact us"*; **LNAI** Schwab-**ACCEPTED**
   then **OUR-side CANCELLED** (`abandon_reason_code=SETUP_INVALID` — ATR setup reverted next bar). **All $0 filled, no
   position.** So the headline behavioral proof (real fill @ qty10 → managed exit) awaits a flip on an **API-eligible**
   symbol whose setup holds. Detail: [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) → 2026-06-17.
4. **Schwab API-open RESTRICTION narrows the live universe.** 3 of 4 06-17 names were Schwab-refused for API opening
   (foreign/manual-handling). A meaningful share of the momentum scanner's small-caps are likely un-openable via the v2
   live API path — the tradeable universe is **narrower than the scanner surfaces**. #326 now auto-evicts these.
5. **Profitability-after-spread — the open validation gate (now POST-go-live).** Still needs a real kept-win sample (not
   2 events) + Schwab-tick spread-adjusted P&L. Idealized sim-fills are pipe validation, NOT a track record. Replay
   Phase 2 data clock running since 2026-06-15 (#282); needs ~a week+ of RTH ticks.
6. **Exit-fill QUALITY — Phase 2 (resting-limit brackets) is the design-first follow-up.** Phase 1 (#333, below) made the
   OMS decide tick-by-tick within ms, but a market-order-on-decision still slips on a violent spike-and-collapse. Phase 2
   = pre-stage scale/floor/stop as **broker-resident bracket orders at entry** so fills execute at exchange speed,
   independent of any OMS reaction. Design-first (lifecycle: partial fills, cancel-on-other-exit, reconciliation across
   restart). Also: **`deploy_preflight` blocks every in-window OMS deploy on the protected-CYN holding** (CYN
   `position_quantity_mismatch` critical + "1 open position" + reconciler-degraded cascade — all benign) and its 5.0s
   HTTP timeout is too tight for the 5.4s `/api/overview` — whitelist `MAI_TAI_PROTECTED_SYMBOLS` + bump the timeout.
7. **⏸️ TIMESALE capture ENABLE is PENDING (attended, next-session open).** PR #335 (`59500bc`) added additive,
   capture-only TIMESALE_EQUITY (true trades) to the v2 streamer — **MERGED + DEPLOYED with the flag OFF (inert,
   byte-identical; v2 PID 2252021→2319110, clean)**. Why pending: our `market_trade_ticks` are LEVELONE quote-snapshots
   (throttled), NOT true trades; TIMESALE was never subscribed (0 rows); Schwab has **no historical T&S endpoint** so it
   must accrue LIVE. ENABLE = set `MAI_TAI_STRATEGY_SCHWAB_1M_V2_TIMESALE_CAPTURE_ENABLED=true` + restart schwab-1m-v2 —
   but do it ATTENDED at the next open (after-hours has no v2 watchlist → can't read the SUBS/entitlement; arming
   unattended risks a flap on the shared CHART_EQUITY streamer that feeds the live ATR bot; zero capture lost — first
   real trades are next RTH). Watch `[V2-WS-SUB]` (services incl TIMESALE_EQUITY) + any `[V2-WS-RESP-ERR]
   service=TIMESALE_EQUITY` (= not entitled) + reconnect/flap; flag-disable ready. Design: `docs/timesale-capture-design.md`.
8. **Tick-confirmation entry research (parallel track, NOT deployed).** After a setup bar, enter only if upticks>downticks
   in the next ~15s. HELPS P5 (6.4:1)/P1 (8.5:1), HURTS P4-burst. P4 is NOT a loss engine (+$7.11 2-day; the real bleeder
   is **P1 MACD-cross −$14.53**); mixed (P4-base + P1/P5-tick) = +$3.41. Tonight's ticks are LEVELONE-grade (see #7);
   DECIDER = a faithful 10-day TIMESALE test ~early July ([[project-mai-tai-tick-confirmation]]). Docs in `/home/trader/`:
   tick_confirmation_findings, combined_tickconfirm_2day, p5_3path_baseline_2day, p4_tickconfirm_optionB_plan,
   intrabar-execution-design, timesale-capture-design.
8. **ORB (P6 OPEN) — fix #352 DEPLOYED, FULL validation gated to the NEXT RTH OPEN.** ORB was **silently inert since
   deploy**: the gateway `trade_tick.timestamp_ns` carries **milliseconds** for Polygon/Massive ticks but ORB read it as
   nanoseconds (`ts/1e9`) → every tick stamped **1970** → the session-anchored aggregator dropped all → **0 OR bars, 0
   trades** (heartbeat `bar_counts:{}`/`last_tick_at:{}` while the stream had live ticks). The strategy-engine already
   defends with `_normalize_tick_timestamp_ns`; ORB didn't. **PR #352 (`f404544`) MERGED + DEPLOYED 2026-06-22 10:39 ET**
   (ORB-side `_normalize_trade_ts_ns` magnitude ladder; CI green, editable-install git-pull + ORB-only restart, fleet
   untouched). **Mechanical fix VALIDATED same-session** (heartbeat `last_tick_at` now shows 2026 timestamps, bars
   complete). **⏳ REMAINING GATE — validate at 2026-06-23 09:30–09:40 ET: `bar_counts` POPULATES (or_bars fill
   09:30–09:34), an OR builds, a breakout evaluates (`[ORB-BREAKOUT]`).** **Real-money flip (qty 10, was targeted 06-22)
   stays BLOCKED until that passes.** Cloud /schedule can't reach the VPS → validate attended/VPS at the open.
   [[project-mai-tai-orb]]
9. **Extended-hours ATR order ROUTING — follow-on to #358 (2026-06-22).** The OMS fix #358 stopped the
   `SETUP_INVALID` cancellation that made v2 ATR silently RTH-only (the guard now fails open for the tape-less v2
   bot). **But the after-hours order still has to FILL:** if v2 routes a **market** order, extended hours often
   requires a **limit** order, so an un-cancelled after-hours ATR intent may still not fill. Verify the next time an
   after-hours ATR fires whether it actually fills; if not, scope extended-hours limit-order routing for v2. Also a
   deliberate-risk note: after-hours ATR = thinner/gappier real-money fills — consider whether to gate it on/off.

---

## ✅ CLEARED (was a go-live blocker)

- **OMS exit path is TICK-BY-TICK — FIXED (#333 `c79e8f5`, deployed live 2026-06-17 ~19:30Z).** Diagnosed off the live
  LNAI ATR-Flip trade: the +2% scale fired **~70s late at 4.345** (not ~4.45). Root cause (DB+code pinned): market-data
  had bids above +2% (4.43–4.46) for a ~14s window during the spike, but `sync_broker_state()` REST ran **inline every
  5s on the same loop that read quotes** → ticks backed up; AND the 5s staleness guard was blind because `received_at`
  was stamped at processing-time, not event-time. Fix = dedicated `_run_tick_consumer` task (market-data on its own task,
  never starved by broker-sync/intents) + last-quote-wins `_coalesce_ticks` + event-time staleness from `produced_at`.
  Behavior-identical for intents/sync/heartbeat + ladder logic; 57 passed/1 xfailed; deployed flat (only protected CYN
  held), clean (0 tracebacks). Design: [`oms-tick-consumer-design.md`](oms-tick-consumer-design.md). **Phase 2
  (resting-limit brackets) = open item #6.** True verdict still wants the next live intrabar spike on a v2 position.
- **04:00 ET watchlist-staleness race — FIXED (#324, deployed) + VERIFIED LIVE 2026-06-17.** At the 08:00 UTC / 04:00 ET
  roll: `bot day-roll fired` (08:00:00.654) → `scanner session-roll fired` (08:00:01.106) → scanner reset; v2 watchlist
  → count=0 with yesterday's 5 symbols UNSUBSCRIBED. **Zero stale symbols survived; no re-promotion race; no errors.**
- **Whole exit ladder live-proven** (2026-06-15, CUPR — scale/floor legs on simulated).
- **ATR fresh-flip qualifier MECHANISM** ✅ validated/complete (2026-06-16, live both directions).

---

## 🟢 LIVE OPS STATE (as of 2026-06-24)

- **2026-06-23 evening deploy (attended):** **v2 restarted → PID 2668268** (#362 EH-routing LIVE — *supersedes the v2=2319110 line below*). **ORB restarted → PID 2667440** (reclaim shadow). **⚠️ strategy-engine NOT restarted (still 2415361)** — its box disk code (main `e76d8b5`, incl. #362's byte-identical leaf import + #363) is AHEAD of runtime; the **next strategy-engine restart deploys #362/#363 — do it attended.** OMS untouched (#362 doesn't change it).
- **#366 snapshot-persist throttle (#350 piece 1) — NOT deployed:** built, flag-gated default-off; awaiting **ATTENDED close-deploy** (`snapshot_persist_throttle_secs`>0 + re-arm the #350 py-spy capture at a 16:00 ET close → confirm gaps <50s).
- **ORB reclaim = SHADOW** on `paper:orb` (alpaca, no creds → rejects); generates signals at the open, posts nothing. Real Webull posting blocked on the 2FA toggle (see Open Items).
- **CYN 8000 sh** still held on `live:schwab_1m_v2` (protected/frozen/inert).
- **2026-06-19 Deploy Main (Juneteenth holiday override) rotated all 5 CORE PIDs** — strategy **2415361** + OMS / control /
  market-data / reconciler (all `since` ~13:29Z, NRestarts=0). **v2 UNCHANGED = 2319110 (untouched, still current).**
  polygon_30s flipped to `paper:polygon_30s` + `simulated`, `MAI_TAI_STRATEGY_PERSIST_OFFLOAD_ENABLED=true` ACTIVE. The
  offload path validates Mon premarket (closed market = no bars yet). Re-fetch any PID via `systemctl show <svc> -p MainPID --value`.
- **Service PIDs (06-17 set):** v2 **2319110** still current (#335 TIMESALE, flag OFF/inert); strategy 2299529 / OMS 2299517
  (#333) **RETIRED by the 06-19 deploy → now 2415361 etc.** *(Retired earlier: v2 2252021 [#326], OMS 2207792 / strategy 2207786 [#333], pre-go-live 2104716/2121312.)*
- **#326 — Schwab-ineligible watchlist eviction: DEPLOYED + restart-verified 2026-06-17.** v2 now evicts symbols Schwab
  refused to open today (`schwab_ineligible_today`, per-account, 60s-cached) from its watchlist, so it stops *emitting*
  for them (the OMS already blocked *re-submission*; this halts the bot at the source — parity with the old schwab_1m
  bot). Proven on the fresh boot: scanner confirmed 6, v2 watchlist = 3 (CLWT/EHGO/YMAT evicted = exactly today's
  ineligible set). ⚠️ **Known ≤60s stale-carryover window at the 04:00 roll** (cache TTL not coordinated with session
  roll) — benign (over-conservative, self-corrects, 3h pre-trade); optional hardening = key the cache on session_date.
- **Mid-session RESTART recovery (FLAT) — measured 2026-06-17:** WS re-subscribe **~4s**; `state.bars` hydrated via
  DB-seed **~2s** (Fix-b) + REST warmup **~17s** (all `warmed=3/3`); buffered streamer bars drained. **Effectively blind
  ~17s, NOT the old ~135-min blackout** — DB-seed + REST warmup backfill the strategy buffer. (Supersedes the 135-min
  worst-case in [[project-mai-tai-v2-entry-warmup-gate]] for the DB-history case.) **Note:** the snapshot `bar_counts`
  telemetry resets to live-only on restart (≠ the eval buffer `state.bars`, which is the warm one).
- **Forward-test watcher** `/tmp/atr_fwd_watch.py` → `/tmp/atr_fwd.log` (flags any live fire age≥5 as GATE-BROKEN).
- **Go-live confirm captures (VPS):** `/tmp/v2_golive_cp1.txt` (04:00 roll), `/tmp/v2_golive_cp2.txt` (7AM session),
  `/tmp/v2_golive_firstfill.txt` (first-fill watch; transient timers `v2-golive-cp{1,2}`, watch fired + exited).
- **Tick-capture retention:** prune-ticks `--keep-days 30`; first effective deletion ~2026-07-15; `market_*_ticks` only.
- **Deploy discipline:** PR + Validate mandatory (CI `validate` GREEN again — open item #2; admin-merge still available),
  direct push forbidden; attended + explicit-GO before any live-money merge/restart; restart ONLY named services + capture PIDs.
  See [[project-mai-tai-multi-agent-deploy-rules]], [`vps-deployment.md`](vps-deployment.md).

---

## 🗓️ RECENT ACTIVITY (newest first — full text in [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md))

- **2026-06-23/24 — Heavy day: v2 EH-routing LIVE · ORB reclaim SHADOW-deployed · #350 root-caused + throttled · Webull real-account integration (2FA-blocked).**
  - **🟢 #362 — v2 extended-hours routing FIX: MERGED + LIVE on v2 (06-23 ~16:00 ET, attended; v2 PID 2668268).** Evidence-pinned root cause: every v2 order hit Schwab as `session=NORMAL`/`orderType=MARKET` → can't fill outside 9:30–16:00; the EXIT/stop-guard path stamped `session=AM/PM`+limit but the ENTRY path never ported the legacy `order_routing_metadata`. Fix = extract `strategy_core/order_routing.py` leaf (byte-identical for legacy), v2 opens now call it (limit@ask + session) in extended hours; RTH unchanged (`session is None → {}`). Mirrors the proven macd_30s/schwab_1m path. ⚠️ **v2 now places REAL PM/aftermarket limit orders.**
  - **🟢 ORB reclaim entry (PR #363) — SHADOW-deployed** (`orb_intrabar_reclaim_enabled=true`; ORB PID 2667440). Cap-off + reclaim-of-OR_high (25s hold) → resting LIMIT@OR_high + 3% trail + qty 5; flag-gated (off = byte-identical bar-close/8%/12%-cap; full ORB suite green). Routes `paper:orb`→alpaca (no creds) → **rejects = shadow** (signals generate, nothing posts, like polygon). Fill-instrumentation stamps intended OR_high + emit_ms; `scripts/orb_fill_slippage.py` reads slippage from `fills`. Real posting pending Webull. [[project-mai-tai-orb]]
  - **ORB design saga (read-only):** HSCS 06-23 **+40% move MISSED — the 12% OR-width cap screened it by 0.30pp** (12.30% vs 12.0%); breakout never evaluated. 7-day Polygon-1s backtest (look-ahead-free pre-09:25 universe): **−1.5% HARD stop = degenerate** (HSCS round-trips to −1.5%); **reclaim entry + TRAILING 3/5/8% ≈ +170–184% net/week** (idealized OR_high fill → magnitude optimistic, direction robust). **Regime-classifier Stage-1 FAILED** — premarket-retention does NOT predict the winning trail (classifier ≈ fixed-5%, captured +1 of +149 oracle headroom) → **default a fixed trail** (3% best/most-consistent on the week); do NOT build the switch.
  - **🟠 Webull real-money ORB account — integration STARTED, 2FA-BLOCKED (PR #364 = design + sandbox probe; real adapter NOT built).** Verified: existing `webull.py` is a stub that can't post; Webull **OpenAPI is live** (SDK `webull-openapi-python-sdk` 2.0.11, HMAC app-key/secret, host `us-oauth-open-api.webull.com`, **requires instrument_id**, sync client → wrap in `to_thread`). Operator set Trading perm + IP whitelist `104.236.43.107/32`; **"Enable 2FA Verification" ON → 403 "Resource not authorized"** (each token needs a one-time Webull-mobile-app approval = re-auth SPOF for a headless bot). **NEXT (operator, portal change rate-limited to ~next day): flip 2FA OFF → I re-probe (read-only) → build the real adapter from the response shapes → wire `live:orb`→webull → attended go-live.** Creds are in the box env only (never chat/code/commits).
  - **🔴 #350 close-freeze ROOT-CAUSED (py-spy) + throttle built.** 185k-sample close capture: **~72% CPU in JSON `iterencode`(52%)+`raw_decode`(20%) on the event loop** — the dashboard/scanner snapshot (`scanner_alert_engine_state` carrying `today_alerts[-5000:]`) re-encoded+committed **per Redis message** via `_publish_strategy_state_snapshot → _persist_scanner_snapshots → _replace_dashboard_snapshot` (the `json.loads(json.dumps(…, default=str))` sanitize = encode+decode, then psycopg re-encodes on commit = **2 encodes + 1 decode**). #350's existing offload covers BAR persistence, **not** this path. **Design = PR #365** (priority: throttle → offload → encode-once). **Piece #1 (throttle) = PR #366** (`snapshot_persist_throttle_secs`, default 0 = off = byte-identical; trailing-edge debounce, force-flush on shutdown/day-roll; 243-test regression green). **Pending ATTENDED close-deploy** (enable + re-arm the #350 capture, confirm gaps <50s) → then decide #2/#3. [[project-mai-tai-polygon-freeze]]
  - **market_capture VALIDATED (read-only):** `market_capture_trades` are genuine Polygon prints (multi-venue, trade conditions, odd lots, sub-penny — NOT quote snapshots), ns timestamps clean, backfill retrievable → **backtest-ready; reroute the decider off `market_trade_ticks` onto `market_capture_trades`.** [[project-mai-tai-market-capture]]
- **2026-06-22 (Mon EOD) — Polygon data-capture infra SHIPPED · #350 close-verdict CPU-bound · OMS after-hours-ATR fix · ATR oracle landed.**
  - **🟢 Central Polygon tick/bar capture — MERGED + DURABLE.** #354 (`market_capture_app` service, Restart=always — raw Polygon trades+quotes from the gateway stream, ms→ns normalized) + #355 (`/v3/trades` historical backfill) + #356 (daily 21:00Z post-close gather of the FULL scanner-qualified universe: trades+quotes+**1-min bars** via Massive REST → `market_capture_bars`). 14-day prune covers all 3 tables. **Chose REST-gather over widening the live stream — deliberately, to NOT burden the #350 CPU-bound strategy-engine** (all consumers drain the shared stream). No-stored-raw-ticks gap closed; Massive REST entitlement CONFIRMED (trades/quotes/1-min-aggs). [[project-mai-tai-market-capture]]
  - **🔴 #350 freeze VALIDATION (both windows) = NOT a full fix, it's CPU-BOUND.** Open 63s gap (09:43 ET) + close **76s gap (16:02 ET)**, CPU pegged **~100% user-space, %wait≈0** during both. The DB-offload IS active + removed the I/O-wait stall, but the loop still saturates one core ~60-76s at peak windows. **PIVOT (armed tomorrow): py-spy the indicator-recompute hotspot.** [[project-mai-tai-polygon-freeze]]
  - **🟡 OMS fix #358 (`f427434`) DEPLOYED — v2 ATR after-hours UNBLOCKED.** Root cause: the Tier-3 setup-revalidation guard (`_intent_setup_invalid_reason`, PR #178) abandoned every v2 ATR-Flip intent that didn't fill instantly, because the isolated v2 bot writes NO decision tape (all v2 bars `decision_status=''` → always `idle != signal` → `SETUP_INVALID`). RTH fills in ~2s before the guard; after-4PM thin liquidity → guard cancels → **ATR was silently RTH-only** (3 good after-4PM setups cancelled today). Fix = **fail-open when the bar has no decision_status** (momentum bots unaffected; 44/44 OMS tests + 2 regression green; OMS-only restart, flat-confirmed). **⚠️ BEHAVIOR CHANGE: v2 ATR now fills after-hours (real money) — see OPEN #9.**
  - **ATR oracle landed (#357)** — `analysis/atr_flip.py::compute_atr_trail` (the reference the live v2 `_update_atr_state` is pinned to) was off-main; landed code-only + import test. Determinism test still passes (live v2 == oracle, no drift).
  - **Read-only research:** TIMESALE_EQUITY is a **dead Schwab service** (not an entitlement — Schwab has no equity T&S; flag rolled back) → true T&S = Polygon. **Intrabar-ALONE = WASH** on today's 8 ATR trades (bot already books the trail touch-price). **Hold-confirmation = nets POSITIVE** (real-tick replay: best net_hold/10s skips 3/5 false-flip losers, keeps winners, +$0.75) — **promising NOT proven** (1 loser-heavy day). Intrabar is the plumbing hold-confirmation needs (build together, design-first, default-off, more days). [[project-mai-tai-tick-confirmation]]
  - **🔜 ARMED for tomorrow 2026-06-23 09:30 ET open (autonomous VPS capture PID 2603075):** (1) **ORB full validation** (OR builds? breakout evaluates? → unblocks the real-money flip) + (2) **#350 CPU profiling** (py-spy). Reported separately ~09:53 ET.
- **2026-06-22 (Mon) — ORB fix shipped · #350 freeze verdict = CPU-bound · TIMESALE = dead Schwab service.**
  (1) **ORB** found silently inert (1970-timestamp bug) → **#352 merged + deployed + mechanically validated** (see OPEN
  ITEM #8); full OR/breakout validation = tomorrow's open; real-money flip stays blocked. (2) **#350 freeze fix —
  OPEN-window verdict = NOT fully fixed, it's CPU-BOUND.** The DB-offload is ACTIVE and removed the I/O-wait stall, but a
  **63s snapshot-batch gap recurred 09:43 ET with CPU pegged ~100% USER-space (`%wait≈0`)** → remaining freeze is
  CPU-bound (likely synchronous indicator recompute on the loop), NOT DB-I/O. **PIVOT = profile the strategy-engine CPU
  hotspot at peak-volume windows** (py-spy on indicator recompute). Close-window confirmation pending ~16:12 ET. (3)
  **TIMESALE capture** enabled 08:47 ET → **Schwab REJECTED (code-11)**. Disambiguated via `GET /trader/v1/userPreference`
  (we HOLD the NP + level2 Market-Data bundle → **not an entitlement wall**) + the schwab-py service catalog (**Schwab's
  streamer defines NO TIMESALE service** — equity streams are only CHART_EQUITY/LEVELONE_EQUITIES/NYSE_BOOK/NASDAQ_BOOK/
  SCREENER_EQUITY; TIMESALE_EQUITY is a legacy TDA name dropped in the migration). **Schwab cannot provide equity
  time-&-sales at all** — not renameable, not requestable. Flag left ON (benign/inert, operator choice; not a bug — do
  NOT re-flag the code-11). **Consequence: the July tick-confirm decider must source true trades from Polygon/Massive,
  NOT Schwab.** [[project-mai-tai-orb]] · [[project-mai-tai-tick-confirmation]]
- **2026-06-19 (Juneteenth, market closed) — POLYGON 30s: freeze root-caused + 3 fixes DEPLOYED, freeze fix ACTIVE.**
  Read-only diagnosis (PROVEN): the **strategy-engine asyncio event loop freezes 50–345s at peak-volume windows** (RTH
  open + 4pm close), process-wide, localized to strategy-engine (stalled bars carried real volume → feed fine; **v2/ATR
  are a SEPARATE service, not this loop**). Root cause = **synchronous bar persistence (`_persist_bar_history` /
  `_persist_revised_closed_bar`) doing SELECT+commit on the event loop** inside the ≤5000-event drain (revise path scales
  with tick volume). Proof: snapshot-batch cadence stopped 96s then 53s on 06-16 19:56–20:03, bars drained in catch-up
  bursts (CRVO 345s lag). Data integrity CLEAN (OHLCV 0 corrupt across 643k rows/39d; stalls = lateness NOT loss; ~4.5%
  synthetic zero-vol bars = backtest caveat). Dashboard "Persist lag" was a MISLABEL (it's bar AGE = now−latest_bar, not
  write-lag → off-hours always elevated/red). **Three held PRs built, diff-reviewed, merged (non-admin squash) +
  DEPLOYED Sunday-eve via Deploy Main** (holiday override `allow_live_restart=true` — the calendar guard false-blocks on
  Juneteenth; verified FLAT first: 0 virtual positions / 0 working orders / only protected CYN):
  - **#348** display-only relabel `Persist lag → Bar age` (chip `lag elevated → bars stale`).
  - **#349** polygon_30s default → `paper:polygon_30s` + `simulated` (was live:/webull, uncredentialed → shadow-reject
    footgun; 06-18 = 262 intents ALL rejected / 0 fills). Routes to SimulatedBrokerAdapter (fills in sim). VPS env flipped
    + 2 stale default-pinning tests updated. **Note: `shadow` is a display label, does NOT gate execution.**
  - **#350** flag-gated **batched persist offload** (the freeze fix): persists capture-at-call → buffer → flushed off-loop
    via `asyncio.to_thread` BEFORE intents publish (+ iteration safety-net + shutdown). **Default OFF = byte-identical**;
    8 new + 217 service tests green; full correctness audit in the PR (one ordered session + flush-before-publish =
    read-after-write safe; per-item commit/rollback isolates failures; persist-path only).
  - **Env now:** `paper:polygon_30s` · `simulated` · `MAI_TAI_STRATEGY_PERSIST_OFFLOAD_ENABLED=true` (ACTIVE, confirmed
    via settings-load). 5 core services restarted (NRestarts=0, 0 errors, heartbeat advancing); v2/orb untouched;
    dashboard = "Bar age" + shadow/simulated.
  - **⚠️ Offload path NOT yet exercised with live bars** (market closed until Mon ~4am ET premarket). **Monday watch (no
    restart):** at 09:30 open + 4pm close run `pidstat -p <strategy_pid> 1` + time the drain. Worked → no 50–345s snapshot
    gap, CPU idle-during-DB. Still freezes → CPU-pegged = CPU-bound (#2) not DB-I/O (#1) → pivot. Memory [[project-mai-tai-polygon-freeze]].
  - **Server cleanup INVENTORY (read-only, nothing deleted):** NO logrotate on `/var/log/project-mai-tai/*.log` (1.4GB
    unbounded); DB 3.6GB — `reconciliation_findings` 1GB/2.4M rows + `strategy_bar_history` 1.7GB UNBOUNDED (only tick
    tables pruned @30d); schwab_ticks JSONL 1.3GB static (stopped 06-08). Disk 14% fine; **4GB RAM is the constraint**
    (ties to freeze DB contention). Retention strategy proposed, awaiting operator approval.

- **2026-06-18 (late) — ORB (P6 "OPEN") BUILT + DEPLOYED PAPER.** Deployed after-close (Juneteenth+weekend → validate
  over the weekend; **real-money flip = separate later gate**, Mon 2026-06-22 qty 10 target). On `main`: **#344**
  (consolidated stack 3a service + 3b entry + 3c heartbeat), **#340** (OMS bid-only TRAIL-8% ratchet, inert at
  trail_pct=0), **#345** (dashboard render allowlists + paper-aware PAPER/LIVE pill), **#346** (register `orb` in
  `runtime_registry`, gated, isolated). `project-mai-tai-orb.service` **active+enabled, NRestarts=0**, heartbeat
  publishing, `account_name=paper:orb`. **Verified rendering** (control-plane :8100): `/api/bots` lists `orb`; compact
  dashboard shows the ORB card with a **PAPER badge**; `/bot/orb` detail page = HTTP 200. Architecture: isolated bot
  (own process like v2) consuming the EXISTING market-data gateway (Polygon/Massive trades, NOT Schwab); universe =
  premarket two-squeeze `momentum_confirmed` (no seed hookup). **Dashboard-render lesson:** a 3c heartbeat is NOT enough —
  `_build_bot_views` builds `data["bots"]` from `configured_strategy_registrations`, not the stream, so a card needs
  THREE layers (registration + render allowlists + paper pill). **Caveats:** each `Deploy Main` showed red "failure" =
  benign reconciler/CYN health-gate (services all healthy, code landed); new systemd unit isn't in `install_units`
  allowlist (installed by hand once); `--delete-branch` on stacked #339 closed the dependent #341 → recovered via a
  consolidated local merge (#344). **OPEN:** the `confirmed_at` pre-09:25 universe mapping is the #1 Monday watch
  (eyeball the armed heartbeat watchlist pre-open — safe-fails to sit-out); restart-while-holding UNTESTED (shares v2's
  open item #1); kill-switch = bot-kill keeps OMS TRAIL-8% but OMS-restart drops the native stop → flatten manually.
  Memory: [`project_mai_tai_orb.md`](.). Entry-rule ref `docs/schwab-1m-v2-entry-criteria.md` sibling
  `docs/orb-opening-range-exit-research.md`.
- **2026-06-18 — ORB opening-range path (P6 "OPEN") — RESEARCH COMPLETE, settled config → deployment scoping.**
  Full writeup: [`orb-opening-range-exit-research.md`](orb-opening-range-exit-research.md); engine
  [`scripts/orb_exit_backtest.py`](../scripts/orb_exit_backtest.py) (cache `/tmp/orb_bars.pkl`).
  **SETTLED: ENTRY = PRIOR** (5-min OR from 09:30 · close>OR_high · vol≥1.5× · close>VWAP · close>EMA9 ·
  width<12% · cutoff 10:30 · one/symbol); **EXIT = TRAIL-8%** (ratchets from HWM). **Data:** stored
  `schwab_1m_v2` bars are watchlist-gated (winners promoted post-breakout) → must source from **Schwab REST
  pricehistory** (validated vs Pine); live prereq = scanner surfaces candidates pre-09:30. **EXIT study
  (25 days, 159 entries, RGNT-06-15 out):** TRAIL-8% the **only** exit with positive median capture (+0.22)
  + highest win 55%; **TRAIL-3% overfit** (7d 0.41 → 25d −0.20); COMBO/multi-layer/EMA all lose. **ENTRY
  study:** a 09:25/7-min/frozen-high sharpening **failed to beat PRIOR** — structure worse net expectancy
  (avg 2.5 vs 3.4), **VWAP/EMA filter INERT** on it (rejected 1 of 492 → precision gap is structural not
  the filter), **volume sweep no sweet spot**, and **without INHD(+314%)/QUCY(+82%) the new structure
  collapses 2.5→1.7 while PRIOR is robust 3.4→3.1.** **Honest framing:** ORB is a **thin-edge,
  runner-dependent** strategy (best ~3.4%/take, 55% win, +0.22 median capture; profit in the tail) —
  **leading candidate for live, NOT a verdict.** **Gap-through caveat kept:** TRAIL-8% is a hard intrabar
  stop, modeled fills optimistic on thin books. **Next:** scope intrabar-on-LEVELONE for ORB (per-path
  isolation + ORB policy + gap-through frequency), then forward-validation, then small attended go-live.
  (PR #337 = the 7-day writeup + engine; this entry supersedes it with the settled conclusion.)
- **2026-06-17 (late) — TIMESALE capture (#335) + tick-confirmation research day.** Audit found our trade ticks are
  LEVELONE quote-snapshots not true trades (Schwab has no historical T&S). Built additive capture-only TIMESALE_EQUITY
  (#335, merged + deployed flag-OFF/inert); **enable pending attended next-open (open item #7).** Also de-flaked 2
  time-dependent strategy-engine data-halt tests (trading-hours gate vs CI-run-time → frozen clock) to keep CI real-green.
  Research (NOT deployed): tick-confirmation per-path (P5/P1 help, P4 doesn't; P4 not a loss engine, P1 the bleeder;
  mixed +$3.41), ATR intrabar false-flip cost ≈ cancels benefit, intrabar-execution + timesale design docs. Open item #8 +
  [[project-mai-tai-tick-confirmation]]; docs in `/home/trader/*.md`.
- **2026-06-17 — OMS tick-by-tick exit consumer (#333) diagnosed → built → CI-green → DEPLOYED live.** From the live LNAI
  scale that filled ~70s late at 4.345: root-caused to quote-consumption lag (broker-sync REST inline-blocking the
  shared read loop) + processing-time staleness stamping; fixed with a dedicated `_run_tick_consumer` task +
  last-quote-wins coalescing + event-time staleness. Attended deploy (v2 flat, only protected CYN), OMS+strategy restart
  only, clean. Also confirmed CI `validate` is green again (open item #2 cleared). Phase 2 (resting brackets) = open #6.
- **2026-06-17 — #326 (Schwab-ineligible eviction) built → reviewed → DEPLOYED → restart-verified.** Ported the old
  schwab_1m bot's watchlist eviction into the isolated v2 bot (`_schwab_ineligible_symbols`, 60s cache, OmsStore loader,
  session_date parity with the OMS write-side). Merged `fe76f06`; v2 restarted; eviction proven on fresh boot.
- **2026-06-17 — Mid-session restart recovery profiled (FLAT):** ~17s blind, comes back with scanner-set-minus-ineligible.
  **restart-while-HOLDING still untested (open item #1).**
- **2026-06-17 — v2 GO-LIVE deployed (attended, reasoned risk).** PR #325 merged (`cbd1a09`); env set (ATR-only +
  go-live + `live:schwab_1m_v2` + schwab); strategy/OMS/v2 restarted clean; all gates verified. **Morning verdict:**
  04:00 roll/race-fix ✅; isolation ✅ (zero P1/P2, zero CYN); **4 ATR orders all $0** (3 Schwab-REJECTED foreign-restricted,
  1 LNAI accepted-then-our-cancel) → first real fill pending (open item #3); Schwab API-open restriction finding (open #4).
- **2026-06-16 — Go-live workstreams scoped; ATR qualifier MECHANISM validated; secondary qualifier rejected; age-gate
  validated; 04:00 race diagnosed; handoff restructured.** (Full detail in the archive.)
- **2026-06-15 — ATR qualifier built+enabled (#320); tick-capture activated (#282); warmup early-fire + DB-seed fixes
  live-proven; v2 OMS exits activated.**

---

## 📚 ARCHIVE INDEX (deep history — open only to dig)

| file | covers |
|---|---|
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | go-live + morning verdict + #326 + restart recovery, OMS exits, ATR qualifier, age-gate, 04:00 race, tick-capture |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine groundwork, regression battles (56 entries) |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | earliest — token-SPOF saga, early v2 scaffolding, streamer fixes (3 entries) |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2-isolated bot's own deep design/status history |
| `session-handoff-global.md` | frozen pre-split monolith (backup; to be retired) |

---

## 🔗 KEY REFERENCE DOCS (design-first / canonical)

- **Entry rules:** [`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md) ·
  [`schwab-1m-v2-atr-flip-entry-design.md`](schwab-1m-v2-atr-flip-entry-design.md) ·
  [`schwab-1m-entry-gates-extracted.md`](schwab-1m-entry-gates-extracted.md)
- **ATR qualifier + warmup:** [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md) ·
  [`v2-atr-early-warmup-fix-design.md`](v2-atr-early-warmup-fix-design.md) ·
  [`v2-warmup-db-seed-fix-design.md`](v2-warmup-db-seed-fix-design.md)
- **Go-live / race fix:** [`v2-paper-to-live-credential-transition-scoping.md`](v2-paper-to-live-credential-transition-scoping.md) ·
  [`v2-0400-watchlist-race-fix-design.md`](v2-0400-watchlist-race-fix-design.md)
- **Exits / ticks / pricing:** [`v2-tick-capture-design.md`](v2-tick-capture-design.md) ·
  [`v2-reference-price-fix-design.md`](v2-reference-price-fix-design.md)
- **Resilience / ops:** [`schwab-1m-v2-loop-resilience-design.md`](schwab-1m-v2-loop-resilience-design.md) ·
  [`vps-deployment.md`](vps-deployment.md)

## 🧠 MEMORY POINTERS (auto-load each session; listed for cross-reference)

[[project-mai-tai-context]] · [[project-mai-tai-0400-watchlist-staleness-race]] ·
[[project-mai-tai-v2-real-account-routing-risk]] · [[project-mai-tai-v2-entry-warmup-gate]] ·
[[project-mai-tai-v2-no-exits]] · [[project-mai-tai-v2-entry-criteria]] ·
[[project-mai-tai-schwab-bar-build-core]] · [[feedback-session-doc-and-memory-discipline]]
