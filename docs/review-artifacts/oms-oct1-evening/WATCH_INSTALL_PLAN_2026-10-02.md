# 2026-10-02 INC1 watch follow-up (separate GO)

**Not part of the 2026-10-01 OMS install.** The installed
`/home/trader/unexercised_watch/watch.py` and its two root cron SHA guards
remain unchanged tonight. #1059 is merged but intentionally uninstalled.
Neither an OMS merge nor an OMS restart updates this installed copy.

Before proposing an exact-SHA after-hours GO, first confirm #1063 WBREAD1 is
independently pinned, merged, installed and healthy in the running OMS. Inventory
the incident sources emitted by the final OMS/ORB code against
`ops/health/unexercised_watch.py`'s INC1 query, classification and delivery
branches. The merged watch includes `oms_v2_overnight_flatten_blocked`, but
currently omits `oms_v2_cw_target_cancel_unconfirmed`; add the latter and any
other confirmed new source in a dedicated reviewed PR. Test one durable page
per new incident ID, repeat-run deduplication, unreadable evidence (fail
closed), and source-specific resolution. Do not install a source that the
watch cannot classify and page correctly. Obtain an independent pin and two
green validations on the exact watch head before merging.

For a separate operator GO naming the final full SHA, schedule one scoped
ops-only install after market hours, with no trading-service restart or flag
change:

1. Verify clean box checkout at that exact SHA, unchanged healthy service
   identities, current installed-watch hash, both existing root cron guards,
   and a readable `inc1-state.json`. Back up the installed watch, both state
   files and the full root crontab with timestamps and SHA-256; never clear
   delivered IDs or close incidents on an absent broker read.
2. Run the reviewed `ops/health/install_unexercised_watch.sh --install` with
   `EXPECTED_SHA` set to the exact checkout SHA. It must atomically install
   the repo watch and update **both** DST-safe root cron blocks; do not use a
   merge or checkout advance as a substitute. Verify installed file SHA-256
   equals the exact Git blob, and both read-back cron guards contain that hash.
3. Observe the next scheduled INC1 and unexercised runs. Record rc, the
   `INC1_STATUS` verdict, open/delivered denominators, raw `inc1-cron.log`
   and `cron.log` paths, and any delivered IDs. Existing delivered IDs must
   not page again. A missing run, unreadable state or unknown source is
   UNKNOWN and requires an operator page; no forced green result.

Journal the source PR/head, GO SHA, backups/hashes, cron diff, installed hash,
state continuity and first-run evidence. If any preflight or verification is
ambiguous, stop and keep the prior watch/cron pair; do not partially re-pin.
