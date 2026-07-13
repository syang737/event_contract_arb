"""Command-line interface: ``run-bot``, ``settle-trades``, ``export-pnl``.

Also exposes ``list-markets`` for quickly inspecting the configured mapping.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from .config import AppConfig, load_config
from .core.models import Exchange
from .engine import ArbEngine, build_clients
from .exchanges.base import MarketResolution
from .execution.settlement import settle_open_trades
from .mapping.adjudicator import RuleAdjudicator
from .mapping.loader import resolve_markets
from .mapping.store import TieringPolicy
from .mapping.sync import sync_once
from .storage.db import Database, TradeRow


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load(exchanges_config: str, markets_config: str, db_url: Optional[str]) -> tuple[AppConfig, Database]:
    config = load_config(exchanges_config, markets_config)
    url = db_url or config.engine.db_url
    # Ensure the parent dir exists for file-backed SQLite URLs.
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
    db = Database(url)
    return config, db


_exchanges_opt = click.option(
    "--exchanges-config", default="config/exchanges.yaml", show_default=True,
    help="Path to exchanges.yaml.",
)
_markets_opt = click.option(
    "--markets-config", default="config/markets.yaml", show_default=True,
    help="Path to markets.yaml.",
)
_db_opt = click.option("--db-url", default=None, help="Override the SQLAlchemy DB URL.")
_loglevel_opt = click.option("--log-level", default="INFO", show_default=True)


@click.group()
def cli() -> None:
    """Polymarket <> Kalshi paper-trading arbitrage engine."""


# --------------------------------------------------------------------------- #
@cli.command("run-bot")
@_exchanges_opt
@_markets_opt
@_db_opt
@_loglevel_opt
@click.option("--mock", is_flag=True, help="Use in-memory demo books instead of live APIs.")
@click.option("--iterations", type=int, default=None, help="Stop after N poll cycles (default: run forever).")
@click.option("--interval", type=float, default=None, help="Override poll interval (seconds).")
@click.option("--sample-quotes", is_flag=True, help="Persist top-of-book snapshots each cycle.")
@click.option("--dynamic-mappings/--no-dynamic-mappings", default=None,
              help="Trade DB `active` mappings merged with YAML pins (overrides config).")
def run_bot(exchanges_config, markets_config, db_url, log_level, mock, iterations, interval,
            sample_quotes, dynamic_mappings):
    """Poll books, detect arbs, and record simulated paper trades."""
    _setup_logging(log_level)
    config, db = _load(exchanges_config, markets_config, db_url)
    if interval is not None:
        config.engine.poll_interval_seconds = interval
    if dynamic_mappings is not None:
        config.mapping.enabled = dynamic_mappings
    # Merge dynamic (DB active) mappings with YAML pins when enabled.
    config.markets = resolve_markets(config, db)

    async def _main() -> None:
        pm, ka = build_clients(config, mock=mock)
        engine = ArbEngine(config, db, pm, ka, sample_quotes=sample_quotes)
        try:
            await engine.run(max_iterations=iterations)
        finally:
            await engine.aclose()

    asyncio.run(_main())


# --------------------------------------------------------------------------- #
@cli.command("settle-trades")
@_exchanges_opt
@_markets_opt
@_db_opt
@_loglevel_opt
@click.option("--mock", is_flag=True, help="Use demo clients (resolves all mapped markets).")
def settle_trades(exchanges_config, markets_config, db_url, log_level, mock):
    """Resolve settled markets and book realized PnL for OPEN trades."""
    _setup_logging(log_level)
    config, db = _load(exchanges_config, markets_config, db_url)
    mappings = {m.id: m for m in config.markets}

    async def _main() -> None:
        pm, ka = build_clients(config, mock=mock)
        if mock:
            # In demo mode, mark every mapped market as resolved so settlement runs.
            for m in config.markets:
                pm.set_resolution(m.id, MarketResolution(resolved=True, yes_won=True))
                ka.set_resolution(m.id, MarketResolution(resolved=True, yes_won=True))
        clients = {Exchange.POLYMARKET: pm, Exchange.KALSHI: ka}
        try:
            result = await settle_open_trades(db, mappings, clients)
        finally:
            await asyncio.gather(pm.close(), ka.close(), return_exceptions=True)

        click.echo(
            f"settled={result.settled} still_open={result.still_open} "
            f"errors={result.errors} realized_pnl=${result.realized_pnl:.2f}"
        )

    asyncio.run(_main())


# --------------------------------------------------------------------------- #
@cli.command("export-pnl")
@_exchanges_opt
@_markets_opt
@_db_opt
@click.option("--format", "fmt", type=click.Choice(["table", "csv", "json"]), default="table", show_default=True)
@click.option("--output", type=click.Path(), default=None, help="Write to a file instead of stdout.")
def export_pnl(exchanges_config, markets_config, db_url, fmt, output):
    """Summarize realized (settled) and expected (open) PnL per trade."""
    config, db = _load(exchanges_config, markets_config, db_url)
    trades = db.all_trades()
    rows = [_trade_to_dict(t) for t in trades]

    realized = sum(t.realized_pnl for t in trades if t.realized_pnl is not None)
    expected_open = sum(
        (t.size - t.total_cost) for t in trades if t.status == "OPEN"
    )

    text = _render(rows, fmt, realized, expected_open)
    if output:
        Path(output).write_text(text, encoding="utf-8")
        click.echo(f"wrote {len(rows)} trades to {output}")
    else:
        click.echo(text)


def _trade_to_dict(t: TradeRow) -> dict:
    return {
        "id": t.id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "market_id": t.market_id,
        "direction": t.direction,
        "size": round(t.size, 4),
        "leg1": f"{t.leg1_exchange}:{t.leg1_side}@{t.leg1_avg_price:.4f}",
        "leg2": f"{t.leg2_exchange}:{t.leg2_side}@{t.leg2_avg_price:.4f}",
        "total_cost": round(t.total_cost, 4),
        "net_edge_per_contract": round(t.net_edge_per_contract, 4),
        "status": t.status,
        "realized_pnl": round(t.realized_pnl, 4) if t.realized_pnl is not None else None,
        "expected_pnl": round(t.size - t.total_cost, 4),
    }


def _render(rows: list[dict], fmt: str, realized: float, expected_open: float) -> str:
    if fmt == "json":
        return json.dumps(
            {"trades": rows, "realized_pnl": realized, "expected_open_pnl": expected_open},
            indent=2,
        )
    if fmt == "csv":
        buf = io.StringIO()
        fieldnames = list(rows[0].keys()) if rows else ["id"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().rstrip("\n")

    # table
    lines = [
        f"{'id':>4} {'market':<20} {'dir':<14} {'size':>7} "
        f"{'cost':>9} {'net_edge':>9} {'status':<8} {'pnl':>9}"
    ]
    lines.append("-" * 92)
    for r in rows:
        pnl = r["realized_pnl"] if r["realized_pnl"] is not None else r["expected_pnl"]
        lines.append(
            f"{r['id']:>4} {r['market_id'][:20]:<20} {r['direction']:<14} "
            f"{r['size']:>7.2f} {r['total_cost']:>9.2f} {r['net_edge_per_contract']:>9.4f} "
            f"{r['status']:<8} {pnl:>9.2f}"
        )
    lines.append("-" * 92)
    lines.append(f"Realized PnL (settled):   ${realized:,.2f}")
    lines.append(f"Expected PnL (open):      ${expected_open:,.2f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
@cli.command("discover-markets")
@_exchanges_opt
@_markets_opt
@_db_opt
@_loglevel_opt
@click.option("--mock", is_flag=True, help="Use the in-memory demo catalog instead of live APIs.")
def discover_markets(exchanges_config, markets_config, db_url, log_level, mock):
    """Fetch both venues' market catalogs and cache them in `venue_markets`."""
    _setup_logging(log_level)
    config, db = _load(exchanges_config, markets_config, db_url)

    async def _main() -> None:
        pm, ka = build_clients(config, mock=mock)
        try:
            pm_markets = await pm.list_markets()
            ka_markets = await ka.list_markets()
        finally:
            await asyncio.gather(pm.close(), ka.close(), return_exceptions=True)
        n_pm = db.upsert_venue_markets(pm_markets)
        n_ka = db.upsert_venue_markets(ka_markets)
        click.echo(f"discovered polymarket={n_pm} kalshi={n_ka} (cached in venue_markets)")

    asyncio.run(_main())


