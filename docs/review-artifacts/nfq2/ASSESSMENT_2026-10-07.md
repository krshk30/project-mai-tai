# NFQ2 Independent Assessment

As of 2026-10-07 09:16 ET. Code base:
`f9c9bd332392e2c905fc39b954421c88970844d7`.
This is an assessment, not a build, pin request, or installation authorization.

## Verdict

- AGREE: BIYA's two recorded pre-market buys were abandoned before submission for
  `NO_FRESH_QUOTE`: 236 Schwab shares and 118 Webull shares.
- DISAGREE with the named call path: this incident used the EH software-rest
  pricer, not `_apply_v2_eh_reactive_entry`. Implementing the proposed hold only
  in the named reactive helper would leave BIYA unfixed.
- DISAGREE with "two stores from one stream": the confirming stream ask is
  Schwab LEVELONE_EQUITIES; OMS prices from its independently consumed gateway
  QuoteTickEvent book. These are process-local caches with different producers.
- UNMEASURED: OMS's exact retained quote age at the two refusals and whether this
  instance was producer lag, consumer lag, or no cached ask. The existing refusal
  logs do not preserve that cache entry. A 2-7 second gateway-lag claim is not
  evidence of the OMS copy's age in this incident.

## Independent Evidence

Read-only pulls from `mai-tai-vps` on October 7. No service, broker, database,
Redis, production file, or deployment configuration was changed.

Raw sources:

- `/var/log/project-mai-tai/oms.log`
- `/var/log/project-mai-tai/schwab-1m-v2.log`
- PostgreSQL `project_mai_tai.trade_intents`, exact two IDs below. Queries used
  `BEGIN READ ONLY`, `SET LOCAL statement_timeout='3s'`, then `ROLLBACK`.

| Item | UTC | Evidence |
| --- | --- | --- |
| Confirming ask | 12:20:00.196 | `[V2-PM-CROSS-ASK] BIYA ask=2.54 ask_source=stream_ask_cache ask_age_ms=0 trigger=2.5400 cross_taken=1` |
| Cross path | 12:20:00.196 | `[V2-RESTING-EH-CROSS]`, `[V2-RESTING-EH-CROSS-STREAM]`; line 2.5191, trigger 2.5400, cap 2.5500 |
| Schwab intent | 12:20:00.242214 | `4497c490-b501-413b-9552-c0e51c0a1b3f`, quantity 236; `resting_entry=true`, `eh_resting=true` |
| Webull intent | 12:20:05.335865 | `6f993e4c-2c4f-4475-8ba8-fe95cfb9d342`, quantity 118; `fanout_leg=webull`, `fanout_source=eh_resting` |
| Schwab abandon | 12:20:00.439 | `NO_FRESH_QUOTE`, no fresh ask within 2000 ms |
| Webull abandon | 12:20:06.322 | Same code and freshness message |

Both intent payloads carry `refusal_origin=client_abort`,
`refusal_code=NO_FRESH_QUOTE`, `resting_band_pct=0.5`,
`resting_wire_stop_price=2.5400`, and `resting_wire_limit_price=2.55`.
Both carry the same fanout slot `d585906e-7867-515b-9524-a1ea3a6b5a2b` and segment
`1791375362370`. These are metadata reads, not broker-terminal proof.

## Own Code Read

- `OMSService._v2_eh_reactive_entry_applies` explicitly excludes both
  `resting_entry=true` and the pre-market resting fanout.
- `OMSService._v2_eh_resting_entry_applies` accepts BIYA's two shapes.
- `_apply_v2_eh_resting_entry` uses `_band_capped_marketable_limit` and
  `oms_v2_eh_resting_entry_quote_max_age_ms`, not the reactive age setting.
- `_handle_quote_tick_event` populates OMS's `_latest_quotes_by_symbol` with
  producer event time. `_fresh_ask` tests that book.
- `SchwabV2BotService._handle_stream_tick` populates `_eh_stream_ask_by_symbol`
  from Schwab LEVELONE_EQUITIES field 2, stamped at strategy observation time.
  The BIYA `ask_age_ms=0` therefore does not measure gateway delivery age.
- ORB's quote-priced abandon and NFQ1's Webull broker-rest mirror are separate
  paths and must remain unchanged.

OMS cannot directly read another process's Python cache. A deeper fix would be
an explicit timestamped Schwab quote publication/consumption contract, with
source-age, ordering, disconnect and cross-broker safety controls. Passing the
existing decision-time ask metadata is not a live shared cache and would not
meet "take the NEXT price" after a hold. No such source/protocol change is built.

## Scope Needed Before Build

Confirm whether the hold covers both v2 EH software-rest and reactive paths, or
BIYA's software-rest path only. Preserve each existing fresh-order pricing
contract: the software-rest band is 0.5%; the separate reactive helper uses its
configured max-cross/buffer contract. The held-only 1% cap must be anchored to
the approved trigger, not silently substituted for either fresh contract.

Any eventual hold must be durable, serialized and account-scoped inside the
economic slot; cancel/replace retirement and restart dispatch require proof.
Neither a hold nor a terminal outcome may be mislabeled as an immediate abort.
No implementation, acceptance replay, mutation result, or full-suite result is
claimed by this assessment.

## Subsequent Scope Resolution

The October 7 scope/pricing replies resolve the disputed build scope: BOTH v2
EH paths, both accounts; a fresh reactive cap changes from 1.0% to 0.5%; only a
durably held, serially claimed order gets 1.0%. The reactive ask +0.3% buffer
remains under that cap. ORB and the NFQ1 broker-rest mirror stay unchanged.
This does not resolve the historical OMS copy-age question: it remains
UNMEASURED and must not be attributed to gateway lag without evidence.
