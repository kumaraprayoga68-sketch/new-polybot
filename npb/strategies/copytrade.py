"""
Copytrade v2. Beda utama dari polybot:

1. Trader dipilih dari EDGE TERUKUR vs harga beli mereka (traders.py), bukan
   win rate / ranking leaderboard.
2. Yang dicopy cuma TRADE BARU (umur <= CT_MAX_SIGNAL_AGE_SEC), bukan semua posisi
   terbuka. Pertama kali lihat trader, histori-nya dilewati.
3. Harga kita dibandingin harga trader: kalau kita bayar > harga trader +
   CT_MAX_SLIPPAGE, skip — edge-nya udah dimakan.
4. Ukuran pakai Kelly dengan peluang = harga trader + edge trader (di-shrink,
   ternormalisasi ke harga), biaya = harga rata-rata kita SETELAH fee & menyapu
   book, dan modal = bankroll dikurangi yang lagi nyangkut di posisi terbuka.
   Kalau Kelly < ukuran order minimum, SKIP (bukan dibulatin ke atas).
5. Mid saat sinyal dicatat, lalu diukur lagi +1j/+6j (marks) — sumber harga
   sama, jadi drift-nya bisa dipercaya.
"""
from __future__ import annotations

import json
import math
import time
from statistics import NormalDist
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
        # Diisi runner: mode eksekusi (paper/live) buat strategi ini, dan
        # pelapor error (log + Telegram). Default aman buat tes / CLI.
        self.mode_for = lambda name: "paper"
        self.on_error = lambda source, e: self.db.log("error", source, str(e))

    # ── pemilihan trader ────────────────────────────────────────────────────
    def stats_stale(self) -> bool:
        i, n = self.stats_progress()
        if i < n:
            return True          # putaran pengukuran belum kelar
        if self.db.get_meta("ct:pool_target") != self._pool_target():
            return True          # setting kandidat berubah -> ukur ulang sekarang
        last = float(self.db.get_meta("ct:stats_at", "0") or 0)
        return time.time() - last > self.cfg.stats_refresh_hours * 3600

    # Naikkan kalau cara ngukur trader berubah -> semua trader diukur ulang.
    STATS_VERSION = "v2"

    def _pool_target(self) -> str:
        if self.cfg.manual_wallets:
            return f"{self.STATS_VERSION}:manual:" + ",".join(sorted(self.cfg.manual_wallets))
        return f"{self.STATS_VERSION}:leaderboard:{self.cfg.leaderboard_pool}"

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

    def required_t(self) -> float:
        """t minimum setelah koreksi multiple testing untuk jumlah kandidat sekarang.

        300 kandidat diuji di t>=1.5 -> ~20 lolos murni karena hoki. Bonferroni
        satu sisi: t >= z(1 - alpha/N). N=100 -> ~3.29, N=300 -> ~3.59."""
        cfg = self.cfg
        if cfg.family_alpha <= 0:
            return cfg.min_trader_t
        n = len(json.loads(self.db.get_meta("ct:pool", "[]") or "[]"))
        n = max(n, len(self.db.trader_stats()), 1)
        z = NormalDist().inv_cdf(1 - min(0.5, cfg.family_alpha / n))
        return max(cfg.min_trader_t, z)

    def followed(self) -> dict[str, float]:
        """wallet -> edge ternormalisasi (shrunk), cuma yang lulus semua syarat:
        cukup market, t lolos koreksi multiple testing, dan edge di holdout positif."""
        cfg = self.cfg
        need_t = self.required_t()
        out = {}
        for r in self.db.trader_stats():
            norm = r["norm_edge"]
            if (r["markets"] >= cfg.min_trader_markets and r["t_stat"] >= need_t
                    and r["shrunk_edge"] > 0 and norm is not None and norm > 0
                    and (r["holdout_n"] or 0) >= cfg.min_holdout_markets
                    and (r["holdout_edge"] or 0) > 0):
                out[r["wallet"]] = norm
            if len(out) >= cfg.follow_top:
                break
        return out

    # ── sinyal ──────────────────────────────────────────────────────────────
    def new_trades(self, wallet: str) -> tuple[list[dict], float | None]:
        """(trade BELI baru yang layak dicek, timestamp terbaru yang kelihatan).

        Cursor TIDAK dimajuin di sini — decisions() yang majuin setelah semua
        trade wallet ini selesai dievaluasi, biar error API di tengah gak bikin
        sinyal hilang. Halaman berikutnya diambil selama semua baris masih
        lebih baru dari cursor (trader yang aktif banget)."""
        last = self.db.trader_last_ts(wallet)
        rows, cursor = self.api.user_trades(wallet, limit=100)
        rows = list(rows)
        pages = 1
        while (cursor and last is not None and pages < self.cfg.max_trade_pages and rows
               and min(float(r.get("timestamp") or 0) for r in rows) > last):
            more, cursor = self.api.user_trades(wallet, limit=100, cursor=cursor)
            if not more:
                break
            rows.extend(more)
            pages += 1
        stamps = [float(r.get("timestamp") or 0) for r in rows]
        newest = max(stamps + [last or 0]) if stamps else last
        if last is None:
            if newest is not None:
                self.db.set_trader_last_ts(wallet, newest)
            return [], None        # pertama kali lihat: jangan copy histori
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
        out.sort(key=lambda r: float(r.get("timestamp") or 0))
        return out, newest

    def free_bankroll(self) -> float:
        """Bankroll dikurangi modal yang lagi nyangkut di posisi terbuka (mode strategi ini)."""
        exp = self.db.open_exposure(self.s.risk.zombie_days, mode=self.mode_for(NAME))
        return max(0.0, self.s.bankroll_usd - exp)

    def evaluate(self, wallet: str, edge: float, t: dict,
                 bankroll: float | None = None) -> Decision | None:
        """`edge` = edge ternormalisasi trader (lihat traders.py)."""
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
        tp = min(0.99, max(0.01, trader_price))
        p_est = min(0.99, max(0.01, trader_price + edge * math.sqrt(tp * (1 - tp))))
        mk = dict(mid=book.mid, ask=book.best_ask)
        bankroll = self.free_bankroll() if bankroll is None else bankroll
        if bankroll <= 0:
            return skip("bankroll habis (semua nyangkut di posisi terbuka)", **mk)

        # dua putaran: tebak size di best ask, simulasi, ulang pakai biaya rata-rata.
        cost = book.best_ask
        shares = 0.0
        for _ in range(2):
            shares = kelly_shares(p_est, cost, bankroll, cfg.kelly_fraction)
            shares = min(shares, self.s.risk.max_order_usd / max(cost, 1e-6) * 0.98)
            if shares <= 0:
                return skip(f"Kelly 0 (p_est {p_est:.3f} <= biaya {cost:.3f})", **mk)
            if shares < book.min_order_size:
                # Dulu dibulatin ke atas ke min size = taruhan lebih gede dari Kelly
                # (bisa 5x) dan bisa nembus hard cap. Kekecilan = gak usah ikut.
                return skip(f"Kelly {shares:.2f} share < order minimum "
                            f"{book.min_order_size:g}", **mk)
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
        bankroll = self.free_bankroll()
        for wallet, edge in follow.items():
            try:
                trades, newest = self.new_trades(wallet)
                for t in trades:
                    d = self.evaluate(wallet, edge, t, bankroll)
                    if d and d.legs[0].token_id not in tokens:
                        tokens.add(d.legs[0].token_id)
                        out.append(d)
                        lg = d.legs[0]
                        fill = lg.book.buy(lg.shares, lg.fee_rate)
                        bankroll = max(0.0, bankroll - (fill.total if fill else 0.0))
            except Exception as e:  # noqa: BLE001 — satu trader error gak boleh matiin yang lain
                # Cursor gak dimajuin: trade-nya dicoba lagi siklus depan (signal_seen
                # nyegah dobel, max_signal_age nyegah ngulang selamanya).
                self.on_error("copytrade", RuntimeError(f"{wallet[:10]}…: {e}"))
                continue
            if newest is not None:
                self.db.set_trader_last_ts(wallet, newest)
        return out
