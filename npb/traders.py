"""
Ngukur SKILL trader dengan cara yang bener: edge per share vs HARGA BELI MEREKA.

polybot pakai win rate. Itu nyesatin: menang 80% tapi selalu beli di 0.90 = rugi.
Di sini, per market yang sudah settle:
    edge = (payout share yang dibeli - biaya beli) / jumlah share
           = rata-rata (hasil 0/1 - harga beli)
Trader layak diikuti kalau edge rata-rata antar market positif DAN signifikan
(t-stat), dihitung dari cukup banyak market. Lalu edge di-shrink ke 0 (Bayes
kasar) biar trader dengan sampel kecil gak kelihatan jago karena hoki.

Batasan yang diterima: jual sebelum settle diabaikan (yang dinilai cuma
keputusan BELI). Market maker bakal kelihatan edge ~0 — dan memang gak layak
dicopy.
"""
from __future__ import annotations

from dataclasses import dataclass

from .api import PolymarketAPI, payouts
from .db import DB
from .stats import mean, t_stat


@dataclass
class TraderSkill:
    wallet: str
    markets: int
    mean_edge: float
    t: float
    shrunk_edge: float


def edge_per_market(trades: list[dict], resolution: dict[str, dict[str, float]]) -> list[float]:
    """Edge per share untuk tiap market settle yang trader BELI."""
    agg: dict[str, list[float]] = {}   # cid -> [shares, cost, payout]
    for t in trades:
        if str(t.get("side", "")).upper() != "BUY":
            continue
        cid = t.get("condition_id") or t.get("conditionId")
        tok = str(t.get("token_id") or t.get("asset") or "")
        res = resolution.get(cid or "")
        if not cid or not tok or res is None:
            continue
        try:
            size, price = float(t["size"]), float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if size <= 0 or not 0 < price < 1:
            continue
        a = agg.setdefault(cid, [0.0, 0.0, 0.0])
        a[0] += size
        a[1] += size * price
        a[2] += size * res.get(tok, 0.0)
    return [(pay - cost) / sh for sh, cost, pay in agg.values() if sh > 0]


def skill_from_edges(wallet: str, edges: list[float], shrink_k: float) -> TraderSkill:
    n = len(edges)
    m = mean(edges)
    return TraderSkill(wallet, n, m, t_stat(edges), m * n / (n + shrink_k) if n else 0.0)


def resolve_cids(api: PolymarketAPI, db: DB, cids: list[str], budget: int) -> dict[str, dict[str, float]]:
    """Ambil payout market (cache di DB). Maksimal `budget` panggilan API baru."""
    out: dict[str, dict[str, float]] = {}
    calls = 0
    for cid in cids:
        cached = db.resolution(cid)
        if cached is not None:
            out[cid] = cached
            continue
        if calls >= budget:
            continue
        calls += 1
        p = payouts(api.market(cid))
        if p is not None:
            db.set_resolution(cid, p)
            out[cid] = p
    return out


def measure(api: PolymarketAPI, db: DB, wallet: str, shrink_k: float,
            max_lookups: int) -> TraderSkill:
    trades = api.user_trades_all(wallet, max_rows=1500)
    # market paling lama dulu: paling mungkin sudah settle.
    seen: dict[str, float] = {}
    for t in trades:
        cid = t.get("condition_id") or t.get("conditionId")
        if cid and str(t.get("side", "")).upper() == "BUY":
            seen[cid] = min(seen.get(cid, 9e18), float(t.get("timestamp") or 0))
    cids = sorted(seen, key=seen.get)
    res = resolve_cids(api, db, cids, max_lookups)
    skill = skill_from_edges(wallet, edge_per_market(trades, res), shrink_k)
    db.upsert_trader_stats(wallet, skill.markets, skill.mean_edge, skill.t, skill.shrunk_edge)
    return skill
