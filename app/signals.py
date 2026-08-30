"""SignalService -- a detektorok jelzese utani lanc osszefogasa.

signal -> OrderBookAnalyzer + TAAnalyzer (parhuzamosan) -> score -> MongoDB
       -> Telegram -> opcionalisan TradingService

Detektor-fuggetlen: barmelyik detektor jelzese ugyanezen az uton megy vegig.
A detektor-specifikus resz a signal "detail" es "lines" mezojeben utazik.
"""
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from . import orderbook, ta, scoring, telegram, events, binance_rest, outcome

log = logging.getLogger("signal")


class SignalService:
    def __init__(self, cfg, db, notifier, trader):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.trader = trader
        self.recent = deque()      # (ts, symbol, direction) a friss jelzesekrol

    async def handle_trigger(self, raw):
        """Egy detektor jelzese. Sose dob kivetelt tovabb a stream fele."""
        try:
            await self._process(raw)
        except Exception as e:
            log.exception("[%s] signal feldolgozas hiba: %s", raw.get("symbol"), e)

    async def _process(self, raw):
        symbol, direction = raw["symbol"], raw["direction"]
        detector = raw["detector"]
        # a kozos beallitasok (order book, EMA, jelzes-ablak) a detector dokumentumban
        # vannak, a kuszob viszont a jelzest ado detektor sajat configjabol jon
        shared = self.cfg.detector
        own = getattr(self.cfg, raw["configKey"], shared)
        min_score = own.get("minSignalScore", shared["minSignalScore"])

        ob, ta_result = await asyncio.gather(
            orderbook.analyze(symbol, raw["price"], direction, shared),
            ta.analyze(symbol, raw["price"], shared),
        )
        # Ha a mozgas nem nagyobb erdemben a spreadnel, akkor nem mozgas tortent,
        # csak valaki atlepte a spreadet -- ezt nem lehet lekereskedni.
        elutasitas = self._spread_check(raw, ob, shared)
        if elutasitas:
            log.info("[%s] %s eldobva: %s", symbol, detector, elutasitas)
            events.add(f"{symbol:<14} {detector} eldobva -- {elutasitas}")
            return

        score, reason, parts = scoring.score_signal(raw, ob, ta_result, shared)
        recent = self._count_recent(detector, symbol, direction,
                                    shared["signalWindowMinutes"])

        signal = {
            "timestamp": datetime.now(timezone.utc),
            "detector": detector,
            "symbol": symbol,
            "direction": direction,
            "price": raw["price"],
            "quoteVolume24h": binance_rest.SYMBOL_VOLUME.get(symbol),
            "strength": round(raw["strength"], 3),
            "detail": raw["detail"],          # detektor-specifikus bizonyitek
            "lines": raw["lines"],
            "ema": ta_result,
            "orderBook": _without_snapshot(ob),
            "score": score,
            "reason": reason,
            "recent": recent,
            "telegram": {"sent": False, "error": None},
            "trade": {"executed": False, "orderId": None, "error": None},
        }

        if score < min_score:
            log.info("[%s] %s SCORE %d/100 -- kuszob (%d) alatt, nem kuldjuk | %s",
                     symbol, detector, score, min_score, reason)
            events.add(f"{symbol:<14} {detector} score {score:>3}/100 -- "
                       f"kuszob ({min_score}) alatt, nem kuldtuk")
            await self._save(signal, raw, ob, ta_result, parts)
            return

        log.warning("[%s] %s SCORE %d/100 | %s", symbol, detector, score, reason)

        signal["trade"] = await self.trader.maybe_open(signal)
        signal["telegram"] = await self.notifier.send(
            symbol, telegram.format_signal(signal), detector)
        await self._save(signal, raw, ob, ta_result, parts)

        kimenet = "TELEGRAM ELKULDVE" if signal["telegram"]["sent"] else \
                  f"Telegram NEM ment ki ({signal['telegram']['error']})"
        events.add(f"{symbol:<14} {detector} score {score:>3}/100 -- {kimenet}  "
                   f"({recent['sameDirection']}. {direction} "
                   f"{recent['windowMinutes']} percen belul)")

    async def _save(self, signal, raw, ob, ta_result, parts):
        symbol = signal["symbol"]
        result = await self.db.signals.insert_one(signal)
        # minden mentett jelzest lemerunk -- a kuszob alattiakat is, kulonben nem
        # derulne ki, hogy jo helyen van-e a kuszob
        asyncio.create_task(outcome.track(self.db, result.inserted_id, signal,
                                          self.cfg.detector))

        # A snapshot mentese kulon van kezelve: ha ez elszall, a signal akkor is
        # megmarad, es hangosan megmondjuk, mi a baj -- nem tunik el egy altalanos
        # "feldolgozas hiba" sorban.
        try:
            snap = await self.db.snapshots.insert_one({
                "timestamp": signal["timestamp"],
                "signalId": result.inserted_id,
                "detector": signal["detector"],
                "symbol": symbol,
                "price": signal["price"],
                "priceHistory": [[ts, p] for ts, p in raw["history"]],
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


    @staticmethod
    def _spread_check(raw, ob, cfg):
        """None, ha rendben; kulonben az elutasitas oka szovegesen."""
        move = raw.get("movePct")
        if ob is None or move is None:
            return None
        spread = ob.get("spreadPct")
        if not spread:
            return None
        kell = cfg["minMoveToSpreadRatio"] * spread
        if move < kell:
            return (f"a mozgas ({move:.3f}%) nem eri el a spread "
                    f"{cfg['minMoveToSpreadRatio']:.0f}-szereset ({kell:.3f}%)")
        return None

    def _count_recent(self, detector, symbol, direction, window_minutes):
        """Hanyadik ez a jelzes ettol a detektortol, ebben az iranyban, az ablakban."""
        now = time.time()
        cutoff = now - window_minutes * 60
        while self.recent and self.recent[0][0] < cutoff:
            self.recent.popleft()
        self.recent.append((now, detector, symbol, direction))
        return {
            "windowMinutes": window_minutes,
            "sameDirection": sum(1 for _, det, s, d in self.recent
                                 if det == detector and s == symbol and d == direction),
            "marketLong": sum(1 for _, det, _, d in self.recent
                              if det == detector and d == "LONG"),
            "marketShort": sum(1 for _, det, _, d in self.recent
                               if det == detector and d == "SHORT"),
        }


def _without_snapshot(ob):
    """A nyers 20 szintes konyv a snapshots collectionbe megy, a signalba nem kell."""
    return {k: v for k, v in ob.items() if k != "snapshot"} if ob else None
