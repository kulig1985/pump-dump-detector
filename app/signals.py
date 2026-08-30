"""SignalService -- a trigger utani lanc osszefogasa.

trigger -> OrderBookAnalyzer + TAAnalyzer (parhuzamosan) -> score -> MongoDB
        -> Telegram -> opcionalisan TradingService
"""
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from . import orderbook, ta, scoring, telegram, events

log = logging.getLogger("signal")


class SignalService:
    def __init__(self, cfg, db, notifier, trader):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.trader = trader
        self.recent = deque()      # (ts, symbol, direction) a friss jelzesekrol

    async def handle_trigger(self, trigger):
        symbol = trigger["symbol"]
        try:
            await self._process(trigger)
        except Exception as e:
            log.exception("[%s] signal feldolgozas hiba: %s", symbol, e)

    async def _process(self, trigger):
        symbol = trigger["symbol"]
        c = self.cfg.detector

        ob, ta_result = await asyncio.gather(
            orderbook.analyze(symbol, trigger["price"], trigger["direction"], c),
            ta.analyze(symbol, trigger["price"], c),
        )
        score, reason, parts = scoring.score_signal(trigger, ob, ta_result, c)
        recent = self._count_recent(symbol, trigger["direction"], c["signalWindowMinutes"])

        ch = trigger["changes"]
        signal = {
            "timestamp": datetime.now(timezone.utc),
            "symbol": symbol,
            "direction": trigger["direction"],
            "price": trigger["price"],
            "priceChange": {"s1": ch[1], "s3": ch[3], "s5": ch[5]},
            "ema": ta_result,
            "orderBook": _without_snapshot(ob),
            "score": score,
            "reason": reason,
            "recent": recent,
            "telegram": {"sent": False, "error": None},
            "trade": {"executed": False, "orderId": None, "error": None},
        }

        if score < c["minSignalScore"]:
            log.info("[%s] SCORE %d/100 -- kuszob (%d) alatt, nem kuldjuk | %s",
                     symbol, score, c["minSignalScore"], reason)
            events.add(f"{symbol:<14} score {score:>3}/100 -- kuszob ({c['minSignalScore']}) "
                       f"alatt, nem kuldtuk")
            await self._save(signal, trigger, ob, ta_result, parts)
            return

        log.warning("[%s] SCORE %d/100 | %s", symbol, score, reason)

        signal["trade"] = await self.trader.maybe_open(signal)
        signal["telegram"] = await self.notifier.send(symbol, telegram.format_signal(signal))
        await self._save(signal, trigger, ob, ta_result, parts)

        kimenet = "TELEGRAM ELKULDVE" if signal["telegram"]["sent"] else \
                  f"Telegram NEM ment ki ({signal['telegram']['error']})"
        events.add(f"{symbol:<14} score {score:>3}/100 -- {kimenet}  "
                   f"({recent['sameSymbolSameDirection']}. {trigger['direction']} "
                   f"{recent['windowMinutes']} percen belul)")

    async def _save(self, signal, trigger, ob, ta_result, parts):
        symbol = signal["symbol"]
        result = await self.db.signals.insert_one(signal)

        # A snapshot mentese kulon van kezelve: ha ez elszall, a signal akkor is
        # megmarad, es hangosan megmondjuk, mi a baj -- nem tunik el egy altalanos
        # "feldolgozas hiba" sorban.
        try:
            snap = await self.db.snapshots.insert_one({
                "timestamp": signal["timestamp"],
                "signalId": result.inserted_id,
                "symbol": symbol,
                "price": signal["price"],
                "priceHistory": [[ts, p] for ts, p in trigger["history"]],
                "orderBook": ob["snapshot"] if ob else None,
                "ema": ta_result,
                "scoreInputs": parts,
            })
            log.info("[%s] elmentve: signals %s + market_snapshots %s",
                     symbol, result.inserted_id, snap.inserted_id)
        except Exception as e:
            log.error("[%s] a market_snapshots mentese NEM sikerult: %s: %s",
                      symbol, type(e).__name__, e)
            events.add(f"{symbol:<14} market_snapshots mentes HIBA: {e}")


    def _count_recent(self, symbol, direction, window_minutes):
        """Hanyadik ez a jelzes az adott iranyban az elmult ablakban."""
        now = time.time()
        cutoff = now - window_minutes * 60
        while self.recent and self.recent[0][0] < cutoff:
            self.recent.popleft()
        self.recent.append((now, symbol, direction))
        return {
            "windowMinutes": window_minutes,
            "sameSymbolSameDirection": sum(1 for _, s, d in self.recent
                                           if s == symbol and d == direction),
            "marketLong": sum(1 for _, _, d in self.recent if d == "LONG"),
            "marketShort": sum(1 for _, _, d in self.recent if d == "SHORT"),
        }


def _without_snapshot(ob):
    """A nyers 20 szintes konyv a snapshots collectionbe megy, a signalba nem kell."""
    return {k: v for k, v in ob.items() if k != "snapshot"} if ob else None
