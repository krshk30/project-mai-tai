# 2026-09-21 — the Webull confirmation exit failed LIVE on its first run after #1014

**Author `claude-1`, written during the session it happened in (10:25 ET). Reviewer `codex-2`. NO code in this PR.**
Operator, 10:20 ET: *"this is recurring, I thought we fixed it last week… this time we need to really make sure we fix it."*

## Verdict

1. **#1014's promise — every confirmation exit ends SOLD, resolved-by-fill, or REPROTECTED + PAGED — was broken on its
   first live exercise.** GLND 10:18 ET: Webull's bracket was cancelled cleanly (`confirmed=2`), and then **nothing**: no sell,
   no re-protect, no page, no log line. 1 of 1 live Webull exercises failed. Schwab's leg of the same exit sold in 2.8 s.
2. **Cause [inferred from code + timing; the return is SILENT, which is itself the defect]:** the SAME freshness guard runs
   TWICE on ONE quote with seconds of inline broker awaits between the two runs. `_evaluate_v2_managed_exit` reads the quote
   once at the top; the first guard (`oms/service.py:5250`–`:5252`) found it fresh and authorised the release; the release
   took 2.433 s inline on the serial tick consumer (which cannot receive a newer quote while it is blocked); the second,
   generic guard (`:5384`, *"stale quote — never act on a gap"*) then found the very same quote **5.17 s** old against a
   5,000 ms limit and returned — bare `return`, after the pending decision had been popped (`:5364`), so no later tick
   retries. **The first pass authorised an irreversible cancel; the second pass vetoed the sell that was its only purpose.**
   Four such bare returns sit between the release and the block that guarantees a terminal state (`:5362`, `:5378`, `:5384`,
   `:5386`).
3. **Why last week's fix missed it** — five reasons, none of them bad luck (section below): the guarantee was scoped to the
   `try:` block and nobody listed the returns outside it; the tests use zero-latency fakes, so no quote can age across a
   release; the one stale-quote test ages the quote BEFORE entry, which exercises the first guard only; the review's
   mutations were all about broker answers; and the fix shipped Saturday with its Webull path UNEXERCISED until this morning.
4. **A second, independent failure the same morning:** NCPL's Webull fill went **undetected for ~786 s** (10:03:35 → 10:16:41
   ET) — no bracket, no managed row — because `GET /trade/order/detail` for that ONE order was refused `429` on **59 of 59**
   polls while other orders' reads were served. Same seam, different hole: the exit path trusts a status read it cannot get.
5. **The watch worked.** `EXITDONE1` went `RECURRENCE` at 10:20 ET: `live:orb: fired=1 sold=0 reprotected=0 gap=1`.
6. **Cost so far: small, by luck and by size.** One share each. NCPL −8.3% on Webull (its native stop filled once attached) vs
   −7.4% on Schwab. GLND: still held at the time of writing, quote 2.85 / 2.86 against a 2.85 entry.

## Tape — GLND, all times ET (log stamps are UTC − 4 h)

| time | event | source |
|---|---|---|
| 10:16:24.767 | Webull buy 1 @ 2.85 detected; 10:16:26.089 pair attached — target 2.9925, stop 2.6220, attempt 1 | `[WEBULL-BARE-FILL]`, `[WEBULL-PROTECT-ATTACHED]` |
| 10:16:25.208 | Schwab buy 2 @ 2.85 | `[OMS-V2-MANAGED-OPEN]` |
| 10:18:02.679 / .684 | confirmation exit FIRED, both accounts, `status=PENDING_EXECUTABLE_BID` | `[OMS-V2-CONFIRMATION-EXIT-FIRED]` |
| 10:18:02.783 | the GLND quote event nearest the start of the evaluation (2.86 / 2.87) | `market_capture_quotes.event_ts` |
| 10:18:04.157 | Schwab protection RELEASED; 10:18:05.477 market sell 2 → **filled 2.865 (+0.5%)**; 10:18:05.505 flat | `[…-PROTECTION]`, `fills`, `[OMS-V2-MANAGED-CLOSE]` |
| ≈10:18:05.520 | Webull leg starts (10:18:07.953 − `inline_seconds=2.433`) | derived |
| **10:18:07.953** | **Webull pair RELEASED `attempt=1/3 requested=2 confirmed=2 inline_seconds=2.433`** — quote age now **5.17 s** | `[…-WEBULL-RELEASED]` |
| 10:18:07.953 → | **no `[OMS-V2-MANAGED-EXIT]` for live:orb, no close in `broker_orders`, no REPROTECTED / PAGE / UNCOVERED / REFUSED** | whole `oms.log`, `broker_orders` |
| 10:18:25 → 10:23:42 (every ~30 s) | `[OMS-OCO-EXIT-FILL] live:orb GLND exit-fill fetch FAILED (transient, e.g. 429)` ×9 | `oms.log` |
| 10:20:08 | `[RECURRENCE] EXITDONE1 … live:orb: fired=1 sold=0 reprotected=0 gap=1` | regression watch `STATUS.txt` |
| 10:23:55 | broker position `live:orb GLND 1` still held; managed row open, so the software ladder still watches it; **no broker stop** | `account_positions`, `virtual_positions` |

