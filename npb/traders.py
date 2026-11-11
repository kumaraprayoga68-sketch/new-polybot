"""
Ngukur SKILL trader dengan cara yang bener: edge per share vs HARGA BELI MEREKA.

polybot pakai win rate. Itu nyesatin: menang 80% tapi selalu beli di 0.90 = rugi.
Di sini, per market yang sudah settle:
    edge = (payout share yang dibeli - biaya beli) / jumlah share
           = rata-rata (hasil 0/1 - harga beli)
Trader layak diikuti kalau edge rata-rata antar market positif DAN signifikan
(t-stat), dihitung dari cukup banyak market. Lalu edge di-shrink ke 0 (Bayes
kasar) biar trader dengan sampel kecil gak kelihatan jago karena hoki.

Dua pengaman tambahan terhadap "kelihatan jago karena hoki":
  * HOLDOUT: market dipisah berdasar waktu. 2/3 lama dipakai buat nilai,
    1/3 terbaru disimpan. Edge di bagian terbaru harus tetap positif — kalau
    cuma bagus di masa lalu, kemungkinan besar itu hoki yang sudah kepilih.
  * Koreksi multiple testing ada di copytrade.followed(): makin banyak kandidat
    yang diuji, makin tinggi t yang dibutuhkan.

Edge juga disimpan dalam bentuk TERNORMALISASI: (hasil - harga) / sqrt(p(1-p)).
Edge absolut +0,05 di harga 0,50 itu biasa; di harga 0,95 hampir mustahil.
Dengan normalisasi, edge trader bisa dipakai di harga berapa pun:
    p_est = harga + edge_norm * sqrt(harga * (1 - harga)).

Batasan yang diterima: jual sebelum settle diabaikan (yang dinilai cuma
keputusan BELI). Market maker bakal kelihatan edge ~0 — dan memang gak layak
dicopy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .api import PolymarketAPI, payouts
from .db import DB
from .stats import mean, t_stat

HOLDOUT_FRAC = 1 / 3


@dataclass
class MarketResult:
    first_ts: float
    edge: float          # per share, absolut
    norm_edge: float     # edge / sqrt(p(1-p)) di harga rata-rata trader


@dataclass
class TraderSkill:
    wallet: str
    markets: int
    mean_edge: float
    t: float
    shrunk_edge: float
    norm_edge: float = 0.0       # shrunk, ternormalisasi (dipakai buat p_est)
    holdout_n: int = 0
    holdout_edge: float = 0.0    # edge absolut rata-rata di market terbaru


def market_results(trades: list[dict], resolution: dict[str, dict[str, float]]) -> list[MarketResult]:
    """Hasil per market settle yang trader BELI, urut dari market paling lama."""
    agg: dict[str, list[float]] = {}   # cid -> [shares, cost, payout, first_ts]
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
        try:
            ts = float(t.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        a = agg.setdefault(cid, [0.0, 0.0, 0.0, ts])
        a[0] += size
        a[1] += size * price
        a[2] += size * res.get(tok, 0.0)
        a[3] = min(a[3], ts)
    out = []
    for sh, cost, pay, ts in agg.values():
        if sh <= 0:
            continue
        p = cost / sh
        e = (pay - cost) / sh
        out.append(MarketResult(ts, e, e / math.sqrt(max(p * (1 - p), 1e-4))))
    out.sort(key=lambda r: r.first_ts)
    return out


def edge_per_market(trades: list[dict], resolution: dict[str, dict[str, float]]) -> list[float]:
    """Edge per share untuk tiap market settle yang trader BELI."""
    return [r.edge for r in market_results(trades, resolution)]


def _shrink(m: float, n: int, k: float) -> float:
    return m * n / (n + k) if n else 0.0


def skill_from_edges(wallet: str, edges: list[float], shrink_k: float) -> TraderSkill:
    n = len(edges)
    m = mean(edges)
    return TraderSkill(wallet, n, m, t_stat(edges), _shrink(m, n, shrink_k))


def skill_from_results(wallet: str, results: list[MarketResult], shrink_k: float,
                       holdout_frac: float = HOLDOUT_FRAC) -> TraderSkill:
    """Skill dari hasil per market (urut waktu). t-stat dihitung di bagian LAMA saja,
    bagian terbaru jadi holdout — jadi seleksi dan validasi gak pakai data yang sama."""
    n = len(results)
    n_hold = int(n * holdout_frac)
    train, hold = results[:n - n_hold], results[n - n_hold:]
    edges = [r.edge for r in train]
    sk = skill_from_edges(wallet, edges, shrink_k)
    sk.markets = n
    sk.norm_edge = _shrink(mean([r.norm_edge for r in train]), len(train), shrink_k)
    sk.holdout_n = len(hold)
    sk.holdout_edge = mean([r.edge for r in hold])
    return sk


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
    skill = skill_from_results(wallet, market_results(trades, res), shrink_k)
    db.upsert_trader_stats(wallet, skill.markets, skill.mean_edge, skill.t, skill.shrunk_edge,
                           norm_edge=skill.norm_edge, holdout_n=skill.holdout_n,
                           holdout_edge=skill.holdout_edge)
    return skill
