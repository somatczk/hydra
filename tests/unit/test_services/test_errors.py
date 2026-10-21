from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.backtest import router as backtest_router
from hydra.dashboard.routes.strategies import router as strategies_router


def test_service_error_answers_like_http_exception_without_app_handlers() -> None:
    app = FastAPI()
    app.include_router(backtest_router)
    app.include_router(strategies_router)
    client = TestClient(app)

    resp = client.get("/api/backtest/strategies/nope/params")
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": {
            "code": "strategy_not_found",
            "message": "Strategy nope not found",
            "state": "not_found",
        }
    }

    resp = client.put("/api/strategies/nope", json={"name": "x", "rules": {"entry_long": None}})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Strategy 'nope' not found"}

    resp = client.get("/api/backtest/results")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "database_unavailable"
