"""Paper-trade execution: fill simulation and settlement."""

from .settlement import SettlementResult, settle_open_trades
from .simulator import SimFill, SimLeg, SimulatedTrade, simulate_trade

__all__ = [
    "SimFill",
    "SimLeg",
    "SimulatedTrade",
    "simulate_trade",
    "SettlementResult",
    "settle_open_trades",
]
