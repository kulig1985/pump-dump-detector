"""SignalService -- a trigger utani lanc osszefogasa.

trigger -> OrderBookAnalyzer + TAAnalyzer (parhuzamosan) -> score -> MongoDB
        -> Telegram -> opcionalisan TradingService
"""
import asyncio
import logging
from datetime import datetime, timezone

from . import orderbook, ta, scoring, telegram

log = logging.getLogger("signal")


class SignalService:
    def __init__(self, cfg, db, notifier, trader):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.trader = trader

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
            "telegram": {"sent": False, "error": None},
            "trade": {"executed": False, "orderId": None, "error": None},
        }

        if score < c["minSignalScore"]:
            log.info("[%s] SCORE %d/100 -- kuszob (%d) alatt, csak mentjuk | %s",
                     symbol, score, c["minSignalScore"], reason)
            await self._save(signal, trigger, ob, ta_result, parts)
            return

        log.warning("[%s] SCORE %d/100 | %s", symbol, score, reason)

        signal["trade"] = await self.trader.maybe_open(signal)
        signal["telegram"] = await self.notifier.send(symbol, telegram.format_signal(signal))
        await self._save(signal, trigger, ob, ta_result, parts)

    async def _save(self, signal, trigger, ob, ta_result, parts):
        result = await self.db.signals.insert_one(signal)
        await self.db.snapshots.insert_one({
            "timestamp": signal["timestamp"],
            "signalId": result.inserted_id,
            "symbol": signal["symbol"],
            "price": signal["price"],
            "priceHistory": [[ts, p] for ts, p in trigger["history"]],
            "orderBook": ob["snapshot"] if ob else None,
            "ema": ta_result,
            "scoreInputs": parts,
        })


def _without_snapshot(ob):
    """A nyers 20 szintes konyv a snapshots collectionbe megy, a signalba nem kell."""
    return {k: v for k, v in ob.items() if k != "snapshot"} if ob else None
