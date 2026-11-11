"""
Scorecard + GERBANG PROMOSI ke live.

Syarat lulus (default, atur via GATE_*):
  - minimal 200 group sudah settle (paper)
  - t-stat PnL per group >= 2
  - batas bawah CI 95% bootstrap ROI > 0
Semua dihitung SETELAH fee dan harga hasil menyapu book. Kalau strategi gak
lulus, `npb promote` nolak — gak ada jalan pintas lewat config.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import GateSettings
from .db import DB
from .stats import bootstrap_ratio_ci, mean, t_stat


@dataclass
class Bucket:
    lo: float
    hi: float
    n: int
    avg_price: float
    win_rate: float
    pnl: float


@dataclass
class Score:
    strategy: str
    mode: str
    n_total: int
    n_resolved: int
    staked: float
    pnl: float
    t: float
    roi_ci: tuple[float, float]
    buckets: list[Bucket] = field(default_factory=list)
    passed: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def roi(self) -> float:
        return self.pnl / self.staked if self.staked else 0.0


def score(db: DB, strategy: str, gate: GateSettings, mode: str = "paper") -> Score:
    groups = db.groups(strategy=strategy, mode=mode)
    done = [g for g in groups if g.resolved]
    pnls = [g.pnl or 0.0 for g in done]
    costs = [g.total_cost for g in done]
    sc = Score(strategy, mode, len(groups), len(done), sum(costs), sum(pnls), t_stat(pnls),
               bootstrap_ratio_ci(pnls, costs))

    # Kalibrasi per harga (relevan buat strategi 1 leg): win rate vs harga rata-rata.
    # Kalau win rate ≈ harga di semua bucket, strategi gak punya informasi lebih
    # dari pasar — persis temuan di data polybot.
    single = [g for g in done if g.legs == 1 and g.shares]
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        b = [g for g in single if lo <= g.avg_price < hi]
        if b:
            sc.buckets.append(Bucket(lo, hi, len(b), mean([g.avg_price for g in b]),
                                     sum(1 for g in b if (g.payout or 0) > 0) / len(b),
                                     sum(g.pnl or 0 for g in b)))

    if sc.n_resolved < gate.min_resolved:
        sc.reasons.append(f"baru {sc.n_resolved} settle (butuh {gate.min_resolved})")
    if sc.t < gate.min_t_stat:
        sc.reasons.append(f"t-stat {sc.t:.2f} < {gate.min_t_stat}")
    if sc.roi_ci[0] <= gate.min_roi_ci_low:
        sc.reasons.append(f"CI bawah ROI {sc.roi_ci[0]:+.2%} <= {gate.min_roi_ci_low:+.2%}")
    sc.passed = not sc.reasons
    return sc


def format_score(sc: Score) -> str:
    lines = [
        f"[{sc.strategy} · {sc.mode}] {sc.n_resolved}/{sc.n_total} settle",
        f"  modal ${sc.staked:,.2f} · PnL ${sc.pnl:+,.2f} · ROI {sc.roi:+.2%} "
        f"(CI95 {sc.roi_ci[0]:+.2%} .. {sc.roi_ci[1]:+.2%}) · t {sc.t:.2f}",
    ]
    for b in sc.buckets:
        lines.append(f"    harga {b.lo:.1f}-{b.hi:.1f}: n={b.n:<5} harga rata2 {b.avg_price:.3f} "
                     f"menang {b.win_rate:.1%}  PnL ${b.pnl:+,.2f}")
    lines.append("  ✅ LULUS gerbang live" if sc.passed
                 else "  ⛔ belum lulus: " + "; ".join(sc.reasons))
    return "\n".join(lines)
