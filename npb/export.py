"""
Ringkasan buat dashboard web: satu file JSON dari isi database.

Workflow GitHub nulis ini ke branch `data` tiap beberapa menit; dashboard statis
(dashboard/index.html) tinggal fetch dari raw.githubusercontent.com.

Repo-nya public, jadi yang diekspor cuma data paper trading + setelan strategi.
Kunci / token / chat id TIDAK PERNAH ikut (Settings gak di-dump utuh).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict

from . import scorecard
from .config import Settings
from .db import DB
from .stats import mean

STRATEGIES = ("copytrade", "arbitrage")

# Kategori kasar dari judul market — buat lihat di mana strategi untung / rugi.
_CATEGORIES = (
    ("crypto up/down", re.compile(r"up or down", re.I)),
    ("crypto", re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|xrp|doge|crypto)\b", re.I)),
    ("esports", re.compile(r"\b(dota|counter-strike|cs2|lol:|league of legends|valorant|"
                           r"overwatch|rocket league|call of duty|r6|rainbow six|starcraft)\b", re.I)),
    ("politik", re.compile(r"\b(election|president|senate|governor|trump|parliament|"
                           r"prime minister|vote|party)\b", re.I)),
    ("olahraga", re.compile(r"(\bvs\.?\b|\bo/u\b|spread|\bwin\b|map \d|game \d|set \d|"
                            r"\bnba\b|\bnfl\b|\bnhl\b|\bmlb\b|premier league|open\b)", re.I)),
)


def category(title: str) -> str:
    for name, rx in _CATEGORIES:
        if rx.search(title or ""):
            return name
    return "lainnya"


def _json_meta(db: DB, key: str):
    try:
        return json.loads(db.get_meta(key, "") or "null")
    except ValueError:
        return None


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


def _pnl_curve(db: DB, mode: str = "paper", limit: int = 2000) -> list[list[float]]:
    """[[waktu settle, PnL kumulatif], ...] untuk group yang sudah settle."""
    points = [(t or 0.0, p) for t, p in db.settled_group_pnls(mode)]
    points.sort(key=lambda p: p[0])
    out, acc = [], 0.0
    for t, p in points:
        acc += p
        out.append([round(t), round(acc, 4)])
    return out[-limit:]


def _hourly(db: DB, now: float, hours: int = 48) -> list[dict]:
    """Jumlah sinyal per jam (ikut/skip) untuk `hours` jam terakhir."""
    start = (int(now) // 3600 - hours + 1) * 3600
    buckets = {start + i * 3600: {"take": 0, "skip": 0} for i in range(hours)}
    for r in db.query("SELECT ts, decision FROM signals WHERE ts >= ?", (start,)):
        b = buckets.get(int(r["ts"]) // 3600 * 3600)
        if b is not None and r["decision"] in b:
            b[r["decision"]] += 1
    return [{"t": t, **v} for t, v in sorted(buckets.items())]


def _skip_reason_key(reason: str) -> str:
    """Kelompokkan alasan skip: buang angka & detail ('slippage: kita 0.45…' -> 'slippage')."""
    return re.split(r"\s*-?[\d:(]", reason or "", maxsplit=1)[0].strip() or "lainnya"


def _skip_reasons(db: DB, now: float) -> list[dict]:
    counts: dict[str, int] = {}
    for r in db.query("SELECT reason FROM signals WHERE decision='skip' AND ts > ?", (now - 86400,)):
        k = _skip_reason_key(r["reason"])
        counts[k] = counts.get(k, 0) + 1
    return [{"reason": k, "n": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


# ── per group, lengkap dengan info sinyal (wallet trader, edge) ───────────────
def _groups_detail(db: DB) -> list[dict]:
    rows = db.query(
        "SELECT o.group_id, o.strategy, o.mode, MIN(o.ts) AS ts, MIN(o.market) AS market, "
        "MIN(o.outcome) AS outcome, MIN(o.signal_id) AS sid, MIN(o.condition_id) AS cid, "
        "MAX(o.end_date) AS end_date, COUNT(*) AS legs, SUM(o.shares) AS shares, "
        "SUM(o.cost) AS cost, SUM(o.fee) AS fee, MAX(o.limit_price) AS limit_price, "
        "SUM(COALESCE(json_extract(r.payouts, '$.\"' || o.token_id || '\"'), 0) * o.shares) "
        "AS payout, SUM(CASE WHEN r.condition_id IS NULL THEN 1 ELSE 0 END) AS open_legs, "
        "MAX(r.resolved_at) AS resolved_at "
        "FROM orders o LEFT JOIN resolutions r ON r.condition_id=o.condition_id "
        "WHERE o.status='filled' GROUP BY o.group_id ORDER BY ts DESC")
    sids = [r["sid"] for r in rows if r["sid"] is not None]
    sig: dict[int, dict] = {}
    for i in range(0, len(sids), 500):
        chunk = sids[i:i + 500]
        for s in db.query(f"SELECT id, ref_price, edge_est, extra FROM signals WHERE id IN "
                          f"({','.join('?' * len(chunk))})", tuple(chunk)):
            try:
                extra = json.loads(s["extra"] or "{}")
            except ValueError:
                extra = {}
            sig[s["id"]] = {"wallet": extra.get("wallet"), "edge_est": s["edge_est"],
                            "trader_price": s["ref_price"]}
    out = []
    for r in rows:
        total = r["cost"] + r["fee"]
        resolved = r["open_legs"] == 0
        s = sig.get(r["sid"], {})
        out.append({
            "group_id": r["group_id"], "ts": r["ts"], "strategy": r["strategy"], "mode": r["mode"],
            "legs": r["legs"],
            "market": r["market"] if r["legs"] == 1 else f"{r['legs']} leg basket",
            "outcome": r["outcome"] if r["legs"] == 1 else "",
            "condition_id": r["cid"] if r["legs"] == 1 else "",
            "category": category(r["market"]) if r["legs"] == 1 else "arbitrage",
            "shares": r["shares"], "avg_price": r["cost"] / r["shares"] if r["legs"] == 1 and r["shares"] else None,
            "limit_price": r["limit_price"], "cost": total, "fee": r["fee"],
            "end_date": r["end_date"] or "", "resolved": resolved,
            "resolved_at": r["resolved_at"] if resolved else None,
            "pnl": (r["payout"] - total) if resolved else None,
            "wallet": s.get("wallet"), "edge_est": s.get("edge_est"),
            "trader_price": s.get("trader_price"),
        })
    # Tandai market yang kita pegang DUA sisinya (bug lama: taruhan lawan diri sendiri).
    sides: dict[tuple, set] = {}
    for g in out:
        if g["condition_id"]:
            sides.setdefault((g["mode"], g["condition_id"]), set()).add(g["outcome"])
    for g in out:
        g["two_sided"] = len(sides.get((g["mode"], g["condition_id"]), ())) > 1
    return out


def _breakdown(groups: list[dict], key: str) -> list[dict]:
    by: dict[str, dict] = {}
    for g in groups:
        k = g.get(key) or "—"
        b = by.setdefault(k, {"key": k, "n": 0, "settled": 0, "wins": 0, "staked": 0.0,
                              "pnl": 0.0, "fee": 0.0, "open_cost": 0.0})
        b["n"] += 1
        b["fee"] += g["fee"]
        if g["resolved"]:
            b["settled"] += 1
            b["staked"] += g["cost"]
            b["pnl"] += g["pnl"]
            b["wins"] += g["pnl"] > 0
        else:
            b["open_cost"] += g["cost"]
    for b in by.values():
        b["roi"] = b["pnl"] / b["staked"] if b["staked"] else None
        b["win_rate"] = b["wins"] / b["settled"] if b["settled"] else None
    return sorted(by.values(), key=lambda b: b["pnl"])


def _summary(groups: list[dict], curve: list[list[float]]) -> dict:
    done = [g for g in groups if g["resolved"]]
    pnls = [g["pnl"] for g in done]
    peak = dd = 0.0
    for _, cum in curve:
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    streak, kind = 0, None
    for g in sorted(done, key=lambda g: g["resolved_at"] or 0, reverse=True):
        k = "menang" if g["pnl"] > 0 else "kalah"
        if kind in (None, k):
            kind, streak = k, streak + 1
        else:
            break
    staked = sum(g["cost"] for g in done)
    best = max(done, key=lambda g: g["pnl"], default=None)
    worst = min(done, key=lambda g: g["pnl"], default=None)
    pick = lambda g: g and {"market": g["market"], "outcome": g["outcome"], "pnl": g["pnl"], "ts": g["ts"]}
    edges = [g["edge_est"] for g in groups if g["edge_est"] is not None]
    return {
        "bets": len(groups), "settled": len(done), "open": len(groups) - len(done),
        "wins": sum(p > 0 for p in pnls), "losses": sum(p <= 0 for p in pnls),
        "win_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
        "staked": staked, "pnl": sum(pnls),
        "avg_stake": mean([g["cost"] for g in groups]) if groups else 0.0,
        "fees": sum(g["fee"] for g in groups),
        "fee_pct": sum(g["fee"] for g in groups) / sum(g["cost"] for g in groups) if groups else None,
        "avg_pnl": mean(pnls) if pnls else None,
        "avg_edge_est": mean(edges) if edges else None,
        "max_drawdown": dd, "streak": streak, "streak_kind": kind,
        "best": pick(best), "worst": pick(worst),
        "two_sided_markets": len({g["condition_id"] for g in groups if g["two_sided"]}),
    }


def _risk(db: DB, settings: Settings, now: float) -> dict:
    r = settings.risk
    out = {}
    for mode in ("paper", "live"):
        exp = db.open_exposure(r.zombie_days, mode=mode)
        realized = db.realized_pnl_since(now - 86400, mode=mode)
        try:
            mtm = json.loads(db.get_meta(f"mtm:{mode}", "") or "{}")
        except ValueError:
            mtm = {}
        fresh = bool(mtm) and now - float(mtm.get("ts", 0)) <= 3 * r.mtm_interval_min * 60
        unreal = float(mtm.get("unrealized", 0.0)) if fresh else 0.0
        loss = realized + min(0.0, unreal)
        markets = db.query(
            "SELECT o.condition_id, MIN(o.market) AS market, SUM(o.cost + o.fee) AS cost, "
            "COUNT(DISTINCT o.token_id) AS sides, COUNT(DISTINCT o.group_id) AS bets "
            "FROM orders o WHERE o.status='filled' AND o.mode=? AND NOT EXISTS "
            "(SELECT 1 FROM resolutions x WHERE x.condition_id=o.condition_id) "
            "GROUP BY o.condition_id ORDER BY cost DESC LIMIT 15", (mode,))
        out[mode] = {
            "exposure": exp, "max_exposure": r.max_exposure_usd,
            "free_bankroll": max(0.0, settings.bankroll_usd - exp),
            "realized_24h": realized, "unrealized": unreal if fresh else None,
            "mtm_ts": mtm.get("ts"), "mtm_positions": mtm.get("positions"),
            "loss_24h": loss, "loss_limit": r.daily_loss_limit_usd,
            "brake": loss < -r.daily_loss_limit_usd,
            "open_markets": [dict(m) for m in markets],
        }
    return out


def _traders(db: DB, followed: dict[str, float], ct) -> tuple[list[dict], float | None]:
    rows = db.trader_stats()
    need_t = ct.required_t() if ct else None
    out = []
    for r in rows[:60]:
        t = dict(r)
        t["followed"] = t["wallet"] in followed
        t["why_not"] = ct.follow_reasons(r, need_t) if ct else []
        out.append(t)
    # inf (gak ada yang lolos BH) gak valid di JSON -> None.
    return out, (need_t if need_t is not None and need_t != float("inf") else None)


def build(db: DB, settings: Settings, followed: dict[str, float] | None = None, ct=None) -> dict:
    r = settings.risk
    now = time.time()
    scores = []
    for name in STRATEGIES:
        for mode in ("paper", "live"):
            sc = scorecard.score(db, name, settings.gate, mode)
            d = asdict(sc)
            d["roi"] = sc.roi
            scores.append(d)

    groups = _groups_detail(db)
    paper = [g for g in groups if g["mode"] == "paper"]
    curve = _pnl_curve(db)

    signals = [dict(s) for s in db.query(
        "SELECT ts, strategy, decision, reason, market, outcome, ref_price, ask, mid, edge_est, extra "
        "FROM signals ORDER BY ts DESC LIMIT 300")]
    for s in signals:
        try:
            s["wallet"] = json.loads(s.pop("extra") or "{}").get("wallet")
        except ValueError:
            s["wallet"] = None
    counts = {f"{x['strategy']}:{x['decision']}": x["n"] for x in db.query(
        "SELECT strategy, decision, COUNT(*) AS n FROM signals WHERE ts > ? "
        "GROUP BY strategy, decision", (now - 86400,))}
    followed = followed or {}
    traders, need_t = _traders(db, followed, ct)
    errors = [dict(e) for e in db.query(
        "SELECT ts, level, source, message FROM log ORDER BY ts DESC LIMIT 40")]

    return {
        "generated_at": now,
        "last_cycle": float(db.get_meta("last_cycle", "0") or 0),
        "mode": settings.mode,
        "live_requested": settings.live_requested,
        "killed": db.killed(),
        "promoted": {n: db.promoted(n) for n in STRATEGIES},
        "risk": asdict(r),
        "gate": asdict(settings.gate),
        "settings": {
            "bankroll_usd": settings.bankroll_usd,
            "loop_interval_sec": settings.loop_interval_sec,
            "paper_refresh_book": settings.paper_refresh_book,
            "fee_rate_override": settings.fees.rate_override,
            "copytrade": {k: v for k, v in asdict(settings.copytrade).items() if k != "manual_wallets"}
                         | {"manual_wallets": len(settings.copytrade.manual_wallets)},
            "arbitrage": asdict(settings.arbitrage),
        },
        "exposure": {m: db.open_exposure(r.zombie_days, mode=m) for m in ("paper", "live")},
        "pnl_24h": {m: db.realized_pnl_since(now - 86400, mode=m) for m in ("paper", "live")},
        "risk_state": _risk(db, settings, now),
        "summary": _summary(paper, curve),
        "by_category": _breakdown(paper, "category"),
        "by_wallet": _breakdown([g for g in paper if g["strategy"] == "copytrade"], "wallet"),
        "scores": scores,
        "bets": [g for g in groups if g["mode"] in ("paper", "live")][:300],
        "archives": db.archives(),
        "signals": signals,
        "signal_counts_24h": counts,
        "drift": _drift(db),
        "pnl_curve": curve,
        "hourly": _hourly(db, now),
        "skip_reasons_24h": _skip_reasons(db, now),
        "traders": traders,
        "traders_required_t": need_t,
        "traders_measured_at": float(db.get_meta("ct:stats_at", "0") or 0),
        "last_signal_ts": (db.query("SELECT MAX(ts) AS t FROM signals")[0]["t"]),
        "last_take_ts": (db.query("SELECT MAX(ts) AS t FROM signals WHERE decision='take'")[0]["t"]),
        "followed_count": len(followed),
        "arb_last_scan": _json_meta(db, "arb:last_scan_result"),
        "traders_progress": [int(db.get_meta("ct:pool_i", "0") or 0),
                             len(json.loads(db.get_meta("ct:pool", "[]") or "[]"))],
        "errors": errors,
    }


def write(path: str, db: DB, settings: Settings, followed: dict[str, float] | None = None,
          ct=None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build(db, settings, followed, ct), f, ensure_ascii=False, separators=(",", ":"))
