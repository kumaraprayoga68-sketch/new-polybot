"""
Konfigurasi terpusat. Semua dibaca dari environment (.env), gak ada yang di-hardcode.

Default SEMUA aman: mode paper, live mati, dan live tetap butuh strategi yang
sudah LULUS gerbang statistik (lihat scorecard.py) + di-promote manual.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def _s(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
DATA_URL = "https://data-api.polymarket.com"


@dataclass(frozen=True)
class RiskSettings:
    # Hard cap per order (USD, termasuk fee). Berlaku paper DAN live.
    max_order_usd: float = 5.0
    # Total duit nyangkut di posisi yang belum resolve.
    max_exposure_usd: float = 100.0
    # Maksimal per market (condition_id), biar gak numpuk di satu kejadian.
    max_market_usd: float = 15.0
    # Rugi realized 24 jam terakhir lewat ini -> semua order baru berhenti.
    daily_loss_limit_usd: float = 20.0
    # Posisi yang end_date-nya lewat > ini hari tapi belum settle = zombie,
    # gak dihitung eksposur (pelajaran polybot: 247 zombie nyekek bot 21 hari).
    zombie_days: int = 14


@dataclass(frozen=True)
class FeeSettings:
    # Rumus fee taker: rate * min(p, 1-p) * shares, rate = base_fee_bps / 10000
    # dari CLOB per market. Kalau FEE_RATE_OVERRIDE >= 0, rate itu yang dipakai.
    # WAJIB dikalibrasi pakai 1 fill live kecil sebelum percaya angka paper.
    rate_override: float = -1.0


@dataclass(frozen=True)
class GateSettings:
    """Syarat statistik sebelum strategi boleh di-promote ke live."""
    min_resolved: int = 200
    min_t_stat: float = 2.0
    # Batas bawah interval kepercayaan 95% (bootstrap) ROI harus > ini.
    min_roi_ci_low: float = 0.0


@dataclass(frozen=True)
class CopytradeSettings:
    enabled: bool = True
    manual_wallets: tuple[str, ...] = ()
    # Kandidat dari leaderboard. Catatan: endpoint v2 leaderboard mengabaikan
    # parameter window (selalu harian), jadi pool ini cuma bahan mentah —
    # yang nentuin diikuti atau nggak adalah skill terukur di traders.py.
    leaderboard_pool: int = 100
    follow_top: int = 15
    # Syarat trader diikuti: edge per share terukur vs harga beli mereka sendiri.
    min_trader_markets: int = 30
    min_trader_t: float = 2.0
    shrink_k: float = 30.0
    stats_refresh_hours: float = 12.0
    max_resolution_lookups: int = 150
    # Sinyal: cuma trade BARU, gak lebih tua dari ini.
    max_signal_age_sec: int = 600
    min_trader_usdc: float = 50.0
    # Tolak kalau harga rata-rata kita lebih mahal dari harga trader + ini.
    max_slippage: float = 0.02
    min_price: float = 0.05
    max_price: float = 0.95
    max_days_to_resolve: int = 30
    min_edge_per_share: float = 0.01
    kelly_fraction: float = 0.25


@dataclass(frozen=True)
class ArbitrageSettings:
    enabled: bool = True
    max_events: int = 400
    # Profit per set (setelah fee) minimal segini baru dieksekusi.
    min_edge_per_set: float = 0.01
    # Prefilter murah pakai bestBid/bestAsk Gamma sebelum ambil order book.
    prefilter_slack: float = 0.03
    max_sets: float = 200.0
    scan_interval_min: float = 5.0


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"                 # "paper" | "live"
    live_enabled: bool = False          # gate kedua, terpisah dari mode
    bankroll_usd: float = 100.0
    db_path: str = "data/npb.db"
    loop_interval_sec: int = 60
    telegram_token: str = ""
    telegram_chat_id: str = ""
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1
    chain_id: int = 137
    risk: RiskSettings = field(default_factory=RiskSettings)
    fees: FeeSettings = field(default_factory=FeeSettings)
    gate: GateSettings = field(default_factory=GateSettings)
    copytrade: CopytradeSettings = field(default_factory=CopytradeSettings)
    arbitrage: ArbitrageSettings = field(default_factory=ArbitrageSettings)

    @property
    def live_requested(self) -> bool:
        return self.mode == "live" and self.live_enabled

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mode=_s("NPB_MODE", "paper").lower(),
            live_enabled=_b("LIVE_TRADING_ENABLED", False),
            bankroll_usd=_f("BANKROLL_USD", 100.0),
            db_path=_s("NPB_DB_PATH", "data/npb.db"),
            loop_interval_sec=_i("LOOP_INTERVAL_SEC", 60),
            telegram_token=_s("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_s("TELEGRAM_CHAT_ID", ""),
            private_key=_s("PRIVATE_KEY", ""),
            funder_address=_s("FUNDER_ADDRESS", ""),
            signature_type=_i("SIGNATURE_TYPE", 1),
            risk=RiskSettings(
                max_order_usd=_f("MAX_ORDER_USD", 5.0),
                max_exposure_usd=_f("MAX_EXPOSURE_USD", 100.0),
                max_market_usd=_f("MAX_MARKET_USD", 15.0),
                daily_loss_limit_usd=_f("DAILY_LOSS_LIMIT_USD", 20.0),
                zombie_days=_i("ZOMBIE_DAYS", 14),
            ),
            fees=FeeSettings(rate_override=_f("FEE_RATE_OVERRIDE", -1.0)),
            gate=GateSettings(
                min_resolved=_i("GATE_MIN_RESOLVED", 200),
                min_t_stat=_f("GATE_MIN_T", 2.0),
                min_roi_ci_low=_f("GATE_MIN_ROI_CI_LOW", 0.0),
            ),
            copytrade=CopytradeSettings(
                enabled=_b("COPYTRADE_ENABLED", True),
                manual_wallets=tuple(_list("TRADER_WALLETS")),
                leaderboard_pool=_i("CT_LEADERBOARD_POOL", 100),
                follow_top=_i("CT_FOLLOW_TOP", 15),
                min_trader_markets=_i("CT_MIN_TRADER_MARKETS", 30),
                min_trader_t=_f("CT_MIN_TRADER_T", 2.0),
                shrink_k=_f("CT_SHRINK_K", 30.0),
                stats_refresh_hours=_f("CT_STATS_REFRESH_HOURS", 12.0),
                max_resolution_lookups=_i("CT_MAX_RESOLUTION_LOOKUPS", 150),
                max_signal_age_sec=_i("CT_MAX_SIGNAL_AGE_SEC", 600),
                min_trader_usdc=_f("CT_MIN_TRADER_USDC", 50.0),
                max_slippage=_f("CT_MAX_SLIPPAGE", 0.02),
                min_price=_f("CT_MIN_PRICE", 0.05),
                max_price=_f("CT_MAX_PRICE", 0.95),
                max_days_to_resolve=_i("CT_MAX_DAYS_TO_RESOLVE", 30),
                min_edge_per_share=_f("CT_MIN_EDGE", 0.01),
                kelly_fraction=_f("CT_KELLY_FRACTION", 0.25),
            ),
            arbitrage=ArbitrageSettings(
                enabled=_b("ARB_ENABLED", True),
                max_events=_i("ARB_MAX_EVENTS", 400),
                min_edge_per_set=_f("ARB_MIN_EDGE", 0.01),
                prefilter_slack=_f("ARB_PREFILTER_SLACK", 0.03),
                max_sets=_f("ARB_MAX_SETS", 200.0),
                scan_interval_min=_f("ARB_SCAN_INTERVAL_MIN", 5.0),
            ),
        )
