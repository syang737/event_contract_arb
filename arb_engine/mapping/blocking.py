"""Candidate generation (blocking).

Reduces the O(N×M) all-pairs comparison to a tractable candidate set using cheap
keys: shared significant keywords, category agreement, close-date proximity, and
(when both sides expose a numeric strike) strike equality. Scoring/adjudication
then rank and confirm the survivors.
"""

from __future__ import annotations

import re
from typing import Iterable

from .models import VenueMarket

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or", "will",
    "be", "is", "are", "by", "with", "this", "that", "it", "as", "no", "yes",
    "market", "resolve", "resolves", "settle", "settles", "than", "who", "what",
    "when", "which", "happen", "if", "above", "below", "over", "under",
}
_STRIKE_EPS = 1e-6


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def keywords(text: str) -> set[str]:
    """Significant tokens: length ≥ 3, not a stopword (digits always kept)."""
    out: set[str] = set()
    for tok in normalize_text(text).split():
        if tok.isdigit() or (len(tok) >= 3 and tok not in _STOPWORDS):
            out.add(tok)
    return out


def _market_keywords(vm: VenueMarket) -> set[str]:
    return keywords(f"{vm.title} {vm.description}")


def _date_ok(a: VenueMarket, b: VenueMarket, tolerance_hours: float) -> bool:
    if a.close_time is None or b.close_time is None:
        return True  # can't disqualify on missing data; scoring handles it
    delta = abs((a.close_time - b.close_time).total_seconds()) / 3600.0
    return delta <= tolerance_hours


def _category_ok(a: VenueMarket, b: VenueMarket) -> bool:
    if not a.category or not b.category:
        return True
    return a.category.strip().lower() == b.category.strip().lower()


def _strike_ok(a: VenueMarket, b: VenueMarket) -> bool:
    # Only disqualify when *both* expose a numeric strike and they differ.
    if a.strike is None or b.strike is None:
        return True
    if abs(a.strike - b.strike) > _STRIKE_EPS:
        return False
    if a.strike_cap is not None and b.strike_cap is not None:
        return abs(a.strike_cap - b.strike_cap) <= _STRIKE_EPS
    return True


def generate_candidates(
    pm_markets: Iterable[VenueMarket],
    ka_markets: Iterable[VenueMarket],
    *,
    min_shared_keywords: int = 1,
    date_tolerance_hours: float = 48.0,
) -> list[tuple[VenueMarket, VenueMarket]]:
    """Return candidate ``(polymarket, kalshi)`` pairs surviving the block filters."""
    ka_list = list(ka_markets)
    ka_keywords = [_market_keywords(k) for k in ka_list]

    # Inverted index: keyword -> Kalshi market indices.
    index: dict[str, set[int]] = {}
    for idx, kws in enumerate(ka_keywords):
        for kw in kws:
            index.setdefault(kw, set()).add(idx)

    pairs: list[tuple[VenueMarket, VenueMarket]] = []
    for pm in pm_markets:
        pm_kws = _market_keywords(pm)
        # Gather Kalshi candidates sharing at least one keyword.
        candidate_idxs: set[int] = set()
        for kw in pm_kws:
            candidate_idxs |= index.get(kw, set())
        for idx in candidate_idxs:
            ka = ka_list[idx]
            if len(pm_kws & ka_keywords[idx]) < min_shared_keywords:
                continue
            if not _category_ok(pm, ka):
                continue
            if not _date_ok(pm, ka, date_tolerance_hours):
                continue
            if not _strike_ok(pm, ka):
                continue
            pairs.append((pm, ka))
    return pairs
