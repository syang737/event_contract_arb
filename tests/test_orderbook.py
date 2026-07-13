from arb_engine.core.models import BookLevel
from arb_engine.core.orderbook import cumulative_depth, simulate_fill_from_book, walk_book


def _asks():
    return [BookLevel(0.40, 100), BookLevel(0.42, 200), BookLevel(0.45, 300)]


def test_single_level_fill():
    avg, filled = simulate_fill_from_book(_asks(), 50)
    assert avg == 0.40
    assert filled == 50


def test_multi_level_vwap():
    # 100 @ .40 + 100 @ .42 = 82 over 200 -> 0.41
    avg, filled = simulate_fill_from_book(_asks(), 200)
    assert filled == 200
    assert abs(avg - 0.41) < 1e-9


def test_partial_fill_when_thin():
    avg, filled = simulate_fill_from_book(_asks(), 10_000)
    assert filled == 600  # total depth
    notional = 100 * 0.40 + 200 * 0.42 + 300 * 0.45
    assert abs(avg - notional / 600) < 1e-9


def test_empty_and_zero():
    assert simulate_fill_from_book([], 10) == (0.0, 0.0)
    assert simulate_fill_from_book(_asks(), 0) == (0.0, 0.0)


def test_walk_book_slices():
    consumed = walk_book(_asks(), 150)
    assert [(lvl.price, lvl.size) for lvl in consumed] == [(0.40, 100), (0.42, 50)]


def test_cumulative_depth_price_cap():
    assert cumulative_depth(_asks()) == 600
    assert cumulative_depth(_asks(), up_to_price=0.42) == 300
