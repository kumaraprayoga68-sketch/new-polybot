"""
Order book + simulasi fill. Ini inti "paper trading jujur": beli dihitung dengan
MENYAPU level ask satu per satu (bukan pakai curPrice/midpoint) dan bayar fee.

Catatan format CLOB /book: bids urut NAIK (bid terbaik di AKHIR), asks urut TURUN
(ask terbaik di AKHIR). Kita urutkan sendiri, jangan percaya urutan respons.
Dan /price?side=BUY itu balikin BID terbaik, bukan ask — polybot lama salah di sini.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .fees import taker_fee


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Fill:
    shares: float
    cost: float          # USDC dibayar ke penjual (belum termasuk fee)
    fee: float
    worst_price: float   # level terakhir yang disentuh = limit price FOK

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares else 0.0

    @property
    def total(self) -> float:
        return self.cost + self.fee


@dataclass
class OrderBook:
    token_id: str
    bids: list[Level] = field(default_factory=list)   # terbaik dulu (harga turun)
    asks: list[Level] = field(default_factory=list)   # terbaik dulu (harga naik)
    tick_size: float = 0.01
    min_order_size: float = 5.0
    neg_risk: bool = False

    @classmethod
    def from_api(cls, data: dict, token_id: str = "") -> "OrderBook":
        def levels(rows):
            out = []
            for r in rows or []:
                try:
                    p, s = float(r["price"]), float(r["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 < p < 1 and s > 0:
                    out.append(Level(p, s))
            return out

        def num(key, default):
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            token_id=token_id or str(data.get("asset_id", "")),
            bids=sorted(levels(data.get("bids")), key=lambda l: -l.price),
            asks=sorted(levels(data.get("asks")), key=lambda l: l.price),
            tick_size=num("tick_size", 0.01),
            min_order_size=num("min_order_size", 5.0),
            neg_risk=bool(data.get("neg_risk", False)),
        )

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def mid(self) -> float | None:
        if self.best_ask is None or self.best_bid is None:
            return None
        return (self.best_ask + self.best_bid) / 2

    def ask_depth(self) -> float:
        return sum(l.size for l in self.asks)

    def buy(self, shares: float, fee_rate: float, limit: float | None = None) -> Fill | None:
        """Simulasi BUY `shares` share. None kalau depth gak cukup (FOK gagal).
        Dengan `limit`, level di atas harga itu gak boleh disentuh (order limit FOK)."""
        if shares <= 0:
            return None
        left, cost, fee, worst = shares, 0.0, 0.0, 0.0
        for lvl in self.asks:
            if limit is not None and lvl.price > limit + 1e-9:
                return None
            take = min(left, lvl.size)
            cost += take * lvl.price
            fee += taker_fee(lvl.price, take, fee_rate)
            worst = lvl.price
            left -= take
            if left <= 1e-9:
                return Fill(shares, cost, fee, worst)
        return None

    def max_shares_within(self, limit_price: float) -> float:
        return sum(l.size for l in self.asks if l.price <= limit_price + 1e-12)

    def cumulative_ask_sizes(self) -> list[float]:
        out, acc = [], 0.0
        for l in self.asks:
            acc += l.size
            out.append(acc)
        return out
