"""Similarity scoring for a candidate pair.

Produces a feature vector and a single composite score in ``[0, 1]``, plus a
polarity hint (does Polymarket YES correspond to Kalshi YES, or is it inverted?).
Everything here is deterministic and dependency-light — fuzzy matching uses the
stdlib ``difflib``. An optional embedder callback can supply a semantic cosine
feature; an optional LLM adjudicator (see ``adjudicator.py``) handles the hard
cases downstream.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Callable, Optional

from .blocking import keywords, normalize_text
from .models import VenueMarket

# Complementary term pairs that flip YES/NO meaning between venues. If one title
# uses term A where the other uses its complement B, the pair is likely inverted.
_COMPLEMENTS = [
    ("democrat", "republican"),
    ("democratic", "republican"),
    ("above", "below"),
    ("over", "under"),
    ("up", "down"),
    ("win", "lose"),
    ("wins", "loses"),
    ("higher", "lower"),
    ("increase", "decrease"),
    ("yes", "no"),
]

_STRIKE_EPS = 1e-6


@dataclass
class PairFeatures:
    fuzzy_title: float
    token_jaccard: float
    keyword_overlap: int
    close_delta_hours: Optional[float]
    category_match: Optional[bool]
    strike_match: Optional[bool]
    embedding_cosine: Optional[float]
    composite: float
    pm_yes_equals_kalshi_yes: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _polarity(pm: VenueMarket, ka: VenueMarket) -> bool:
    """Heuristic: True if PM YES == Kalshi YES, False if inverted.

    Deliberately conservative — only flips to inverted on an explicit
    complementary-term signal. The LLM adjudicator can override for hard cases.
    """
    pm_tokens = keywords(f"{pm.title} {pm.description}")
    ka_tokens = keywords(f"{ka.title} {ka.description}")
    for a, b in _COMPLEMENTS:
        if (a in pm_tokens and b in ka_tokens and a not in ka_tokens) or (
            b in pm_tokens and a in ka_tokens and b not in ka_tokens
        ):
            return False
    return True


def _close_delta_hours(pm: VenueMarket, ka: VenueMarket) -> Optional[float]:
    if pm.close_time is None or ka.close_time is None:
        return None
    return abs((pm.close_time - ka.close_time).total_seconds()) / 3600.0


def _strike_match(pm: VenueMarket, ka: VenueMarket) -> Optional[bool]:
    if pm.strike is None or ka.strike is None:
        return None
    return abs(pm.strike - ka.strike) <= _STRIKE_EPS


def score_pair(
    pm: VenueMarket,
    ka: VenueMarket,
    *,
    embedder: Optional[Callable[[str, str], float]] = None,
    date_scale_hours: float = 48.0,
) -> PairFeatures:
    """Compute similarity features + composite score for one candidate pair."""
    pm_text = f"{pm.title} {pm.description}"
    ka_text = f"{ka.title} {ka.description}"
    fuzzy = _fuzzy(pm.title, ka.title)
    jaccard = _jaccard(keywords(pm_text), keywords(ka_text))
    overlap = len(keywords(pm_text) & keywords(ka_text))
    delta = _close_delta_hours(pm, ka)
    category_match = (
        None
        if not pm.category or not ka.category
        else pm.category.strip().lower() == ka.category.strip().lower()
    )
    strike_match = _strike_match(pm, ka)
    embed = embedder(pm_text, ka_text) if embedder is not None else None

    # Weighted composite over the features that are present.
    weighted: list[tuple[float, float]] = [(0.45, fuzzy), (0.25, jaccard)]
    if delta is not None:
        weighted.append((0.15, max(0.0, 1.0 - delta / date_scale_hours)))
    if category_match is not None:
        weighted.append((0.10, 1.0 if category_match else 0.0))
    if strike_match is not None:
        weighted.append((0.15, 1.0 if strike_match else 0.0))
    if embed is not None:
        weighted.append((0.40, embed))
    total_w = sum(w for w, _ in weighted)
    composite = sum(w * v for w, v in weighted) / total_w if total_w else 0.0

    return PairFeatures(
        fuzzy_title=round(fuzzy, 4),
        token_jaccard=round(jaccard, 4),
        keyword_overlap=overlap,
        close_delta_hours=None if delta is None else round(delta, 3),
        category_match=category_match,
        strike_match=strike_match,
        embedding_cosine=None if embed is None else round(embed, 4),
        composite=round(composite, 4),
        pm_yes_equals_kalshi_yes=_polarity(pm, ka),
    )
