# deploy

Prometheus alert rules with their promtool tests, the Grafana dashboard, and a local full-stack compose file.
Production runs from the romulus repo, not from here. The repo-wide rules in the root `AGENTS.md` apply.

## Commands
```bash
uv run pytest tests/unit/test_deploy -q                       # rules and panels against hydra.core.metrics
promtool check rules deploy/prometheus-rules.yml              # if promtool is installed locally
promtool test rules deploy/prometheus-rules.test.yml
```
CI runs the same two promtool commands inside `prom/prometheus:v3.15.0` (the image romulus pins), job
"promtool check + test" in `.github/workflows/ci.yml`. Do not run Docker locally for this unless the task owns infra.

## Invariants
- Every `hydra_*` series a rule or a panel reads is declared in `hydra.core.metrics.CONTRACT` and has a runtime
  producer; a new metric goes into the contract first and producers only call its setters
  (`tests/unit/test_deploy/test_prometheus_rules.py::test_every_hydra_series_in_rules_is_in_the_contract`,
  `tests/unit/test_deploy/test_grafana_dashboards.py::test_every_series_a_rule_or_panel_reads_has_a_runtime_producer`).
- Every alert has both a firing and a non-firing case in `prometheus-rules.test.yml`
  (`test_prometheus_rules.py::test_every_alert_has_a_firing_and_a_silent_promtool_case`).
- Thresholds are shared with the code, not copied by hand: feed staleness matches the readiness check, the ML drift
  alert matches `hydra.ml.retraining`, drawdown is a fraction (0.10, not 10)
  (`test_prometheus_rules.py::test_feed_thresholds_match_the_readiness_check`,
  `::test_ml_drift_alert_pins_retrainings_shared_constants`, `::test_drawdown_rule_threshold_matches_fraction_unit`).
- Alerts without an honest producer stay deleted (`::test_rules_without_an_honest_producer_are_gone`); panels follow
  the same rule (`test_grafana_dashboards.py::test_no_panel_without_an_honest_source`).
- Dashboard JSON keeps a stable format: no tabs, one trailing newline
  (`test_grafana_dashboards.py::test_dashboard_json_formatting_is_stable`). The same tests cover
  `config/grafana/hydra-overview.json` and `config/grafana/hydra-system.json`.
- `compose.yml` is a local approximation, not the deployment: it needs `HYDRA_CREDENTIAL_KEY` in the environment,
  runs the engine in `api.auth_mode: dev`, and the dashboard image's `/api` does not reach the engine.

## Where things are
- `prometheus-rules.yml` / `prometheus-rules.test.yml`: the rules and their promtool unit tests.
- `grafana-dashboard.json`: the main dashboard. The running Prometheus, Alertmanager and Grafana live in romulus.