## The code path (main `f2f4615`, `src/project_mai_tai/oms/service.py`)

`_handle_quote_tick_event` stores the quote, then `for acct in self._v2_accounts(): await self._evaluate_v2_managed_exit(…)`
— Schwab first, Webull second, **sequentially, on the serial tick consumer**. Inside `_evaluate_v2_managed_exit` (`:5202`):

1. `quote = self._latest_quotes_by_symbol.get(symbol)` — read once, at the top.
2. confirmation branch: `protection = await self._prepare_confirmation_webull_leg(…)` (`:5310`) — cancel **and read** both
   legs, up to 3 attempts with 0.5 s + 1.0 s back-off, all awaited inline.
3. `protection == "released"` ⇒ the pending decision is claimed and **popped** (`:5362`–`:5364`).
4. **Then** the generic guards run — each a bare `return`, none calling `_finish_or_recover_confirmation_leg`:

| line | guard | after a release it means |
|---|---|---|
| `:5362` | `if confirmation_pending.get(key) is not confirmation: return` | another task owns the decision — but THIS task just cancelled the pair |
| `:5378` | `if not quote: return` | no quote ⇒ abandon, pair gone |
| **`:5384`** | **`if age_ms > oms_v2_exit_quote_max_age_ms (5000): return  # stale quote — never act on a gap`** | **the candidate: 10:18:07.953 − 10:18:02.783 = 5.17 s** |
| `:5386` | `if bid <= 0: return` | no bid ⇒ abandon, pair gone |

5. Only after those does the `try:` begin, inside which every exit path — snapshot missing, dedup, different position, emit
   outcome — goes through `_finish_or_recover_confirmation_leg`, which spawns the re-protect + page. That is #1014's guarantee,
   and it starts four returns too late.

**The guard is right and in the wrong place.** "Never act on a gap" protects a decision from a stale price. Here the decision
was already taken on a fresh quote at 10:18:02; by `:5384` the only thing left to act on is an irreversible cancel we have
already performed, and the guard's effect is to walk away from it.

**It is close to deterministic, not bad luck.** The quote's age at `:5384` = (Schwab leg, inline) + (Webull release, inline).
Today 2.8 s + 2.4 s = 5.2 s. Any day both legs hold the position and Schwab's close takes more than ~2.5 s, the Webull leg
arrives stale. ⚠ n = 1; the 09-18 failures predate #1014 and had a different cause (double cancel scored "unconfirmed").

## Why the previous fix (#1014) missed it

Asked by the operator at 10:26 ET. `codex-2` authored #1014; **`claude-1` reviewed and pinned it — this miss is as much the
review's as the code's.**

1. **The guarantee was written around a block, not around the release.** "Every exit ends SOLD / resolved / REPROTECTED+PAGED"
   is enforced by routing every `return` INSIDE the `try:` through `_finish_or_recover_confirmation_leg`. The release happens
   ABOVE that `try:`. Nobody — author or reviewer — enumerated the returns between the two. The operator's own rule from
   09-18, *review what the PR did NOT write*, was not applied to the 30 lines that mattered.
2. **The fakes answer instantly, so time never passes.** The suite replays real broker ANSWERS (request ids, codes) through
   `_FanoutAdapter`, whose release returns at once. Real latency today: Schwab leg 2.8 s, Webull release 2.4 s. A test in which
   the release costs no clock can never age a quote across it.
3. **The stale-quote test proves the wrong half.** `test_stale_quote_cannot_release_webull_protection` makes the quote 10 s
   old BEFORE the call and asserts no cancel happens — it proves the FIRST guard. No test makes the quote go stale DURING the
   release. It read as "staleness is covered"; it covered entry, not exit.
4. **My review mutated broker answers, not time.** The pin record lists the mutations — in-flight key leak, back-off values,
   class-D retry, unanswerable-as-released, budget exhausted. All about what the broker says. I withheld the first head for
   inline sleeps on the tick consumer (L2), accepted 0.5 s + 1.0 s, had `inline_seconds=` logged — and never set that number
   beside the 5,000 ms quote budget it spends. The figure that killed today's exit, `inline_seconds=2.433`, is printed in the
   log line I asked for.
5. **Merged ≠ deployed ≠ proven.** #1014 went live Saturday 08:10 ET with no Webull confirmation exit until today 10:18 ET.
   The handoff said so ("UNEXERCISED"), correctly — but it means Monday's first real exit was the first test of this path
   with real latency, with money on it.

**What changes in how fixes are accepted on this seam** (proposed, for the operator): (a) every exit test runs with a clock
the fake broker ADVANCES by measured latencies (today's: 2.8 s, 2.4 s; 09-18's from the tape); (b) the reviewer lists every
`return` / `raise` / `await` between an irreversible broker call and the terminal-state guarantee, in the pin record, by line;
(c) a fix on this seam is called DONE only after `EXITDONE1` shows it exercised clean — until then the board row stays open
and says UNEXERCISED.

## Class sweep — what else releases first and can return silently afterwards

