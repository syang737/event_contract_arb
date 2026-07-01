# event_contract_arb

A Python **paper-trading** arbitrage engine between **Polymarket** (CLOB) and
**Kalshi** (event contracts). It continuously pulls order books for a curated
list of matched markets, detects YES/NO cross-exchange arbitrage after modeling
fees and slippage, simulates trades, and logs PnL locally to SQLite.

> No live orders are placed. This is a research / paper-trading tool. Account
> management, custody, and compliance logic are out of scope.

## The core idea

For a market that exists on both venues, buying **YES on one venue** and **NO on
the other** for a combined cost below `$1.00`/contract locks in a profit: at
settlement exactly one leg pays `$1`, so payout is always `size × $1`.

```
gross_edge = 1 − (avg_yes + avg_no)
net_edge   = gross_edge − (fee_yes + fee_no) / size
adj_edge   = net_edge − safety_buffer          # slippage / rounding / model error
```

An opportunity is actionable when `adj_edge ≥ min_edge_cents/100` and all
liquidity, staleness, time-to-expiry, and capital filters pass.

## Architecture

```
arb_engine/
├── config.py                 # YAML -> typed pydantic config
├── core/
│   ├── models.py             # Exchange/Side/Direction, BookLevel, BookSide,
│   │                         #   MarketBook, Quote, ArbLeg, ArbOpportunity
│   ├── orderbook.py          # walk_book / simulate_fill_from_book (slippage)
│   ├── fees.py               # Polymarket & Kalshi fee models + FeeEngine
│   └── arbitrage.py          # ArbDetector: 2 directions × size optimization
├── exchanges/
│   ├── base.py               # ExchangeClient ABC, MarketResolution, errors
│   ├── polymarket_client.py  # CLOB REST /book + /markets (per-token books)
│   ├── kalshi_client.py      # trade-api v2 /orderbook + /markets (bid->ask norm)
│   └── mock_client.py        # in-memory books for offline demo + tests
├── execution/
│   ├── simulator.py          # SimulatedTrade + per-leg fills (re-walks book)
│   └── settlement.py         # resolve markets, book realized PnL
├── storage/
│   └── db.py                 # SQLAlchemy models + Database helper (SQLite)
├── engine.py                 # async poll loop + risk state / gating
└── cli.py                    # run-bot / settle-trades / export-pnl / list-markets

config/
├── exchanges.yaml            # connectivity, proxy, fees, engine settings
└── markets.yaml              # static cross-venue market mapping
```

### Order-book normalization

All prices are normalized to **dollars per contract (`0..1`)** and sizes to
**contracts**, with each outcome's `asks` representing liquidity you *consume
when buying*. This lets the detector treat both venues identically.

* **Polymarket** already quotes `0..1`; each market has two ERC-1155 outcome
  tokens, so the client fetches `/book` for the YES and NO token separately.
* **Kalshi** quotes integer **cents** and its book only exposes *resting bids*
  per side. A resting NO bid at `p`¢ is an offer to sell YES at `(100−p)`¢, so
  the client derives each outcome's `asks` from the opposite side's bids.

## Installation

```bash
pip install -r requirements.txt      # or: pip install -e ".[dev]"
```

Python 3.11+.

## Configuration

Two YAML files under `config/` (see the inline comments in each):

* **`exchanges.yaml`** — base/WS URLs, per-exchange **proxy** (Polymarket
  geo-block support via HTTP/SOCKS), fee parameters, and engine-wide settings
  (poll interval, `safety_buffer`, staleness window, capital caps, DB URL,
  default per-market params).
* **`markets.yaml`** — the manually-curated mapping of "the same" market across
  venues: Polymarket `conditionId` + YES/NO token ids, Kalshi `ticker`, an
  optional `close_time`, and per-market risk `params` overrides.

### Proxy / network

Each exchange block accepts an optional `proxy` (`type` + `url`, e.g.
`socks5://user:pass@host:1080`). Clients pass it straight to `httpx`, so
higher-level code stays transport-agnostic. Kalshi additionally supports
`env: demo|prod`, which switches the base URL.

### Fees

* **Kalshi:** `ceil(rate · C · P · (1−P))` rounded up to the next cent
  (`rate` default `0.07`; override per market, e.g. `0.035` for S&P/Nasdaq).
* **Polymarket:** symmetric-around-50% taker model
  `rate · min(P, 1−P) · size` (`rate` default `0` — historically free —
  overridable per category as fees roll out).

Both are config-driven so v1 runs with sensible defaults and can be refined
against official schedules without touching the arbitrage math.

## Usage

```bash
# Inspect the configured mapping
python -m arb_engine.cli list-markets

# Run offline against synthetic books (no network) — great first run
python -m arb_engine.cli run-bot --mock --iterations 3 --interval 0

# Run against the live REST APIs (respects configured proxies), 1 cycle
python -m arb_engine.cli run-bot --iterations 1

# Settle resolved markets and book realized PnL
python -m arb_engine.cli settle-trades          # live resolution endpoints
python -m arb_engine.cli settle-trades --mock    # demo: resolves everything

# Report PnL (table | csv | json)
python -m arb_engine.cli export-pnl
python -m arb_engine.cli export-pnl --format csv --output pnl.csv
```

`--db-url` overrides the SQLite database on any command; `--sample-quotes`
persists top-of-book snapshots each cycle. Installing the package also exposes
the `arb-engine` console script.

### Example (mock) session

```
PAPER TRADE #1 us_pres_2028_dem YES_PM_NO_KA size=213.00 net_edge=0.0619 exp_pnl=13.18
cycle 1: books=4 errors=0 arbs=1 trades=1 skipped=0
```

The size optimizer chose 213 contracts — the largest size whose total notional
stayed under the market's `$200` cap — buying YES on Polymarket (~`$0.44`) and
NO on Kalshi (~`$0.48`) for a locked ~`6.2`¢/contract net edge.

## Data model (SQLite / SQLAlchemy)

* `markets` — static mapping + metadata.
* `quotes` — optional sampled top-of-book snapshots.
* `arbs` — every detected opportunity (executed or not), with all components.
* `trades` — simulated paper trades, both legs, and settlement fields.
* `fills` — per-level fills backing each trade leg.

## Risk controls (enforced even in paper mode)

* **Liquidity:** cumulative ask depth ≥ `min_liquidity` on both legs.
* **Staleness:** skip books older than `staleness_seconds`.
* **Time to expiry:** skip markets within `min_hours_to_expiry` of `close_time`.
* **Capital:** `max_total_notional`, per-exchange `max_notional_per_exchange`,
  per-arb `max_notional_per_arb`, and `max_open_trades_per_market`.

## Testing

```bash
python -m pytest
```

The suite covers fee formulas, order-book fills, arbitrage detection (both
directions, size optimization, and every filter), client REST parsing (via
`httpx.MockTransport`, including Kalshi bid→ask normalization and Polymarket
geo-block handling), the end-to-end engine loop, and settlement PnL.

## Scope / roadmap

**In scope (implemented):** REST polling, normalized books, fee + slippage
modeling, arb detection with size optimization, paper-trade simulation, SQLite
storage, settlement, PnL export, proxy-aware clients.

**Future:** WebSocket streaming for lower latency (AsyncAPI / CLOB WS), the
official Polymarket Python SDK `ClobClient` for live order lifecycle, Kalshi
RSA-PSS request signing for authenticated/live trading, and richer fee/resolution
sourcing from each venue's SDK and on-chain data.
