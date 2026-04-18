# TOS Schwab Validation - 2026-04-18

This note narrows the TOS validation to what is provable from source code versus what depends on VPS environment settings.

## Code-Proven Behavior

Current local code shows that `tos` is not using the generic shared MACD entry path. It has a dedicated ThinkScript-style entry mode:

- `entry_logic_mode="tos_script"` is set in
  [trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py:230)
- Entry dispatch routes TOS through `_check_tos_script_paths(...)` in
  [entry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/entry.py:2104)

That dedicated TOS handler enforces only the two intended entry paths:

- `P1_MACD_CROSS` requires `macd_cross_above`, `macd_increasing`, `volume > vol_min`, and VWAP filter pass
  [entry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/entry.py:2133)
  [entry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/entry.py:2139)
- `P2_VWAP_BREAKOUT` requires VWAP cross above, `macd_above_signal`, `macd_increasing`, and `volume > vol_min`
  [entry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/entry.py:2149)

This means the earlier `P3_MACD_SURGE` concern should be removed for current local code. The generic shared engine still has P3, but `tos` does not use that branch when `entry_logic_mode="tos_script"` is active.

## Intended TOS Config Shape

Current local `make_tos_variant(...)` sets TOS to a ThinkScript-style 1-minute entry profile:

- `entry_logic_mode = "tos_script"`
- `require_vwap_filter = True`
- `allow_vwap_cross_entry = True`
- `vol_min = 5000`
- `cooldown_bars = 5`
- `entry_vwap_mode = "session_aware"`
- `dead_zone_start = "00:00"` and `dead_zone_end = "00:00"` intentionally disable the ThinkScript dead zone
- `stoch_entry_cap = 101.0` effectively disables Stoch blocking for TOS entries

Reference:
[trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py:209)
[trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py:215)
[trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py:227)
[trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py:230)
[trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py:231)
[trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py:233)

## Routing And Quantity Support In Code

Current local code also supports:

- per-strategy provider override for `tos`
  [settings.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py:268)
  [settings.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py:271)
- account display rewriting from `paper:*` to `live:*` when provider is Schwab
  [settings.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py:292)
- TOS runtime registration using `provider_for_strategy("tos")`
  [runtime_registry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/runtime_registry.py:128)
  [runtime_registry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/runtime_registry.py:136)
- strategy-engine passing `quantity=self.settings.strategy_tos_default_quantity`
  [strategy_engine_app.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py:1373)
- gateway symbol lane and Schwab stream lane split
  [strategy_engine_app.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py:1867)
  [strategy_engine_app.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py:1916)

## Environment-Driven Live Values

These live values are supported by code, but they are not source defaults:

- `tos provider = schwab`
- `tos execution mode = live`
- `tos default quantity = 10`
- `tos account display name = live:tos_runner_shared`

Why this matters:

- source defaults still show `strategy_tos_default_quantity = 100`
  [settings.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py:134)
- source defaults still show `strategy_tos_account_name = "paper:tos_runner_shared"`
  [settings.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py:135)

So the correct claim is:

- source code proves that TOS can be routed to Schwab and can take a quantity override from settings
- the actual live Schwab + quantity `10` behavior depends on VPS environment configuration

## Focused Test Coverage

Relevant tests already in the tree:

- Schwab-backed TOS leaves the gateway symbol lane and joins the Schwab stream lane
  [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py:2203)
- TOS uses configured default quantity
  [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py:2219)
- Gateway quote leakage is blocked for Schwab-backed TOS
  [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py:2253)
- Snapshot batch quote leakage is blocked for Schwab-backed TOS
  [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py:2329)
- Runtime registry can route only TOS to Schwab
  [test_oms_risk_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_oms_risk_service.py:379)

## Final Assessment

The corrected TOS assessment is:

- current local/VPS TOS entry logic looks correct for the intended ThinkScript-style 1-minute bot
- the earlier `P3_MACD_SURGE` leak concern should be removed
- the remaining caveat is visibility, not logic
- public GitHub `main` will not prove the live Schwab/qty/account values until the relevant branch is pushed or the environment settings are separately documented
