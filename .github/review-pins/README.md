# Repository-contained independent review pins

Independent review records live as immutable JSON files on the dedicated
`review-pins` branch. Keeping the ledger on a separate branch avoids an
impossible self-reference: putting a record keyed to a PR head in that same PR
would change the head SHA that the record claims to cover.

The required `independent-review-pin` check runs from trusted `main` via
`pull_request_target`. It fetches the PR only as Git data, reads the committed
ledger, and requires union coverage of every commit in the exact current PR
range. Every review range must have:

- a full base and head SHA;
- exactly one recognized agent marker on every reviewed commit;
- a reviewer different from every commit author in that range;
- an immutable ledger file whose introducing commit has the reviewer's agent
  marker; and
- a non-empty description of what was checked.

A later push leaves earlier pins in the ledger but fails coverage until the new
commits are independently reviewed and pinned. Narrow pins may be combined;
the union must cover the whole current PR range.

## Recording a review

Check out the target PR and the `review-pins` branch in separate worktrees.
Then use the trusted script from `main` to create the record:

```bash
python scripts/review_pin_gate.py record \
  --repo /path/to/target-pr \
  --ledger /path/to/review-pins-worktree \
  --pr 123 \
  --base <full-range-base-sha> \
  --head <full-reviewed-head-sha> \
  --reviewer claude-1 \
  --summary "read the resolution hunks and ran the ordered rehearsal"
```

Commit only the generated `records/**.json` file on `review-pins`, using the
reviewer's marker, and push it without rewriting ledger history. Because that
push correctly leaves the PR head unchanged, it does not emit a `synchronize`
event. Add the `review-pinned` label to trigger a fresh check (remove and add it
again for a later review round):

```bash
gh pr edit 123 --add-label review-pinned
```

The label is only a trigger. It is not read by the gate and grants no authority
without a valid committed record.

## Activation

After this gate itself is independently reviewed and merged:

1. create the `review-pins` branch with an empty `records/` directory marker;
2. prohibit force pushes and deletion on that branch;
3. create the `review-pinned` label used only to retrigger the check;
4. add `independent-review-pin` to `main`'s required status checks alongside
   `validate`; and
5. preserve the current `strict: true` setting.

Activation is an operator-visible repository-setting change. It must happen
after the workflow exists on trusted `main`, not from this PR branch.

## Limits

This is mechanical separation of authorship, not proof of review diligence.

- Branch protection currently has `enforce_admins: false`, so an operator with
  administrator authority can bypass required checks. The scheduled audit is
  the detection layer for that path.
- Claude and Codex use the same GitHub identity. The ledger commit trailer and
  target commit trailers let the check prove the declared agents differ, but
  cannot prove which human or process actually performed the review.
