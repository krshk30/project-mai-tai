# SLOTCLEAR1 soft-rest boot recovery

Base: `1e15adb03c3758e647d333828bbe3090e24e832e`. Independent read-only pulls on October 8; raw source paths and UTC stamps are in `restart-events.json` and `aixi-1008.json`.

## Verdict

AGREE with the lost-memory soft-rest cause; old lifecycle class, exposed by durable owner restoration. AIXI's software rest armed at 12:21:02.269 UTC, opportunity 1791462062227, underlying short segment 1791461820000. The 12:30:14.201 restart restored phase=resting at 12:30:15.242. A historical replay BUY then persisted awaiting_fill at 12:30:15.299 despite no new fill/position. Reconstructed cap cannot be treated as cap-only while this owner exists. The fresh BUY is the 12:39 bar delivered 12:40:02.534 UTC (08:39 bar ET), not a print at 08:39.

Positive evidence: an exact v2_wait_dispatch begin at 12:21:02.245985, configured primary+mirror, with NO attempt. Publication is recorded before xadd. This is not an inference from missing broker rows. Three earlier AIXI orders and two earlier Webull fills exist; they belong to the retired prior opportunity, not this rest. Source: the immutable read-only SQL receipt.

## Sweep

Kept logs since September 22: 20 starts, 1 with a logged pending software rest, 1 symbol/start pair (AIXI). `replay_restarts.py` checks every extracted start; `restart-denominator.json` records each one. Log absence is not complete broker proof. Earlier starts without the dispatch protocol remain UNMEASURED and cannot be automatically retired. The later 08:58 AIXI restart restores awaiting_fill, which is intentionally NOT widened into this resting-only recovery.

## Proof contract

One bounded off-loop startup read, before emitters, market-data tasks or seeding. Existing SLOTCLEAR fresh-flip flag gates it; no new switch. Overall startup wait 15 seconds; direct HTTP reads bounded at 5 seconds; existing fast SQL session bounds apply. No tick-path SQL/HTTP. Failure retains ownership.

Freshness/account coverage: direct Schwab account hash-to-number map plus exact securitiesAccount identity/currentBalances; complete Webull holdings pagination for its configured account, serial pages spaced 2 seconds, no adapter cache. Both accounts must be covered. First request timestamp must still be within the existing 15,000 ms ownership evidence bound when retirement is applied. Unreadable/malformed/foreign/paginated-unknown books refuse. Schwab refresh mode refuses; GETs bypass the auto-refresh wrapper.

The exact opportunity must have exactly one durable begin, no attempt, and no pending intent, order, later fill, open managed/virtual episode or working protection on either account. Flat direct books alone cannot release an in-flight buy. Filled/consumed/provisional owners and unreadable retry budgets stay owned. Canonical identity-first retirement is reused; the exact unconsumed restored fanout identity is removed only after durable retirement succeeds. Historical seeding still caps; only today's existing fresh-flip path can grant the first live entry.

## Limits

The replay uses all 101 stored AIXI bars and recorded flip timing through both math-only seeding and historical callback delivery, then the real on_observed_bar/live BUY and quote/draft queues. The prior short level is computed (2.010536023440064); a controlled legal-cent confirming ask of 2.03 emits exactly one primary 295 and mirror 147 draft, then no duplicate. The quote is an explicit in-band control, not a claimed historical print/cache observation.

The real callback exposed an additional identity-transport defect: ATR clears atr_short_flip_bar_ts on BUY before SLOTCLEAR reads it, and strict admission compares the cleared value to the retry segment. The ATR decision now carries its causal short segment; the fresh-BUY marker carries that same identity into strict admission on that exact bar only. Neither budget counts nor entry/window rules change. Historical delivery cannot mint this marker; both callbacks are covered. The new marker is included in the existing state probe.

Broker books in unit tests are explicit proof controls, not historical account reads at 08:30. Historical direct-flat freshness at that instant is UNMEASURED. A fresh 09:16 ET read-only invocation of the new proof code refused the old AIXI opportunity (`direct-proof-0916ET.json`), so no live release/PASS is claimed. No fill outcome is claimed. No production action, pin or activation in this lane. Parent owns the sole main baseline; full-suite parity remains pending its failed-name comparison.
