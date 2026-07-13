"""Slippage modeling via order-book walks."""

from __future__ import annotations

from typing import Iterable

from .models import BookLevel

_EPS = 1e-9


def walk_book(asks: Iterable[BookLevel], desired_size: float) -> list[BookLevel]:
    """Return the slices of ``asks`` consumed to fill up to ``desired_size``.

    ``asks`` must be ascending by price. Each returned :class:`BookLevel` carries
    the price of the level and the (possibly partial) size taken from it.
    """
    if desired_size <= 0:
        return []

    remaining = desired_size
    consumed: list[BookLevel] = []
    for level in asks:
        if remaining <= _EPS:
            break
        take = level.size if level.size < remaining else remaining
        consumed.append(BookLevel(price=level.price, size=take))
        remaining -= take
    return consumed


def simulate_fill_from_book(
    asks: Iterable[BookLevel], desired_size: float
) -> tuple[float, float]:
    """Volume-weighted average fill over ``asks`` (ascending by price).

    Returns ``(avg_price, filled_size)``; ``filled_size`` may be below
    ``desired_size`` when the book is too thin, and ``(0.0, 0.0)`` if nothing
    can be filled.
    """
    consumed = walk_book(asks, desired_size)
    filled = sum(lvl.size for lvl in consumed)
    if filled <= _EPS:
        return (0.0, 0.0)
    notional = sum(lvl.price * lvl.size for lvl in consumed)
    return (notional / filled, filled)


def cumulative_depth(asks: Iterable[BookLevel], up_to_price: float | None = None) -> float:
    """Total contracts available on the ask side, optionally capped at a price."""
    total = 0.0
    for level in asks:
        if up_to_price is not None and level.price > up_to_price + _EPS:
            break
        total += level.size
    return total
