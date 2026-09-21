# Deploy-gate bootstrap

The production checkout may predate `deploy_oms_strategy_authorized.sh`. Advancing that checkout
first would bypass the gate it is meant to enforce. The reviewed bootstrap therefore installs an
immutable tool release outside the production repository, verifies it, and runs the gate from
there while `/home/trader/project-mai-tai` remains on the old commit.

## First installation

Set `SHA` to the exact full commit named by the operator's deploy approval. These commands fetch
objects but do not move the production checkout:

```bash
SHA=<approved-40-character-sha>
PROD=/home/trader/project-mai-tai
STAGE=/home/trader/mai-tai-deploy-gate/stage/$SHA
TOOLS=/home/trader/mai-tai-deploy-gate/releases/$SHA

git -C "$PROD" fetch origin main
test "$(git -C "$PROD" rev-parse origin/main)" = "$SHA"
mkdir -p "$(dirname "$STAGE")"
git -C "$PROD" worktree add --detach "$STAGE" "$SHA"
"$STAGE/ops/systemd/install_deploy_gate_tools.sh" "$STAGE" "$SHA"
```

The installer requires a clean checkout at `SHA`, verifies the committed SHA-256 manifest, checks
that every shell entry point is committed with mode `100755`, copies the payload to `TOOLS`, and
verifies it again there. It never edits the production checkout.

After the operator's SHA-specific approval record exists, invoke the reviewed release exactly
once:

```bash
"$TOOLS/ops/systemd/deploy_oms_strategy_authorized.sh" \
  "$PROD" "$SHA" /home/trader/operator-go.txt "$TOOLS"
```

The fourth argument selects the external reviewed components. The gate verifies
`DEPLOY_GATE_SOURCE_SHA` and every payload checksum before any preflight or checkout movement.
Only the authorized `deploy_service.sh` call can fast-forward production to `SHA`.
