"""
Satu siklus bot: strategi -> executor -> settle -> ukur drift -> laporan.

Setiap langkah dibungkus sendiri: satu API mati gak boleh matiin langkah lain,
TAPI error-nya harus kelihatan (log DB + Telegram, diredam 1 jam per sumber).
"""
from __future__ import annotations

import threading
import time

from . import scorecard
from .api import PolymarketAPI, payouts
from .config import CLOB_URL, Settings
from .db import DB
from .executor import Executor
from .notify import Telegram
from .strategies.arbitrage import Arbitrage
from .strategies.copytrade import Copytrade

MARK_HORIZONS = (3600, 6 * 3600)


class Bot:
    def __init__(self, settings: Settings, api: PolymarketAPI | None = None,
                 db: DB | None = None, telegram: Telegram | None = None, log=print):
        self.s = settings
        self.api = api or PolymarketAPI()
        self.db = db or DB(settings.db_path)
        self.tg = telegram or Telegram(settings.telegram_token, settings.telegram_chat_id)
        self.log = log
        live = None
        if settings.live_requested:
            from .broker import LiveBroker

            live = LiveBroker(CLOB_URL, settings.private_key, settings.funder_address,
                              settings.chain_id, settings.signature_type)
        self.executor = Executor(settings, self.db, live, self.tg.send)
        self.copytrade = Copytrade(settings, self.api, self.db)
        self.arbitrage = Arbitrage(settings, self.api, self.db)
        self._alerted: dict[str, float] = {}
        self.stop_event = threading.Event()

    # ── util ────────────────────────────────────────────────────────────────
    def _error(self, source: str, e: Exception) -> None:
        msg = f"{source}: {e}"
        self.log(f"⚠️  {msg}")
        self.db.log("error", source, msg)
        if time.time() - self._alerted.get(source, 0) > 3600:
            self._alerted[source] = time.time()
            self.tg.send(f"🚨 {msg}")

    def _step(self, source: str, fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 — termasuk ApiError
            self._error(source, e)
            return None

    # ── langkah ─────────────────────────────────────────────────────────────
    def run_decisions(self, strategy) -> list[str]:
        lines = []
        for d in strategy.decisions(log=self.log):
            res = self.executor.submit(d)
            tag = "🟢" if res.status == "filled" else ("🛑" if res.status == "partial" else "⏭️")
            line = f"{tag} [{res.mode}] {d.note} -> {res.status}"
            if res.status == "filled":
                line += f" ${res.total:.2f}"
            elif res.reason:
                line += f" ({res.reason})"
            self.log(line)
            if res.status in ("filled", "partial"):
                lines.append(line)
        return lines

    def resolve(self, budget: int = 300) -> int:
        n = 0
        for cid in self.db.unresolved_condition_ids()[:budget]:
            p = payouts(self.api.market(cid, fresh=True))
            if p is not None:
                self.db.set_resolution(cid, p)
                n += 1
        return n

    def mark(self) -> int:
        n = 0
        for h in MARK_HORIZONS:
            for sig in self.db.signals_needing_mark(h):
                self.db.add_mark(sig["id"], h, self.api.midpoint(sig["token_id"]))
                n += 1
        return n

    def auto_demote(self) -> None:
        """Strategi live yang jelas rugi dicabut promosinya otomatis."""
        for name in ("copytrade", "arbitrage"):
            if not self.db.promoted(name):
                continue
            sc = scorecard.score(self.db, name, self.s.gate, mode="live")
            if sc.n_resolved >= 30 and sc.roi_ci[1] < 0:
                self.db.set_meta(f"promoted:{name}", None)
                msg = f"{name} di-DEMOTE otomatis: live ROI CI95 atas {sc.roi_ci[1]:+.2%} < 0"
                self.db.log("warn", "gate", msg)
                self.tg.send(f"⚠️ {msg}")

    def cycle(self) -> None:
        lines: list[str] = []
        killed = self.db.killed()
        if killed:
            self.log(f"⛔ kill switch aktif ({killed}) — cuma settle & ukur.")
        else:
            if self.s.arbitrage.enabled:
                last = float(self.db.get_meta("arb:last_scan", "0") or 0)
                if time.time() - last >= self.s.arbitrage.scan_interval_min * 60:
                    self.db.set_meta("arb:last_scan", str(time.time()))
                    lines += self._step("arbitrage", self.run_decisions, self.arbitrage) or []
            if self.s.copytrade.enabled:
                lines += self._step("copytrade", self.run_decisions, self.copytrade) or []
                # Dicicil di akhir siklus, biar gak nahan sinyal dari trader yang udah diikuti.
                if self.copytrade.stats_stale():
                    self._step("copytrade.stats", self.copytrade.refresh_stats, self.log)
        settled = self._step("resolve", self.resolve) or 0
        self._step("marks", self.mark)
        self._step("gate", self.auto_demote)
        self.db.set_meta("last_cycle", str(time.time()))
        if settled:
            self.log(f"✔️  {settled} market settle")
        if lines:
            self.tg.send("\n".join(lines[:20]) + (f"\n… +{len(lines) - 20}" if len(lines) > 20 else ""))

    def loop(self) -> None:
        self.log(f"▶️  loop tiap {self.s.loop_interval_sec}s · mode {self.s.mode} "
                 f"(live {'MINTA' if self.s.live_requested else 'mati'})")
        while not self.stop_event.is_set():
            started = time.time()
            self._step("cycle", self.cycle)
            self.stop_event.wait(max(1.0, self.s.loop_interval_sec - (time.time() - started)))

    # ── laporan ─────────────────────────────────────────────────────────────
    def status(self) -> str:
        r = self.s.risk
        lines = [
            f"mode: {self.s.mode} · live diminta: {self.s.live_requested} · "
            f"broker live: {'ada' if self.executor.live else 'tidak'}",
            f"kill switch: {self.db.killed() or 'mati'}",
        ]
        for mode in ("paper", "live"):
            exp = self.db.open_exposure(r.zombie_days, mode=mode)
            day = self.db.realized_pnl_since(time.time() - 86400, mode=mode)
            lines.append(f"{mode}: eksposur ${exp:.2f}/{r.max_exposure_usd:.0f} · PnL 24j ${day:+.2f}")
        for name in ("copytrade", "arbitrage"):
            lines.append(f"{name}: jalan di {self.executor.mode_for(name)}"
                         f"{' (PROMOTED)' if self.db.promoted(name) else ''}")
        lines.append(f"trader diikuti: {len(self.copytrade.followed())}")
        return "\n".join(lines)

    def telegram_handlers(self) -> dict:
        def score(_):
            return "\n\n".join(scorecard.format_score(scorecard.score(self.db, n, self.s.gate, m))
                               for n in ("copytrade", "arbitrage") for m in ("paper", "live"))

        def kill(args):
            self.db.set_meta("kill", " ".join(args) or "manual dari Telegram")
            return "⛔ kill switch NYALA. Order baru berhenti. /resume buat lanjut."

        def resume(_):
            self.db.set_meta("kill", None)
            return "▶️ kill switch mati."

        def traders(_):
            rows = self.db.trader_stats()[:15]
            return "\n".join(f"{r['wallet'][:10]}… n={r['markets']} edge {r['mean_edge']:+.3f} "
                             f"t {r['t_stat']:.1f} shrunk {r['shrunk_edge']:+.3f}"
                             for r in rows) or "belum ada data trader"

        return {
            "status": lambda _: self.status(),
            "score": score,
            "kill": kill,
            "resume": resume,
            "traders": traders,
            "help": lambda _: "/status /score /traders /kill [alasan] /resume",
        }
