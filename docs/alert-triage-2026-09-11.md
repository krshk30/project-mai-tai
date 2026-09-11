# Actionable Alert Triage - 2026-09-11

## Decision

Alert only on an actionable live-money condition, once when the condition becomes active. Keep
diagnostics visible on the box, but do not send detection, recovery, aggregate, paper, or scoreboard
messages to the phone.

The operator's target is approximately five actionable pages on a day that previously produced 34.
This is a routing and attribution change only. It does not place, cancel, or modify orders.

## Measured Baseline

The 34-message snapshot comprised 20 repeated fleet-health pages, 8 bar-gap detection/recovery/halt
messages, 3 reconciliation pages, and 3 pre-open pages. The repeated conditions continued after that
snapshot; a later read found 21 fleet pages and 9 bar-gap messages. That growth is the evidence for
transition deduplication rather than a fixed cooldown.

## Routing Policy

| Source | Classification | Phone policy |
|---|---|---|
| OMS order lifecycle | LIVE_MONEY | Page once on RED; a resolved recurrence can page again |
| Stops armed | LIVE_MONEY | Page once on RED; a resolved recurrence can page again |
| Strategy bar freshness | PAPER | Log only |
| v2 bar continuity | DIAGNOSTIC | Log only in fleet health; the dedicated gap watcher owns escalation |
| D6 outcome acceptance | SCOREBOARD | Log only |
| Fleet aggregate | SUMMARY | Never page; it is a count, not a finding |
| Bar-gap detection | DIAGNOSTIC | Repair immediately; do not page yet |
| Bar gap unresolved for 5 minutes | LIVE_MONEY | Page once |
| Bar-gap recovery or market halt | DIAGNOSTIC | Log only; no GREEN or halt page |
| Reconciliation mismatch owned by us | LIVE_MONEY | Page once in either mismatch direction |
| Broker position with no ownership evidence | MANUAL | INFO only: not ours, taking no action |

Delivery is part of the state transition. A failed ntfy request does not consume the alert; the next
watch run retries it. Recovery clears the active fingerprint silently, allowing a later recurrence to
page as a new incident.

## Reconciliation Ownership

Ownership is derived from the current net fill balance for each `(account, symbol)`, and cross-checked
against both live OMS books. Order-history presence is not a discriminator: it marks both manual and
bot-traded symbols and depends on retention.

| Broker position | Net fill balance | Live OMS books | Result |
|---:|---:|---:|---|
| positive | zero | zero | INFO: manual/not ours; no page and no action |
| positive | nonzero | zero | CRITICAL: our fill exists but the live books cannot manage it |
| zero | nonzero | any | CRITICAL: our fills say we hold shares absent at broker |
| zero | zero | positive | CRITICAL: a live OMS book claims shares absent at broker |
| positive | nonzero | positive | CRITICAL if broker, fills, or books disagree in either direction |
| positive | zero | positive | CRITICAL: ownership sources conflict; fail closed |
| equal | equal | equal | No finding |

Configured order size is payload context only and never decides ownership. The proposed shortcut was
arithmetically invalid for the live examples: 500 and 1,000 are both multiples of Schwab's configured
quantity 2.

## Corrected Live Examples

XHLD 500 shares and TNON 1,000 shares on `live:schwab_1m_v2` were manual. Their all-time bot fill
balances were zero, and the bot has never submitted those quantities. XHLD's last bot activity was
2026-08-12; TNON's was 2026-09-10. Under this policy both are INFO and cannot page.

The earlier XHLD count of 210 orders was invalid because it omitted both account and date filters. It
included all accounts and paper history. It must not be reused as ownership evidence.

## Alerts Retained

The following remain phone-worthy on their existing proven routes: seed-exposure CANNOT_SEE,
seed-exposure exposed count above zero, the once-per-session pre-open readiness result, and v2 state
restoration incomplete. Seed exposure now records only its active transition, so a clean evaluation
silently clears the latch and a later recurrence can page. Pre-open readiness now records one
delivery per ET session and retries if delivery was not accepted.

The raw "new refusal class" monitor is retired from the design. It paged on correct refusal behavior.
The polarity-aware regression watch from PR #956 is its replacement. The repository's separate A7
refusal-provenance watcher is not that monitor and is not removed by this work.

## Deployment

This change is weekend work. It must not deploy during market hours. Deployment requires the normal
after-close safety checks, and the first live run must verify both a page-worthy transition and a
log-only condition rather than treating silence as proof.

All five scheduled wrappers run from `/home/trader/project-mai-tai/ops/health/` in root's crontab.
The pre-open and seed-exposure callers now use the versioned alert adapter from that same checkout,
not the older `/home/trader/preopen_alert.sh` copy. The reconciliation service must be restarted for
the fill-balance classifier; the cron wrappers require no service restart.
