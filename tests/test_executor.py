from dataclasses import replace

from npb.broker import Leg, LegResult
from npb.config import RiskSettings
from npb.executor import Decision, Executor

from .conftest import make_book


def leg(cid="c1", price=0.5, shares=5, size=100, token="t1"):
    return Leg(cid, token, "Yes", "m", shares, make_book([(price, size)], token=token), 0.0,
               "2099-01-01")


def test_paper_fill_recorded(db, settings):
    ex = Executor(settings, db)
    res = ex.submit(Decision("copytrade", [leg()]))
    assert res.status == "filled" and res.mode == "paper"
    g = db.groups()[0]
    assert g.cost == 2.5 and not g.resolved


def test_hard_cap_blocks(db, settings):
    s = replace(settings, risk=RiskSettings(max_order_usd=2.0))
    res = Executor(s, db).submit(Decision("copytrade", [leg()]))
    assert res.status == "blocked" and "hard cap" in res.reason
    assert db.groups() == []


def test_exposure_and_market_caps(db, settings):
    s = replace(settings, risk=RiskSettings(max_exposure_usd=6.0, max_market_usd=100))
    ex = Executor(s, db)
    assert ex.submit(Decision("copytrade", [leg("a")])).status == "filled"
    assert ex.submit(Decision("copytrade", [leg("b")])).status == "filled"
    res = ex.submit(Decision("copytrade", [leg("c")]))
    assert res.status == "blocked" and "eksposur" in res.reason

    s2 = replace(settings, risk=RiskSettings(max_market_usd=4.0))
    ex2 = Executor(s2, db)
    res = ex2.submit(Decision("copytrade", [leg("a")]))
    assert res.status == "blocked" and "per market" in res.reason


def test_kill_switch(db, settings):
    db.set_meta("kill", "test")
    res = Executor(settings, db).submit(Decision("copytrade", [leg()]))
    assert res.status == "blocked" and "kill" in res.reason


def test_daily_loss_limit(db, settings):
    s = replace(settings, risk=RiskSettings(daily_loss_limit_usd=2.0))
    ex = Executor(s, db)
    ex.submit(Decision("copytrade", [leg("lost", token="tl")]))
    db.set_resolution("lost", {"tl": 0.0})          # rugi $2.50
    res = ex.submit(Decision("copytrade", [leg("next")]))
    assert res.status == "blocked" and "rugi" in res.reason


def test_live_needs_all_gates(db, settings):
    class FakeLive:
        mode = "live"

        def buy(self, l):
            return LegResult("filled", l.shares, 1.0, 0.0, 0.5)

    live = FakeLive()
    # mode live diminta, broker ada, tapi strategi belum di-promote -> paper
    s = replace(settings, mode="live", live_enabled=True)
    ex = Executor(s, db, live_broker=live)
    assert ex.mode_for("copytrade") == "paper"
    db.set_meta("promoted:copytrade", "1")
    assert ex.mode_for("copytrade") == "live"
    # gate kedua mati -> paper walau promoted
    ex2 = Executor(replace(settings, mode="live", live_enabled=False), db, live_broker=live)
    assert ex2.mode_for("copytrade") == "paper"


def test_partial_fill_kills_live(db, settings):
    calls = []

    class FlakyLive:
        mode = "live"

        def buy(self, l):
            calls.append(l.token_id)
            if len(calls) == 1:
                return LegResult("filled", l.shares, 2.5, 0.0, 0.5)
            return LegResult("rejected", error="FOK gagal")

    db.set_meta("promoted:arbitrage", "1")
    s = replace(settings, mode="live", live_enabled=True)
    ex = Executor(s, db, live_broker=FlakyLive())
    res = ex.submit(Decision("arbitrage", [leg("a", token="x"), leg("b", token="y")]))
    assert res.status == "partial"
    assert db.killed()
