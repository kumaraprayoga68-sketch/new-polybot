"""
Ringkasan buat dashboard web: satu file JSON dari isi database.

Workflow GitHub nulis ini ke branch `data` tiap beberapa menit; dashboard statis
(dashboard/index.html) tinggal fetch dari raw.githubusercontent.com.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict

from . import scorecard
from .config import Settings
from .db import DB
from .stats import mean

STRATEGIES = ("copytrade", "arbitrage")


def _drift(db: DB) -> list[dict]:
    """Rata-rata perubahan mid setelah sinyal, dipisah take vs skip."""
    rows = db.query(
        "SELECT s.strategy, s.decision, m.horizon_sec, m.mid - s.mid AS d FROM marks m "
        "JOIN signals s ON s.id=m.signal_id WHERE m.mid IS NOT NULL AND s.mid IS NOT NULL")
    by: dict[tuple, list[float]] = {}
    for r in rows:
        by.setdefault((r["strategy"], r["decision"], r["horizon_sec"]), []).append(r["d"])
    return [{"strategy": k[0], "decision": k[1], "horizon_h": k[2] / 3600, "n": len(v),
             "avg_drift": mean(v)} for k, v in sorted(by.items())]


def build(db: DB, settings: Settings, followed: dict[str, float] | None = None) -> dict:
    r = settings.risk
    now = time.time()
    scores = []
    for name in STRATEGIES:
        for mode in ("paper", "live"):
            sc = scorecard.score(db, name, settings.gate, mode)
            d = asdict(sc)
            d["roi"] = sc.roi
            scores.append(d)

    groups = sorted(db.groups(), key=lambda g: g.ts, reverse=True)[:150]
    bets = []
    for g in groups:
        legs = db.query("SELECT market, outcome FROM orders WHERE group_id=? AND status='filled'",
                        (g.group_id,))
        bets.append({
            "ts": g.ts, "strategy": g.strategy, "mode": g.mode, "legs": g.legs,
            "market": legs[0]["market"] if len(legs) == 1 else f"{g.legs} leg basket",
            "outcome": legs[0]["outcome"] if legs else "",
            "avg_price": g.avg_price if g.legs == 1 else None,
            "cost": g.total_cost, "fee": g.fee, "end_date": g.end_date,
            "resolved": g.resolved, "pnl": g.pnl,
        })

    signals = [dict(s) for s in db.query(
        "SELECT ts, strategy, decision, reason, market, outcome, ref_price, ask, mid, edge_est "
        "FROM signals ORDER BY ts DESC LIMIT 200")]
    counts = {f"{x['strategy']}:{x['decision']}": x["n"] for x in db.query(
        "SELECT strategy, decision, COUNT(*) AS n FROM signals WHERE ts > ? "
        "GROUP BY strategy, decision", (now - 86400,))}
    traders = [dict(t) for t in db.trader_stats()[:30]]
    followed = followed or {}
    for t in traders:
        t["followed"] = t["wallet"] in followed
    errors = [dict(e) for e in db.query(
        "SELECT ts, level, source, message FROM log ORDER BY ts DESC LIMIT 25")]

    return {
        "generated_at": now,
        "last_cycle": float(db.get_meta("last_cycle", "0") or 0),
        "mode": settings.mode,
        "live_requested": settings.live_requested,
        "killed": db.killed(),
        "promoted": {n: db.promoted(n) for n in STRATEGIES},
        "risk": asdict(r),
        "gate": asdict(settings.gate),
        "exposure": {m: db.open_exposure(r.zombie_days, mode=m) for m in ("paper", "live")},
        "pnl_24h": {m: db.realized_pnl_since(now - 86400, mode=m) for m in ("paper", "live")},
        "scores": scores,
        "bets": bets,
        "signals": signals,
        "signal_counts_24h": counts,
        "drift": _drift(db),
        "traders": traders,
        "traders_measured_at": float(db.get_meta("ct:stats_at", "0") or 0),
        "traders_progress": [int(db.get_meta("ct:pool_i", "0") or 0),
                             len(json.loads(db.get_meta("ct:pool", "[]") or "[]"))],
        "errors": errors,
    }


def write(path: str, db: DB, settings: Settings, followed: dict[str, float] | None = None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build(db, settings, followed), f, ensure_ascii=False, separators=(",", ":"))
