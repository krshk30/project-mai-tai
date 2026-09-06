# PR #905 pin stands, but it repeats a false premise of mine: the +5% / −8% settings change

**Recorded 2026-09-06 by claude-1. Append-only, per the `pr-898-two-child-bound.md` and
`pr-902-uncovered-current-count.md` precedents.**

My pin of **PR #905 @ `6606c7c9`** closed with this observation:

> the CONF3 row previously said it **BLOCKS the operator's +5%/-8% settings change**, and that
> clause is now removed. Deploying the fan-out addresses the MECHANISM behind the dispersion, but
> with the live close UNEXERCISED the dispersion is not yet PROVEN closed.

**The premise is false.** `+5%` target / `−8%` stop — together with reclaim off and one trade per
segment — are **PAPER BOT settings**. They are **not** a pending change to live `schwab_1m_v2`,
which remains on its current settings. Corrected by the operator 2026-09-06.

⇒ **Nothing was ever gated on CONF3.** The words "it blocks the operator's settings change" describe
a dependency that did not exist. I introduced that framing and carried it through the CONF3 build
brief, the relay blocks to `codex-2`, the CONF3 board row, and this pin summary without ever
checking which bot those numbers belonged to.

**What is unaffected — the case for the fix does not rest on it.** CONF3's justification is the
measured live evidence at **current** settings:

- **3 of 3** confirmation fires orphaned the `live:orb` fan-out leg;
- median **44m06s** left open after the Schwab counterpart closed;
- leg-vs-leg dispersion **+2.17 / +2.98 / −4.21 pp** on one decision.

That dispersion is unmanaged, unbounded, and invisible to a paper harness hardcoded to
`live:schwab_1m_v2`. It stands on its own and needs no future settings change to matter.

**What else stands:** everything the pin actually verified — the three-way SHA read from the box,
the restart scope, the ATR probe var proven present in `/proc/3135615/environ`, migrations off, zero
tracebacks, and UNEXERCISED preserved in all three places. **The #905 pin itself is unaffected**;
only this one observation carried a false premise.

⇒ **The lesson, for me:** I inherited a pair of numbers from a paper-exit discussion and attached
them to the live bot without asking which bot they configured — then repeated them until they read
as established. A number that arrives as motivation gets the same scrutiny as one that arrives as
support.
