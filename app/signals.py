"""SignalService -- a CANDIDATE-bol SIGNAL lesz.

A detektor annyit mond: "ez a mozgas rendkivuli ezen a paron". Itt gyulnek ossze a
kontextus-informaciok (order book, EMA), majd a jelzes mentodik es kimegy Telegramra.

Ami NINCS itt szandekosan: kereskedelmi terv, hozam/kockazat, dijszamitas, score.
A rendszer arra valo, hogy eszrevegye a pumpot, dumpot es a fordulot -- nem arra,
hogy megmondja, hogyan kereskedd le.
"""
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from . import orderbook, ta, telegram, events, binance_rest
from .links import binance_url

log = logging.getLogger("signal")


class SignalService:
    def __init__(self, cfg, db, notifier, trader):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.trader = trader
        self.recent = deque()          # (ts, detektor, symbol, irany)
        self.signals_today = 0

    async def handle_trigger(self, raw):
        """Egy detektor CANDIDATE-je. Sose dob kivetelt tovabb a stream fele."""
        try:
            await self._process(raw)
        except Exception as e:
            log.exception("[%s] feldolgozas hiba: %s", raw.get("symbol"), e)

    async def _process(self, raw):
        symbol, direction = raw["symbol"], raw["direction"]
        detector = raw["detector"]
        c = self.cfg.detector

        # Az order book es az EMA CSAK kontextus: informaciokent kerulnek a jelzesbe,
        # egyik sem utasithat el semmit.
        ob, ta_result = await asyncio.gather(
            orderbook.analyze(symbol, raw["price"], direction, c),
            ta.analyze(symbol, raw["price"], c),
        )
        recent = self._count_recent(detector, symbol, direction,
                                    c["signalWindowMinutes"])

        reasons = list(raw["reasons"])
        metrics = dict(raw["metrics"])
        if ob:
            if ob.get("spreadPct") is not None:
                reasons.append(f"spread {ob['spreadPct']:.3f}%")
                metrics["spreadPct"] = ob["spreadPct"]
            fal = ob.get("nearestSellWall") if direction == "LONG" \
                else ob.get("nearestBuyWall")
            reasons.append(f"fal a mozgas iranyaban {fal['distancePct']:.2f}%-ra"
                           if fal else "nincs fal a mozgas iranyaban")

        signal = {
            "timestamp": datetime.now(timezone.utc),
            "detector": detector,
            "symbol": symbol,
            "direction": direction,
            "price": raw["price"],
            "url": binance_url(symbol),
            "quoteVolume24h": binance_rest.SYMBOL_VOLUME.get(symbol),
            "reasons": reasons,
            "metrics": metrics,
            "ema": ta_result,
            "orderBook": _without_snapshot(ob),
            "recent": recent,
            "telegram": {"sent": False, "error": None},
            "trade": {"executed": False, "orderId": None, "error": None},
        }

        self.signals_today += 1
        log.info("SIGNAL     %-14s %-5s ar %.8g  %s  %s",
                 symbol, direction, raw["price"],
                 raw["reasons"][0] if raw["reasons"] else "", binance_url(symbol))
        events.add(f"{symbol:<14} SIGNAL {direction:<5} {detector}")

        signal["trade"] = await self.trader.maybe_open(signal)
        signal["telegram"] = await self.notifier.send(
            symbol, telegram.format_signal(signal,
                                           self.cfg.telegram.get("appLinkTemplate", "")),
            detector)
        await self._save(signal, raw, ob, ta_result)

    async def _save(self, signal, raw, ob, ta_result):
        symbol = signal["symbol"]
        result = await self.db.signals.insert_one(signal)
        try:
            await self.db.snapshots.insert_one({
                "timestamp": signal["timestamp"],
                "signalId": result.inserted_id,
                "detector": signal["detector"],
                "symbol": symbol,
                "price": signal["price"],
                "priceHistory": [[ts, p] for ts, p in raw["history"]],
                "orderBook": ob["snapshot"] if ob else None,
                "ema": ta_result,
                "metrics": signal["metrics"],
            })
        except Exception as e:
            log.error("[%s] a market_snapshots mentese NEM sikerult: %s: %s",
                      symbol, type(e).__name__, e)

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
            "detectorLong": sum(1 for _, det, _, d in self.recent
                                if det == detector and d == "LONG"),
            "detectorShort": sum(1 for _, det, _, d in self.recent
                                 if det == detector and d == "SHORT"),
        }


def _without_snapshot(ob):
    """A nyers konyv a snapshots collectionbe megy, a signalba nem kell."""
    return {k: v for k, v in ob.items() if k != "snapshot"} if ob else None
