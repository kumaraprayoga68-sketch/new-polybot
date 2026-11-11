"""
Executor: SATU-SATUNYA jalan dari keputusan strategi ke order.

Urutan cek sebelum order (semua berlaku juga di paper, biar paper = latihan live):
  1. kill switch
  2. hard cap per order (MAX_ORDER_USD)
  3. plafon eksposur total + per market
  4. batas rugi 24 jam (realized + belum-realized dari mark-to-market runner)
Mode live cuma kalau: NPB_MODE=live + LIVE_TRADING_ENABLED=true + strategi itu
sudah di-PROMOTE (lulus gerbang statistik). Strategi yang belum lulus tetap jalan
di paper walau bot mode live — jadi bukti terus terkumpul.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .broker import Leg, LegResult, PaperBroker
from .config import Settings
from .db import DB, new_group_id


@dataclass
class Decision:
    strategy: str
    legs: list[Leg]
    signal_id: int | None = None
    note: str = ""


@dataclass
class ExecResult:
    status: str                      # "filled" | "rejected" | "partial" | "blocked"
    mode: str
    group_id: str = ""
    reason: str = ""
    legs: list[LegResult] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(l.cost + l.fee for l in self.legs if l.status == "filled")


class Executor:
    def __init__(self, settings: Settings, db: DB, live_broker=None, notify=None, api=None):
        self.s = settings
        self.db = db
        self.paper = PaperBroker(api if settings.paper_refresh_book else None)
        self.live = live_broker
        self.notify = notify or (lambda msg: None)

    def mode_for(self, strategy: str) -> str:
        if self.s.live_requested and self.live is not None and self.db.promoted(strategy):
            return "live"
        return "paper"

    def check_risk(self, d: Decision, mode: str) -> str | None:
        """Alasan penolakan, atau None kalau boleh."""
        r = self.s.risk
        if (why := self.db.killed()):
            return f"kill switch aktif: {why}"
        est = []
        for leg in d.legs:
            fill = leg.book.buy(leg.shares, leg.fee_rate)
            if fill is None:
                return f"depth gak cukup buat {leg.shares:.2f} share {leg.outcome}"
            if fill.total > r.max_order_usd + 1e-9:
                return f"leg ${fill.total:.2f} > hard cap ${r.max_order_usd:.2f}"
            est.append((leg, fill.total))
        total = sum(t for _, t in est)
        exposure = self.db.open_exposure(r.zombie_days, mode=mode)
        if exposure + total > r.max_exposure_usd + 1e-9:
            return f"eksposur ${exposure:.2f} + ${total:.2f} > plafon ${r.max_exposure_usd:.2f}"
        per_market: dict[str, float] = {}
        for leg, t in est:
            per_market[leg.condition_id] = per_market.get(leg.condition_id, 0.0) + t
        for cid, t in per_market.items():
            if self.db.market_exposure(cid, mode=mode) + t > r.max_market_usd + 1e-9:
                return f"market {cid[:10]}… lewat plafon per market ${r.max_market_usd:.2f}"
        # Beli sisi lawan dari posisi sendiri = taruhan saling batal, yang pasti
        # hilang cuma fee + spread. Kejadian di paper: dua trader yang diikuti
        # pasang di sisi berlawanan (MOUZ vs G2), bot ngikutin dua-duanya.
        for leg in d.legs:
            held = self.db.open_tokens(leg.condition_id, mode=mode) - {leg.token_id}
            if held:
                return f"udah pegang sisi lawan di market {leg.condition_id[:10]}…"
        realized = self.db.realized_pnl_since(time.time() - 86400, mode=mode)
        unrealized = min(0.0, self.unrealized(mode))
        if realized + unrealized < -r.daily_loss_limit_usd:
            return (f"rugi 24 jam ${-(realized + unrealized):.2f} (realized ${-realized:.2f} + "
                    f"posisi terbuka ${-unrealized:.2f}) > batas ${r.daily_loss_limit_usd:.2f}")
        return None

    def unrealized(self, mode: str) -> float:
        """PnL belum-realized posisi terbuka dari mark-to-market terakhir runner.
        0 kalau belum ada / basi (> 3x interval) — gak nebak."""
        try:
            m = json.loads(self.db.get_meta(f"mtm:{mode}", "") or "{}")
        except ValueError:
            return 0.0
        max_age = 3 * self.s.risk.mtm_interval_min * 60
        if not m or time.time() - float(m.get("ts", 0)) > max_age:
            return 0.0
        return float(m.get("unrealized", 0.0))

    def submit(self, d: Decision) -> ExecResult:
        mode = self.mode_for(d.strategy)
        why = self.check_risk(d, mode)
        if why:
            return ExecResult("blocked", mode, reason=why)
        broker = self.live if mode == "live" else self.paper
        gid = new_group_id()
        results: list[LegResult] = []
        for i, leg in enumerate(d.legs):
            res = broker.buy(leg)
            results.append(res)
            self.db.add_order(
                strategy=d.strategy, mode=mode, group_id=gid, signal_id=d.signal_id,
                condition_id=leg.condition_id, token_id=leg.token_id, outcome=leg.outcome,
                market=leg.market, end_date=leg.end_date, shares_req=leg.shares,
                limit_price=res.limit_price, status=res.status, shares=res.shares,
                cost=res.cost, fee=res.fee, error=res.error)
            if res.status != "filled":
                if i == 0:
                    return ExecResult("rejected", mode, gid, res.error, results)
                # Leg pertama udah ke-fill, sisanya gagal: posisi gak lagi ter-hedge.
                # Jangan coba "beresin sendiri" — stop semua & minta cek manual.
                msg = (f"{d.strategy}: leg {i + 1}/{len(d.legs)} gagal setelah leg lain "
                       f"ke-fill ({res.error}). Posisi TIDAK ter-hedge. "
                       + ("Bot di-STOP." if mode == "live" else "(paper: dicatat, bot jalan terus)"))
                if mode == "live":
                    self.db.set_meta("kill", msg)
                self.db.log("error", "executor", msg)
                self.notify(f"🛑 {msg}")
                return ExecResult("partial", mode, gid, msg, results)
        return ExecResult("filled", mode, gid, d.note, results)
