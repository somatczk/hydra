# src/hydra/strategy

Strategy config, the rule DSL, the indicator context and the built-ins; also covers `src/hydra/indicators/`.
Repo-wide rules are in the root `AGENTS.md`.

## Commands

```bash
uv run pytest tests/unit/test_strategy tests/unit/test_indicators -q -m "not slow"
uv run mypy src/hydra/strategy/ src/hydra/indicators/ --strict   # CI checks both packages strict
uv run lint-imports                                              # strategy sits below risk/ml/execution
```

## Invariants

Test files without a `tests/` prefix are in `tests/unit/test_strategy/`.

- A loadable `strategy_class` is under `hydra.strategy.builtin.` only; the prefix is checked before any import
  (`engine.py` `ALLOWED_CLASS_PREFIX`, `test_engine.py`). Built-ins: breakout, composite, mean_reversion,
  ml_ensemble, momentum, rule_based, trend_following. Shipped configs and templates use `rule_based`
  (`test_templates_load.py::test_shipped_strategy_config_is_rule_based`).
- The rule DSL (`condition_schema.py`, `rule_engine.py`, `builtin/rule_based.py`) reads `parameters.rules`
  (`entry_long`, `exit_long`, `entry_short`, `exit_short`), AND/OR groups, comparators `less_than`, `greater_than`,
  `crosses_above`, `crosses_below`, `equals`; a value is a number or an indicator reference such as
  `"lag:period=60"`.
- Top-level `parameters` reach conditions only through bindings, resolved once by `rule_based.param_bindings`
  (`param_key`, `param_overrides` for any indicator param, `value_param_key`, `value_ref_overrides`, then the implicit
  `{indicator}_{param}` name-match); the strategy, `strategy_params` and the hyperopt/walk-forward name check all read
  it. An implicit name whose sites hold different values is ambiguous: never listed, and setting it fails the build;
  `{}` opts a condition out (`tests/unit/test_strategy/test_param_override.py`). The dashboard builder ports
  `param_bindings`; both are pinned to one shared fixture, so change them together
  (`tests/unit/test_strategy/test_param_bindings_parity.py`, `dashboard/src/components/builder/bindings.test.ts`).
  Saves and updates take optional top-level tunables (bound, numeric names only); an update without them keeps
  the stored values of names still bound.
- Rules use single-output indicators only. `bollinger_bands`, `keltner_channels` and `ichimoku` are refused
  (`context.py` `MULTI_OUTPUT`); parameters are range-checked against `PARAM_LIMITS` (period 2-200). Bad rules
  fail when the strategy is built, never per bar (`test_builtin_rule_based.py::test_invalid_rules_fail_at_construction`).
- Every public function in `indicators/library.py` is offered to the builder automatically
  (`indicator_registry.py`). Prefix helpers with `_`, list a multi-output function in `MULTI_OUTPUT`, add a new
  indicator to `_CATEGORY_MAP`, and keep it evaluable with its defaults
  (`test_indicator_registry.py::test_every_offered_indicator_evaluates_in_a_context`).
- Indicators fill warm-up with NaN and never look ahead; Donchian `rolling_*` cover the N bars before the current
  one; vectorized versions equal their loop originals bit for bit
  (`tests/unit/test_indicators/test_regime.py::test_no_lookahead`,
  `::test_donchian_on_highs_and_lows_excludes_the_current_bar`, `tests/unit/test_indicators/test_loop_equivalence.py`).
  TA-Lib is a hard import.
- `StrategyContext` caches indicator arrays per (symbol, timeframe, name, params) and drops them in `add_bar`; bars
  written any other way would serve stale indicators.
- Builder and MCP saves write `required_history` from `rule_engine.rules_required_history`: the longest indicator
  lookback over every condition and value reference (EMA windows x3, Wilder smoothers x6, ADX x8), at least 50. An
  update re-derives it and keeps a larger stored value; a YAML import keeps its own. Every shipped rule config
  declares at least its derived value (`tests/unit/test_strategy/test_required_history.py`,
  `tests/unit/test_strategy/test_shipped_configs.py`). A new indicator needs its lookback there.
- `required_history` is the bars a strategy needs before it signals; the session sizes the context from it and
  `StrategyEngine.warm_up` refuses more warm-up bars than the context holds. A hot reload may grow it only within
  the context (`tests/unit/test_execution/test_session_manager.py::test_reload_refuses_required_history_growth_beyond_the_context`).
  A change to `parameters.rules` needs a restart, like any non-parameter field.
- The optional `schedule` block (UTC `active_hours_utc`, `active_days` with Monday=0) gates ENTRY signals only, in
  `StrategyEngine` before publication, so paper, live and backtests skip the same entries; exits, protective stops,
  flatten, the kill switch and manual orders ignore it (`tests/unit/test_backtest/test_schedule.py`).
- The `risk:` block (`StrategyRiskConfig`) is the protective exit placed on every entry fill: default
  `fixed_pct` 5 %; a trailing stop cannot carry a take-profit. `exchange.leverage` is 1-3 and only for FUTURES.
- The strategy package never imports `hydra.ml`: the ML-native strategy gets its predictor through `ml_access.py`,
  attached by a higher layer at session start (`tests/unit/test_repo/test_p5c_seam_ownership.py`). `ml_overlay`
  config lives here; the veto runs in the session.

## Where things are

- `config.py`: `StrategyConfig` and its risk, sizing, exchange, schedule and `ml_overlay` blocks.
- `engine.py`: class loading, `validate_strategy_config`, `write_strategy_config`, `StrategyEngine`.
- `context.py`: `StrategyContext`, `resolve_indicator`, `MULTI_OUTPUT`, `PARAM_LIMITS`.
- `indicator_registry.py`: builder metadata; `builtin/`: the classes; `ml_access.py`: the prediction channel.
- `config/strategies/` and `config/templates/`: the shipped YAML.
