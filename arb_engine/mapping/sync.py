"""One refresh cycle of the mapping pipeline.

discover → block → score → adjudicate → tier/store → retire stale. Schedulable
via the ``sync-mappings`` CLI (cron / trigger) or an internal loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..core.models import Exchange
from ..exchanges.base import ExchangeClient
from ..storage.db import Database
from .adjudicator import Adjudicator, RuleAdjudicator
from .blocking import generate_candidates
from .discovery import refresh_catalog
from .scoring import score_pair
from .store import MappingStore, TieringPolicy


@dataclass
class SyncReport:
    pm_markets: int = 0
    ka_markets: int = 0
    candidates: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    retired: int = 0

    def _bump(self, status: str) -> None:
        self.status_counts[status] = self.status_counts.get(status, 0) + 1

    def __str__(self) -> str:
        tallies = " ".join(f"{k}={v}" for k, v in sorted(self.status_counts.items()))
        return (
            f"catalog pm={self.pm_markets} ka={self.ka_markets} | "
            f"candidates={self.candidates} | {tallies or 'no matches'} | "
            f"retired={self.retired}"
        )


async def sync_once(
    db: Database,
    pm_client: ExchangeClient,
    ka_client: ExchangeClient,
    *,
    adjudicator: Optional[Adjudicator] = None,
    policy: Optional[TieringPolicy] = None,
    min_shared_keywords: int = 1,
    date_tolerance_hours: float = 48.0,
) -> SyncReport:
    """Run a full discovery + matching + lifecycle cycle."""
    adjudicator = adjudicator or RuleAdjudicator(max_close_delta_hours=date_tolerance_hours)
    store = MappingStore(db, policy=policy)
    report = SyncReport()

    catalog = await refresh_catalog(db, pm_client, ka_client)
    pm_all = catalog[Exchange.POLYMARKET]
    ka_all = catalog[Exchange.KALSHI]
    report.pm_markets = len(pm_all)
    report.ka_markets = len(ka_all)

    pm_open = [m for m in pm_all if m.is_open]
    ka_open = [m for m in ka_all if m.is_open]

    candidates = generate_candidates(
        pm_open,
        ka_open,
        min_shared_keywords=min_shared_keywords,
        date_tolerance_hours=date_tolerance_hours,
    )
    report.candidates = len(candidates)

    for pm, ka in candidates:
        features = score_pair(pm, ka, date_scale_hours=date_tolerance_hours)
        verdict = adjudicator.judge(pm, ka, features)
        _id, status = store.record(pm, ka, verdict, features)
        report._bump(status)

    # Retire mappings whose legs are no longer live in the catalog.
    live_pm = {m.venue_id for m in pm_open}
    live_ka = {m.venue_id for m in ka_open}
    report.retired = store.retire_stale(live_pm, live_ka)

    return report
