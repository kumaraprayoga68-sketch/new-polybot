"""Tes buat perbaikan review: seleksi trader, Kelly, DB, settle, cursor, rem rugi, paper fill."""
import json
import math
import time
from dataclasses import replace

import pytest

from npb.api import payouts
from npb.broker import Leg, PaperBroker
from npb.config import CopytradeSettings, RiskSettings
from npb.db import DB, new_group_id
from npb.executor import Decision, Executor
from npb.notify import Telegram
from npb.runner import Bot
from npb.strategies.copytrade import Copytrade
from npb.traders import market_results, skill_from_results

from .conftest import make_book

W = "0xtrader"


def order(db, cid, tok, cost, shares=10, mode="paper", end="2099-01-01", gid=None):
    db.add_order(strategy="copytrade", mode=mode, group_id=gid or new_group_id(), signal_id=None,
                 condition_id=cid, token_id=tok, outcome="Yes", market="m", end_date=end,
                 shares_req=shares, limit_price=cost / shares, status="filled", shares=shares,
                 cost=cost)


# ── seleksi trader ───────────────────────────────────────────────────────────
def test_holdout_uses_newest_markets():
    trades, res = [], {}
    for i in range(30):                 # 20 lama menang, 10 terbaru kalah
        trades.append({"condition_id": f"c{i}", "token_id": f"t{i}", "price": 0.5, "size": 10,
                       "side": "BUY", "timestamp": 1000 + i})
        res[f"c{i}"] = {f"t{i}": 1.0 if i < 20 else 0.0}
    sk = skill_from_results(W, market_results(trades, res), shrink_k=0)
    assert sk.markets == 30 and sk.holdout_n == 10
    assert sk.mean_edge == pytest.approx(0.5)        # dinilai dari 20 lama saja
    assert sk.holdout_edge == pytest.approx(-0.5)    # yang terbaru jelek


def test_normalized_edge():
    trades = [{"condition_id": "c", "token_id": "t", "price": 0.9, "size": 10, "side": "BUY",
               "timestamp": 1}]
    r = market_results(trades, {"c": {"t": 1.0}})[0]
    assert r.edge == pytest.approx(0.1)
    assert r.norm_edge == pytest.approx(0.1 / math.sqrt(0.09))


def test_multiple_testing_raises_required_t(api, db, settings):
    ct = Copytrade(settings, api, db)
    db.set_meta("ct:pool", json.dumps([f"0x{i}" for i in range(300)]))
    assert ct.required_t() == pytest.approx(3.59, abs=0.01)
    # t=3 dulu lolos (min 2.0), sekarang nggak dengan 300 kandidat
    db.upsert_trader_stats(W, 100, 0.05, 3.0, 0.04, norm_edge=0.1, holdout_n=30, holdout_edge=0.05)
    assert ct.followed() == {}
    off = Copytrade(replace(settings, copytrade=CopytradeSettings(family_alpha=0)), api, db)
    assert W in off.followed()


# ── Kelly ────────────────────────────────────────────────────────────────────
def _ct(api, db, settings, ask=0.41, norm=0.1, **cfg):
    db.upsert_trader_stats(W, 100, 0.05, 4.0, 0.05, norm_edge=norm, holdout_n=30, holdout_edge=0.05)
    api.markets["c1"] = {"closed": False, "accepting_orders": True, "taker_base_fee": 0,
                         "end_date_iso": "2099-01-01T00:00:00Z"}
    api.book_map["y1"] = make_book([(ask, 1000)], bids=[(ask - 0.02, 1000)], token="y1")
    c = CopytradeSettings(max_days_to_resolve=100_000, family_alpha=0, **cfg)
    return Copytrade(replace(settings, copytrade=c), api, db)


def _trade(tx="tx1", **kw):
    t = {"condition_id": "c1", "token_id": "y1", "price": 0.40, "size": 500, "side": "BUY",
         "timestamp": time.time(), "transaction_hash": tx, "outcome": "Yes", "title": "M"}
    t.update(kw)
    return t


def test_kelly_below_min_size_skips_instead_of_rounding_up(api, db, settings):
    ct = _ct(api, db, settings, norm=0.05)          # edge kecil -> Kelly < 5 share
    db.set_trader_last_ts(W, 0)
    api.trades[W] = [_trade()]
    assert ct.decisions(log=lambda *_: None) == []
    assert "order minimum" in db.query("SELECT reason FROM signals")[0]["reason"]