# --------------------------------------------------------------------------- #
@cli.command("sync-mappings")
@_exchanges_opt
@_markets_opt
@_db_opt
@_loglevel_opt
@click.option("--mock", is_flag=True, help="Use the in-memory demo catalog instead of live APIs.")
@click.option("--auto-accept", is_flag=True, default=None,
              help="Auto-activate high-confidence matches (overrides config).")
def sync_mappings(exchanges_config, markets_config, db_url, log_level, mock, auto_accept):
    """Discover + match markets across venues; tier matches into the mapping store."""
    _setup_logging(log_level)
    config, db = _load(exchanges_config, markets_config, db_url)
    mc = config.mapping
    policy = TieringPolicy(
        auto_accept=mc.auto_accept if auto_accept is None else auto_accept,
        accept_threshold=mc.accept_threshold,
    )
    adjudicator = RuleAdjudicator(
        min_confidence=mc.min_confidence, max_close_delta_hours=mc.date_tolerance_hours
    )

    async def _main() -> None:
        pm, ka = build_clients(config, mock=mock)
        try:
            report = await sync_once(
                db, pm, ka,
                adjudicator=adjudicator,
                policy=policy,
                min_shared_keywords=mc.min_shared_keywords,
                date_tolerance_hours=mc.date_tolerance_hours,
            )
        finally:
            await asyncio.gather(pm.close(), ka.close(), return_exceptions=True)
        click.echo(str(report))

    asyncio.run(_main())


