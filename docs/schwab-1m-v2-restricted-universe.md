# schwab_1m_v2 — why are API-open-restricted names in the universe? (TICKET — separate from the re-arm fix)

**Owner:** open · **Priority:** medium (independent of, and complementary to, the re-arm fix) · **Filed:**
2026-07-09

## The problem

From the DB (06-24..07-08): **55 ATR emits → 29 (53%) opened no position — 27 rejected, 2 cancelled** —
and they **cluster in names Schwab's API will not open a position in**:

| name | emits | filled |
|---|--:|--:|
| AZI | 5 | 0 (5 rejected) |
| CUPR / JEM / TDTH | 2 each | 0 |
| TC / DXF / EHGO / DGNX / DSY / UPC / BTCT / LGCL / IOTR / BYAH / NVVE | ≥1 | 0 |

Every one of these emits is **structurally un-fillable** on this account, yet the strategy still selects
the name, emits, and (pre-fix) **burns the short segment** — so the segment's real flip is missed. This
is the **dominant live miss source** (see `schwab-1m-v2-atr-flip-rearm-fix-design.md` §2.0).

## Why this is a SEPARATE lever from the re-arm fix

- The **re-arm fix** stops a burned segment from *blocking the next flip* — it makes the miss recoverable.
- **This ticket** asks the prior question: *why emit on a name that can never fill?* Removing
  restricted names at the source means we never burn the segment in the first place — cheaper than
  recovering from it, and it stops wasting emit/poll cycles and cluttering the DB with dead orders.

They compose: fix re-arm first (recoverable), then prune the universe (don't burn at all). Neither
substitutes for the other.

## Questions to answer before proposing a change

1. **Where does the universe come from?** The scanner promotes symbols; does anything downstream check
   Schwab open-ability before v2 acts? (Recall `#326` evicts foreign names *after* an API-open block — so
   the block is currently discovered by *failing*, not pre-filtered.)
2. **Is restriction knowable ahead of the emit?** Is there a Schwab endpoint / cached attribute
   (marginability, shortability, hard-to-borrow, foreign-ordinary flag) that predicts the reject, or is
   the reject only learnable by trying? If predictable → pre-filter at selection. If not → a
   learn-and-remember evict list (persist the reject, don't re-emit for N days).
3. **Blast radius.** Is this v2-only, or do ORB / polygon_30s select the same restricted names? A
   universe-level open-ability gate might belong upstream of all bots, not inside v2.
4. **False-positive risk.** Some names may be openable at some times (borrow availability changes).
   A permanent blacklist vs a decaying one — measure how often a once-rejected name later fills.

## Non-goals / guardrails

- Do **not** fold this into the re-arm PR (one change at a time; it would confound the D3/D5 re-run).
- Respects the OMS scoping invariant — this is entry-side *selection*, not position-touching.

## When to pick up

After the re-arm fix is deployed and D3/D5 re-run. The DB reject list (above) is the seed data; step 1 is
answering Q2 (predictable vs learn-by-failing), which decides pre-filter vs evict-and-remember.
