"""Adjudication: decide equivalence + polarity for a candidate pair.

``Adjudicator`` is the pluggable seam. ``RuleAdjudicator`` (the default) is
deterministic and offline: it applies hard guardrails (close-time tolerance,
strike exactness) and thresholds the composite score. A future ``LLMAdjudicator``
can implement the same interface to read both markets' full resolution criteria
for the ambiguous middle band without changing any caller.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from .models import MappingVerdict, VenueMarket
from .scoring import PairFeatures


class Adjudicator(abc.ABC):
    @abc.abstractmethod
    def judge(self, pm: VenueMarket, ka: VenueMarket, features: PairFeatures) -> MappingVerdict:
        """Return an equivalence verdict (incl. polarity + confidence)."""


@dataclass
class RuleAdjudicator(Adjudicator):
    """Deterministic thresholds + hard guardrails.

    ``equivalent`` requires the composite to clear ``min_confidence`` *and* both
    guardrails to pass. Confidence equals the composite so the caller can tier it
    (auto-accept vs review queue).
    """

    min_confidence: float = 0.55
    max_close_delta_hours: float = 48.0

    def judge(self, pm: VenueMarket, ka: VenueMarket, features: PairFeatures) -> MappingVerdict:
        polarity = features.pm_yes_equals_kalshi_yes

        # Hard guardrails — never call a pair equivalent if these fail.
        if features.strike_match is False:
            return MappingVerdict(
                equivalent=False,
                pm_yes_equals_kalshi_yes=polarity,
                confidence=features.composite,
                reason=f"strike mismatch (composite={features.composite:.2f})",
                method="rule",
                reason_code="strike_mismatch",
            )
        if (
            features.close_delta_hours is not None
            and features.close_delta_hours > self.max_close_delta_hours
        ):
            return MappingVerdict(
                equivalent=False,
                pm_yes_equals_kalshi_yes=polarity,
                confidence=features.composite,
                reason=f"close times differ by {features.close_delta_hours:.0f}h",
                method="rule",
                reason_code="close_time",
            )

        equivalent = features.composite >= self.min_confidence
        reason = (
            f"composite={features.composite:.2f} fuzzy={features.fuzzy_title:.2f} "
            f"jaccard={features.token_jaccard:.2f}"
            + ("" if polarity else " [inverted polarity]")
            + ("" if equivalent else f" < min_confidence={self.min_confidence:.2f}")
        )
        return MappingVerdict(
            equivalent=equivalent,
            pm_yes_equals_kalshi_yes=polarity,
            confidence=features.composite,
            reason=reason,
            method="rule",
            reason_code="ok" if equivalent else "below_threshold",
        )
