# Sole-writer linear NFQ onto SLOT rebase

Human correction received October7: no merge commits. SLOT PR1113 remains
9951b473f8203590f48c667770c07e1f9c3096b6. Rebase the original three NFQ marker
commits from f9c9bd332392e2c905fc39b954421c88970844d7 onto that SLOT head.
Original NFQ backup branch codex/nfq2-before-slot-rebase-20261007 points to
0318ef84dc11b1b052073092828b80ae23c1bf54; portable backup bundle is
/tmp/nfq2-original-0318-before-slot-rebase-20261007.bundle.

Own scratch branch codex/nfq2-on-slotclear1-rebase-20261007 in
/tmp/codex-nfq2-on-slotclear1-linear-20261007. Original local NFQ and SLOT
worktrees remain untouched. Internal merge proof branch stays as a backup,
not a publication candidate. Source/tests/ops MUST be byte-identical to tested
freeze5d77d48f213203d23ee2ef6d8ac354655ecc0e83 before publication. No runtime
changes are authorized by this history correction. One explicit
force-with-lease of PR1112 expects remote0318; PR1113 is not pushed.
