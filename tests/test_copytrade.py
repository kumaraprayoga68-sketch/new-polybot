import time
from dataclasses import replace

import pytest

from npb.config import CopytradeSettings
from npb.strategies.copytrade import Copytrade, kelly_shares
from npb.traders import edge_per_market, skill_from_edges

from .conftest import make_book

W = "0xtrader"


def trade(cid="c1", tok="y1", price=0.40, size=500, ts=None, side="BUY", tx="tx1"):
    return {"condition_id": cid, "token_id": tok, "price": price, "size": size, "side": side,
            "timestamp": ts if ts is not None else time.time(), "transaction_hash": tx,
            "outcome": "Yes", "title": "Market"}


def setup(api, db, settings, ask=0.41, edge=0.08, **cfg):
    db.upsert_trader_stats(W, 100, edge, 4.0, edge, norm_edge=edge / 0.49, holdout_n=30,
                           holdout_edge=edge)
    api.markets["c1"] = {"closed": False, "accepting_orders": True, "taker_base_fee": 0,
                         "end_date_iso": "2099-01-01T00:00:00Z"}
    api.book_map["y1"] = make_book([(ask, 1000)], bids=[(ask - 0.02, 1000)], token="y1")
    c = CopytradeSettings(max_days_to_resolve=100_000, **cfg)
    return Copytrade(replace(settings, copytrade=c), api, db)


def test_edge_uses_entry_price_not_win_rate():
    # 3 menang di 0.9 + 1 kalah di 0.9: win rate 75% tapi edge negatif.
    trades = [trade(cid=f"c{i}", tok=f"y{i}", price=0.9) for i in range(4)]
    res = {f"c{i}": {f"y{i}": (1.0 if i < 3 else 0.0)} for i in range(4)}
    edges = edge_per_market(trades, res)
    assert sum(edges) / len(edges) == pytest.approx(0.75 - 0.9)


def test_edge_aggregates_per_market_and_skips_unresolved():
    trades = [trade(price=0.4, size=100), trade(price=0.6, size=100), trade(cid="c2", tok="y2")]
    edges = edge_per_market(trades, {"c1": {"y1": 1.0}})
    assert edges == [pytest.approx(1 - 0.5)]


def test_shrinkage_pulls_small_samples_to_zero():
    small = skill_from_edges(W, [0.2] * 5, shrink_k=30)
    big = skill_from_edges(W, [0.2] * 300, shrink_k=30)
    assert small.shrunk_edge < big.shrunk_edge < 0.2


def test_kelly_shares():
    assert kelly_shares(0.5, 0.5, 100, 0.25) == 0
    assert kelly_shares(0.6, 0.5, 100, 0.25) == pytest.approx(0.25 * 0.2 * 100 / 0.5)


def test_first_sight_does_not_copy_history(api, db, settings):
    ct = setup(api, db, settings)
    api.trades[W] = [trade()]
    assert ct.decisions(log=lambda *_: None) == []        # baru kenal: cuma set cursor
    api.trades[W] = [trade(tx="tx2", ts=time.time() + 1)]
    ds = ct.decisions(log=lambda *_: None)
    assert len(ds) == 1 and ds[0].legs[0].token_id == "y1"


def test_skips_old_small_and_sell_trades(api, db, settings):
    ct = setup(api, db, settings)
    db.set_trader_last_ts(W, 0)
    api.trades[W] = [trade(ts=time.time() - 3600, tx="old"), trade(size=10, tx="small"),
                     trade(side="SELL", tx="sell")]
    assert ct.decisions(log=lambda *_: None) == []


def test_slippage_guard(api, db, settings):
    ct = setup(api, db, settings, ask=0.45, edge=0.3)   # trader 0.40, kita 0.45 > +0.02
    db.set_trader_last_ts(W, 0)
    api.trades[W] = [trade()]
    assert ct.decisions(log=lambda *_: None) == []
    row = db.query("SELECT decision, reason FROM signals")[0]
    assert row["decision"] == "skip" and "slippage" in row["reason"]


def test_untrusted_trader_not_followed(api, db, settings):
    ct = setup(api, db, settings)
    hold = dict(norm_edge=0.1, holdout_n=30, holdout_edge=0.05)
    db.upsert_trader_stats("0xlucky", 10, 0.3, 5.0, 0.07, **hold)     # sampel kecil
    db.upsert_trader_stats("0xnoise", 200, 0.01, 0.8, 0.01, **hold)   # gak signifikan
    db.upsert_trader_stats("0xfaded", 200, 0.05, 4.0, 0.04, norm_edge=0.1, holdout_n=60,
                           holdout_edge=-0.02)                       # holdout negatif
    db.upsert_trader_stats("0xold", 200, 0.05, 4.0, 0.04)            # belum diukur ulang (v1)
    assert set(ct.followed()) == {W}


def test_size_capped_by_hard_cap(api, db, settings):
    ct = setup(api, db, settings, edge=0.3)
    db.set_trader_last_ts(W, 0)
    api.trades[W] = [trade()]
    d = ct.decisions(log=lambda *_: None)[0]
    assert d.legs[0].shares * 0.41 <= settings.risk.max_order_usd


def test_stats_measured_in_batches(api, db, settings):
    for i in range(5):
        api.trades[f"0xw{i}"] = []
    ct = Copytrade(settings, api, db)
    assert ct.stats_stale()
    ct.refresh_stats(log=lambda *_: None, batch=2)
    assert ct.stats_progress() == (2, 5) and ct.stats_stale()
    ct.refresh_stats(log=lambda *_: None, batch=2)
    ct.refresh_stats(log=lambda *_: None, batch=2)
    assert ct.stats_progress() == (5, 5)
    assert not ct.stats_stale()          # putaran kelar, belum lewat refresh_hours


def test_live_game_with_past_end_date_still_copied(api, db, settings):
    from datetime import datetime, timedelta, timezone

    ct = setup(api, db, settings)
    # Kasus nyata NBA: end_date = tanggal pertandingan 00:00, sekarang udah lewat ~7 jam.
    api.markets["c1"]["end_date_iso"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    db.set_trader_last_ts(W, 0)
    api.trades[W] = [trade()]
    assert len(ct.decisions(log=lambda *_: None)) == 1

    api.markets["c1"]["end_date_iso"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    api.trades[W] = [trade(tx="tx-stale")]
    assert ct.decisions(log=lambda *_: None) == []


def test_pool_setting_change_triggers_new_round(api, db, settings):
    for i in range(3):
        api.trades[f"0xw{i}"] = []
    ct = Copytrade(settings, api, db)
    ct.refresh_stats(log=lambda *_: None, batch=10)
    assert not ct.stats_stale()
    bigger = Copytrade(replace(settings, copytrade=CopytradeSettings(leaderboard_pool=300)), api, db)
    assert bigger.stats_stale()          # kandidat diperbanyak -> langsung ukur ulang
    bigger.refresh_stats(log=lambda *_: None, batch=10)
    assert not bigger.stats_stale()