def test_kelly_uses_free_bankroll(api, db, settings):
    s = replace(settings, bankroll_usd=100, risk=RiskSettings(max_order_usd=1000))
    ct = _ct(api, db, s, norm=0.3)
    full = ct.evaluate(W, 0.3, _trade(tx="a"), bankroll=100).legs[0].shares
    order(db, "other", "o1", 60.0)                  # $60 nyangkut
    assert ct.free_bankroll() == pytest.approx(40)
    half = ct.evaluate(W, 0.3, _trade(tx="b")).legs[0].shares
    assert half == pytest.approx(full * 0.4, rel=0.02)


# ── DB ───────────────────────────────────────────────────────────────────────
def test_aggregates_match_python_groups(db):
    order(db, "a", "ta", 4.0)
    order(db, "b", "tb", 6.0)
    db.set_resolution("a", {"ta": 1.0})
    gid = new_group_id()                            # basket 2 leg, 1 settle
    order(db, "c", "tc", 3.0, gid=gid)
    order(db, "d", "td", 3.0, gid=gid)
    db.set_resolution("c", {"tc": 0.0})
    order(db, "z", "tz", 5.0, end="2000-01-01")     # zombie
    assert db.open_exposure(14) == pytest.approx(6.0 + 6.0)
    assert db.realized_pnl_since(0) == pytest.approx(10 - 4.0)
    db.set_resolution("d", {"td": 1.0})
    assert db.realized_pnl_since(0) == pytest.approx(6.0 + (10 - 6.0))
    assert db.realized_pnl_since(time.time() + 60) == 0


def test_market_exposure_per_mode(db, settings):
    order(db, "m", "t", 10.0, mode="paper")
    assert db.market_exposure("m", mode="paper") == 10
    assert db.market_exposure("m", mode="live") == 0


def test_migrates_old_trader_table(tmp_path):
    import sqlite3

    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE trader_stats (wallet TEXT PRIMARY KEY, markets INTEGER, "
              "mean_edge REAL, t_stat REAL, shrunk_edge REAL, updated_at REAL)")
    c.execute("INSERT INTO trader_stats VALUES ('0xa', 50, 0.1, 3, 0.05, 0)")
    c.commit()
    c.close()
    db = DB(p)
    r = db.trader_stats()[0]
    assert r["norm_edge"] is None and r["holdout_n"] is None


# ── settle ───────────────────────────────────────────────────────────────────
def test_payouts_split_market():
    m = {"closed": True, "tokens": [{"token_id": "a", "winner": False, "price": 0.5},
                                    {"token_id": "b", "winner": False, "price": 0.5}]}
    assert payouts(m) == {"a": 0.5, "b": 0.5}
    pending = {"closed": True, "tokens": [{"token_id": "a", "winner": False, "price": 0.62},
                                          {"token_id": "b", "winner": False, "price": 0.38}]}
    assert payouts(pending) is None


def test_resolve_rotates_when_over_budget(api, db, settings):
    for i in range(5):
        order(db, f"c{i}", f"t{i}", 1.0, end=f"2099-01-0{i + 1}")
    bot = Bot(settings, api=api, db=db, telegram=Telegram("", ""), log=lambda *_: None)
    seen = []
    api.market = lambda cid, fresh=False: seen.append(cid)
    bot.resolve(budget=2)
    bot.resolve(budget=2)
    bot.resolve(budget=2)
    assert seen[:4] == ["c0", "c1", "c2", "c3"] and seen[4] == "c4"


# ── cursor ───────────────────────────────────────────────────────────────────
def test_cursor_not_advanced_when_evaluate_fails(api, db, settings):
    ct = _ct(api, db, settings, norm=0.3)
    db.set_trader_last_ts(W, 0)
    api.trades[W] = [_trade()]
    errors = []
    ct.on_error = lambda src, e: errors.append(e)
    real = api.market
    api.market = lambda cid, fresh=False: (_ for _ in ()).throw(RuntimeError("API mati"))
    assert ct.decisions(log=lambda *_: None) == []
    assert errors and db.trader_last_ts(W) == 0       # dicoba lagi siklus depan
    api.market = real
    assert len(ct.decisions(log=lambda *_: None)) == 1
    assert db.trader_last_ts(W) > 0


def test_paginates_active_trader(api, db, settings):
    ct = _ct(api, db, settings, norm=0.3)
    db.set_trader_last_ts(W, time.time() - 300)
    now = time.time()
    page2 = [_trade(tx="old-but-new", timestamp=now - 100, token_id="y1")]
    calls = []

    def user_trades(wallet, limit=100, cursor=None):
        calls.append(cursor)
        if cursor is None:
            return [_trade(tx=f"p1-{i}", timestamp=now - i) for i in range(3)], "next"
        return page2, None

    api.user_trades = user_trades
    trades, newest = ct.new_trades(W)
    assert calls == [None, "next"] and len(trades) == 4 and newest == pytest.approx(now)


