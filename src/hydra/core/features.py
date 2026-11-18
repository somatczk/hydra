"""The registry of non-bar series strategies can read (``ts.features``).

A rule names a feature as the indicator ``feature.<ref>``; the context aligns its stored rows to
each bar's close as of their ``available_at`` and reports NaN once the newest one is older than
``max_age``. Sources register their refs here; an unregistered ref is refused at strategy build.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

PREFIX = "feature."


@dataclass(frozen=True)
class FeatureDef:
    ref: str  # '<source>.<key>'
    scope: Literal["symbol", "market"]  # a market feature is stored with symbol ''
    max_age: timedelta  # older than this at a bar's close reads NaN


FEATURES: dict[str, FeatureDef] = {}
