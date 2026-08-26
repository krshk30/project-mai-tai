# Schwab 1m v2 post-close lifecycle: listen, enter, or exit

**Status:** design and first implementation, 2026-08-26. Review before merge; deploy before PR
#801 in a separate attended `schwab-1m-v2` window.

## The rule in plain English

The bot may keep **listening** after 16:00 ET, but it may not keep or create permission to **buy**.
The only live-money state allowed after the close is a position that filled before 16:00 and has not
finished exiting. That symbol stays subscribed and exit-managed until broker-confirmed flat.

These are three different states:

| state | after 16:00 | authority |
|---|---|---|
| `LISTEN` | allowed | watchlist plus held-symbol exit coverage |
| `ENTRY_ARMED` | forbidden | `cw_armed` and the resting/reclaim entry state |
| `EXIT_MANAGED` | required while held | position state, OMS managed position, and broker coverage |

This is why five symbols can still receive bars while **zero** are armed. DAIC and YYGH were not
special because they were the only symbols listened to. Their own post-close BUY flips happened to
set two symbol-local arms; the other listened symbols did not have that transition at that moment.
The variation was bar state, not subscription state.

## Lifecycle at 16:00 ET

The close boundary applies the following cases in order:

| observed state | action | resulting lifecycle |
|---|---|---|
| flat, no working BUY | release arm and entry claims | keep listening if still selected; no entry |
| unfilled resting BUY | request cancellation, then release entry identity/state | cancellation is requested, never described as confirmed |
| partially filled BUY | request cancellation of the remainder; preserve filled quantity | filled quantity remains exit-managed; no new entry |
| filled position | release entry permission; preserve quantity and exit coverage | keep listening and managing exits until flat |
| position or cancel outcome unreadable | release entry permission; retain protection/coverage | no new entry; outcome is `COULD_NOT_TELL` |

Protective SELL orders are not entry orders and are not cancelled by this boundary. A bar-close SELL
flip for a held position remains executable after 16:00. The change does not force liquidation at
16:00; it only forbids new exposure.

At 04:00 ET the next scanner session begins. The close period is therefore `[configured close,
next 04:00)`, not “the rest of the calendar day.” A 01:00 process pass belongs to the prior close
and must not consume the next 16:00 boundary key.

## Implementation

### Strategy boundary

`SchwabV2Strategy._entry_window_closed_for_session()` resolves the same configured close used by
the canonical entry emit gate. On every completed bar during the close period,
`_evaluate_completed_bar()`:

1. updates ATR state so a held-position SELL flip remains observable;
2. releases entry-side state and requests cancellation of any resting BUY;
3. logs `[V2-POST-CLOSE-ENTRY-BLOCKED]` when the bar is a late BUY flip;
4. evaluates the held-position close path;
5. returns before the reactive, resting, reclaim, and ordinary entry state machines.

This closes the defect at its source. The former emit-only guard stopped a BUY order but allowed the
bar state machine to arm, so the service truth and the restart gate disagreed until another flip or
restart.

### Timed sweep and cancel delivery

The existing five-second position poll performs one boundary census per 04:00-anchored session via
`release_entry_state_at_window_close()`. It applies to every symbol; held, operator-protected,
resting, and mid-warmup are **not entry-arm exceptions**.

The census marker is `[V2-ENTRY-WINDOW-EXIT-ONLY]` and carries:

```text
evaluated, released, arms_released, cancel_requested,
held_positions, armed_after_close
```

The polarity is stated on the line: `armed_after_close` must be zero. `evaluated=0` is measured but
empty; absence of the marker means the boundary did not run.

Cancellation drafts are built before segment identity is cleared, then drained directly from the
position poll. They do not wait for another bar and do not pass through the entry-window emit gate.
An unreadable position poll emits `[V2-POSITION-READ-UNKNOWN]`, preserves the last known position
state, and blocks entry permission; it never translates an unreadable result into broker-flat.

## Known causes addressed

- DAIC and YYGH remained armed while flat after 16:00 even though the entry emit gate was closed.
- A later symbol could replace either one in the armed set as post-close bars produced new BUY
  flips; the set was changing because arming continued until 20:00.
- The restart gate treated those arms as live entry risk while the emit gate treated them as inert.
- A working entry order at the boundary could otherwise remain live after its strategy arm was
  cleared.

## What this does not solve

- It does not prove that a broker accepted a cancellation. The current strategy-to-OMS handoff is a
  cancellation **request**. Outcome consumption and the bounded 10-second `could_not_tell` contract
  remain the separate C2 design work.
- It does not change any exit threshold, protective order, quantity, broker route, or §82 reading-A
  slot policy.
- It does not reconcile phantom internal positions with broker truth.
- It does not make a zero late-BUY-block count a pass. Zero blocked against zero post-close BUY flips
  is `UNEXERCISED`; the boundary census is the independent proof that the sweep ran.

## Falsifiers

The design is wrong if any of these happens:

- `cw_armed_segments()` is non-empty after the close census.
- A post-close BUY flip sets `cw_armed`, queues an open draft, or consumes a resting/reclaim slot.
- A held position loses quantity, subscription coverage, a protective SELL, or its bar-close SELL
  exit because its entry arm was released.
- A partial fill cancels the filled position rather than only the unfilled entry remainder.
- A Webull cancellation is stamped with a newly minted segment id instead of the entry order's id.
- The same close runs a second time at 01:00, or that 01:00 pass suppresses the next 16:00 census.
- A cancelled order is reported as confirmed before its durable outcome is read.

## First increment and what it proves alone

This PR is one deployable increment: close-boundary entry-state release, per-bar re-arm prevention,
direct cancel draining, the two success markers, and controls for both polarities. It proves that the
v2 process cannot retain or create entry permission during the close period while its existing held
position exit path remains reachable.

It does **not** prove broker cancellation. Live acceptance must therefore report `cancel_requested`
separately from any later terminal outcome.

## Deployment and live acceptance

Deploy this change before PR #801, not stacked with it. The service mutation is
`schwab-1m-v2` only. Review and deploy an immutable head after 16:00 ET in an attended window.

Acceptance on the first live boundary/restart:

1. exact main SHA on the box; new v2 PID; `NRestarts=0`; raw healthy heartbeat after start;
2. six runtime flags re-read from `/proc/<pid>/environ`;
3. `[V2-ENTRY-WINDOW-EXIT-ONLY]` present with its `evaluated` denominator and
   `armed_after_close=0`;
4. all watchlist plus held-coverage symbols remain subscribed;
5. if a post-close BUY flip occurs, `[V2-POST-CLOSE-ENTRY-BLOCKED]` is nonzero and no open intent
   follows it; otherwise that marker is `UNEXERCISED`, not passed;
6. any resting BUY at the boundary reports `cancel_requested`; confirmation is graded only from the
   OMS/broker terminal outcome;
7. a held-position SELL-flip control remains reachable;
8. bar-hole pre/post checklist and the recurring restart backfill burst are reported separately.
