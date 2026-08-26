# V2 extended-hours protection capability matrix

**Status:** design and evidence boundary only. No entry repricing, venue call, flag change, or live
behavior is authorized by this document.

## Why this is separate from entry repricing

`per-broker-eligibility-webull-fallback-design.md` already locks the broker-independent fan-out and
the extended-hours entry shape: both venue legs are wanted; an ineligible broker loses only its own
leg; and a market-like EH entry is represented as a capped, marketable LIMIT because native stops do
not trigger there. That answers the entry question.

Protection has the opposite objective. Entry repricing must avoid chasing a thin ask. Once shares
are held and the protective condition fires, the objective is to reduce risk without pretending an
unfillable order is protection. The entry cap is therefore not reusable policy. Sharing formatting
and tick-size helpers is safe; sharing the entry decision rule is not.

## Evidence labels

- **LIVE:** accepted/fired on a real account and durable evidence exists.
- **BROKER-REFUSED:** a real place or targeted broker probe produced a terminal refusal.
- **CODE-ROUTED:** current code deliberately transforms or declines the shape; this is not proof the
  untransformed broker capability is impossible.
- **UNEXERCISED:** no accepted and no refused real control establishes the capability.

## Capability matrix -- order type x side x session

| side / intent | session | order shape | Webull | Schwab | current v2 consequence |
|---|---|---|---|---|---|
| BUY entry | RTH | MARKET | **LIVE** | **LIVE** | Reactive entries may submit immediately. |
| BUY entry | RTH | LIMIT | **LIVE** | **LIVE** | Current price-committed cross path. |
| BUY entry | RTH | STOP / STOP_LIMIT | STOP master is broker-limited; bare `STOP_LIMIT` mirror is **LIVE** after #735/#799. | **LIVE** resting `STOP_LIMIT`. | Broker-specific entry shapes are intentional; do not force symmetry. |
| BUY entry | EH | MARKET | **CODE-ROUTED**, not sent directly. | **CODE-ROUTED**, not sent directly. | OMS produces a fresh-ask, marketable LIMIT; the no-chase entry cap remains entry-only. |
| BUY entry | EH | LIMIT | **LIVE** single-leg with EH metadata. | **LIVE** AM/PM LIMIT. | Supported entry transport. |
| BUY entry | EH | STOP / STOP_LIMIT | **CODE-ROUTED** to software cross; adapter documents stop family as RTH-only. | **BROKER-REFUSED** outside `NORMAL`. | Software observes the cross and emits a LIMIT; no native EH trigger is assumed. |
| SELL exit | RTH | MARKET | **LIVE**, subject to position/reservation truth. | **LIVE**. | Full-close route. A refusal is an outcome, never a confirmed close. |
| SELL exit | RTH | LIMIT | **LIVE**. | **LIVE**. | Scale/profit route. |
| SELL protection | RTH | STOP / OCO | Native entry OCO and later Webull exit children have **LIVE** fills; standalone single-leg STOP remains a different capability. | Native bracket path is **LIVE**; exit-only attach shape is separately guarded. | Native broker protection may stand the software ladder down only when armed evidence exists. |
| SELL exit | EH | MARKET | **CODE-ROUTED**, not sent directly. | **CODE-ROUTED**, not sent directly. | OMS converts the protective decision to a marketable SELL LIMIT from a fresh bid. |
| SELL exit | EH | LIMIT | **LIVE** software ladder transport. | **LIVE** AM/PM LIMIT transport. | This is the only currently exercised cross-broker EH exit shape. |
| SELL protection | EH | STOP / OCO | Combo attach with `ALL_DAY` is **BROKER-REFUSED** on a real held position. Standalone single-leg STOP is **UNEXERCISED**, closed by operator decision rather than disproven. | STOP leg outside `NORMAL` is **BROKER-REFUSED** by targeted probes. | No native EH stop is credited on either venue. The in-process software ladder is the protection. |

The Webull distinction in the last row is load-bearing. “The combo was refused” does not prove a
standalone stop is impossible; `webull-premarket-protection-decision.md` records that the single-leg
probe was never run. It remains `UNEXERCISED` unless the operator explicitly reopens an attended
probe. This design does not reopen it.

## Protective repricing contract

When the v2 exit engine fires outside RTH:

1. Read a fresh bid for the exact broker account/symbol decision. No fresh quote is
   `could_not_tell`; it is not permission to synthesize a price.
2. A profit-taking scale uses the bid as its LIMIT. A hard-stop/full-close uses a configurable
   protective buffer below the bid so the LIMIT is marketable, then snaps in the safe direction to
   the venue tick.
3. The buffer is an **exit execution** parameter. It is not the entry max-cross cap, does not move the
   strategy's trigger, and does not wait for a more attractive price.
4. The durable outcome loop reads accepted / working / partial / filled / rejected. `accepted` is not
   filled, and a rejected protective order keeps the managed row owned and loud.
5. Retry is bounded by attempt identity and the latest broker outcome. A later quote can reprice a
   still-working protective LIMIT only after the prior attempt is terminal or a broker-supported
   replace is confirmed. No parallel unpaired sells.

This largely describes current OMS intent routing; the missing work is a single report that proves
which matrix cell each protective attempt used and whether it reached a terminal outcome. The
matrix is the specification for that report, not authorization to broaden the entry re-pricer.

## Known causes this addresses

- An engineer reusing the EH entry cap for an exit and silently turning protection into “do not
  chase,” even though a held position has a different objective.
- Treating native STOP/OCO support as session-independent.
- Treating Webull combo refusal as proof about its untested single-leg endpoint.
- Calling an accepted EH LIMIT “protected” without reading its later outcome.

## It does not address

- The C3 post-exit stale-held refusal loop or the C2 resting-cancel outcome consumer.
- Whether Webull exposes complete/read-only order history or combo children.
- Venue-specific price improvement, queue priority, or the correct operator-selected exit buffer.
- Entry selection, entry caps, or reading-A slot accounting.

## What it cannot know

Static source and historical rows cannot prove an unexercised venue capability. They also cannot
prove that an accepted LIMIT stayed marketable until fill, or distinguish a missing broker-history
row from a genuinely absent order. Those remain `UNEXERCISED` / `could_not_tell` until a broker reply
and terminal evidence establish them.

## What would falsify it

- A real Webull EH combo protective pair is accepted and later reaches a terminal child outcome.
- A real Schwab STOP/OCO is accepted with an AM/PM session.
- A direct EH MARKET order is sent and accepted on either venue rather than being transformed.
- An EH protective LIMIT is reported successful without a terminal filled quantity.
- The implementation imports the entry max-cross cap into protective exit pricing.

## First increment

Add no behavior. Emit/read one capability-census row per protective attempt:

```text
broker=<account> session=RTH|AM|PM side=sell intent=scale|close
requested_type=<type> wire_type=<type> quote_age_ms=<n>
evaluated=<n> accepted=<n> filled=<n> rejected=<n> could_not_tell=<n>
```

It proves alone that every attempted protection is assigned to a named matrix cell and that both the
success and refusal polarities are observable. It does not prove the chosen order shape is optimal.
