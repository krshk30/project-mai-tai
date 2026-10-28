# PRICE1 SELL Controls / Existing Rate Selection

SOURCE ONLY, after0cd73b3ae0830703350d33878521cae1039b7184 at15:39:56 ET.
No tests/import/AST/compile/provider/runtime. No production delta vs529.

Gap: original new entry controls asserted SELL bypass on the shared helper, not
an actual adapter dispatch. New test_price1_native_exits.py authors12 controls:
3 strategy codes x PM/RTH x Schwab/Webull, actual adapter submit methods with
fake HTTP/SDK only. OLOX .97 SELL passes once with exact side/price/quantity,
no PRICE1 log and no fabricated fill. It is NOT the original10-21 native receipt.
Schwab uses the real _authorized_order_request_json before mock HTTP; the mock
does not swallow arbitrary keywords or forge an entry guard. Webull uses its
real builder with the existing fake SDK and a declared synthetic instrument.
No MOM entry guard is mocked away because SELL must not acquire that capability.

Main-only whole modules now5 own files: test_price1_floor.py,
test_price1_dispatch.py, test_price1_retained_waits.py,
test_price1_candidate_census.py, test_price1_native_exits.py.
Intended61+54+12=127 controls, ALL UNRUN / collection unverified.

## Existing P2 / Preservation Selection

Source-read original node:
tests/unit/test_sunday_v2_bar_tick_rate.py::test_sunday_60s_real_bar_and_quote_handlers_stay_under_50ms
It drives actual bot _handle_quote + _handle_bar, >=60s / >=200each/s, original
max loop/quote/bar <50ms. Its40 captured live flags plus own3 newer flags and MI
fixture are explicit source inputs; no new source changes/thresholds/log waivers.
Main's aa162 quiet PASS belongs to aa162, NOT this PRICE1/new composed head.
Required on final composition; it is still partial callback coverage, NOT proof
of every newly added entry/floor path or all tonight flags/native transport.

For MOMNEG+PRICE1 signal P2 use Cicero's approved real live-admission measurement
on the exact combined source, not standalone-head measurements. ORB final-thread
fences and L2 WAIT_PRICE1 composed controls remain separate required selections.
No new benchmark authoring / executor overlap / unchanged failed D3 rerun occurs
here; Cicero owns D3 diagnosis and original gates remain unchanged.

Missing actual10-12 prefix/config/clock contract remains explicitly documented in
PRICE1-CENSUS-V3-CLOCK-CONTRACT-20261022.md. This extra SELL source coverage does
not fabricate candidate counts, native outcomes, performance, original-case PASS
or install eligibility. All source awaits independent review and main execution.
