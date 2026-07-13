# CLAUDE.md

Guidance for AI agents working in this repository.

## What this is

A Python **paper-trading** arbitrage engine between **Polymarket** (CLOB) and
**Kalshi** (event contracts). It polls order books for a curated set of matched
markets, detects YES/NO cross-exchange arbs after modeling fees + slippage,
simulates trades, and logs PnL to SQLite. **It never places live orders.**

Core identity: buying YES on one venue and NO on the other for a combined cost
below `$1.00`/contract locks a profit, because at settlement exactly one leg
pays `$1` → payout is always `size × $1`.

## Commands

```bash
# Install (Python 3.11+)
pip install -r requirements.txt          # or: pip install -e ".[dev]"

# Tests (pytest is in asyncio auto mode — async tests need no decorator)
python -m pytest                          # full suite
python -m pytest tests/test_arbitrage.py -q
python -m pytest -k fee                    # filter by name

# Run the engine
python -m arb_engine.cli list-markets
python -m arb_engine.cli run-bot --mock --iterations 3 --interval 0   # offline, no network
python -m arb_engine.cli run-bot --iterations 1                        # live REST (needs network/proxy)
python -m arb_engine.cli settle-trades --mock
python -m arb_engine.cli export-pnl --format table   # table | csv | json

# Automated cross-venue mapping (discover -> match -> review -> trade)
python -m arb_engine.cli discover-markets --mock                       # cache both catalogs
python -m arb_engine.cli sync-mappings --mock                          # match + tier into store
python -m arb_engine.cli review-mappings                               # list the proposal queue
python -m arb_engine.cli review-mappings --accept 1                    # (or --reject 1)
python -m arb_engine.cli list-mappings --status active
python -m arb_engine.cli run-bot --mock --dynamic-mappings --iterations 1  # trade active mappings
```

There is **no configured linter/formatter or CI**. Keep style consistent with
the surrounding code (type hints everywhere, `from __future__ import annotations`
at the top of every module).

## Data flow

```
cli.py → engine.ArbEngine.poll_once()
  1. fetch_book() on both clients (async, in parallel)        exchanges/*_client.py
  2. detect() → ArbOpportunity list (ranked by net profit)    core/arbitrage.py
  3. risk gate (capital / per-exchange / per-market caps)     engine.RiskState
  4. simulate_trade() re-walks books, builds fills            execution/simulator.py
  5. persist trade + arb rows                                 storage/db.py
settle-trades: resolve markets → book realized PnL            execution/settlement.py
```

## Module map

| Path | Responsibility |
|---|---|
| `arb_engine/config.py` | YAML → typed pydantic (`AppConfig`). `resolved_params()` merges per-market overrides over `engine.defaults`. |
| `arb_engine/core/models.py` | `Exchange`/`Side`/`Direction` enums; `BookLevel`, `BookSide`, `MarketBook`, `Quote`, `ArbLeg`, `ArbOpportunity`. |
| `arb_engine/core/orderbook.py` | `walk_book` / `simulate_fill_from_book` (VWAP over depth) + `cumulative_depth`. |
| `arb_engine/core/fees.py` | `PolymarketFeeModel`, `KalshiFeeModel`, `FeeEngine` (dispatch by exchange). |
| `arb_engine/core/arbitrage.py` | `ArbDetector`: two directions × discrete size optimization + all filters. |
| `arb_engine/exchanges/base.py` | `ExchangeClient` ABC, `MarketResolution`, error types. |
| `arb_engine/exchanges/{polymarket,kalshi}_client.py` | Async `httpx` REST clients. |
| `arb_engine/exchanges/mock_client.py` | In-memory books for `--mock` + tests; `build_demo_clients()`. |
| `arb_engine/execution/{simulator,settlement}.py` | Trade sim (+ per-level fills) and settlement PnL. |
| `arb_engine/storage/db.py` | SQLAlchemy ORM (`markets`/`quotes`/`arbs`/`trades`/`fills`) + `Database` helper. |
| `arb_engine/engine.py` | Async poll loop, `RiskState`, `build_clients()`. |
| `arb_engine/mapping/` | Automated contract mapping: `discovery`, `blocking`, `scoring`, `adjudicator`, `store`, `sync`, `loader`. See below. |
| `arb_engine/cli.py` | Click commands: `run-bot`, `settle-trades`, `export-pnl`, `list-markets`, `discover-markets`, `sync-mappings`, `review-mappings`, `list-mappings`. |
| `config/{exchanges,markets}.yaml` | Connectivity/proxy/fees/engine/`mapping` settings, and the static cross-venue mapping (manual pins). |

