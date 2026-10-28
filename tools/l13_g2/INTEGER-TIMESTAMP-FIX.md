# L13 G2 integer timestamp source append / ALL NEW CONTROLS UNRUN

Main's C789 six-module run:104PASS1FAIL1explicitP3SKIP,10.07s,exit1.
Actual failed value249.999872ms versus exact250.0ms, not250.000128;
original raw/XML preserved (XML46932fab/raw d0f3b020 prefixes as reported).
No performance/actual P3 run started, no unchanged retry authorized by this file.

Cause: TradeSink converted produced_at through float timestamp()*1e9 before
integer conversion, losing128ns at this epoch. New datetime_epoch_ns normalizes
an AWARE datetime to UTC and derives integer days/seconds/microseconds from the
Unix epoch. SIP nanoseconds are NOT rounded. Subtraction is integer; only the
final difference converts to milliseconds. Naive timestamps fail, not localize.

Original exact [250.0] assertion is unchanged. Eight additive intended controls:
five integer UTC vectors (pre-epoch, microsecond,2038/2100 and+05:30), naive
refusal, a1ns difference at2100, and exact250ms at a non-UTC offset. No tolerance,
threshold/cadence/workload/logger/GC/heartbeat/ownership change; no production
edits. New helper runs ONLY in the controlled sink measurement, not app code.

G2 contract module now48 intended functional cases; other source modules
unchanged. Eight NEW controls UNRUN; main reviews the new composed head then
owns the new serial claim. Prior104passes are not a qualification of this head.
Use the same six WHOLE modules as C789 on the reviewed composition, with fresh
raw/XML, not a single failed-node retry; P1/P3 remain separate later exact claims.
