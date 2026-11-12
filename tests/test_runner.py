import json
from dataclasses import replace

from npb.config import ArbitrageSettings, CopytradeSettings
from npb.notify import Telegram
from npb.runner import Bot
from npb.scorecard import score

from .conftest import make_book


def test_full_cycle_paper_arb_then_settle(api, db, settings):
    mk = []
    for cid in "abc":
        mk.append({"conditionId": cid, "question": cid, "outcomes": json.dumps(["Yes", "No"]),
                   "clobTokenIds": json.dumps([f"{cid}-y", f"{cid}-n"]),
                   "outcomePrices": json.dumps(["0.3", "0.7"]), "bestAsk": .3, "bestBid": .29,
                   "active": True, "closed": False, "endDate": "2099-01-01T00:00:00Z"})
        api.book_map[f"{cid}-y"] = make_book([(0.30, 50)], token=f"{cid}-y")
        api.markets[cid] = {"taker_base_fee": 0, "closed": False, "tokens": []}
    api.events = [{"title": "E", "negRisk": True, "markets": mk}]
    s = replace(settings, copytrade=CopytradeSettings(enabled=False),
                arbitrage=ArbitrageSettings(prefilter_slack=0.0))
    bot = Bot(s, api=api, db=db, telegram=Telegram("", ""), log=lambda *_: None)

    bot.cycle()
    groups = db.groups(strategy="arbitrage")
    assert len(groups) == 1 and groups[0].legs == 3 and not groups[0].resolved

    for cid in "abc":
        api.markets[cid] = {"closed": True, "tokens": [
            {"token_id": f"{cid}-y", "winner": cid == "b"},
            {"token_id": f"{cid}-n", "winner": cid != "b"}]}
    bot.cycle()
    g = db.groups(strategy="arbitrage")[0]
    assert g.resolved and g.pnl > 0
    sc = score(db, "arbitrage", s.gate)
    assert sc.n_resolved == 1 and not sc.passed      # 1 sampel jelas belum cukup


def test_kill_switch_stops_trading_but_keeps_settling(api, db, settings):
    db.set_meta("kill", "test")
    bot = Bot(settings, api=api, db=db, telegram=Telegram("", ""), log=lambda *_: None)
    bot.cycle()
    assert db.groups() == []
    assert "kill switch: test" in bot.status()


def test_arbitrage_scan_result_recorded_even_when_empty(api, db, settings):
    from npb import export

    bot = Bot(replace(settings, copytrade=CopytradeSettings(enabled=False)), api=api, db=db,
              telegram=Telegram("", ""), log=lambda *_: None)
    bot.cycle()
    d = export.build(db, settings)
    assert d["arb_last_scan"]["prefilter"] == 0 and d["arb_last_scan"]["profitable"] == 0
    assert d["followed_count"] == 0 and d["last_take_ts"] is None
