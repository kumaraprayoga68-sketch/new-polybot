"""
Klien endpoint PUBLIK Polymarket (Gamma, CLOB, Data API v2). Read-only.

Pelajaran dari polybot: dua kali bot mati senyap karena endpoint diretire dan
error ditelan jadi list kosong ("nol posisi" gak bisa dibedain dari "API mati").
Di sini: 404 = resource gak ada (balikin None), error lain yang menetap = raise
ApiError. Runner yang nangkep, nyatet, dan ngirim alert.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .book import OrderBook
from .config import CLOB_URL, DATA_URL, GAMMA_URL

_HEADERS = {"User-Agent": "new-polybot/0.1", "Accept": "application/json"}


class ApiError(RuntimeError):
    def __init__(self, url: str, status: int | None, body: str):
        super().__init__(f"{url} -> HTTP {status}: {body[:200]}")
        self.url, self.status, self.body = url, status, body


def _rows(data: Any) -> list[dict]:
    """Data API v2 bungkus hasil di {"data": [...]}; dulu list polos."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [r for r in data["data"] if isinstance(r, dict)]
    return []


def _next_cursor(data: Any) -> str | None:
    if isinstance(data, dict):
        pag = data.get("pagination") or {}
        if pag.get("has_more"):
            return pag.get("next_cursor")
    return None


class PolymarketAPI:
    def __init__(self, session: requests.Session | None = None, timeout: float = 12.0,
                 retries: int = 2, market_ttl_sec: float = 300.0):
        self.s = session or requests.Session()
        self.s.headers.update(_HEADERS)
        self.timeout = timeout
        self.retries = retries
        self.market_ttl = market_ttl_sec
        self._market_cache: dict[str, tuple[float, dict | None]] = {}

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _request(self, method: str, url: str, **kw) -> Any:
        status, body = None, ""
        for attempt in range(self.retries + 1):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                body = str(e)
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ApiError(url, None, body) from e
            status = r.status_code
            if status == 404:
                return None
            if status == 429 or status >= 500:
                body = r.text
                if attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise ApiError(url, status, body)
            if status >= 400:
                # 4xx selain 404/429 gak bakal beda kalau diulang: kontrak berubah.
                raise ApiError(url, status, r.text)
            try:
                return r.json()
            except ValueError as e:
                raise ApiError(url, status, r.text) from e
        raise ApiError(url, status, body)

    def get(self, url: str, params: dict | None = None) -> Any:
        return self._request("GET", url, params=params)

    # ── CLOB ─────────────────────────────────────────────────────────────────
    def book(self, token_id: str) -> OrderBook | None:
        data = self.get(f"{CLOB_URL}/book", {"token_id": token_id})
        return OrderBook.from_api(data, token_id) if isinstance(data, dict) else None

    def books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Banyak order book sekaligus (POST /books), fallback satu-satu."""
        out: dict[str, OrderBook] = {}
        if not token_ids:
            return out
        try:
            data = self._request("POST", f"{CLOB_URL}/books",
                                 json=[{"token_id": t} for t in token_ids])
            for d in data or []:
                if isinstance(d, dict) and d.get("asset_id"):
                    out[str(d["asset_id"])] = OrderBook.from_api(d)
        except ApiError:
            out = {}
        for t in token_ids:
            if t not in out:
                b = self.book(t)
                if b is not None:
                    out[t] = b
        return out

    def midpoint(self, token_id: str) -> float | None:
        data = self.get(f"{CLOB_URL}/midpoint", {"token_id": token_id})
        try:
            return float(data["mid"]) if data else None
        except (KeyError, TypeError, ValueError):
            return None

    def market(self, condition_id: str, fresh: bool = False) -> dict | None:
        """Info market CLOB: tokens (+winner), closed, neg_risk, fee, tick, end date."""
        now = time.time()
        hit = self._market_cache.get(condition_id)
        if hit and not fresh and now - hit[0] < self.market_ttl:
            return hit[1]
        data = self.get(f"{CLOB_URL}/markets/{condition_id}")
        data = data if isinstance(data, dict) else None
        self._market_cache[condition_id] = (now, data)
        return data

    # ── GAMMA ────────────────────────────────────────────────────────────────
    def events_page(self, limit: int = 100, offset: int = 0) -> list[dict]:
        data = self.get(f"{GAMMA_URL}/events", {
            "active": "true", "closed": "false", "archived": "false",
            "limit": limit, "offset": offset,
        })
        return _rows(data)

    def iter_events(self, max_events: int, page: int = 100):
        offset = 0
        while offset < max_events:
            rows = self.events_page(limit=min(page, max_events - offset), offset=offset)
            if not rows:
                return
            yield from rows
            if len(rows) < page:
                return
            offset += len(rows)

    # ── DATA API v2 ──────────────────────────────────────────────────────────
    def user_trades(self, wallet: str, limit: int = 100,
                    cursor: str | None = None) -> tuple[list[dict], str | None]:
        params: dict[str, Any] = {"user": wallet, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self.get(f"{DATA_URL}/v2/trades", params)
        return _rows(data), _next_cursor(data)

    def user_trades_all(self, wallet: str, max_rows: int = 1500) -> list[dict]:
        rows, cursor = [], None
        while len(rows) < max_rows:
            page, cursor = self.user_trades(wallet, limit=500, cursor=cursor)
            rows.extend(page)
            if not page or not cursor:
                break
            time.sleep(0.2)
        return rows[:max_rows]

    def leaderboard(self, limit: int = 100) -> list[str]:
        """Wallet dari leaderboard. Window diabaikan endpoint-nya (selalu harian)."""
        wallets, cursor = [], None
        while len(wallets) < limit:
            params: dict[str, Any] = {"limit": min(100, limit - len(wallets))}
            if cursor:
                params["cursor"] = cursor
            data = self.get(f"{DATA_URL}/v2/leaderboard", params)
            for r in _rows(data):
                w = r.get("user_id") or r.get("proxyWallet") or r.get("proxy_wallet")
                if w:
                    wallets.append(str(w).lower())
            cursor = _next_cursor(data)
            if not cursor:
                break
        return wallets[:limit]


def token_for_outcome(market: dict, outcome: str) -> dict | None:
    for t in market.get("tokens") or []:
        if str(t.get("outcome", "")).strip().lower() == outcome.strip().lower():
            return t
    return None


def payouts(market: dict | None) -> dict[str, float] | None:
    """{token_id: payout per share} kalau market sudah settle, selain itu None.

    closed=True belum tentu settle (bisa nunggu UMA). Settle = ada winner, ATAU
    market dibatalin / dibagi rata (50-50): gak ada winner dan semua token
    dihargai persis 0.5. Tanpa kasus kedua, posisi begitu nyangkut "belum settle"
    selamanya — ngisi eksposur dan gak pernah masuk scorecard.
    """
    if not market or not market.get("closed"):
        return None
    tokens = market.get("tokens") or []
    if not tokens:
        return None
    if any(t.get("winner") for t in tokens):
        return {str(t.get("token_id")): (1.0 if t.get("winner") else 0.0) for t in tokens}
    try:
        prices = [float(t.get("price")) for t in tokens]
    except (TypeError, ValueError):
        return None
    if len(tokens) == 2 and all(abs(p - 0.5) < 1e-9 for p in prices):
        return {str(t.get("token_id")): 0.5 for t in tokens}
    return None
