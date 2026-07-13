"""Persistence + lifecycle transitions for discovered mappings.

Wraps :class:`Database` with the *policy* for turning an adjudicator verdict into
a stored status, and for retiring mappings whose legs have gone away. Keeps the
matching package free of raw ORM juggling.

Status model: ``proposed`` (awaiting review) → ``active`` (drives trading) /
``rejected``; and ``retired`` when a leg settles/delists. Human decisions
(``active``/``rejected`` set via the review CLI) are never silently downgraded by
a later sync — only re-validated, and retired if they stop being equivalent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from ..storage.db import Database
from .models import MappingVerdict, VenueMarket
from .scoring import PairFeatures


@dataclass
class TieringPolicy:
    """How a verdict maps to a stored status."""

    auto_accept: bool = False  # phase 2 default: propose only
    accept_threshold: float = 0.80  # confidence needed to auto-activate


class MappingStore:
    def __init__(self, db: Database, policy: Optional[TieringPolicy] = None):
        self.db = db
        self.policy = policy or TieringPolicy()

    def _tier(self, verdict: MappingVerdict) -> str:
        if not verdict.equivalent:
            return "rejected"
        if self.policy.auto_accept and verdict.confidence >= self.policy.accept_threshold:
            return "active"
        return "proposed"

    def record(
        self,
        pm: VenueMarket,
        ka: VenueMarket,
        verdict: MappingVerdict,
        features: PairFeatures,
    ) -> tuple[int, str]:
        """Upsert a candidate mapping; returns ``(id, status)``."""
        existing = self.db.get_mapping_by_pair(pm.venue_id, ka.venue_id)
        status = self._tier(verdict)

        if existing is not None and existing.status == "active":
            # Don't downgrade a live mapping on a confidence wobble; retire only if
            # it is no longer equivalent at all.
            status = "active" if verdict.equivalent else "retired"
        elif existing is not None and existing.status == "rejected":
            # Respect a human rejection unless it now clears review as equivalent.
            if not verdict.equivalent:
                status = "rejected"

        fields = {
            "pm_condition_id": pm.venue_id,
            "ka_ticker": ka.venue_id,
            "pm_yes_token": pm.yes_token,
            "pm_no_token": pm.no_token,
            "pm_yes_equals_kalshi_yes": verdict.pm_yes_equals_kalshi_yes,
            "label": pm.title or ka.title,
            "category": pm.category or ka.category,
            "close_time": pm.close_time or ka.close_time,
            "status": status,
            "confidence": verdict.confidence,
            "score_json": json.dumps(features.to_dict()),
            "reason": verdict.reason,
            "method": verdict.method,
        }
        mapping_id = self.db.upsert_mapping(fields)
        return mapping_id, status

    def retire_stale(self, live_pm_ids: set[str], live_ka_ids: set[str]) -> int:
        """Retire proposed/active mappings whose legs are no longer live."""
        retired = 0
        for row in self.db.mappings():
            if row.status not in ("proposed", "active"):
                continue
            if row.pm_condition_id not in live_pm_ids or row.ka_ticker not in live_ka_ids:
                if self.db.set_mapping_status(row.id, "retired"):
                    retired += 1
        return retired
