"""Statistik kecil tanpa numpy: mean, t-stat, bootstrap CI rasio."""
from __future__ import annotations

import math
import random


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def t_stat(xs: list[float]) -> float:
    """t = mean / (sd / sqrt(n)). 0 kalau data < 2 atau sd 0."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0
    return m / math.sqrt(var / n)


def bootstrap_ratio_ci(num: list[float], den: list[float], iters: int = 2000,
                       alpha: float = 0.05, seed: int = 7) -> tuple[float, float]:
    """CI (1-alpha) untuk sum(num)/sum(den) dengan resampling berpasangan."""
    n = len(num)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    out = []
    for _ in range(iters):
        idx = [rng.randrange(n) for _ in range(n)]
        d = sum(den[i] for i in idx)
        if d > 0:
            out.append(sum(num[i] for i in idx) / d)
    if not out:
        return (0.0, 0.0)
    out.sort()
    lo = out[int(alpha / 2 * len(out))]
    hi = out[min(len(out) - 1, int((1 - alpha / 2) * len(out)))]
    return (lo, hi)
