"""
Copytrade v2. Beda utama dari polybot:

1. Trader dipilih dari EDGE TERUKUR vs harga beli mereka (traders.py), bukan
   win rate / ranking leaderboard.
2. Yang dicopy cuma TRADE BARU (umur <= CT_MAX_SIGNAL_AGE_SEC), bukan semua posisi
   terbuka. Pertama kali lihat trader, histori-nya dilewati.
3. Harga kita dibandingin harga trader: kalau kita bayar > harga trader +
   CT_MAX_SLIPPAGE, skip — edge-nya udah dimakan.
4. Ukuran pakai Kelly dengan peluang = harga trader + edge trader (di-shrink),
   dan biaya = harga rata-rata kita SETELAH fee & menyapu book.
5. Mid saat sinyal dicatat, lalu diukur lagi +1j/+6j (marks) — sumber harga
   sama, jadi drift-nya bisa dipercaya.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .. import traders
from ..api import PolymarketAPI
from ..broker import Leg
from ..config import Settings
from ..db import DB
from ..executor import Decision
from ..fees import rate_from_bps

NAME = "copytrade"


def days_until(end_iso: str | None) -> float | None:
    if not end_iso:
        return None
    try:
        d = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d - datetime.now(timezone.utc)).total_seconds() / 86400


def kelly_shares(p: float, cost_per_share: float, bankroll: float, fraction: float) -> float:
    """Jumlah share dari fractional Kelly untuk kontrak biner bayar $1."""
    c = cost_per_share
    if not 0 < c < 1 or p <= c:
        return 0.0
    f = (p - c) / (1 - c)
    return fraction * f * bankroll / c


class Copytrade:
    name = NAME

    def __init__(self, settings: Settings, api: PolymarketAPI, db: DB):
        self.s = settings
        self.cfg = settings.copytrade
        self.api = api
        self.db = db

    # ── pemilihan trader ────────────────────────────────────────────────────
    def stats_stale(self) -> bool:
        i, n = self.stats_progress()
        if i < n:
            return True          # putaran pengukuran belum kelar
        if self.db.get_meta("ct:pool_target") != self._pool_target():
            return True          # setting kandidat berubah -> ukur ulang sekarang
        last = float(self.db.get_meta("ct:stats_at", "0") or 0)
        return time.time() - last > self.cfg.stats_refresh_hours * 3600

    def _pool_target(self) -> str:
        if self.cfg.manual_wallets:
            return "manual:" + ",".join(sorted(self.cfg.manual_wallets))
        return f"leaderboard:{self.cfg.leaderboard_pool}"

    def stats_progress(self) -> tuple[int, int]:
        """(sudah diukur, total) untuk putaran pengukuran yang lagi jalan."""
        pool = json.loads(self.db.get_meta("ct:pool", "[]") or "[]")
        return int(self.db.get_meta("ct:pool_i", "0") or 0), len(pool)

    def refresh_stats(self, log=print, batch: int | None = None) -> int:
        """Ukur skill trader secara DICICIL: `batch` trader per panggilan.

        Ngukur 100 trader sekaligus makan 30+ menit dan nahan siklus pertama —
        selama itu gak ada sinyal, gak ada scan arbitrage, dashboard kosong.
        Sekarang tiap siklus ngukur sebagian; putaran baru dimulai setelah
        putaran lama kelar DAN sudah lewat CT_STATS_REFRESH_HOURS.
        """
        batch = batch or self.cfg.stats_batch
        i, n = self.stats_progress()
        target = self._pool_target()
        if i >= n or self.db.get_meta("ct:pool_target") != target:
            pool = list(self.cfg.manual_wallets) or self.api.leaderboard(self.cfg.leaderboard_pool)
            self.db.set_meta("ct:pool_target", target)
            self.db.set_meta("ct:pool", json.dumps([w.lower() for w in pool]))
            i, n = 0, len(pool)
            self.db.set_meta("ct:pool_i", "0")
        pool = json.loads(self.db.get_meta("ct:pool", "[]") or "[]")
        chunk = pool[i:i + batch]
        log(f"[copytrade] ngukur skill trader {i + 1}-{i + len(chunk)} dari {n}…")

        def one(w):
            try:
                return traders.measure(self.api, self.db, w, self.cfg.shrink_k,
                                       self.cfg.max_resolution_lookups)
            except Exception as e:  # noqa: BLE001
                self.db.log("warn", "traders", f"{w}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=6) as ex:
            done = [s for s in ex.map(one, chunk) if s]
        self.db.set_meta("ct:pool_i", str(i + len(chunk)))
        if i + len(chunk) >= n:
            self.db.set_meta("ct:stats_at", str(time.time()))
        return len(done)

    def followed(self) -> dict[str, float]:
        """wallet -> shrunk edge, cuma yang lulus syarat."""
        out = {}
        for r in self.db.trader_stats():
            if (r["markets"] >= self.cfg.min_trader_markets and r["t_stat"] >= self.cfg.min_trader_t
                    and r["shrunk_edge"] > 0):
                out[r["wallet"]] = r["shrunk_edge"]
            if len(out) >= self.cfg.follow_top:
                break
        return out

    # ── sinyal ──────────────────────────────────────────────────────────────
    def new_trades(self, wallet: str) -> list[dict]:
        rows, _ = self.api.user_trades(wallet, limit=50)
        stamps = [float(r.get("timestamp") or 0) for r in rows]
        last = self.db.trader_last_ts(wallet)
        if stamps:
            self.db.set_trader_last_ts(wallet, max(stamps + [last or 0]))
        if last is None:
            return []          # pertama kali lihat: jangan copy histori
        now = time.time()
        out = []
        for r, ts in zip(rows, stamps):
            if ts <= last or now - ts > self.cfg.max_signal_age_sec:
                continue
            if str(r.get("side", "")).upper() != "BUY":
                continue
            try:
                usdc = float(r.get("size", 0)) * float(r.get("price", 0))
            except (TypeError, ValueError):
                continue
            if usdc >= self.cfg.min_trader_usdc:
                out.append(r)
        return out

    def evaluate(self, wallet: str, edge: float, t: dict) -> Decision | None:
        cfg = self.cfg
        cid = t.get("condition_id") or t.get("conditionId") or ""
        tok = str(t.get("token_id") or t.get("asset") or "")
        key = f"ct:{wallet}:{t.get('transaction_hash', '')}:{tok}"
        if not cid or not tok or self.db.signal_seen(key):
            return None
        trader_price = float(t["price"])
        base = dict(condition_id=cid, token_id=tok, outcome=str(t.get("outcome", "")),
                    market=str(t.get("title", "")), ref_price=trader_price,
                    extra={"wallet": wallet, "trader_edge": edge})

        def skip(reason, **kw):
            self.db.add_signal(NAME, key, decision="skip", reason=reason, **base, **kw)
            return None

        m = self.api.market(cid)
        if not m or m.get("closed") or not m.get("accepting_orders", True):
            return skip("market tutup / gak terima order")
        days = days_until(m.get("end_date_iso"))
        # end_date market olahraga = tanggal pertandingan jam 00:00, jadi pertandingan
        # yang lagi jalan hari ini kelihatan "lewat" beberapa jam. Market yang beneran
        # tutup udah kebuang di cek accepting_orders di atas; di sini cuma buang yang
        # lewat jauh (limbo, nunggu sengketa).
        if days is not None and days < -2:
            return skip(f"end date lewat {-days:.1f} hari, belum settle")
        if days is not None and days > cfg.max_days_to_resolve:
            return skip(f"resolve {days:.1f} hari lagi (> {cfg.max_days_to_resolve})")
        book = self.api.book(tok)
        if book is None or book.best_ask is None:
            return skip("order book kosong")
        fee_rate = rate_from_bps(m.get("taker_base_fee"), self.s.fees.rate_override)
        p_est = min(0.99, max(0.01, trader_price + edge))
        mk = dict(mid=book.mid, ask=book.best_ask)

        # dua putaran: tebak size di best ask, simulasi, ulang pakai biaya rata-rata.
        cost = book.best_ask
        shares = 0.0
        for _ in range(2):
            shares = kelly_shares(p_est, cost, self.s.bankroll_usd, cfg.kelly_fraction)
            shares = min(shares, self.s.risk.max_order_usd / max(cost, 1e-6) * 0.98)
            if shares <= 0:
                return skip(f"Kelly 0 (p_est {p_est:.3f} <= biaya {cost:.3f})", **mk)
            shares = max(shares, book.min_order_size)
            fill = book.buy(shares, fee_rate)
            if fill is None:
                return skip("depth gak cukup", **mk)
            cost = fill.total / fill.shares
        shares = round(shares, 2)
        our_edge = p_est - cost
        if not cfg.min_price <= cost <= cfg.max_price:
            return skip(f"harga {cost:.3f} di luar band", edge_est=our_edge, **mk)
        if cost > trader_price + cfg.max_slippage:
            return skip(f"slippage: kita {cost:.3f} vs trader {trader_price:.3f}",
                        edge_est=our_edge, **mk)
        if our_edge < cfg.min_edge_per_share:
            return skip(f"edge {our_edge:.3f} < {cfg.min_edge_per_share}", edge_est=our_edge, **mk)

        sid = self.db.add_signal(NAME, key, decision="take", reason=f"edge {our_edge:.3f}",
                                 edge_est=our_edge, **mk, **base)
        leg = Leg(cid, tok, base["outcome"], base["market"], shares, book, fee_rate,
                  str(m.get("end_date_iso") or "")[:10])
        return Decision(NAME, [leg], sid, f"copy {wallet[:8]}… edge {our_edge:.3f}")

    def decisions(self, log=print) -> list[Decision]:
        follow = self.followed()
        if not follow:
            log("[copytrade] belum ada trader yang lulus syarat skill — gak ada sinyal.")
            return []
        out, tokens = [], set()
        for wallet, edge in follow.items():
            for t in self.new_trades(wallet):
                d = self.evaluate(wallet, edge, t)
                if d and d.legs[0].token_id not in tokens:
                    tokens.add(d.legs[0].token_id)
                    out.append(d)
        return out