## Automated mapping (`arb_engine/mapping/`)

The static `config/markets.yaml` is only *pins*; the pipeline discovers and
maintains mappings for transient series. Flow:
`discovery.list_markets()` (per client) → `blocking.generate_candidates` (category
× close-date × keyword × strike) → `scoring.score_pair` (difflib fuzzy + Jaccard +
date/category/strike + polarity heuristic) → `adjudicator.RuleAdjudicator.judge`
(equivalence + guardrails) → `store.MappingStore` (tiers into
`proposed|active|rejected`, retires dead legs) → `loader.resolve_markets` (merges
DB `active` over YAML pins for the engine). `sync.sync_once` orchestrates one cycle
(`sync-mappings` CLI). Tables: `venue_markets` (catalog cache), `market_mappings`
(status + confidence + provenance). Adjudicator is a pluggable ABC — an embedding
scorer / LLM adjudicator can slot in without touching callers (config toggles
`mapping.use_embeddings` / `use_llm`, currently no-ops).

## Invariants & gotchas (read before editing core logic)

- **Normalization is the whole trick.** All prices are dollars/contract in
  `[0,1]`; sizes are contracts. Every `BookSide.asks` represents *liquidity you
  consume when buying* that outcome (ascending price). Buying YES or NO is always
  a walk down `asks`. Preserve this when touching any client or the detector.
- **Kalshi book quirk.** Kalshi quotes integer **cents** and only returns
  *resting bids* per side. A NO bid at `p`¢ is an offer to sell YES at `(100−p)`¢,
  so `KalshiClient` derives each outcome's `asks` from the *opposite* side's bids
  (`_to_levels(..., invert=True)`). Polymarket already quotes 0–1 and has a
  separate token/book per outcome, fetched individually.
- **Fee formulas** (config-driven, defaults matter):
  - Kalshi: `ceil(rate·C·P·(1−P))` rounded **up to the next cent**, `rate` default `0.07`.
  - Polymarket: `rate·min(P,1−P)·size`, `rate` default `0` (historically free).
- **Size optimization** in `_evaluate_direction` relies on monotonicity: it
  requires a *full* fill on both legs and `break`s once a size can't be filled or
  the notional cap is exceeded (larger sizes only get worse). Don't turn those
  `break`s into `continue`.
- **Settlement PnL is outcome-independent.** Because a trade holds YES+NO across
  venues, `realized_pnl = size − total_cost` regardless of who wins. Resolution
  endpoints are queried only to confirm the market actually settled.
- **Polarity.** A Kalshi market can be *inverted* — its YES pays on the event's
  NO. `MarketMapping.pm_yes_equals_kalshi_yes` records this; `event_side_book()`
  (`core/models.py`) swaps the Kalshi book sides, and **both** the detector and
  the simulator must go through it. Don't read `ka_book.yes/.no` directly for a
  canonical event outcome.
- **In-memory SQLite needs `StaticPool`** (already handled in `Database.__init__`)
  or each session gets a fresh empty DB. `all_trades()` eager-loads `fills`
  (`selectinload`) to avoid `DetachedInstanceError` after the session closes.
- **Offline-first.** Outbound APIs aren't reachable here and Polymarket
  geo-blocks; use `--mock` to run/demo. Real clients are unit-tested via
  `httpx.MockTransport` (see `tests/test_clients.py`) — mirror that pattern rather
  than hitting the network.
- **pydantic config is strict** (`extra="forbid"`). Adding a YAML key requires a
  matching field in `config.py` or loading fails at startup — which is intended.

## Adding things

- **New market:** add an entry to `config/markets.yaml` (Polymarket
  `conditionId` + YES/NO token ids, Kalshi ticker, optional `close_time`, optional
  `params` overrides). No code change needed.
- **New exchange:** implement `ExchangeClient` (`fetch_book` → normalized
  `MarketBook`, `get_resolution`), add a fee model in `core/fees.py`, and wire it
  into `engine.build_clients`. The detector is currently PM↔Kalshi specific.
- **New tests:** use the builders in `tests/conftest.py` (`make_mapping`,
  `make_config`, `book`). Async tests need no decorator (asyncio auto mode).

## Git / branches

Development branch: `claude/polymarket-kalshi-arb-engine-gn6pry`; PRs target
`main`. The repo was bootstrapped empty, so `main` is an empty root commit that
serves purely as the PR base.
