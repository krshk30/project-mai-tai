# Two NEW359 full-suite findings: test-only closure proposal

Galileo. Separate root `/private/tmp/oct24-l11-359-full-test-closure-20261024`,
branch `codex/oct24-l11-359-full-test-closure`, exact parent359beec1b5051f84d5b0495cd8e61c91b8a6c3b6.
No APP/sql/ops/harness edits; Main359 source and actual case bindings do not move.
No author tests/import/AST/APP/replay/PG/provider execution. All changed selectors
below UNRUN, Main/C1 source review/claim required. Original full106FAIL retained:
104A2 common plus these2 new; no literal fullPASS or automatic retry inferred.

## Independent literals, not production-computed expectations

The real parser proof must equal literal
`85773763c8f060aa5c8322938149ca52270bab91b39521cfa4ab2601bb58d007`.
Independent bytes (no newline):
`[["IZM",1791878400000,10.0,10.05,9.95,9.99,20000]]`.
Only shell SHA256 of those fixed bytes was used, no production fingerprint call.

For canonical-versus-original proof, an explicit40-row candle fixture avoids
having to derive an expected value from generated floating-price operands.
`tests/fixtures/keepwatch/producer_literal_history40.json` is a manually specified
40-row full prefix, integer closes40..1, exact same minute timestamps/20kvolumes.
Its SHA including newline is
`1d0a8834cef32bc126d2b25c2b6bef0b575edf273064a8b3ba472edf04a2ef16`.
Compact literal-array bytes, no newline, independently SHA256:
`39bda40c60000aad19da9d7446b22b6ce71f423f91c1b495e5b2052f17d37574`.
This literal now pins the actual derived hash; its inequality from the original
raw response hash remains separately asserted. Original proof identity/source,
raw hash/IDs immutability, derived source, prepare and entries-held assertions
all remain. No assertion/guard/coverage reduction; full40 bars are still used.

The shared test fixture accepts optional explicit bars only for this changed
test. All other callers retain exactly the prior generated input/default path.
The actual constructor, safetyON, unreadable ownership/falseflip initializers,
standing-order preimage and production methods remain unchanged. This is not
historical-case, admission/native or current-source fingerprint evidence.

## Catalog arithmetic after static source audit

Actual catalog contains L11 KEEPWATCH expectedTrue, sole consumer schwab-1m-v2;
L12 expectedTrue, consumers schwab-1m-v2+oms. Both code defaults remainFalse.
The PMPRINT contract now checks the L11 consumer/default explicitly alongside
the existing L12 checks. Its legacy projection excludes BOTH added flags:
169 without momentum,172 including momentum, same literal historical arithmetic
161+2+3+3 /164+2+3+3. Current complete totals are172/175, not171/174.
The catalog itself, expected values, service ownership and install authority
are NOT changed. No absence expectation or genericTrue fallback is added.

## Main-only changed selector proposal

```text
tests/unit/test_keepwatch_session_producer_pin.py::test_real_rest_parser_and_streamer_are_one_producer_not_two_transport_pins
tests/unit/test_keepwatch_session_producer_pin.py::test_canonical_source_is_distinct_and_original_provider_attestation_is_immutable
tests/unit/test_pmprint1_tonight_flags.py::test_s6_catalog_matches_all_on_live_set_and_149_checks_with_restoration_on
tests/unit/test_no_test_reimplements_logic.py::test_no_test_computes_its_expected_value_from_production_code
```

Use existing reviewed controlled memory/network/processDENY fence with installed
asyncio plugin explicitly present, fresh private outputs/basetemp. No repeat39
other producer controls/4math gates/PMPRINT functional module/full by author.
This source change does not grant any focused or full execution itself. Main
owns exact source carry/final-full disposition, independent of remaining30.
