# src/hydra/dashboard

The FastAPI app (REST, WebSockets, probes, the MCP mount). The repo-wide rules in the root `AGENTS.md` apply;
this file covers the local mechanics.

## Commands
```bash
uv run pytest tests/unit/test_dashboard -q -m "not slow"   # from a worktree only (root rule)
uv run pytest tests/unit/test_dashboard/test_auth_coverage.py -q   # after any route, path or middleware change
uv run mypy src/hydra/dashboard/ --strict   # the package is on CI's strict line (no relaxation)
```

## Invariants
- Auth is one pure-ASGI middleware, `security.py`. Every HTTP and WebSocket path needs a principal except
  `PUBLIC_PATHS` (`/livez`, `/readyz`, `/metrics` and the two RFC 9728 metadata paths
  `/.well-known/oauth-protected-resource[/mcp]`) and the MCP mount (`MCP_PATH_PREFIX`, `/mcp` and `/mcp/...`)
  (`test_auth_coverage.py::test_public_paths_are_exactly_the_reviewed_list`, `::test_every_operation_needs_credentials`).
- Principals: `service` (bearer `HYDRA_API_TOKEN`, constant-time compare), `user` (`Remote-User` from a TCP peer in
  `api.trusted_proxy_cidrs`, proxy mode only), `dev` (anyone, `api.auth_mode: dev`, local compose only).
  `trusted_proxy_cidrs` holds Traefik's static /32 (or /128); the startup posture line warns on wider networks.
- Browser principals (`user`, `dev`) must send `X-Hydra-Request: 1`, an allowed `Origin` if any, and a same-origin
  `Sec-Fetch-Site` on non-GET requests, and an allowed `Origin` on WebSocket upgrades (`test_csrf.py`).
- Routes carry no auth code: read the caller with `current_principal(request)`, log security actions with
  `audit(principal, action, **fields)` on the `hydra.audit` logger (`test_security.py::test_audit_line_carries_principal_and_fields`).
- Errors use one envelope, `{"detail": {"code", "message", "state", ...}}`, built by `routes/_db.py`. A router that
  calls `hydra.services` sets `route_class=ServiceErrorRoute`, so a `ServiceError` answers as the same HTTP error;
  request-validation 422s use the envelope too (`install_error_handlers`).
- Config is loaded once by `lifespan.py` (`bind_config`); routes read it with `deps.get_config`, helpers without a
  request with `deps.current_config`.
- With `trader.url` set the lifespan binds `trader_client.RemoteTrader` and never builds a SessionManager; a
  transport error or timeout is 503 `engine_unreachable` (`test_trader_client.py`).
- MCP: one shared `MCPServer('hydra')` in `mcp/__init__.py`, mounted at `/mcp/` (plus its RFC 9728 metadata) only
  when MCP OAuth is configured (`mcp.oauth_issuer`, `mcp.oauth_audience`, `mcp.resource_url`, all or none; needs
  `mcp.allowed_hosts`). Authentik is the authorization server; `mcp/auth.py` verifies each access token with
  `mcp/tokens.py` (RS256 via the issuer's JWKS, exact `iss`, `aud`, `exp`, required scopes) and binds
  `Principal('service', 'mcp')`, never `user`, so an agent cannot approve its own gated actions; no CSRF, WebSocket
  upgrades closed (`test_mcp_oauth.py`, `test_mcp_transport.py::test_no_oauth_mounts_no_mcp_surface`,
  `::test_bearer_failures_are_401_envelopes_naming_the_metadata`). Tests sign tokens against
  `tests/fixtures/mcp_oauth.py`'s in-process issuer and pass its `transport` to `mount_mcp`.
- New MCP tools go in a module under `mcp/tools/` (`live`, `models`, `research`, `risk`, `sessions`,
  `strategies`; shared engine bindings in `_deps.py`), imported by `mcp/tools/__init__.py`. A gated tool creates an
  approval in the engine's `ApprovalStore` and answers `approval_required`; the action runs only through the
  executor registered on the store. A `ServiceError` becomes a tool error through `_deps.tool_error`.
- Probes (`health.py`, `routes/health.py`): `HealthMonitor` runs every 15 s and is the only writer of the readiness
  gauges. `/livez` needs startup done and a monitor run within 3 intervals; `/readyz` serves the last result while
  at most 2 intervals old. Engine checks: db, migrations, sessions (no zombie), feeds, risk, plus redis only while
  the opt-in event fan-out is on (`events.bus: redis`; the split API checks db, migrations, trader — its trader check
  embeds the trader's own redis result) (`test_health_routes.py`, `test_health_split.py`).
- Unit tests call the real app as the bearer-token service (autouse `default_api_auth` in
  `tests/unit/test_dashboard/conftest.py`); auth tests override that fixture to send exact headers.

## Where things are
- `api.py` app assembly and WebSocket routes (`/ws/market`, `/ws/trades`, `/ws/portfolio`, `/ws/signals`, `/ws/risk`);
  `ws_relay.py` polls `trades` every 2 s and ticks `/ws/trades` (the fill tick per new row, plus one
  `session_status` tick when a session leaves the running set), so both reach the browser in both process modes;
  `risk_relay.py` polls the risk tables every 2 s, ticks `/ws/risk` (a `risk_event` per new row, a diffed
  `risk_state` heartbeat) and hands every row to the Telegram sink — the lifespan starts it at startup, so the
  alerter pages with no browser connected.
- `routes/` one router per area; `routes/_db.py` envelope and pool helpers.
- `lifespan.py` startup and shutdown order (documented in its docstring).
