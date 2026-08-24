# Webull order-history inventory probe

Status: executable specification; **do not run from this build task**. The IDE runs it on the box,
where the Webull SDK, token, and credentials already exist.

## Purpose and safety boundary

`scripts/webull_order_inventory_probe.py` asks one question: can Webull's account-wide history
listing enumerate venue orders that Mai Tai never recorded?

It is read-only by construction. The only allowed SDK calls are:

- `OrderOperationV3.get_order_history`
- `OrderOperationV3.get_order_detail`

The source contains no preview, place, replace, or cancel call. It does not restart services,
change flags, write the database, or modify the Webull account. It emits JSON Lines to stdout;
the operator chooses where to tee the evidence.

## Fixed positive control

The date window must include 2026-08-21. These broker-generated `combo_order_id` values are embedded
in the executable so the rotating OMS logs are not the only durable copy:

| Combo order ID | Symbol | Independent evidence |
|---|---|---|
| `31IUL7OCV3K860JRGF0LLE4MI8` | SUGP | OMS logged successful venue reply before constructor crash |
| `NVHC4FQV179G0KKQS0GAPMA4EA` | JUNS | same |
| `JMH2DE9M85S48LBG3IU5HORI4B` | USDE | same |
| `6THU0AUEPQJG6J9ISV6I50GHA9` | EXYN | same |
| `VHGU4AR1TEVN2QSSSDAEFAQP09` | USDE | broker screen corroborates symbol/time and stop `7.5905` |

Each control must occur in the **history sweep itself**, have the expected symbol, and expose at
least two child order records. The last control must include stop `7.5905`. Detail cannot rescue a
history control: if a complete, parseable, short-terminated sweep fails any of those checks, the
whole run is `VOID`. If transport, shape, or pagination fails first, there was no complete assay;
that is `COULD_NOT_TELL`, not a negative control.

## Pagination contract

The documented response is an array of group objects, each with an `orders` array. A group can make
the returned order-record count exceed `page_size`. Therefore:

1. Default `page_size=100`, the documented maximum.
2. Count nested order records, not only group wrappers.
3. Print `page`, `group_count`, `order_record_count`, `page_size`, input cursor, and output cursor.
4. `order_record_count < page_size` is the only clean terminal page.
5. A count equal to or greater than `page_size` requires another call using the last child
   `client_order_id`.
6. A missing/repeated cursor, repeated page fingerprint, repeated child client ID across pages, or
   reaching `max_pages` before a short page is `COULD_NOT_TELL`.

A cursor chain ending in a short page proves only that this API traversal completed according to
the returned cursors. It cannot prove the server did not skip a page internally.

## Four verdicts

- `FOUND` (exit 0): all controls reproduced in history; pagination ended on a short page; every
  requested target was found.
- `CONFIRMED_ABSENT_VIA_DETAIL` (exit 3): controls and pagination passed, and an operator-supplied
  **client_order_id** missing from history received authoritative `ORDER_NOT_FOUND` from detail.
- `COULD_NOT_TELL` (exit 2): auth/transport/429/server failure, invalid response shape, incomplete
  pagination, detail failure, a detail success that does not echo the ID, or an absence lookup in
  the wrong identifier namespace.
- `VOID` (exit 4): a complete sweep omits or contradicts any known-positive control.

`get_order_detail` takes `client_order_id`. The fixed controls are broker `combo_order_id` values.
An `ORDER_NOT_FOUND` after passing a combo ID into that client-ID parameter is **not** proof of
absence; it stays `COULD_NOT_TELL`, while the failed control makes the overall run `VOID`.

## Rate cost

Production permits two requests per two seconds for history and detail. The probe intentionally
spaces **all** calls 2.1 seconds apart: at most half the published quota, no probe-created burst,
and one nominal request slot left for the running OMS/status poller. The published limits are per
endpoint, but the probe conservatively paces history and detail together.

For `P` requested history pages and `M` misses in a **complete** history sweep, cost is:

```
requests = P + M
pacing floor = max(0, requests - 1) * 2.1 seconds
wall time = pacing floor + API latency
```

