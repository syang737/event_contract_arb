"""Normalized catalog + adjudication models for the mapping pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..core.models import Exchange, utcnow

# Venue statuses that mean the market is still tradable / discoverable.
_OPEN_STATUSES = {"active", "open", "initialized", "unopened"}
# Venue statuses that mean the market is terminal and its mappings should retire.
_TERMINAL_STATUSES = {"closed", "settled", "finalized", "determined", "resolved", "delisted"}


@dataclass
class VenueMarket:
    """A single venue's market, normalized across Polymarket and Kalshi.

    ``strike``/``strike_cap`` capture bracket-market thresholds (e.g. Kalshi
    ``floor_strike``/``cap_strike``) so numeric equality can gate a match. ``raw``
    keeps the original payload for the adjudicator / auditing.
    """

    exchange: Exchange
    venue_id: str  # Polymarket conditionId or Kalshi ticker
    title: str
    description: str = ""
    category: Optional[str] = None
    close_time: Optional[datetime] = None
    status: str = "active"
    yes_token: Optional[str] = None  # Polymarket YES outcome token id
    no_token: Optional[str] = None
    strike_type: Optional[str] = None  # greater | less | between | binary | ...
    strike: Optional[float] = None
    strike_cap: Optional[float] = None  # upper bound for "between" brackets
    event_key: Optional[str] = None  # PM event slug / Kalshi event_ticker
    series_key: Optional[str] = None  # Kalshi series_ticker
    fetched_at: datetime = field(default_factory=utcnow)
    raw: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status.lower() in _OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in _TERMINAL_STATUSES


@dataclass
class MappingVerdict:
    """An adjudicator's decision about one candidate pair."""

    equivalent: bool
    pm_yes_equals_kalshi_yes: bool = True
    confidence: float = 0.0
    reason: str = ""
    method: str = "rule"
