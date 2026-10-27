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


class LiveNotArmedError(RuntimeError):
    """The ``live_armed`` system_config row is absent or off: no live session may start."""

    code = "live_not_armed"


class LiveCredentialsError(RuntimeError):
    """No stored, decryptable, environment-validated credentials for the live venue."""

    code = "live_credentials_missing"


class LiveCapitalUnavailableError(RuntimeError):
    """``system_config.live_capital`` is missing or unusable; there is no live allocation."""

    code = "live_capital_missing"


class UnsupportedStrategyError(ValueError):
    """The strategy class cannot be loaded for a session."""

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


class SessionPersistError(RuntimeError):
    """The session stopped, but its terminal status could not be written (retried in background)."""

    code = "session_stop_not_persisted"


class ConfigReloadRefusedError(ValueError):
    """A config change touches more than ``parameters``; the session must be restarted."""

    code = "restart_required"

    def __init__(self, changed_fields: list[str]) -> None:
        super().__init__(f"restart required to change: {', '.join(changed_fields)}")
        self.changed_fields = changed_fields


class SubmitUnknownError(RuntimeError):
    """A submit timed out or the network failed mid-call: the order may rest; never resubmit.

    The reconciler resolves the outcome by ``client_order_id`` (seam #8 carries it as a
    structured envelope field, not message text).
    """

    code = "order_submit_unknown"

    def __init__(self, client_order_id: str, message: str) -> None:
        super().__init__(f"{message} (client_order_id {client_order_id})")
        self.client_order_id = client_order_id


class ExchangeRateLimitedError(RuntimeError):
    """The exchange refused the call with a rate limit or temporary ban (418/429)."""

    code = "exchange_rate_limited"