# --------------------------------------------------------------------------- #
@cli.command("list-mappings")
@_exchanges_opt
@_markets_opt
@_db_opt
@click.option("--status", default=None, help="Filter by status (proposed|active|retired|rejected).")
def list_mappings(exchanges_config, markets_config, db_url, status):
    """Print stored cross-venue mappings."""
    _, db = _load(exchanges_config, markets_config, db_url)
    rows = db.mappings(status)
    if not rows:
        click.echo("(no mappings)")
        return
    for m in rows:
        polarity = "aligned" if m.pm_yes_equals_kalshi_yes else "INVERTED"
        click.echo(
            f"#{m.id:<4} [{m.status:<8}] conf={m.confidence:.2f} {polarity:<8} "
            f"{m.pm_condition_id[:16]}… <-> {m.ka_ticker}\n"
            f"      {m.label[:70]}\n      {m.reason}"
        )


# --------------------------------------------------------------------------- #
@cli.command("review-mappings")
@_exchanges_opt
@_markets_opt
@_db_opt
@click.option("--accept", "accept_id", type=int, default=None, help="Activate mapping by id.")
@click.option("--reject", "reject_id", type=int, default=None, help="Reject mapping by id.")
def review_mappings(exchanges_config, markets_config, db_url, accept_id, reject_id):
    """Review the proposal queue: accept/reject, or list what's pending."""
    _, db = _load(exchanges_config, markets_config, db_url)

    if accept_id is not None:
        ok = db.set_mapping_status(accept_id, "active")
        click.echo(f"mapping #{accept_id} -> active" if ok else f"mapping #{accept_id} not found")
        return
    if reject_id is not None:
        ok = db.set_mapping_status(reject_id, "rejected")
        click.echo(f"mapping #{reject_id} -> rejected" if ok else f"mapping #{reject_id} not found")
        return

    proposed = db.mappings("proposed")
    if not proposed:
        click.echo("review queue empty")
        return
    click.echo(f"{len(proposed)} mapping(s) awaiting review:")
    for m in proposed:
        polarity = "aligned" if m.pm_yes_equals_kalshi_yes else "INVERTED"
        click.echo(
            f"#{m.id:<4} conf={m.confidence:.2f} {polarity:<8} "
            f"{m.pm_condition_id[:16]}… <-> {m.ka_ticker} — {m.label[:50]}"
        )
    click.echo("accept with:  review-mappings --accept <id>   reject with: --reject <id>")


# --------------------------------------------------------------------------- #
@cli.command("list-markets")
@_exchanges_opt
@_markets_opt
def list_markets(exchanges_config, markets_config):
    """Print the configured cross-venue market mapping."""
    config = load_config(exchanges_config, markets_config)
    for m in config.markets:
        p = config.resolved_params(m)
        click.echo(
            f"- {m.id}: {m.label}\n"
            f"    polymarket: {m.polymarket.market_id} (YES={m.polymarket.yes_token}, NO={m.polymarket.no_token})\n"
            f"    kalshi:     {m.kalshi.ticker}\n"
            f"    params:     min_edge={p.min_edge_cents}c min_liq={p.min_liquidity} "
            f"max_notional=${p.max_notional_per_arb}"
        )


if __name__ == "__main__":  # pragma: no cover
    cli()
