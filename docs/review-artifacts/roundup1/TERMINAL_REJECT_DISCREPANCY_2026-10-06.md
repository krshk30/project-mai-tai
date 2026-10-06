# Terminal-reject evidence, October 6

Own read-only check at 17:07:13-17:07:33 ET. No application, environment,
service, timer, ledger or database writes. Install1 has not started.

Webull list-today: HTTP200, two bounded pages, 86 orders, complete pagination;
serialized decoded reply sizes 32,552 and 23,763 bytes. Zero working OLOX/MOBX
orders. Raw local receipt `/tmp/install1-webull-terminal-reject-listtoday-20261006.json`,
SHA256 `22432bca997bba6e90ba9ee45687e47fd92ccf4674bfe09de8040dfae8f7bc4b`.
Three no-ID Webull rows have broker-source rejected events recording HTTP417
DAY_BUYING_POWER_INSUFFICIENT and zero fills: MOBX07bc532a7fdc,
OLOX0f1814197899 and OLOX496d3bf92b2a.

Schwab OLOX order `d56dac04-058e-47d1-b759-a9c9ffbbd9eb`, client ID
`schwab_1m_v2-OLOX-open-b95fc559b0a2`: rejected, no broker ID, zero fills.
Own bounded queries found **zero** broker_order_events by order ID or exact
payload client ID, including an all-date check. The exact linked trade intent
`99ee8c8c-0e8a-452f-b828-d09cd55ec9e7` is rejected and records
`refusal_origin=client_abort`, `refusal_code=rpg_stale_strategy_authorization`.
Raw repeatable transaction receipt
`/tmp/install1-olox-client-abort-proof-discrepancy-20261006.txt`, SHA256
`a73dfdd54c38679f92145d26c9b4b728e0e1bce9a9cbadaec6e3e35faf365ba1`.

The requested disposition specifically names a recorded reject event and says
broker_order_events carries client-origin proof. That source claim does not
match this pull. Asked whether the exact recorded client-abort intent is an
acceptable proof source; no fabricated event, rejected-status-only admission,
ownership release or broader gate waiver. Install2 merges held until Install1
is reported complete.
