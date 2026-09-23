# RETRY-ONE causal backtest

Window: 2026-08-24 through 2026-09-23. Both live broker legs; no dollars.
Source commit: `4014fb8e79904608c8d76b7a8861e278d02b6863`.

## Population

- Managed rows: 431; fill-mapped legs: 403; unknown: 28.
- Observed logical trips: 275; causal trips including modeled tails: 274; modeled: 19.
- Observed later trips without stored fresh-cross proof: 20 (excluded).

## Results

| Max retries | Logical trips | Broker trades | Winners | Losers | Flats | Sum return % |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 85 | 127 | 70 | 57 | 0 | -4.8969 |
| 1 | 163 | 253 | 144 | 109 | 0 | -5.2979 |
| 2 | 218 | 339 | 186 | 152 | 1 | -83.4562 |

As traded: 403 broker trades, 222 winners, 179 losers, sum -49.6522%.

Comparison: max_retries=1 minus as-traded = 44.3543 points; max_retries=1 minus max_retries=0 = -0.4010 points.

## Broker split

- max=0 live:schwab_1m_v2: 52 trades, 28W/24L/0F, sum 2.0286%.
- max=0 live:orb: 75 trades, 42W/33L/0F, sum -6.9254%.
- max=1 live:schwab_1m_v2: 109 trades, 57W/52L/0F, sum -36.3939%.
- max=1 live:orb: 144 trades, 87W/57L/0F, sum 31.0960%.
- max=2 live:schwab_1m_v2: 150 trades, 78W/72L/0F, sum -71.1260%.
- max=2 live:orb: 189 trades, 108W/80L/1F, sum -12.3303%.

## Forgone winners at one retry

- 2026-08-24:BTCT:live:orb:10:02
- 2026-08-24:BTCT:live:schwab_1m_v2:10:02
- 2026-08-24:DAIC:live:schwab_1m_v2:11:54
- 2026-08-24:DAIC:live:schwab_1m_v2:12:56
- 2026-08-24:LUCY:live:schwab_1m_v2:15:52
- 2026-08-24:PMI:live:schwab_1m_v2:10:03
- 2026-08-24:PMI:live:schwab_1m_v2:12:15
- 2026-08-24:PMI:live:schwab_1m_v2:15:13
- 2026-08-24:XPON:live:schwab_1m_v2:11:05
- 2026-08-24:XPON:live:schwab_1m_v2:13:31
- 2026-08-24:XPON:live:schwab_1m_v2:14:38
- 2026-08-24:XPON:live:schwab_1m_v2:14:55
- 2026-08-25:AIXI:live:orb:14:24
- 2026-08-25:AIXI:live:schwab_1m_v2:14:24
- 2026-08-25:DAIC:live:schwab_1m_v2:14:02
- 2026-08-25:DAIC:live:schwab_1m_v2:14:05
- 2026-08-25:DAIC:live:schwab_1m_v2:15:44
- 2026-08-25:DAIC:live:schwab_1m_v2:15:48
- 2026-08-26:CRE:live:orb:12:49
- 2026-08-26:CRE:live:orb:15:24
- 2026-08-26:XPON:live:schwab_1m_v2:13:29
- 2026-08-26:XPON:live:schwab_1m_v2:14:47
- 2026-08-26:XPON:live:schwab_1m_v2:15:50
- 2026-08-26:XPON:live:schwab_1m_v2:15:56
- 2026-08-26:YYGH:live:orb:10:31
- 2026-08-26:YYGH:live:orb:11:30
- 2026-08-26:YYGH:live:orb:13:24
- 2026-08-26:YYGH:live:orb:15:25
- 2026-08-27:PPCB:live:orb:14:15
- 2026-08-31:AEHL:live:orb:14:14
- 2026-08-31:AEHL:live:orb:15:46
- 2026-08-31:NCRA:live:orb:15:10
- 2026-08-31:NCRA:live:orb:15:51
- 2026-09-01:BIAF:live:orb:12:39
- 2026-09-01:BIAF:live:schwab_1m_v2:12:39
- 2026-09-01:BIAF:live:schwab_1m_v2:14:30
- 2026-09-01:FLYE:live:schwab_1m_v2:13:48
- 2026-09-01:RDAC:live:orb:14:37
- 2026-09-01:RDAC:live:schwab_1m_v2:14:37
- 2026-09-01:SSM:live:orb:10:43
- 2026-09-01:SSM:live:orb:11:29
- 2026-09-01:SSM:live:orb:14:58
- 2026-09-02:PPBT:live:orb:13:13
- 2026-09-02:PPBT:live:orb:14:20
- 2026-09-02:VIOT:live:orb:14:45
- 2026-09-04:CDTG:live:orb:13:35
- 2026-09-04:IMRN:live:schwab_1m_v2:13:33
- 2026-09-08:BNC:live:schwab_1m_v2:12:09
- 2026-09-08:BNC:live:schwab_1m_v2:13:25
- 2026-09-09:SUNE:live:orb:14:23
- 2026-09-09:SUNE:live:schwab_1m_v2:14:23
- 2026-09-09:YMAT:live:orb:14:32
- 2026-09-15:MYSZ:live:orb:11:19
- 2026-09-15:VEEA:live:orb:14:19
- 2026-09-15:VEEA:live:orb:15:12
- 2026-09-15:VEEA:live:schwab_1m_v2:14:19
- 2026-09-15:VEEA:live:schwab_1m_v2:15:12
- 2026-09-16:MEDS:live:orb:14:52
- 2026-09-16:MEDS:live:schwab_1m_v2:14:52
- 2026-09-16:QCLS:live:orb:14:43
- 2026-09-16:QCLS:live:schwab_1m_v2:14:43
- 2026-09-16:ZTG:live:orb:15:03
- 2026-09-17:AEMD:live:orb:15:40
- 2026-09-17:AEMD:live:schwab_1m_v2:15:40
- 2026-09-17:DAIC:live:orb:14:56
- 2026-09-17:DAIC:live:schwab_1m_v2:14:56
- 2026-09-18:ZTG:live:orb:15:06
- 2026-09-21:GLND:live:orb:14:18
- 2026-09-21:GRML:live:schwab_1m_v2:14:47
- 2026-09-21:NCPL:live:orb:13:53
- 2026-09-21:VRME:live:orb:13:46
- 2026-09-21:VRME:live:schwab_1m_v2:13:46
- 2026-09-23:BENF:live:orb:15:27
- 2026-09-23:BENF:live:schwab_1m_v2:15:27

