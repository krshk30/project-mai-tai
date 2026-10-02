# Gateway owner recovery review evidence (2026-10-02)

Scope: code and tests only. No production install or restart.

## Behavior

- Every applied subscription event persists the complete in-memory consumer-owner map, `_migration_complete=1`, and its stream checkpoint before changing the live subscription union. A deleted Redis hash is therefore rebuilt by the next event rather than retaining only that event's consumer.
- When the migration marker is absent, restoration ignores any stale checkpoint and replays the full retained subscription stream. The first retained event for each consumer must be a `replace`; an initial `add` or `remove` is refused rather than guessed.
- A marked hash remains authoritative and only events newer than its checkpoint are replayed. The existing owner union and shared gateway subscription logic are otherwise unchanged.

## Verification

- `test_deleted_owner_hash_is_fully_rebuilt_by_one_event_before_restart` covers five consumers, hash deletion, one paper release event, truncated history, and restart. It checks the complete hash after every initial event and after the release.
- `test_unmarked_owner_hash_rebuilds_from_all_retained_replace_events` covers a partial hash with a stale checkpoint.
- `test_unmarked_owner_history_with_first_add_refuses_instead_of_guessing` covers the fail-closed case.
- Focused gateway, heartbeat reload, Momentum Option A, and paper-service suites: 81 passed. Ruff and `git diff --check` passed.

This does not prove recovery of a consumer whose complete history has already been trimmed and is absent from both the hash and the gateway's memory. That state needs external owner evidence and remains a deployment hard stop.
