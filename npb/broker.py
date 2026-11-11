"""
Broker: tempat order beneran "terjadi".

PaperBroker  — simulasi FOK dengan menyapu order book yang dilihat strategi saat
               memutuskan, plus fee. Gak ada lagi fill di curPrice/midpoint.
LiveBroker   — py-clob-client, order limit FOK dengan limit = level terburuk
               yang disentuh simulasi. Kalau harga udah lari, order gagal (bukan
               ke-fill di harga jelek).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .book import OrderBook


@dataclass
class Leg:
    condition_id: str
    token_id: str
    outcome: str
    market: str
    shares: float
    book: OrderBook
    fee_rate: float
    end_date: str = ""


@dataclass
class LegResult:
    status: str            # "filled" | "rejected" | "error"
    shares: float = 0.0
    cost: float = 0.0
    fee: float = 0.0
    limit_price: float = 0.0
    error: str = ""


def round_up_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(min(0.999, math.ceil(price / tick - 1e-9) * tick), 6)


class PaperBroker:
    """Kalau dikasih `api`, order book diambil ULANG tepat sebelum fill dan diisi
    dengan limit = harga terburuk yang dihitung saat memutuskan — persis order
    FOK di LiveBroker. Antara keputusan dan fill ada jeda (siklus lain, leg lain
    di basket), jadi kalau harga lari, paper juga gagal. Tanpa `api` (tes),
    fill pakai book yang dilihat strategi."""
    mode = "paper"

    def __init__(self, api=None):
        self.api = api

    def buy(self, leg: Leg) -> LegResult:
        sim = leg.book.buy(leg.shares, leg.fee_rate)
        if sim is None:
            return LegResult("rejected", error="depth order book gak cukup (FOK)")
        limit = round_up_to_tick(sim.worst_price, leg.book.tick_size)
        fill = sim
        if self.api is not None:
            try:
                fresh = self.api.book(leg.token_id)
            except Exception as e:  # noqa: BLE001 — sama kayak live: order gagal, loop jalan terus
                return LegResult("error", limit_price=limit, error=f"ambil book: {e}")
            fill = fresh.buy(leg.shares, leg.fee_rate, limit=limit) if fresh else None
            if fill is None:
                return LegResult("rejected", limit_price=limit,
                                 error=f"harga lari / depth hilang di limit {limit:.3f} (FOK)")
        return LegResult("filled", fill.shares, fill.cost, fill.fee, limit)


class LiveBroker:
    """Kirim order beneran. Cuma dibikin kalau semua gerbang live lolos."""
    mode = "live"

    def __init__(self, host: str, private_key: str, funder: str, chain_id: int,
                 signature_type: int):
        if not private_key or not funder:
            raise RuntimeError("PRIVATE_KEY / FUNDER_ADDRESS kosong — live gak bisa jalan.")
        from py_clob_client.client import ClobClient

        self.client = ClobClient(host, key=private_key, chain_id=chain_id,
                                 signature_type=signature_type, funder=funder)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def buy(self, leg: Leg) -> LegResult:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        sim = leg.book.buy(leg.shares, leg.fee_rate)
        if sim is None:
            return LegResult("rejected", error="depth order book gak cukup (FOK)")
        limit = round_up_to_tick(sim.worst_price, leg.book.tick_size)
        try:
            signed = self.client.create_order(
                OrderArgs(token_id=leg.token_id, price=limit, size=round(leg.shares, 2), side=BUY),
                PartialCreateOrderOptions(tick_size=str(leg.book.tick_size),
                                          neg_risk=leg.book.neg_risk),
            )
            resp = self.client.post_order(signed, OrderType.FOK) or {}
        except Exception as e:  # noqa: BLE001 — order gagal gak boleh bikin loop crash
            return LegResult("error", limit_price=limit, error=str(e))
        if not resp.get("success", False) or resp.get("errorMsg"):
            return LegResult("rejected", limit_price=limit,
                             error=str(resp.get("errorMsg") or resp)[:300])
        # BUY: makingAmount = USDC dibayar, takingAmount = share diterima.
        try:
            cost = float(resp.get("makingAmount") or sim.cost)
            shares = float(resp.get("takingAmount") or sim.shares)
        except (TypeError, ValueError):
            cost, shares = sim.cost, sim.shares
        # Fee asli belum kebaca dari respons; pakai estimasi model (lihat fees.py).
        return LegResult("filled", shares, cost, sim.fee, limit)
