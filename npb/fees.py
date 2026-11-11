"""
Model fee taker Polymarket.

Diasumsikan: fee = rate * min(p, 1-p) * shares, rate = base_fee (bps) / 10000 dari
CLOB (/markets/{cid} field taker_base_fee, atau /fee-rate). Contoh di Nov 2026:
banyak market balikin base_fee=1000.

⚠️ Rumus persis fee Polymarket bisa berubah per kategori. Model ini sengaja
konservatif. Sebelum percaya hasil paper, kalibrasi: kirim 1 order live kecil,
bandingin fee beneran vs taker_fee(), lalu set FEE_RATE_OVERRIDE kalau beda.
"""
from __future__ import annotations


def rate_from_bps(bps, override: float = -1.0) -> float:
    if override is not None and override >= 0:
        return override
    try:
        return max(0.0, float(bps or 0)) / 10_000
    except (TypeError, ValueError):
        return 0.0


def taker_fee(price: float, shares: float, rate: float) -> float:
    if rate <= 0 or shares <= 0:
        return 0.0
    return rate * min(price, 1 - price) * shares
