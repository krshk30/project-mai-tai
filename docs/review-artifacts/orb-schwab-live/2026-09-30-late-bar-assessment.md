# ORB Schwab late-bar assessment, 2026-09-30

Status: draft, not approved for live activation.

## Rule 1 call

The observer's `missing_last_closed_schwab_bar` decisions are a real mismatch with
the entry card: a bar not yet saved was treated as a negative MACD decision. This
does **not** prove a missed buy on 09-30. The completed 09:27, 09:28 and 09:29
Schwab-bar MACD histograms for both VBIO and LGHL were negative when their bars
became available. The eight observed checks had zero broker orders.

The eight decision timestamps and symbols are from
`/var/log/project-mai-tai/orb-schwab.log`. The corresponding 09:27-09:29 bars
were saved in `strategy_bar_history` 1.783-3.260 seconds after minute close;
each observed check preceded its relevant save. Unit tests use those exact
check/save timestamps for both the actual negative sign and a clearly synthetic
positive-sign counterfactual. They do not claim broker fills.

## Scope and remaining safety blocker

The draft makes the MACD gate three-way and retries the missing bar for up to
90 seconds. Only a computed negative cancels a still-unfilled order. A new buy
after 09:28:15 must carry the deferred 09:27-bar proof, and 09:29:30 remains a
hard no-entry boundary. The original 5-second **stream-bar processing** age is
retained at first receipt, not weakened to 90 seconds. OMS's ORB-only gate and
working-parent watchdog use the same three-way verdict. ATR intent handling is
untouched.

The one-of-two-share fill card is not yet met. Today
`_orb_schwab_cancel_refusal` refuses a parent with any buy fill, while
`oms/orb_schwab_eod.py` defers strategy exits until the parent is fully filled.
The OTOCO request builds two-share child sell legs. The adapter's separate
exit-only OCO has not been broker-previewed against a held share, per its own
docstring. Read-only `broker_order_events` evidence has no partial-filled buy
event since 2026-09-01; older partials are other strategies, not this ORB
bracket. Whether Schwab creates one-share protective children on a partial
fill is therefore UNKNOWN. Unit mocks cannot establish it. The partial-fill
exit/cancel/re-protection path needs a separately proven design and tests before
the live flag can be enabled.

No production checkout, service, environment flag or FLAGGATE expectation was
changed. A later live activation would add an OMS restart (and its strategy
companion) to the plan and require independent review and a new exact-SHA GO.
