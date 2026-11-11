import json
from dataclasses import replace

import pytest

from npb.config import ArbitrageSettings, FeeSettings
from npb.strategies.arbitrage import Arbitrage, best_sizing, candidate_baskets, parse_event

from .conftest import make_book


def market(cid, ask, bid, closed=False, yes_price=None):
    return {
        "conditionId": cid, "question": cid, "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"{cid}-y", f"{cid}-n"]),
        "outcomePrices": json.dumps([str(yes_price if yes_price is not None else ask),
                                     str(1 - (yes_price or ask))]),
        "bestAsk": ask, "bestBid": bid, "active": True, "closed": closed,
        "enableOrderBook": True, "acceptingOrders": True, "endDate": "2099-01-01T00:00:00Z",
    }


def event(markets, augmented=False):
    return {"title": "E", "negRisk": True, "negRiskAugmented": augmented, "markets": markets}


def test_binary_and_non_negrisk_ignored():
    assert parse_event({"negRisk": False, "markets": [market("a", .3, .2)]}) is None


def test_event_with_winner_skipped_and_resolved_no_dropped():
    assert parse_event(event([market("a", .3, .2, closed=True, yes_price=1.0),
                              market("b", .3, .2), market("c", .3, .2)])) is None
    ms, _ = parse_event(event([market("a", .3, .2, closed=True, yes_price=0.0),
                               market("b", .3, .2), market("c", .3, .2)]))
    assert [m.condition_id for m in ms] == ["b", "c"]


def test_placeholder_markets_make_event_augmented():
    # Kasus nyata (Guinea-Bissau): flag augmented False, tapi ada "Candidate A".."Other"
    # yang gak aktif & gak punya harga. YES basket di sini bisa bayar $0.
    placeholder = {"conditionId": "p", "question": "Candidate A", "active": False,
                   "closed": False, "outcomePrices": None, "bestAsk": 1, "bestBid": 0}
    ev = event([market("a", .2, .19), market("b", .2, .19), placeholder])
    ms, augmented = parse_event(ev)
    assert augmented and [m.condition_id for m in ms] == ["a", "b"]
    assert all(b.side != "YES" for b in candidate_baskets(ev, 0.03))


def test_past_end_date_event_skipped():
    ev = event([market("a", .2, .19), market("b", .2, .19)])
    ev["endDate"] = "2025-11-24T04:59:00Z"
    assert parse_event(ev) is None


def test_prefilter_yes_and_no_baskets():
    cheap = event([market("a", .30, .29), market("b", .30, .29), market("c", .30, .29)])
    assert [b.side for b in candidate_baskets(cheap, 0.0)] == ["YES"]
    rich = event([market("a", .41, .40), market("b", .41, .40), market("c", .41, .40)])
    sides = [b.side for b in candidate_baskets(rich, 0.0)]
    assert sides == ["NO"]
    nob = [b for b in candidate_baskets(rich, 0.0) if b.side == "NO"][0]
    assert nob.payout_per_set == 2.0
    # augmented: YES basket gak aman -> dibuang
    assert candidate_baskets(event(cheap["markets"], augmented=True), 0.0) == []


def test_best_sizing_equal_shares_and_depth_limit():
    books = [make_book([(0.30, 20), (0.40, 100)]), make_book([(0.30, 20), (0.40, 100)]),
             make_book([(0.30, 20), (0.40, 100)])]
    sz = best_sizing(books, [0, 0, 0], payout=1.0, max_sets=100, per_leg_cap=100, min_edge=0.01)
    # 20 set di 0.30 = profit 0.10/set; di atasnya 1.20/set = rugi -> berhenti di 20.
    assert sz.sets == 20
    assert sz.profit == pytest.approx(20 * (1 - 0.9))


def test_best_sizing_respects_fees_and_cap():
    books = [make_book([(0.32, 1000)]) for _ in range(3)]
    assert best_sizing(books, [0, 0, 0], 1.0, 100, 100, 0.01) is not None
    # fee 10% * min(p,1-p) = 0.032/leg -> 0.96 + 0.096 > 1 -> gak ada peluang
    assert best_sizing(books, [0.1] * 3, 1.0, 100, 100, 0.01) is None
    sz = best_sizing(books, [0, 0, 0], 1.0, 1000, per_leg_cap=5.0, min_edge=0.01)
    assert sz.sets * 0.32 <= 5.0


def test_strategy_builds_equal_share_legs(api, db, settings):
    api.events = [event([market("a", .30, .29), market("b", .30, .29), market("c", .30, .29)])]
    for cid in "abc":
        api.book_map[f"{cid}-y"] = make_book([(0.30, 50)], token=f"{cid}-y")
        api.markets[cid] = {"taker_base_fee": 0}
    s = replace(settings, fees=FeeSettings(), arbitrage=ArbitrageSettings(prefilter_slack=0.0))
    ds = Arbitrage(s, api, db).decisions(log=lambda *_: None)
    assert len(ds) == 1
    shares = {l.shares for l in ds[0].legs}
    assert len(shares) == 1 and len(ds[0].legs) == 3
    # sinyal yang sama dalam 10 menit gak dobel
    assert Arbitrage(s, api, db).decisions(log=lambda *_: None) == []
