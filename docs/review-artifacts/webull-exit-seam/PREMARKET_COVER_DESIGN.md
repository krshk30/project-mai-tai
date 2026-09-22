# #8 — cover for a Webull share PRE-MARKET (design; the operator picks, then it is built)

**Written by `claude-1`, 2026-09-22 ~14:20 ET.** Board item #8 (operator 09-22: "start build 4 and 8"). #4 is PR'd
alongside this note; #8 needs a choice first, because *no bracket can rest on Webull pre-market* — so "cover" there
cannot mean what it means in RTH.

## The fact that shapes everything (broker-proven, do not re-test)

Webull validates a pre-market protective pair against the **prior close**, not the live tape. Every gapper's stop is
"below the market" by that reference and is refused `STOP_LOSS_PRICE_LT_MARKETPRICE`. The session enum is **not** the
lever (`ALL_DAY` was sent on XOS 08-18 and refused identically). 100% of attach refusals over 6 sessions were pre-market;
every RTH fill got its pair. ⇒ A pre-market Webull share is held with the **software ladder as its only cover**, always.

## What #4 already changes for pre-market (in the PR next to this note)

Before #4, a pre-market hard stop whose sell was refused ended in **nothing** — YMAT 09-09 08:41:53 ET: 3 refusals in
one second, then silence. With #4 the same stop runs on the shared path: refused sell ⇒ recovery ⇒ re-attach is
impossible pre-market ⇒ **page `CW_HARD_STOP exit protection FAILED: YMAT on live:orb; check now`** within ~1 s, and
the ladder retries every 10 s. The share is still uncovered, but it is no longer *silently* uncovered.

## The population (live:orb, 09-08 → 09-22, read from the box 14:15 ET)

7 of 104 Webull entries were pre-market. How they ended: YMAT 09-09 07:45 (**−8.5%**, the named miss), TNON +3.4, MYSZ −0.4,
DAIC +6.3, IMCC +3.5, TOPS 09-22 −2.1; YMAT's second entry 09:20 (+3.8, the only one still held across 09:30).
Six of seven were closed **before** 09:30. So any 09:30 mechanism reaches about **1 in 7** pre-market shares.

## The options

| | what | reaches | cost | status |
|---|---|---|---|---|
| **A** | Page on a FAILED pre-market software exit | every YMAT-shaped miss | 0 — it is in #4 | **built** |
| **B** | Pre-market "held with no broker stop" line at fill time (`WARNING`, not a page) | all 7 | tiny | proposed |
| **C** | RTH-edge bracket: attach a pair at 09:30 to a pre-market share still held (#647, `oms_v2_rth_edge_bracket_enabled`, **OFF on the box**) | ~1 in 7 | a flag — but the code is from 08-04 and has **never been exercised**; needs the deploy-and-watch discipline | built dark |
| **D** | Pre-market SINGLE-LEG stop sell (not a combo) | all 7, *if* Webull accepts it | one live 1-share probe pre-market (a preview 200 proves nothing here) | untested; the prior-close reference very likely refuses it too |
| E | Extend the uncovered-share page to pre-market as `cause=premarket_no_pair` | all 7 | small | **not recommended**: it would page a *critical* incident on every pre-market fill; the ladder is doing its job until a sell is refused, which A already pages |

## Recommendation

1. **A** is the fix for the named miss and ships with #4. Nothing more is needed to stop a YMAT repeat from being silent.
2. **B** as a one-line log at fill (`[WEBULL-BARE-FILL]` already says it — check the wording says "PRE-MARKET: no pair
   can rest; software ladder only until 09:30"), no new page.
3. **C** is the operator's call: it is a flag, it reaches 1 in 7, and it is unexercised code. If turned on, it is a
   watched deploy like any other, one flag per day, never the same day as LC1.
4. **D** only if the operator wants *true* pre-market broker cover: one deliberate 1-share probe, placed by hand,
   result recorded either way. Not a code change until the broker has answered.

## What this note does NOT claim

No option makes a pre-market Webull share "covered" the way an RTH share is. A is a page, C is 09:30 onward, D is a
question to the broker. The honest statement for the handoff stays: **pre-market Webull shares ride the software
ladder; a refused exit now pages instead of vanishing.**
