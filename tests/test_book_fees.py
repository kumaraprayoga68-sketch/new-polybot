import pytest

from npb.book import OrderBook
from npb.fees import rate_from_bps, taker_fee

from .conftest import make_book


def test_from_api_sorts_best_first():
    # Format asli CLOB: bids naik (terbaik di akhir), asks turun (terbaik di akhir).
    raw = {
        "asset_id": "abc",
        "bids": [{"price": "0.01", "size": "100"}, {"price": "0.06", "size": "10"}],
        "asks": [{"price": "0.99", "size": "100"}, {"price": "0.10", "size": "10"}],
        "tick_size": "0.001", "min_order_size": "5", "neg_risk": True,
    }
    b = OrderBook.from_api(raw)
    assert b.best_bid == 0.06 and b.best_ask == 0.10
    assert b.mid == pytest.approx(0.08)
    assert b.tick_size == 0.001 and b.neg_risk


def test_buy_walks_levels_and_charges_fee():
    b = make_book([(0.40, 10), (0.50, 10)])
    fill = b.buy(15, fee_rate=0.1)
    assert fill.cost == pytest.approx(10 * 0.40 + 5 * 0.50)
    assert fill.fee == pytest.approx(0.1 * 0.40 * 10 + 0.1 * 0.50 * 5)
    assert fill.worst_price == 0.50
    assert fill.avg_price == pytest.approx(6.5 / 15)


def test_buy_fails_without_depth():
    assert make_book([(0.4, 3)]).buy(5, 0.0) is None


def test_fee_formula_symmetric_and_override():
    assert taker_fee(0.2, 10, 0.1) == pytest.approx(taker_fee(0.8, 10, 0.1))
    assert rate_from_bps(1000) == 0.1
    assert rate_from_bps(1000, override=0.02) == 0.02
    assert rate_from_bps(None) == 0.0
