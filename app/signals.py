"""SignalService -- a CANDIDATE-bol SIGNAL vagy REJECTED lesz.

A detektor csak annyit mond: "ez a mozgas rendkivuli ezen a paron". Itt dol el,
hogy kereskedheto-e:

    order book akadaly   -> wall_immediately_ahead
    spread a mozgashoz   -> spread_too_wide
    hozam / kockazat     -> poor_reward_risk

Nincs 0-100 score. Minden dontes mogott egy megnevezett ok es egy mert szam all,
es mindketto Mongo-ba kerul -- igy utolag aggregalhato, mi miert esett ki.
"""
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from . import orderbook, ta, telegram, events, binance_rest, outcome, plan
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
        self.rejected_today = 0

    async def handle_trigger(self, raw):
        """Egy detektor CANDIDATE-je. Sose dob kivetelt tovabb a stream fele."""
        try:
            await self._process(raw)
        except Exception as e:
            log.exception("[%s] feldolgozas hiba: %s", raw.get("symbol"), e)

    # ------------------------------------------------------------------ validacio

    async def _process(self, raw):
        symbol, direction = raw["symbol"], raw["direction"]
        detector = raw["detector"]
        c = self.cfg.detector

        ob, ta_result = await asyncio.gather(
            orderbook.analyze(symbol, raw["price"], direction, c),
            ta.analyze(symbol, raw["price"], c),
        )
        terv = plan.build(raw, c)

        ok = self._validate(raw, ob, terv, c)
        if ok:
            await self._save_rejected(raw, ok, ob, terv, ta_result)
            return

        await self._emit(raw, ob, ta_result, terv, c)

    @staticmethod
    def _validate(raw, ob, terv, c):
        """None, ha rendben; kulonben a gepi elutasitasi ok."""
        # a mozgas legyen nagyobb, mint amennyibe a be- es kiszallas kerul
        if ob and ob.get("spreadPct") and raw.get("movePct") is not None:
            if raw["movePct"] < c["minMoveToSpreadRatio"] * ob["spreadPct"]:
                return "spread_too_wide"

        # kozvetlenul a mozgas iranyaban allo fal elrontja a scalpet
        if ob:
            akadaly = ob.get("obstacleAhead")
            if akadaly and akadaly["distancePct"] <= c["wallBlockDistancePct"]:
                return "wall_immediately_ahead"

        if terv is None:
            return "no_usable_plan"
        if terv["rewardRisk"] < c["minRewardRisk"]:
            return "poor_reward_risk"
        return None

    # ------------------------------------------------------------------ kimenet

    async def _emit(self, raw, ob, ta_result, terv, c):
        symbol, direction = raw["symbol"], raw["direction"]
        detector = raw["detector"]
        recent = self._count_recent(detector, symbol, direction,
                                    c["signalWindowMinutes"])

        reasons = list(raw["reasons"])
        metrics = dict(raw["metrics"])
        if ob:
            if ob.get("spreadPct") is not None:
                reasons.append(f"spread {ob['spreadPct']:.3f}%")
                metrics["spreadPct"] = ob["spreadPct"]
            reasons.append("nincs fal a mozgas iranyaban" if not ob.get("obstacleAhead")
                           else f"fal {ob['obstacleAhead']['distancePct']:.2f}%-ra")
        reasons.append(f"hozam/kockazat {terv['rewardRisk']}:1")
        metrics["rewardRisk"] = terv["rewardRisk"]

        signal = {
            "timestamp": datetime.now(timezone.utc),
            "status": "signal",
            "detector": detector,
            "symbol": symbol,
            "direction": direction,
            "price": raw["price"],
            "url": binance_url(symbol),
            "quoteVolume24h": binance_rest.SYMBOL_VOLUME.get(symbol),
            "reasons": reasons,
            "metrics": metrics,
            "plan": terv,
            "ema": ta_result,
            "orderBook": _without_snapshot(ob),
            "recent": recent,
            "telegram": {"sent": False, "error": None},
            "trade": {"executed": False, "orderId": None, "error": None},
        }

        self.signals_today += 1
        log.info("SIGNAL     %-14s %-5s %s  rr %.1f:1  %s",
                 symbol, direction, raw["reasons"][0] if raw["reasons"] else "",
                 terv["rewardRisk"], binance_url(symbol))
        events.add(f"{symbol:<14} SIGNAL {direction:<5} {detector}  "
                   f"rr {terv['rewardRisk']}:1")

        signal["trade"] = await self.trader.maybe_open(signal)
        # minden SIGNAL azonnal megy Telegramra -- nincs kapu elotte
        signal["telegram"] = await self.notifier.send(
            symbol,
            telegram.format_signal(signal, self.cfg.telegram.get("appLinkTemplate", "")),
            detector)
        await self._save(signal, raw, ob, ta_result)

    async def _save_rejected(self, raw, ok, ob, terv, ta_result):
        self.rejected_today += 1
        log.info("REJECTED   %-14s %-5s %s", raw["symbol"], raw["direction"], ok)
        events.add(f"{raw['symbol']:<14} REJECTED {raw['direction']:<5} {ok}")
        metrics = dict(raw["metrics"])
        if ob and ob.get("spreadPct") is not None:
            metrics["spreadPct"] = ob["spreadPct"]
        if terv:
            metrics["rewardRisk"] = terv["rewardRisk"]
        try:
            await self.db.signals.insert_one({
                "timestamp": datetime.now(timezone.utc),
                "status": "rejected",
                "detector": raw["detector"],
                "symbol": raw["symbol"],
                "direction": raw["direction"],
                "price": raw["price"],
                "reasons": [ok],
                "metrics": metrics,
            })
        except Exception as e:
            log.warning("[%s] elutasitas mentese sikertelen: %s", raw["symbol"], e)

    # ------------------------------------------------------------------ mentes

    async def _save(self, signal, raw, ob, ta_result):
        symbol = signal["symbol"]
        result = await self.db.signals.insert_one(signal)
        if self.cfg.detector.get("outcomeEnabled"):
            asyncio.create_task(outcome.track(self.db, result.inserted_id, signal,
                                              self.cfg.detector))
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
                "metrics": signal["metrics"],
            })
            log.debug("[%s] elmentve: signals %s + market_snapshots %s",
                      symbol, result.inserted_id, snap.inserted_id)
        except Exception as e:
            log.error("[%s] a market_snapshots mentese NEM sikerult: %s: %s",
                      symbol, type(e).__name__, e)

    def _count_recent(self, detector, symbol, direction, window_minutes):
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