On the expected successful control path, `M=0`. If a complete sweep omits all five controls, the
probe spends five detail diagnostics but still returns `VOID`. If the sweep itself is incomplete,
it does not mislabel unobserved IDs as misses and does not fan out detail calls. It does not retry a
429; a rate refusal is evidence that the run could not tell, not an invitation to create a retry
flood.

The final JSON event prints history/detail/total request counts, page count beside every aggregate,
enforced spacing, pacing floor, actual pacing sleep, and elapsed time.

## Intended on-box invocation

Run only after reviewing the branch. The command below preserves only the five Webull settings and
runs as `trader`, so the SDK uses the same user's token storage. It does not touch systemd:

```bash
umask 077
sudo bash -lc '
  set -a
  source /etc/project-mai-tai/project-mai-tai.env
  set +a
  sudo --preserve-env=MAI_TAI_WEBULL_APP_KEY,MAI_TAI_WEBULL_APP_SECRET,MAI_TAI_WEBULL_ACCOUNT_ID,MAI_TAI_WEBULL_REGION_ID,MAI_TAI_WEBULL_BASE_URL \
    -u trader /home/trader/project-mai-tai/.venv/bin/python \
    /path/to/reviewed-branch/scripts/webull_order_inventory_probe.py \
    --account live:orb --start-date 2026-08-21 --end-date 2026-08-21 \
    --page-size 100 --max-pages 100
' | tee "/tmp/webull-order-inventory-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
```

Do not point `/path/to/reviewed-branch` at production `main` until this branch is intentionally
merged. The build task does not run this command.

The raw evidence contains account order history. `umask 077` keeps the created artifact private to
the invoking user; do not paste the raw file into chat or a public issue.

Additional locally known client IDs can be repeated with `--target-client-id ID`. Listing misses
for those IDs receive one detail confirmation each.

## What history must prove before reconciliation trusts it

One green control run is necessary, not sufficient. `get_order_history` becomes a trustworthy
reconciliation input only after all of these hold:

### Coverage

- The five external controls reproduce under the correct account and date window.
- Manual orders, SDK simple orders, exit-only protective pairs, and entry OTOCOs are all visible.
- Account/market/session filters do not hide an order class used by the system.
- Pagination is repeatably complete at several page sizes, and totals agree after deduplication.

### Freshness

- A newly accepted known order is compared against detail and history until history first exposes
  it; repeat for accepted, partial, filled, cancelled, and rejected transitions.
- The observed lag distribution has a stated reconciliation SLA. Until then history is a discovery
  source, not a real-time truth source.
- Detail remains the status authority for a known client ID because Webull explicitly warns that
  list endpoints may lag.

### Combo children

- Each known combo exposes every child, not merely the group ID.
- Children carry stable `client_order_id` and broker `order_id`, symbol, side, order type, status,
  total/filled quantity, prices, and timestamps.
- After one OCO child fills, history shows the fill and the sibling cancellation under the same
  combo identity. A group-level status alone is insufficient for exit reconciliation.

### Partial fills

- A real partial fill appears as `PARTIAL_FILLED` with
  `0 < filled_quantity < total_quantity`, a meaningful average `filled_price`, and a fill timestamp.
- Repeated reads prove whether `filled_quantity` is cumulative and monotonic. Reconciliation cannot
  consume it safely until that is established; treating a cumulative quantity as a delta would
  double-book fills.
- The final filled/cancelled state and quantities agree with Order Detail and the broker screen.

### What a complete sweep can and cannot prove

A successful sweep can prove that, for one account, date window, SDK/API version, and cursor chain,
the endpoint returned a complete-looking population that included the external controls. It can
discover venue orders absent from local tables.

It cannot prove immediate absence while lists may lag; cannot prove the server did not silently
skip a cursor range; cannot see order classes excluded by permissions or undocumented filters; and
cannot turn `ORDER_NOT_FOUND` in the wrong identifier namespace into evidence. Those remain
`COULD_NOT_TELL`, not zero.
