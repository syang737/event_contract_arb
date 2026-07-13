"""One refresh cycle of the mapping pipeline.

discover → block → score → adjudicate → tier/store → retire stale. Schedulable
via the ``sync-mappings`` CLI (cron / trigger) or an internal loop.

Emits diagnostics through the ``arb_engine.mapping.sync`` logger:
* INFO  — catalog/filter/candidate counts, verdict tallies, rejection-reason
  breakdown, and the closest rejected near-misses (to help calibrate thresholds).
* DEBUG — every candidate's titles, composite score, and verdict reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..core.models import Exchange
from ..exchanges.base import ExchangeClient
from ..storage.db import Database
from .adjudicator import Adjudicator, RuleAdjudicator
from .blocking import generate_candidates
from .discovery import refresh_catalog
from .models import VenueMarket
from .scoring import score_pair
from .store import MappingStore, TieringPolicy

log = logging.getLogger("arb_engine.mapping.sync")


@dataclass
class SyncReport:
    pm_markets: int = 0
    ka_markets: int = 0
    pm_tradable: int = 0
    ka_tradable: int = 0
    candidates: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    reject_reasons: dict[str, int] = field(default_factory=dict)
    # (confidence, reason, pm_title, ka_title) for the closest rejected pairs.
    near_misses: list[tuple[float, str, str, str]] = field(default_factory=list)
    retired: int = 0

    def _bump_status(self, status: str) -> None:
        self.status_counts[status] = self.status_counts.get(status, 0) + 1

    def _bump_reason(self, code: str) -> None:
        self.reject_reasons[code] = self.reject_reasons.get(code, 0) + 1

    def __str__(self) -> str:
        tallies = " ".join(f"{k}={v}" for k, v in sorted(self.status_counts.items()))
        reasons = " ".join(f"{k}={v}" for k, v in sorted(self.reject_reasons.items()))
        out = (
            f"catalog pm={self.pm_markets} ka={self.ka_markets} | "
            f"tradable pm={self.pm_tradable} ka={self.ka_tradable} | "
            f"candidates={self.candidates} | {tallies or 'no matches'} | "
            f"retired={self.retired}"
        )
        if reasons:
            out += f"\n  reject reasons: {reasons}"
        return out


def _tradable(markets: list[VenueMarket], now: datetime, exclude_expired: bool) -> list[VenueMarket]:
    if exclude_expired:
        return [m for m in markets if m.is_tradable(now)]
    return [m for m in markets if m.is_open]


async def sync_once(
    db: Database,
    pm_client: ExchangeClient,
    ka_client: ExchangeClient,
    *,
    adjudicator: Optional[Adjudicator] = None,
    policy: Optional[TieringPolicy] = None,
    min_shared_keywords: int = 1,
    date_tolerance_hours: float = 48.0,
    exclude_expired: bool = True,
    now: Optional[datetime] = None,
    near_miss_limit: int = 10,
) -> SyncReport:
    """Run a full discovery + matching + lifecycle cycle."""
    from ..core.models import utcnow

    now = now or utcnow()
    adjudicator = adjudicator or RuleAdjudicator(max_close_delta_hours=date_tolerance_hours)
    store = MappingStore(db, policy=policy)
    report = SyncReport()

    catalog = await refresh_catalog(db, pm_client, ka_client)
    pm_all = catalog[Exchange.POLYMARKET]
    ka_all = catalog[Exchange.KALSHI]
    report.pm_markets = len(pm_all)
    report.ka_markets = len(ka_all)

    pm_open = _tradable(pm_all, now, exclude_expired)
    ka_open = _tradable(ka_all, now, exclude_expired)
    report.pm_tradable = len(pm_open)
    report.ka_tradable = len(ka_open)
    log.info(
        "discovered catalog pm=%d ka=%d; tradable pm=%d ka=%d (exclude_expired=%s)",
        report.pm_markets, report.ka_markets, report.pm_tradable, report.ka_tradable,
        exclude_expired,
    )

    candidates = generate_candidates(
        pm_open,
        ka_open,
        min_shared_keywords=min_shared_keywords,
        date_tolerance_hours=date_tolerance_hours,
    )
    report.candidates = len(candidates)
    log.info("generated %d candidate pairs (min_shared_keywords=%d)", len(candidates), min_shared_keywords)

    # Track the closest rejected pairs so a too-strict threshold is obvious.
    rejected_scored: list[tuple[float, str, str, str]] = []

    for pm, ka in candidates:
        features = score_pair(pm, ka, date_scale_hours=date_tolerance_hours)
        verdict = adjudicator.judge(pm, ka, features)
        _id, status = store.record(pm, ka, verdict, features)
        report._bump_status(status)

        log.debug(
            "candidate %s <-> %s: composite=%.3f verdict=%s (%s)",
            pm.venue_id, ka.venue_id, features.composite, status, verdict.reason,
        )
        if not verdict.equivalent:
            report._bump_reason(verdict.reason_code or "unknown")
            rejected_scored.append((features.composite, verdict.reason, pm.title, ka.title))

    # Surface the highest-scoring rejects — these are the calibration frontier.
    rejected_scored.sort(key=lambda t: t[0], reverse=True)
    report.near_misses = rejected_scored[:near_miss_limit]
    if report.reject_reasons:
        log.info(
            "verdicts: %s | reject reasons: %s",
            " ".join(f"{k}={v}" for k, v in sorted(report.status_counts.items())),
            " ".join(f"{k}={v}" for k, v in sorted(report.reject_reasons.items())),
        )
    if report.near_misses:
        log.info("closest rejected near-misses (composite | reason):")
        for score, reason, pm_title, ka_title in report.near_misses:
            log.info("  %.3f  PM %.60r  <->  KA %.60r  [%s]", score, pm_title, ka_title, reason)

    # Retire mappings whose legs are no longer live in the catalog.
    live_pm = {m.venue_id for m in pm_open}
    live_ka = {m.venue_id for m in ka_open}
    report.retired = store.retire_stale(live_pm, live_ka)
    log.info("retired %d stale mapping(s)", report.retired)

    return report