## Named replays

- VSA 2026-09-23: {"logical_trips": 3, "trips": [{"closed_at_et": "2026-09-23T15:30:35.251000-04:00", "fresh_cross_at_et": null, "legs": [{"account": "live:orb", "entry_at": "2026-09-23T15:29:25.265000-04:00", "entry_order_type": "STOP_LIMIT", "entry_price": "3.87000000", "exit_at": "2026-09-23T15:30:35.251000-04:00", "exit_price": "3.57010000", "exit_reason": "CW_HARD_STOP", "return_pct": "-7.7494", "source": "observed"}], "line": "3.8590", "opened_at_et": "2026-09-23T15:29:25.265000-04:00", "source": "observed"}, {"closed_at_et": "2026-09-23T15:53:00-04:00", "fresh_cross_at_et": "2026-09-23T15:50:00-04:00", "legs": [{"account": "live:schwab_1m_v2", "entry_at": "2026-09-23T15:50:00-04:00", "entry_order_type": "MODELED_STOP", "entry_price": "3.8590", "exit_at": "2026-09-23T15:53:00-04:00", "exit_price": "4.051950", "exit_reason": "TARGET", "return_pct": "5.0000", "source": "modeled"}, {"account": "live:orb", "entry_at": "2026-09-23T15:50:00-04:00", "entry_order_type": "MODELED_STOP", "entry_price": "3.8590", "exit_at": "2026-09-23T15:53:00-04:00", "exit_price": "4.051950", "exit_reason": "TARGET", "return_pct": "5.0000", "source": "modeled"}], "line": "3.8590", "opened_at_et": "2026-09-23T15:50:00-04:00", "source": "modeled"}, {"closed_at_et": "2026-09-23T16:00:00-04:00", "fresh_cross_at_et": "2026-09-23T15:55:00-04:00", "legs": [{"account": "live:schwab_1m_v2", "entry_at": "2026-09-23T15:55:00-04:00", "entry_order_type": "MODELED_STOP", "entry_price": "3.8590", "exit_at": "2026-09-23T16:00:00-04:00", "exit_price": "3.62000000", "exit_reason": "SESSION_END", "return_pct": "-6.1933", "source": "modeled"}, {"account": "live:orb", "entry_at": "2026-09-23T15:55:00-04:00", "entry_order_type": "MODELED_STOP", "entry_price": "3.8590", "exit_at": "2026-09-23T16:00:00-04:00", "exit_price": "3.62000000", "exit_reason": "SESSION_END", "return_pct": "-6.1933", "source": "modeled"}], "line": "3.8590", "opened_at_et": "2026-09-23T15:55:00-04:00", "source": "modeled"}]}
- DCOY 2026-09-22: {"logical_trips": 3, "trips": [{"closed_at_et": "2026-09-22T10:55:11.267000-04:00", "fresh_cross_at_et": null, "legs": [{"account": "live:orb", "entry_at": "2026-09-22T10:53:05.526000-04:00", "entry_order_type": "STOP_LIMIT", "entry_price": "5.79900000", "exit_at": "2026-09-22T10:55:11.267000-04:00", "exit_price": "5.60000000", "exit_reason": "CONFIRMATION_EXIT", "return_pct": "-3.4316", "source": "observed"}, {"account": "live:schwab_1m_v2", "entry_at": "2026-09-22T10:53:05-04:00", "entry_order_type": "STOP_LIMIT", "entry_price": "5.79000000", "exit_at": "2026-09-22T10:55:07-04:00", "exit_price": "5.61010000", "exit_reason": "CONFIRMATION_EXIT", "return_pct": "-3.1071", "source": "observed"}], "line": "5.7912", "opened_at_et": "2026-09-22T10:53:05-04:00", "source": "observed"}, {"closed_at_et": "2026-09-22T11:05:14.676000-04:00", "fresh_cross_at_et": "2026-09-22T11:03:27-04:00", "legs": [{"account": "live:orb", "entry_at": "2026-09-22T11:03:27.494000-04:00", "entry_order_type": "STOP_LIMIT", "entry_price": "5.79000000", "exit_at": "2026-09-22T11:05:14.676000-04:00", "exit_price": "5.60000000", "exit_reason": "CONFIRMATION_EXIT", "return_pct": "-3.2815", "source": "observed"}, {"account": "live:schwab_1m_v2", "entry_at": "2026-09-22T11:03:27-04:00", "entry_order_type": "STOP_LIMIT", "entry_price": "5.79000000", "exit_at": "2026-09-22T11:05:10-04:00", "exit_price": "5.68500000", "exit_reason": "CONFIRMATION_EXIT", "return_pct": "-1.8135", "source": "observed"}], "line": "5.7912", "opened_at_et": "2026-09-22T11:03:27-04:00", "source": "observed"}, {"closed_at_et": "2026-09-22T12:01:55.792000-04:00", "fresh_cross_at_et": "2026-09-22T11:18:00-04:00", "legs": [{"account": "live:orb", "entry_at": "2026-09-22T11:52:48.612000-04:00", "entry_order_type": "STOP_LIMIT", "entry_price": "5.33000000", "exit_at": "2026-09-22T12:01:55.792000-04:00", "exit_price": "4.90000000", "exit_reason": "OCO_RESOLVED_FLAT", "return_pct": "-8.0675", "source": "observed"}, {"account": "live:schwab_1m_v2", "entry_at": "2026-09-22T11:52:48-04:00", "entry_order_type": "STOP_LIMIT", "entry_price": "5.32500000", "exit_at": "2026-09-22T12:01:55-04:00", "exit_price": "4.90000000", "exit_reason": "OCO_RESOLVED_FLAT", "return_pct": "-7.9812", "source": "observed"}], "line": "5.3309", "opened_at_et": "2026-09-22T11:52:48-04:00", "source": "observed"}]}

## Known limits

- Observed legs use real fills; only missing tail retries are modeled.
- A fresh cross requires one completed stored bar below the line and a later bar high at or above it.
- Sparse Schwab bars can make a real fresh cross unmeasurable; those observed trips are excluded, never inferred.
- The cross bar proves entry only; outcome grading starts on the next bar because its intrabar order is unknown.
- Counterfactual target and stop touched in the same post-entry minute is scored STOP.
