import random

from npb.config import GateSettings
from npb.db import new_group_id
from npb.scorecard import score
from npb.stats import bootstrap_ratio_ci, t_stat


def add_bet(db, i, price, won, strategy="copytrade", mode="paper"):
    cid, tok = f"c{i}", f"t{i}"
    db.add_order(strategy=strategy, mode=mode, group_id=new_group_id(), signal_id=None,
                 condition_id=cid, token_id=tok, outcome="Yes", market="m", end_date="2099-01-01",
                 shares_req=10, limit_price=price, status="filled", shares=10, cost=10 * price)
    db.set_resolution(cid, {tok: 1.0 if won else 0.0})


def test_t_stat_and_ci():
    assert t_stat([1, 1, 1]) == 0.0
    assert t_stat([1, 2, 3]) > 3
    lo, hi = bootstrap_ratio_ci([1, -1, 2], [10, 10, 10])
    assert lo <= 2 / 30 <= hi


def test_no_edge_strategy_fails_gate(db):
    rng = random.Random(1)
    for i in range(400):               # menang persis sesuai harga = gak ada edge
        add_bet(db, i, 0.5, rng.random() < 0.5)
    sc = score(db, "copytrade", GateSettings())
    assert not sc.passed
    assert any(abs(b.win_rate - b.avg_price) < 0.1 for b in sc.buckets)


def test_real_edge_passes_gate(db):
    rng = random.Random(2)
    for i in range(400):
        add_bet(db, i, 0.5, rng.random() < 0.62)
    sc = score(db, "copytrade", GateSettings())
    assert sc.passed, sc.reasons


def test_too_few_bets_fails(db):
    for i in range(20):
        add_bet(db, i, 0.5, True)
    sc = score(db, "copytrade", GateSettings())
    assert not sc.passed and "settle" in sc.reasons[0]


def test_paper_and_live_scored_separately(db):
    add_bet(db, 1, 0.5, True, mode="live")
    assert score(db, "copytrade", GateSettings(), "paper").n_total == 0
    assert score(db, "copytrade", GateSettings(), "live").n_total == 1
