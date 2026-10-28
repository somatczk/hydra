"""The trader process: the engine behind the internal bearer-gated API (D5).

``python -m hydra.trader`` runs the :class:`~hydra.execution.session_manager.SessionManager`
(the engine: risk state, recovery, feeds, sessions) and serves one endpoint per
:class:`~hydra.services.trader.TraderBackend` op for the dashboard API's
:class:`~hydra.dashboard.trader_client.RemoteTrader` — the two-process layout behind
``HYDRA_TRADER__URL``.
"""