# ── rem rugi + paper fill ────────────────────────────────────────────────────
def _leg(price=0.5, shares=5, token="t1", cid="c1"):
    return Leg(cid, token, "Yes", "m", shares, make_book([(price, 100)], token=token), 0.0,
               "2099-01-01")


def test_unrealized_loss_counts_toward_daily_limit(api, db, settings):
    s = replace(settings, risk=RiskSettings(daily_loss_limit_usd=5.0))
    order(db, "open", "to", 10.0, shares=20)       # beli 20 share @0.50
    api.mids["to"] = 0.10                           # sekarang cuma $2
    bot = Bot(s, api=api, db=db, telegram=Telegram("", ""), log=lambda *_: None)
    bot.mark_to_market(force=True)
    assert bot.executor.unrealized("paper") == pytest.approx(-8.0)
    res = bot.executor.submit(Decision("copytrade", [_leg(cid="new")]))
    assert res.status == "blocked" and "posisi terbuka" in res.reason


def test_paper_refetches_book_and_respects_limit(api):
    leg = _leg(price=0.50)
    api.book_map["t1"] = make_book([(0.55, 100)], token="t1")     # harga lari
    r = PaperBroker(api).buy(leg)
    assert r.status == "rejected" and "harga lari" in r.error
    api.book_map["t1"] = make_book([(0.49, 100)], token="t1")     # malah lebih murah
    r = PaperBroker(api).buy(leg)
    assert r.status == "filled" and r.cost == pytest.approx(5 * 0.49)


def test_paper_partial_does_not_kill(api, db, settings):
    api.book_map["x"] = make_book([(0.5, 100)], token="x")
    api.book_map["y"] = make_book([(0.9, 100)], token="y")        # leg 2 lari
    ex = Executor(settings, db, api=api)
    res = ex.submit(Decision("arbitrage", [_leg(token="x", cid="a"), _leg(token="y", cid="b")]))
    assert res.status == "partial" and not db.killed()


# ── bet dua sisi ─────────────────────────────────────────────────────────────
def test_copytrade_skips_opposite_side_and_stacking(api, db, settings):
    ct = _ct(api, db, settings, norm=0.3)
    api.book_map["n1"] = make_book([(0.41, 1000)], bids=[(0.39, 1000)], token="n1")
    order(db, "c1", "n1", 4.0)                      # udah pegang sisi NO
    db.set_trader_last_ts(W, 0)
    api.trades[W] = [_trade()]                      # trader beli YES
    assert ct.decisions(log=lambda *_: None) == []
    assert "sisi lawan" in db.query("SELECT reason FROM signals")[0]["reason"]


def test_two_traders_opposite_sides_same_cycle(api, db, settings):
    ct = _ct(api, db, settings, norm=0.3)
    db.upsert_trader_stats("0xb", 100, 0.05, 4.0, 0.05, norm_edge=0.3, holdout_n=30, holdout_edge=0.05)
    api.book_map["n1"] = make_book([(0.41, 1000)], bids=[(0.39, 1000)], token="n1")
    db.set_trader_last_ts(W, 0)
    db.set_trader_last_ts("0xb", 0)
    api.trades[W] = [_trade()]
    api.trades["0xb"] = [_trade(tx="b1", token_id="n1", outcome="No")]
    ds = ct.decisions(log=lambda *_: None)
    assert len(ds) == 1


def test_executor_blocks_opposite_side(db, settings):
    order(db, "c1", "no", 4.0)
    res = Executor(settings, db).submit(Decision("copytrade", [_leg(token="yes", cid="c1")]))
    assert res.status == "blocked" and "sisi lawan" in res.reason


# ── arsip ────────────────────────────────────────────────────────────────────
def test_archive_resets_paper_but_keeps_data(db, settings):
    from npb import export

    order(db, "a", "ta", 5.0)
    order(db, "b", "tb", 5.0)
    db.set_resolution("a", {"ta": 0.0})                  # rugi $5
    db.set_meta("mtm:paper", json.dumps({"ts": time.time(), "unrealized": -3}))
    with pytest.raises(ValueError):
        db.archive("v1")
    assert db.archive("arsip-v1") == 2
    assert db.groups(mode="paper") == [] and db.open_exposure(14, mode="paper") == 0
    assert db.realized_pnl_since(0, mode="paper") == 0
    assert db.archives() == [{"label": "arsip-v1", "bets": 2, "settled": 1, "pnl": -5.0}]
    d = export.build(db, settings)
    assert d["bets"] == [] and d["summary"]["bets"] == 0 and d["archives"][0]["pnl"] == -5.0
    res = Executor(settings, db).submit(Decision("copytrade", [_leg(cid="new")]))
    assert res.status == "filled"                       # rem rugi & mtm ikut reset
