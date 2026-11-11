import json

from npb import export
from npb.db import new_group_id


def test_export_has_bets_scores_and_drift(db, settings, tmp_path):
    gid = new_group_id()
    db.add_order(strategy="copytrade", mode="paper", group_id=gid, signal_id=None,
                 condition_id="c", token_id="t", outcome="Yes", market="Will X?",
                 end_date="2099-01-01", shares_req=10, limit_price=0.4, status="filled",
                 shares=10, cost=4.0, fee=0.1)
    db.set_resolution("c", {"t": 1.0})
    sid = db.add_signal("copytrade", "k1", token_id="t", market="Will X?", mid=0.40,
                        decision="take")
    db.add_mark(sid, 3600, 0.45)
    db.upsert_trader_stats("0xabc", 50, 0.05, 2.5, 0.04)

    out = tmp_path / "d.json"
    export.write(str(out), db, settings, {"0xabc": 0.04})
    d = json.loads(out.read_text(encoding="utf-8"))

    assert d["bets"][0]["market"] == "Will X?" and d["bets"][0]["pnl"] == 10 - 4.1
    cp = [s for s in d["scores"] if s["strategy"] == "copytrade" and s["mode"] == "paper"][0]
    assert cp["n_resolved"] == 1 and not cp["passed"]
    assert abs(d["drift"][0]["avg_drift"] - 0.05) < 1e-9
    assert d["traders"][0]["followed"] is True
    assert d["signal_counts_24h"] == {"copytrade:take": 1}
