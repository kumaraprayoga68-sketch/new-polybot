"""
Arbitrage basket di event NEG-RISK (multi-outcome, tepat satu yang YES).

Kenapa bukan YES+NO di satu market biner (versi polybot)? Book YES dan NO itu
cermin (ask NO = 1 - bid YES), jadi ask YES + ask NO = 1 + spread, gak pernah < 1.
"Peluang" polybot muncul karena /price?side=BUY ternyata balikin BID, bukan ask.

Dua basket yang dicek di tiap event neg-risk dengan N market:
  YES basket: beli 1 YES di SEMUA market   -> bayar $1 per set (tepat satu menang)
  NO basket : beli 1 NO  di SEMUA market   -> bayar $(N-1) per set
Untung kalau total biaya per set (nyapu book + fee) < bayaran. Semua leg dibeli
dengan JUMLAH SHARE YANG SAMA (polybot salah: dolar sama = share beda = gak hedge).

Risiko yang tersisa: eksekusi gak atomic (leg dikirim satu-satu), dan event
"augmented" (ada outcome placeholder/Other) bisa resolve ke outcome di luar daftar
— YES basket di event begitu di-skip, NO basket tetap aman (bayaran >= N-1).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..api import PolymarketAPI
from ..book import OrderBook
from ..broker import Leg
from ..config import Settings
from ..db import DB
from ..executor import Decision
from ..fees import rate_from_bps

NAME = "arbitrage"


@dataclass
class EventMarket:
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    best_bid: float | None
    best_ask: float | None
    end_date: str
    fee_bps: float | None = None


@dataclass
class Basket:
    event: str
    side: str                 # "YES" | "NO"
    markets: list[EventMarket]
    payout_per_set: float

    def token(self, m: EventMarket) -> str:
        return m.yes_token if self.side == "YES" else m.no_token


@dataclass
class Sizing:
    sets: float
    cost: float               # termasuk fee
    profit: float

    @property
    def edge_per_set(self) -> float:
        return self.profit / self.sets if self.sets else 0.0


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _past(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d < datetime.now(timezone.utc)


def parse_event(ev: dict) -> tuple[list[EventMarket], bool] | None:
    """(market yang bisa dibeli, augmented). None kalau event gak aman di-basket.

    "augmented" = ada outcome yang gak bisa kita beli tapi bisa jadi pemenang:
    flag negRiskAugmented, ATAU market placeholder (active=False, belum closed,
    gak ada harga — mis. "Candidate A".."Other"). Ketemu di live: event Guinea-
    Bissau flag-nya False tapi punya 27 placeholder; YES basket di situ kelihatan
    untung 44% padahal bisa bayar $0.
    """
    if not (ev.get("negRisk") or ev.get("enableNegRisk")):
        return None
    # Tanggal akhir lewat tapi belum settle = event limbo (hasil sengketa/tertunda).
    # Mispricing di sini biasanya informasi, bukan arbitrase.
    if _past(ev.get("endDate")):
        return None
    out = []
    augmented = bool(ev.get("negRiskAugmented"))
    for m in ev.get("markets") or []:
        if not m.get("active", True) and not m.get("closed") and not m.get("outcomePrices"):
            augmented = True
            continue
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            prices = json.loads(m.get("outcomePrices") or "[]")
        except (TypeError, ValueError):
            return None
        if len(outcomes) != 2 or len(tokens) != 2:
            return None
        yes_i = 0 if str(outcomes[0]).lower() == "yes" else 1
        if m.get("closed") or not m.get("active", True):
            yes_price = _f(prices[yes_i]) if len(prices) == 2 else None
            if yes_price is not None and yes_price <= 0.01:
                continue        # jelas sudah resolve NO: bukan bagian basket
            return None         # pemenang / status gak jelas -> jangan sentuh event ini
        if m.get("enableOrderBook") is False or m.get("acceptingOrders") is False:
            return None
        out.append(EventMarket(
            condition_id=m.get("conditionId", ""), question=m.get("question", ""),
            yes_token=str(tokens[yes_i]), no_token=str(tokens[1 - yes_i]),
            best_bid=_f(m.get("bestBid")), best_ask=_f(m.get("bestAsk")),
            end_date=str(m.get("endDate") or "")[:10],
        ))
    if len(out) < 2:
        return None
    return out, augmented


def candidate_baskets(ev: dict, slack: float) -> list[Basket]:
    parsed = parse_event(ev)
    if not parsed:
        return []
    markets, augmented = parsed
    title = str(ev.get("title") or ev.get("slug") or "")
    out = []
    asks = [m.best_ask for m in markets]
    bids = [m.best_bid for m in markets]
    if not augmented and all(a is not None for a in asks) and sum(asks) < 1 + slack:
        out.append(Basket(title, "YES", markets, 1.0))
    if all(b is not None for b in bids) and sum(bids) > 1 - slack:
        out.append(Basket(title, "NO", markets, float(len(markets) - 1)))
    return out


def best_sizing(books: list[OrderBook], fee_rates: list[float], payout: float,
                max_sets: float, per_leg_cap: float, min_edge: float) -> Sizing | None:
    """Jumlah set dengan profit terbesar. Profit linear per potongan, jadi cukup
    cek di titik patah (kumulatif size tiap level) + batas cap per leg."""
    min_size = max(b.min_order_size for b in books)
    cands = {max_sets}
    for b in books:
        cands.update(c for c in b.cumulative_ask_sizes() if c <= max_sets)
        # batas cap: share terbanyak yang total-nya masih <= cap
        acc_cost, acc_sh = 0.0, 0.0
        for lvl in b.asks:
            room = per_leg_cap - acc_cost
            if room <= 0:
                break
            take = min(lvl.size, room / (lvl.price * 1.1))   # 10% buffer buat fee
            acc_cost += take * lvl.price * 1.1
            acc_sh += take
            if take < lvl.size:
                break
        cands.add(acc_sh)
    best = None
    for s in sorted(c for c in cands if c >= min_size):
        s = round(s, 2)
        total, ok = 0.0, True
        for b, r in zip(books, fee_rates):
            fill = b.buy(s, r)
            if fill is None or fill.total > per_leg_cap:
                ok = False
                break
            total += fill.total
        if not ok:
            continue
        profit = payout * s - total
        if profit / s >= min_edge and (best is None or profit > best.profit):
            best = Sizing(s, total, profit)
    return best


class Arbitrage:
    name = NAME

    def __init__(self, settings: Settings, api: PolymarketAPI, db: DB):
        self.s = settings
        self.cfg = settings.arbitrage
        self.api = api
        self.db = db

    def opportunities(self, log=print) -> list[tuple[Basket, Sizing, list[OrderBook], list[float]]]:
        """Basket yang untung setelah fee & sapu book. Gak nyentuh DB."""
        cfg = self.cfg
        baskets = []
        for ev in self.api.iter_events(cfg.max_events):
            baskets.extend(candidate_baskets(ev, cfg.prefilter_slack))
        log(f"[arbitrage] {len(baskets)} basket lolos prefilter")
        out = []
        for bk in baskets:
            tokens = [bk.token(m) for m in bk.markets]
            books = self.api.books(tokens)
            if len(books) != len(tokens):
                continue
            rates = []
            for m in bk.markets:
                info = self.api.market(m.condition_id) or {}
                rates.append(rate_from_bps(info.get("taker_base_fee"), self.s.fees.rate_override))
            ordered = [books[t] for t in tokens]
            sz = best_sizing(ordered, rates, bk.payout_per_set, cfg.max_sets,
                             self.s.risk.max_order_usd, cfg.min_edge_per_set)
            if sz is not None:
                out.append((bk, sz, ordered, rates))
        self._last_scan = {"ts": time.time(), "prefilter": len(baskets), "profitable": len(out)}
        return out

    def decisions(self, log=print) -> list[Decision]:
        out = []
        opps = self.opportunities(log)
        # Buat dashboard: biar kelihatan scan-nya jalan walau hasilnya 0.
        self.db.set_meta("arb:last_scan_result", json.dumps(self._last_scan))
        for bk, sz, books, rates in opps:
            # Satu basket per event per 10 menit, biar gak nembak berulang di book yang sama.
            key = (f"arb:{bk.side}:{bk.markets[0].condition_id}:{len(bk.markets)}:"
                   f"{int(time.time() // 600)}")
            sid = self.db.add_signal(
                NAME, key, condition_id=bk.markets[0].condition_id, market=bk.event,
                edge_est=sz.edge_per_set, decision="take",
                reason=f"{bk.side} basket {len(bk.markets)} leg, {sz.sets:.2f} set, "
                       f"profit ${sz.profit:.2f}",
                extra={"side": bk.side, "payout": bk.payout_per_set, "cost": sz.cost})
            if sid is None:
                continue
            legs = [Leg(m.condition_id, bk.token(m), bk.side, m.question, sz.sets, b, r, m.end_date)
                    for m, b, r in zip(bk.markets, books, rates)]
            out.append(Decision(NAME, legs, sid,
                                f"{bk.side} basket '{bk.event[:40]}' +${sz.profit:.2f}"))
        return out
