# R6 Independent Assessment - Comparison Conflict

**AGREE on the availability defect; DISAGREE that the #620 clamp can never
change colour. R6 admission is not built or represented as green yet.**
The reviewer runner is cherry-picked unchanged from `64b00b9d`, as requested.
No trading source, flag, deployment or production state has been changed.

The first-available-bar coverage and other readiness work remain required.
Tonight's21:00 review target is not moved by this report; the comparison
disposition must be resolved before a green R6 claim.

## Own Runner Baseline

Executed the exact reviewer-owned runner through the real factory on its
exported UTC CSV/event inputs, with `PYTHONPATH=src:.`. Result:123 events,
zero ERROR, zero MISMATCH, zero buys while incomplete. At+10:

| Kind | Events | MATCH | HELD | No+10 reading |
|---|---:|---:|---:|---:|
| Re-add |86 |27 |37 |22 |
| Re-add+hold |13 |5 |5 |3 |
| Bars-missing hold |4 |2 |2 |0 |
| Reported pause |20 |0 |20 |0 |
| Total |123 |34 |64 |25 |

This confirms the availability failure, not install readiness. An earlier
invocation with `PYTHONPATH=src` could not import `tests` and produced123 ERROR
rows despite process rc0. It is excluded, not called PASS. The runner's output
must be inspected; process exit0 alone is not an acceptance receipt.

## Own Clamp-versus-Spanning Assessment

For each recorded event's first and+10 readings, reconstructed the entire
stored series through that reading using production `build_session_line`
(which already preserves indicator state and clamps true range across sparse
pairs), independently compared with the gap-spanning `atr_oracle` over those
same bars. Only sparse series were counted:105 comparisons,104 equal states,
one unequal state. This is a mathematical assessment of the proposed carry
rule, **not** an admitted result from the current gate.

| PMI10-02 reading ET | Production carry with #620 clamp | Spanning oracle |
|---|---|---|
|16:45, first colour divergence |SHORT, trail5.852338786738176 |LONG, trail5.7027 |
|17:15, re-add16:36:07 +10 reading |LONG, trail5.727122541190053 |SHORT, trail5.8432 |

23 retained bars run14:46 through17:15. The first sparse pair is14:51->16:15
(84 minutes); later sparse pairs include16:42->16:45,16:45->16:56 and17:10->17:15.
The clamp changes Wilder loss, then the trail crossing can change colour.
Therefore STATE equality to an oracle that spans the hole is not a universal
invariant of the requested R6 mathematics. No colour was forced to match and
no clamp, tolerance, reset rule or runner was edited to hide this.

**Safety nuance:** PMI's+10 is11 observed live arrivals, not10 contiguous bars.
Its final contiguous suffix is1. This episode must remain entry-held under the
existing ten-contiguous-live-bar rule, irrespective of the comparison oracle.
It is outside the entry window, but line/exit correctness still applies.
Availability acceptance must not turn a count of sparse arrivals into clean
bar coverage, or demand that this case release merely because11 bars exist.

## Independent Source Confirmation

Own low-priority box SELECT at09:15:23 ET: `SET TRANSACTION READ ONLY`,5s
statement timeout,500ms lock timeout, one symbol, exact14:46-17:15 window,
LIMIT24 with refusal above23. Returned23 rows/5977 bytes. Bar times, source
and every OHLCV value match the committed counterexample fixture23/23.
All23 `created_at` values in the reviewer's CSV were truncated to whole
seconds; the own direct capture retains exact subseconds. The fixture labels
the CSV source rather than claiming its arrival stamps are exact DB bytes.

Initial trader invocation could not read the root-only environment file and
made no query. The second, root read-only invocation succeeded; no permissions
were changed, no token refreshed and no services, orders, ledger or Redis
acted on.

## Tests And Requested Disposition

Three recorded assessment assertions pass, together with the ten factory
controls13/13. Ruff and whitespace checks pass. Tests are:

- `test_recorded_pmi_clamp_can_change_colour_not_only_trail`, two recorded
  readings with separately pinned clamp/spanning values.
- `test_recorded_pmi_plus10_is_not_ten_contiguous_live_bars`.

These pin the disagreement and safety denominator; they do not implement R6
or claim the new availability acceptance passes. No new full-suite result is
claimed for this evidence-only follow-up.

Recommended reviewer disposition: keep #620 and the ten-contiguous-live-bar
hold; use a separately implemented clamp-aware oracle for sparse R6 cases,
and the existing spanning oracle for fully repaired/contiguous series. The
reviewer owns any acceptance-runner change. Alternatively retain the current
gate and mark this recorded contradiction blocked. An explicit question and
counterexample were sent; no comparison rule is silently weakened.

## Receipts

| Raw source/result | SHA256 |
|---|---|
| Reviewer CSV `v2_bars_0928_1006.csv` |680042a1c4472c4914d36859701b68ebf6fbcf7f21896ad67f24cecee271758d |
| Reviewer UTC events `v2_events_0928_1006.txt` |888d6ccab0b81b289b1c714f1fef1193d23f6b709951a016b2dabd04ab2b648c |
| `/tmp/codex-line-restore-baseline-validpath-20261006.log` |67d53adf71051ef2c00e27cbdc0255eed5d47442ecf8c187e827df22c07f7d65 |
| `/tmp/codex-line-restore-baseline-validpath-20261006.json` |e44f780b2102e62c95e6926d9b5bd754f8a80cf09e9942103fe2be563a70ff2d |
| `/tmp/codex-r6-pmi-direct-read-root-20261006.json` |17003fa5953a26e01d897bc76c0cc063184b78b1b6dfeab94a3c038ea14c89e5 |

The reviewer CSV/events remain at their supplied scratchpad path, not copied
to production. The23-row fixture is committed, no invented candles, along
with the reproducible tests. No acceptance result is inferred from this report.
