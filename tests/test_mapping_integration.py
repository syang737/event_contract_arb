"""Phase 3: trust tiers, review workflow, hybrid loader, and dynamic engine run."""

from __future__ import annotations

from arb_engine.core.models import Exchange
from arb_engine.engine import ArbEngine
from arb_engine.exchanges.mock_client import build_demo_clients
from arb_engine.mapping.loader import (
    load_active_mappings,
    merge_mappings,
    resolve_markets,
)
from arb_engine.mapping.store import TieringPolicy
from arb_engine.mapping.sync import sync_once
from arb_engine.storage.db import Database

from .conftest import make_config, make_mapping


def _demo_config():
    return make_config(
        make_mapping("us_pres_2028_dem", min_edge_cents=2.0, min_liquidity=50.0,
                     max_notional_per_arb=200.0)
    )


async def test_auto_accept_tiers_into_active():
    cfg = _demo_config()
    db = Database("sqlite:///:memory:")
    pm, ka = build_demo_clients(cfg)
    report = await sync_once(
        db, pm, ka, policy=TieringPolicy(auto_accept=True, accept_threshold=0.6)
    )
    assert report.status_counts.get("active", 0) >= 1
    assert len(db.mappings("active")) >= 1


async def test_review_queue_accept_activates():
    cfg = _demo_config()
    db = Database("sqlite:///:memory:")
    pm, ka = build_demo_clients(cfg)
    await sync_once(db, pm, ka, policy=TieringPolicy(auto_accept=False))

    proposed = db.mappings("proposed")
    assert proposed and not db.mappings("active")
    # Simulate `review-mappings --accept <id>`.
    db.set_mapping_status(proposed[0].id, "active")
    assert len(db.mappings("active")) == 1


def test_merge_prefers_yaml_pins():
    pin = make_mapping("pinned")
    db = Database("sqlite:///:memory:")
    # A dynamic mapping over the same PM/KA pair as the pin must be dropped.
    db.upsert_mapping({
        "pm_condition_id": pin.polymarket.market_id, "ka_ticker": pin.kalshi.ticker,
        "pm_yes_token": "X", "pm_no_token": "Y", "status": "active", "confidence": 0.9,
        "pm_yes_equals_kalshi_yes": True, "label": "dyn",
    })
    dynamic = load_active_mappings(db)
    merged = merge_mappings([pin], dynamic)
    assert len(merged) == 1
    assert merged[0].id == "pinned"  # the pin, not the dynamic row


def test_load_active_skips_rows_without_tokens():
    db = Database("sqlite:///:memory:")
    db.upsert_mapping({
        "pm_condition_id": "0xNO_TOKENS", "ka_ticker": "TKR", "status": "active",
        "confidence": 0.9, "pm_yes_equals_kalshi_yes": True,
    })
    assert load_active_mappings(db) == []


async def test_end_to_end_dynamic_mapping_drives_a_trade():
    """sync -> auto-accept -> engine trades the discovered `active` mapping."""
    cfg = _demo_config()
    db = Database("sqlite:///:memory:")
    pm, ka = build_demo_clients(cfg)

    # Discover + auto-activate mappings; then clear YAML pins so only the
    # dynamically-discovered mapping is in play.
    await sync_once(db, pm, ka, policy=TieringPolicy(auto_accept=True, accept_threshold=0.6))
    cfg.markets = []
    cfg.mapping.enabled = True
    cfg.markets = resolve_markets(cfg, db)
    assert cfg.markets and all(m.id.startswith("auto_") for m in cfg.markets)

    engine = ArbEngine(cfg, db, pm, ka)
    report = await engine.poll_once()
    assert report.trades_executed >= 1

    trades = db.all_trades()
    assert trades and trades[0].leg1_exchange == Exchange.POLYMARKET.value
