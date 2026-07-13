"""Hybrid mapping source: merge DB `active` mappings with YAML pins.

YAML entries are manual pins and always win on conflict (matched by the
Polymarket conditionId + Kalshi ticker pair). DB-active mappings supply the
dynamic, self-updating set. Rows missing Polymarket token ids are skipped —
without them the engine can't fetch the Polymarket book.
"""

from __future__ import annotations

from ..config import (
    KalshiMarketRef,
    MarketMapping,
    PolymarketMarketRef,
)
from ..storage.db import Database, MarketMappingRow


def mapping_row_to_mapping(row: MarketMappingRow) -> MarketMapping:
    return MarketMapping(
        id=f"auto_{row.id}",
        label=row.label or f"{row.pm_condition_id}::{row.ka_ticker}",
        category=row.category,
        close_time=row.close_time,
        polymarket=PolymarketMarketRef(
            market_id=row.pm_condition_id,
            yes_token=row.pm_yes_token or "",
            no_token=row.pm_no_token or "",
        ),
        kalshi=KalshiMarketRef(ticker=row.ka_ticker),
        params=None,
        pm_yes_equals_kalshi_yes=row.pm_yes_equals_kalshi_yes,
    )


def load_active_mappings(db: Database) -> list[MarketMapping]:
    """DB `active` mappings that carry the token ids needed to fetch books."""
    out: list[MarketMapping] = []
    for row in db.mappings("active"):
        if not row.pm_yes_token or not row.pm_no_token:
            continue
        out.append(mapping_row_to_mapping(row))
    return out


def merge_mappings(
    yaml_pins: list[MarketMapping], dynamic: list[MarketMapping]
) -> list[MarketMapping]:
    """YAML pins first (they win); dynamic mappings for pairs not already pinned."""
    pinned_pairs = {(m.polymarket.market_id, m.kalshi.ticker) for m in yaml_pins}
    merged = list(yaml_pins)
    for m in dynamic:
        if (m.polymarket.market_id, m.kalshi.ticker) in pinned_pairs:
            continue
        merged.append(m)
    return merged


def resolve_markets(config, db: Database) -> list[MarketMapping]:
    """Effective market list for the engine, honoring `mapping.enabled`."""
    if not config.mapping.enabled:
        return list(config.markets)
    return merge_mappings(list(config.markets), load_active_mappings(db))
