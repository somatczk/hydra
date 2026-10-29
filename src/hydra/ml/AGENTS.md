# src/hydra/ml

Features, dataset, training, ONNX export/serving, the registry, drift, the session consumers.
Repo-wide rules are in the root `AGENTS.md`.

## Commands

```bash
uv run pytest tests/unit/test_ml tests/unit/test_backtest/test_worker_ml_train.py tests/unit/test_repo/test_p5c_seam_ownership.py -q -m "not slow"
uv run mypy src/hydra/ml/ --strict # CI checks this package strict
uv run lint-imports                # ml: beside risk, above strategy, below execution
uv run pytest tests/integration/test_ml_registry_pg.py tests/integration/test_ml_backtest_job_pg.py tests/integration/test_ml_session_start_pg.py -m integration  # HYDRA_DB_*
```

## Invariants

Test files without a `tests/` prefix are in `tests/unit/test_ml/`.

- One frozen `FeatureSpec` (`features.py`) pins the windows, the horizon, the feature names and a version that bumps
  whenever the feature set changes. `build_features` and `build_live_features` share one path, so the last training
  row equals the live vector (`test_features.py::test_last_training_row_equals_live_vector_exactly`).
- The grid is 1h; 4h and 1d features are timestamp-joined and visible only once their bar has closed, never
  count-aligned (`test_features.py::test_no_lookahead_boundary`).
- The dataset reads stored bars through an injected `MarketDataRepository` and reports provenance per timeframe,
  including the feed/auto source mix; same rows plus same spec give a byte-identical matrix
  (`test_dataset.py::test_same_rows_and_spec_hash_produce_byte_identical_matrix`).
- Training classifies the sign of the forward return and evaluates walk-forward with an embargo of one horizon;
  it reports OOS accuracy next to the base rate and the mean signed return, no Sharpe
  (`test_training.py::test_embargo_is_the_horizon_between_train_end_and_test_start`, `::test_no_fake_sharpe`).
- xgboost, onnxmltools (the `ml-training` extra) and onnxruntime are imported inside functions, never at module
  top, so `hydra.ml` imports without the extra.
- Exports embed the spec in the ONNX metadata; an export without a spec carries `feature_version=0` and serving
  refuses it, as it refuses a spec mismatch (`test_export.py::test_export_without_a_spec_is_marked_untrusted`,
  `test_consumers.py::test_a_spec_mismatch_refuses`).
- The registry (`registry.py`, table `ml_models`) holds immutable versions: register verifies the artifact's
  sha256, promote archives the previous Production, rollback re-promotes the last archived one; one version per
  `training_job_id`, so a re-run serves its registered version; concurrent registers of one name serialise on a
  per-name advisory lock around `MAX(version) + 1` (`tests/integration/test_ml_registry_pg.py`). The pool is always
  injected.
- Every consumer attaches through `serving.py` (registry name, Production version, artifact under `ml.models_dir`,
  onnxruntime) and fails closed and loud on any missing step
  (`test_consumers.py::test_a_model_that_cannot_attach_fails_closed_and_loud`).
- Sessions always attach Production. A research job (backtest, hyperopt, walk-forward) may pin `model_version_id`, any
  stage, bound in a context variable for that run only and recorded in its `provenance.ml_models`; never register a
  pin process-wide (`test_consumers.py::test_a_pinned_staging_version_serves_the_run_and_never_a_session`).
- The overlay only vetoes: an entry passes at or above `confidence_threshold`, `shadow` records the would-veto. The
  session touches `hydra.ml` only inside `execution/session_wiring.py` `_on_signal`, and the strategy layer never
  imports it (`tests/unit/test_repo/test_p5c_seam_ownership.py`).
- Drift PSI bins are the reference deciles stored on the registry row; the NaN rate is a separate score.
  `PSI_ALERT_THRESHOLD` 0.2 and `PSI_RETRAIN_THRESHOLD` 0.25 are the shared bands the trader's startup scan reads
  (`test_drift.py::test_thresholds_stay_shared_constants`); `HydraMLDriftHigh` fires above 0.2.
- Training runs as the bounded `ml_train` job in the backtest worker: 1h grid only, windows and horizon capped by
  `MAX_TRAIN_*` in `backtest/jobs.py` (`tests/unit/test_backtest/test_worker_ml_train.py`).

## Where things are

- `consumers.py`: the overlay and `validate_session_ml`, the session-start gate. `retraining.py`: PSI scores;
  `trader/main.py` `measure_startup_drift` runs the scan.
- `docker/ml/` and `docker-compose.ml.yml` do not work: `hydra.ml.training` has no `__main__` entry point.
