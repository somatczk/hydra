"""Typed session-path errors, importable without FastAPI.

Routes map each one to the ``_db.py`` error envelope; the ``code`` attribute is the envelope code.
"""

from __future__ import annotations


class KillSwitchActiveError(RuntimeError):
    """The kill switch is latched; no new session or opening order may start."""

    code = "kill_switch_active"


class DuplicateSessionError(ValueError):
    """The strategy already has a running session."""

    code = "session_already_running"


class UnsupportedVenueError(ValueError):
    """Only Binance SPOT is accepted until P4 (D10)."""

    code = "unsupported_venue"


class LiveTradingDisabledError(PermissionError):
    """Live trading is disabled until P3."""

    code = "live_trading_disabled"


class UnsupportedStrategyError(ValueError):
    """The strategy class cannot run in a session yet (DCA/Grid until P2)."""

    code = "strategy_unsupported"


class PaperCapitalUnavailableError(RuntimeError):
    """``system_config.paper_capital`` is missing or unreadable; there is no fallback."""

    code = "paper_capital_unavailable"


class SessionNotRunningError(ValueError):
    """A manual order targeted a session that has no running runtime."""

    code = "session_not_running"


class SpotShortError(ValueError):
    """A SELL on spot with nothing to sell would open a naked short."""

    code = "spot_short"
