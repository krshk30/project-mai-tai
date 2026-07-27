# Attended check — fill-anchored Webull bracket (PR #562)

**Status: staged, flag OFF.** Code is live on the box (`53689a9`); the env line exists and parses as
`False`. Enabling is a one-word edit plus an OMS restart.

## What is being proven

The combo bracket is placed as ONE atomic order, so both exit legs are priced off the pre-trade
**reference** before the master has filled. The Webull leg enters at MARKET on the ATR cross —
exactly where slippage lives — so the realised bracket drifts off spec. The fix recomputes both legs
from the ACTUAL fill and `replace_order`s them.

⚠️ **The unproven part is the broker contract**: whether v3 `replace_order` accepts a *partial*
combo — the two exit legs, master omitted because it is already filled. That is the ONLY reason this
needs a human. Everything else is unit-tested.

## Baseline measured 2026-07-27 (flag OFF, 12 combos)

    ALIGNED  8    DRIFTED  4        (aligned = target +2.00% / stop -5.00% of the FILL, ±0.30%)

    LGHL 12:32  fill 1.200  target +1.67%  stop -5.83%   <- worst
    BIYA 14:00  fill 3.936  target +1.63%  stop -5.49%
    BIYA 12:51  fill 4.120  target +1.70%  stop -5.34%   <- realised -5.83% after stop slippage
    FIEE 13:00  fill 5.980  target +3.18%  stop -3.85%   <- drifted the other way: stop too TIGHT

**One third of trades were materially off-spec.** With the flag ON, every filled master should read
ALIGNED.

## Procedure

**1. Pre-open — turn it on**

```bash
sudo cp /etc/project-mai-tai/project-mai-tai.env \
        /etc/project-mai-tai/project-mai-tai.env.bak.pre-realign-on.$(date -u +%Y%m%dT%H%M%SZ)
sudo sed -i 's/^MAI_TAI_WEBULL_BRACKET_REALIGN_ON_FILL_ENABLED=.*/MAI_TAI_WEBULL_BRACKET_REALIGN_ON_FILL_ENABLED=true/' \
        /etc/project-mai-tai/project-mai-tai.env
sudo grep -n REALIGN_ON_FILL /etc/project-mai-tai/project-mai-tai.env | cat -A   # expect ...=true$
```

**2. Confirm it PARSES before restarting** (an env value was mangled once; always read back through
Settings, never trust the file alone):

```
.venv/bin/python -c "from project_mai_tai.settings import get_settings; \
  print(get_settings().webull_bracket_realign_on_fill_enabled)"      # must print True
```

**3. Restart the OMS with the fleet flat** (check, THEN restart — separate commands, so the check
can actually gate the restart):

```sql
SELECT count(*) FROM virtual_positions WHERE quantity <> 0;   -- expect 0
```
```bash
sudo systemctl restart project-mai-tai-oms.service
```

**4. Watch the FIRST fan-out fill.** Success looks like exactly one line per combo:

```
Webull bracket realigned to fill for BIYA: fill=4.120 vs ref=4.1050 (drift 0.37%)
  -> target 4.1871->4.20 stop 3.8998->3.91
```

```bash
sudo grep -E "bracket realign" /var/log/project-mai-tai/oms.log | tail
```

**5. Verify at the BROKER, not in our logs** — our own record is not proof the legs moved:

```
.venv/bin/python /home/trader/verify_realign.py
```
Every filled master from today should print `ALIGNED`.

## Reading the result

| what you see | meaning | action |
|---|---|---|
| `bracket realigned ...` + broker shows ALIGNED | **working** | leave on, keep watching |
| `bracket realign failed ... ORIGINAL bracket left in place` | broker rejected the partial combo | **the CONFIRM-AT-TEST case.** Position is still protected. Turn the flag off; the payload shape needs work (likely: include the filled MASTER leg, or replace legs individually) |
| no realign line at all on a filled combo | drift was under 0.10%, or the flag did not load | check step 2 |
| drift line present but broker still DRIFTED | replace accepted but did not apply | turn off, investigate |

## Rollback

```bash
sudo sed -i 's/^MAI_TAI_WEBULL_BRACKET_REALIGN_ON_FILL_ENABLED=.*/MAI_TAI_WEBULL_BRACKET_REALIGN_ON_FILL_ENABLED=false/' \
        /etc/project-mai-tai/project-mai-tai.env
sudo systemctl restart project-mai-tai-oms.service
```

A failed realign is **not** an emergency: the original bracket stays in place, so the position is
protected at the old (merely imperfect) prices. Protection is never traded for precision.

## Not covered by this check

The stop, once triggered, is a market-on-trigger order and still slips (BIYA: stop 3.900 → filled
3.880). Realigning fixes the *placement*, not the slippage. BIYA 12:51's −5.83% was roughly
two-thirds anchoring (−0.34%) and one-third slippage (−0.51%) — this fix removes the former only.
