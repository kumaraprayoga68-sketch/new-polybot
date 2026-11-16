"""
Kategori kasar dari judul market. Dipakai dashboard (PnL per kategori) dan
copytrade (CT_EXCLUDE_CATEGORIES), jadi definisinya harus satu tempat.
"""
from __future__ import annotations

import re

CATEGORIES = (
    # Market "Bitcoin Up or Down - 4:15AM-4:20AM ET": umurnya 5-15 menit, kita
    # ngikutin trade sampai 10 menit kemudian -> hampir selalu telat.
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
    for name, rx in CATEGORIES:
        if rx.search(title or ""):
            return name
    return "lainnya"
