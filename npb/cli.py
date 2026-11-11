"""Entry point: python -m npb <command>."""
from __future__ import annotations

import argparse
import sys
import threading

from . import scorecard
from .config import Settings

STRATEGIES = ("copytrade", "arbitrage")


def _bot(settings: Settings):
    from .runner import Bot

    return Bot(settings)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="npb", description="new-polybot")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="jalankan 1 siklus (atau --loop)")
    run.add_argument("--loop", action="store_true")
    run.add_argument("--telegram", action="store_true", help="sekalian dengerin command Telegram")
    sub.add_parser("status")
    sc = sub.add_parser("score", help="scorecard + status gerbang live")
    sc.add_argument("--mode", choices=("paper", "live"), default="paper")
    sub.add_parser("traders", help="ukur ulang skill trader sekarang")
    sub.add_parser("arb-scan", help="scan arbitrage sekali, tanpa eksekusi")
    sub.add_parser("resolve", help="cek market yang sudah settle")
    pr = sub.add_parser("promote", help="izinkan strategi live (kalau lulus gerbang)")
    pr.add_argument("strategy", choices=STRATEGIES)
    de = sub.add_parser("demote")
    de.add_argument("strategy", choices=STRATEGIES)
    k = sub.add_parser("kill")
    k.add_argument("reason", nargs="*")
    sub.add_parser("resume")
    a = p.parse_args(argv)

    s = Settings.from_env()
    bot = _bot(s)

    if a.cmd == "run":
        if a.telegram and bot.tg.enabled:
            threading.Thread(target=bot.tg.listen, args=(bot.telegram_handlers(),),
                             kwargs={"stop": bot.stop_event.is_set}, daemon=True).start()
        try:
            bot.loop() if a.loop else bot.cycle()
        except KeyboardInterrupt:
            bot.stop_event.set()
    elif a.cmd == "status":
        print(bot.status())
    elif a.cmd == "score":
        for name in STRATEGIES:
            print(scorecard.format_score(scorecard.score(bot.db, name, s.gate, a.mode)))
            print()
    elif a.cmd == "traders":
        bot.copytrade.refresh_stats()
        for r in bot.db.trader_stats()[:25]:
            print(f"{r['wallet']} n={r['markets']:<4} edge {r['mean_edge']:+.4f} "
                  f"t {r['t_stat']:5.2f} shrunk {r['shrunk_edge']:+.4f}")
        print(f"\ndiikuti: {len(bot.copytrade.followed())} trader")
    elif a.cmd == "arb-scan":
        opps = bot.arbitrage.opportunities()
        for bk, sz, _, _ in opps:
            print(f"{bk.side} basket {len(bk.markets)} leg · {bk.event[:60]} · {sz.sets:.2f} set "
                  f"· biaya ${sz.cost:.2f} · profit ${sz.profit:+.2f} ({sz.edge_per_set:+.3f}/set)")
        print(f"{len(opps)} peluang setelah fee.")
    elif a.cmd == "resolve":
        print(f"{bot.resolve()} market settle")
    elif a.cmd == "promote":
        res = scorecard.score(bot.db, a.strategy, s.gate, "paper")
        print(scorecard.format_score(res))
        if not res.passed:
            print(f"\n⛔ {a.strategy} BELUM lulus gerbang — gak di-promote.")
            return 1
        bot.db.set_meta(f"promoted:{a.strategy}", "1")
        print(f"\n✅ {a.strategy} di-promote. Live baru jalan kalau NPB_MODE=live "
              f"DAN LIVE_TRADING_ENABLED=true.")
    elif a.cmd == "demote":
        bot.db.set_meta(f"promoted:{a.strategy}", None)
        print(f"{a.strategy} balik ke paper.")
    elif a.cmd == "kill":
        bot.db.set_meta("kill", " ".join(a.reason) or "manual")
        print("⛔ kill switch NYALA.")
    elif a.cmd == "resume":
        bot.db.set_meta("kill", None)
        print("▶️ kill switch mati.")
    return 0