Same function, other software exits (`CW_FLIP` releases via `_release_native_oco_for_cw_flip` INSIDE the `try:`, after the
quote guards — so the ordering hole is specific to the confirmation branch). **Not yet swept, and owed before any code:**
`CW_HARD_STOP`, `CW_FLOOR`, overnight flatten and the late-close guard on Webull each take the pair back through the older
one-try release; whether any of them can return between a confirmed cancel and a submitted sell has to be read line by line.
That is sweep item 1, and today is the reason it cannot be "several days".

## Second failure — NCPL, the fill nobody saw (times ET)

| time | event |
|---|---|
| 09:59:03 | Webull mirror accepted (stop 1.2137 / limit 1.2198) — BARE by design; the bracket attaches when the fill is DETECTED |
| 10:01:04 → 10:16:26 | `Webull order-status fetch failed` for that order: **59 of 59** polls, 4 per minute, all `429` on `GET /trade/order/detail`. In the last 5 minutes it was the ONLY order failing (18 of 18) |
| 10:03:35 | Schwab leg filled 2 @ 1.21. Webull's position table shows 1 @ 1.21 from 10:04:09 — the broker knew; our order row still said `accepted`, 0 fills |
| 10:10:33 | Schwab's native stop filled 2 @ 1.12 (**−7.4%**) |
| **10:16:40.710** | first successful read ⇒ `[WEBULL-BARE-FILL]`; 10:16:41.339 pair attached (stop 1.1132). **~786 s with no bracket and no managed row** |
| 10:17:44.527 | Webull native stop filled 1 @ 1.11 (**−8.3%**); the software close racing it was correctly suppressed (`[OMS-EXIT-PAIR-RESOLVED] … leg FILLED`) |

Not random throttling: one order starved on every cycle while another order's fill (GLND, 10:16:24) was read within the same
minute. **Hypothesis, NOT verified:** a fixed polling order plus a small per-window allowance at Webull refuses the same order
every time. `codex-2`, 10:28 ET [reported, not yet verified by me]: open orders are sorted newest `updated_at` FIRST and polled
sequentially; `fetch_order_update` does no scheduling of its own — so an order whose reads keep failing never gets a fresh
`updated_at`, stays at the tail, and is the one still waiting when the allowance runs out. A failure that keeps itself at the
back of the queue. `account_positions` had the truth 12 minutes before the order poll did — and nothing listens to it for this
purpose.

## The fix — what "really fixed" has to mean this time

Proposed for after the 16:00 close, in `oms/service.py` (held by `claude-1`), `codex-2` reviews. No code before the close.

1. **Reproduce first.** A test that drives `_evaluate_v2_managed_exit` with a release that takes 2.5 s of clock and a quote
   that is 2.8 s old on entry — and FAILS on main exactly as today: released, no emit, no recovery, nothing logged.
2. **One freshness decision per exit, taken BEFORE the irreversible step — never re-litigated after it.** The pre-release
   guard already exists and passed today; the defect is re-applying it to the same snapshot after seconds of our own awaits.
   After a confirmed release the quote is a price REFERENCE for a market close, not a permission.
3. **After a release there are exactly three ways out** — sell, resolved-by-fill, re-protect + page. Every `return` between the
   release and the emit routes through `_finish_or_recover_confirmation_leg`. For a MARKET close the bid is a reference, not a
   price: a quote that aged during OUR OWN release must not abort the close of a position whose bracket we cancelled.
4. **No silent return anywhere on an exit path.** Each abort logs `reason=`; today's cause had to be inferred from a stopwatch.
5. **Get the release off the serial tick consumer**, or at least stop paying for the Schwab leg's latency in the Webull leg's
   freshness budget (evaluate legs concurrently, or Webull first). This is the "retries off the tick path" half of sweep item 1.
6. **Fill detection must not depend on one starvable read.** When an order read fails N times and `account_positions` shows the
   shares, treat the position as filled: open the managed row, attach the bracket, page. Plus the per-endpoint call counter
   from `WEBULL_API_BUDGET.md`, so the starvation itself can be measured and then removed.
7. **Tests replay REAL tapes, each seen red first:** GLND 09-21 (this file), NCPL 09-21, GIPR ×2 and IMCC 09-18, DLXY 09-16
   (accepted-then-cancelled in a halt). A mutation per guarantee: delete the recovery call ⇒ red; move the release back above
   the guards ⇒ red.
8. **Live proof, stated in advance:** `EXITDONE1` `gap=0` on `live:orb` over 5 sessions in which a Webull confirmation exit
   actually FIRED; zero-fire sessions do not count; one gap reopens it.

## What this write-up cannot see

- Which of the four returns actually fired. `:5384` fits the clock to within 0.2 s; none of them logs. Step 1 above settles it.
- Whether `[OMS-OCO-EXIT-FILL] … fetch FAILED (429)` ×9 is a consequence (the sync looking for a fill on legs we cancelled) or
  a contributor. It began 17 s AFTER the silent return, so it is not the cause of the missing sell.
- How GLND ends. At the time of writing the share is still held with no broker stop; the operator chose to wait 10 minutes.
- Whether Webull's 429 allowance is per order, per endpoint, per app key or per account.
