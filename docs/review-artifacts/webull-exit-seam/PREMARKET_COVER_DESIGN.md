# #8 — cover for a Webull share PRE-MARKET (design; the operator picks, then it is built)

**Written by `claude-1`, 2026-09-22 ~14:20 ET; corrected 14:45 ET after `codex-2`'s review (B was already built; no delivery-latency promise). Operator 14:40 ET: "I would recommend A and C".** Board item #8 (operator 09-22: "start build 4 and 8"). #4 is PR'd
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
impossible pre-market ⇒ **page `CW_HARD_STOP exit protection FAILED: YMAT on live:orb; check now`** — an asynchronous
incident write, delivered by the INC1 pager on its next minute — and the ladder retries every 10 s, never while a
recovery is still running. The share is still uncovered, but it is no longer *silently* uncovered.

## The population (live:orb, 09-08 → 09-22, read from the box 14:15 ET)

7 of 104 Webull entries were pre-market. How they ended: YMAT 09-09 07:45 (**−8.5%**, the named miss), TNON +3.4, MYSZ −0.4,
DAIC +6.3, IMCC +3.5, TOPS 09-22 −2.1; YMAT's second entry 09:20 (+3.8, the only one still held across 09:30).
Six of seven were closed **before** 09:30. So any 09:30 mechanism reaches about **1 in 7** pre-market shares.

## The options

| | what | reaches | cost | status |
|---|---|---|---|---|
| **A** | Page on a FAILED pre-market software exit | every YMAT-shaped miss | 0 — it is in #4 | **built** |
| **B** | Pre-market "held with no broker stop" line at fill time (`WARNING`, not a page) | all 7 | 0 | **built** — `[WEBULL-PREMARKET-UNPROTECTED]` fires at fill and names the software ladder as the exit owner (codex-2 correction: I had called this proposed and cited `[WEBULL-BARE-FILL]`) |
| **C** | RTH-edge bracket: attach a pair at 09:30 to a pre-market share still held (#647, `oms_v2_rth_edge_bracket_enabled`, **OFF on the box**) | ~1 in 7 | a flag — but the code is from 08-04 and has **never been exercised**; needs the deploy-and-watch discipline | built dark |
| **D** | Pre-market SINGLE-LEG stop sell (not a combo) | all 7, *if* Webull accepts it | one live 1-share probe pre-market (a preview 200 proves nothing here) | untested; the prior-close reference very likely refuses it too |
| E | Extend the uncovered-share page to pre-market as `cause=premarket_no_pair` | all 7 | small | **not recommended**: it would page a *critical* incident on every pre-market fill; the ladder is doing its job until a sell is refused, which A already pages |

## Operator's pick (14:40 ET): A + C

A ships in #1032. **C = turn on `oms_v2_rth_edge_bracket_enabled`** (#647, built 08-04, never exercised): at 09:30 ET a pre-market
Webull share still held gets a pair attached. It is a flag, so it is an OMS restart on a flat book, a watched deploy, on its
own day — **not the same day as LC1** (one flag per day, so a bad day has one suspect). First read: the first pre-market
share held across 09:30 must show `[WEBULL-PROTECT-ATTACHED]` at 09:30:xx with `session=RTH`; population ≈ 1 in 7 pre-market
entries, so it may take a week to be exercised — UNEXERCISED until then, not proven.

## Recommendation (as written before the pick)

1. **A** is the fix for the named miss and ships with #4. Nothing more is needed to stop a YMAT repeat from being silent.
2. **B** already exists (`[WEBULL-PREMARKET-UNPROTECTED]`); nothing to build.
3. **C** is the operator's call: it is a flag, it reaches 1 in 7, and it is unexercised code. If turned on, it is a
   watched deploy like any other, one flag per day, never the same day as LC1.
4. **D** only if the operator wants *true* pre-market broker cover: one deliberate 1-share probe, placed by hand,
   result recorded either way. Not a code change until the broker has answered.

## What this note does NOT claim

No option makes a pre-market Webull share "covered" the way an RTH share is. A is a page, C is 09:30 onward, D is a
question to the broker. The honest statement for the handoff stays: **pre-market Webull shares ride the software
ladder; a refused exit now pages instead of vanishing.**
