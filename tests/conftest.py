import time

import pytest

from npb.book import Level, OrderBook
from npb.config import Settings
from npb.db import DB


def make_book(asks, bids=(), token="t", min_size=5.0, tick=0.01, neg_risk=False):
    return OrderBook(token, [Level(p, s) for p, s in sorted(bids, reverse=True)],
                     [Level(p, s) for p, s in sorted(asks)], tick, min_size, neg_risk)


class FakeAPI:
    """Pengganti PolymarketAPI: semua data di-set manual oleh test."""

    def __init__(self):
        self.markets = {}
        self.book_map = {}
        self.trades = {}
        self.events = []
        self.mids = {}

    def market(self, cid, fresh=False):
        return self.markets.get(cid)

    def book(self, token):
        return self.book_map.get(token)

    def books(self, tokens):
        return {t: self.book_map[t] for t in tokens if t in self.book_map}

    def midpoint(self, token):
        return self.mids.get(token)

    def user_trades(self, wallet, limit=100, cursor=None):
        return list(self.trades.get(wallet, [])), None

    def user_trades_all(self, wallet, max_rows=1500):
        return list(self.trades.get(wallet, []))

    def iter_events(self, max_events, page=100):
        return iter(self.events[:max_events])

    def leaderboard(self, limit=100):
        return list(self.trades)[:limit]


@pytest.fixture
def db():
    return DB(":memory:")


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def now():
    return time.time()
