"""Paper-trading arbitrage engine between Polymarket (CLOB) and Kalshi.

The package is organised per the design spec:

* ``arb_engine.config``            – typed configuration loading (YAML -> pydantic).
* ``arb_engine.core.models``       – normalized quote / book / opportunity models.
* ``arb_engine.core.orderbook``    – fill simulation over book depth.
* ``arb_engine.core.fees``         – per-exchange fee estimation.
* ``arb_engine.core.arbitrage``    – cross-exchange arb detection + size search.
* ``arb_engine.exchanges``         – Polymarket / Kalshi / mock market-data clients.
* ``arb_engine.execution``         – trade simulation and settlement.
* ``arb_engine.storage``           – SQLAlchemy models and helpers.
* ``arb_engine.engine``            – async polling orchestration loop.
* ``arb_engine.cli``               – ``run-bot`` / ``settle-trades`` / ``export-pnl``.
"""

__version__ = "0.1.0"
