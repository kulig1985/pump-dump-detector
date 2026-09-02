"""SignalService -- a megerositett setupbol SIGNAL lesz.

A detektor mar mindent eldontott (a konyv es az EMA is befolyasolta a dontest, a
cache-bol). Itt csak mentes, Telegram, trade es az eredmenymeres inditasa tortenik.

A JELZES UTJAN NINCS HALOZATI VARAKOZAS: sem order book lekeres, sem klines.
Korabban epp a jelzes pillanataban vartunk ezekre.
"""
import logging
from datetime import datetime, timezone

from . import telegram, events, binance_rest
from .links import binance_url

log = logging.getLogger("signal")


class SignalService:
    def __init__(self, cfg, db, notifier, trader, outcome=None, book=None):
        self.cfg = cfg
        self.outcome = outcome
        self.book = book               # BookCache -- a nyers konyv a snapshothoz
        self.db = db
        self.notifier = notifier
        self.trader = trader
        self.signals_today = 0

    async def handle_trigger(self, raw):
        """Egy detektor CANDIDATE-je. Sose dob kivetelt tovabb a stream fele."""
        try:
            await self._process(raw)
        except Exception as e:
            log.exception("[%s] feldolgozas hiba: %s", raw.get("symbol"), e)

    async def _process(self, raw):
        symbol, direction = raw["symbol"], raw["direction"]

        signal = {
            "timestamp": datetime.now(timezone.utc),
            "detector": raw["detector"],
            "setup": raw.get("setup") or direction,
            "symbol": symbol,
            "direction": direction,
            "price": raw["price"],
            "url": binance_url(symbol),
            "quoteVolume24h": binance_rest.SYMBOL_VOLUME.get(symbol),
            "metrics": dict(raw["metrics"]),
            "telegram": {"sent": False, "error": None},
            "trade": {"executed": False, "orderId": None, "error": None},
        }

        # ELOSZOR mentes + eredmenymeres inditasa, CSAK UTANA a halozat. A Telegram
        # HTTP hivas masodpercekig is tarthat -- addig mar mernunk kell az arat.
        signal_id = await self._save(signal, raw)

        self.signals_today += 1
        log.info("SIGNAL     %-14s %-5s ar %.8g  %s",
                 symbol, direction, raw["price"], binance_url(symbol))
        events.add(f"{symbol:<14} SIGNAL {direction}")

        signal["trade"] = await self.trader.maybe_open(signal)
        signal["telegram"] = await self.notifier.send(
            symbol, telegram.format_signal(signal,
                                           self.cfg.telegram.get("appLinkTemplate", "")),
            raw["detector"])
        if signal_id is not None:
            await self.db.signals.update_one(
                {"_id": signal_id},
                {"$set": {"telegram": signal["telegram"], "trade": signal["trade"]}})

    async def _save(self, signal, raw):
        """Mentes + az eredmenymeres inditasa. Visszaadja a signal id-t."""
        symbol = signal["symbol"]
        result = await self.db.signals.insert_one(signal)
        # innentol folyamatosan merjuk, mi tortenik az arral (MFE/MAE, TP/SL)
        if self.outcome:
            self.outcome.track(result.inserted_id, symbol, signal["setup"],
                               signal["direction"], signal["price"])
        try:
            await self.db.snapshots.insert_one({
                "timestamp": signal["timestamp"],
                "signalId": result.inserted_id,
                "detector": signal["detector"],
                "symbol": symbol,
                "price": signal["price"],
                "setup": signal["setup"],
                "priceHistory": [[ts, p] for ts, p in raw["history"]],
                "orderBook": self.book.snapshot(symbol) if self.book else None,
                "metrics": signal["metrics"],
            })
        except Exception as e:
            log.error("[%s] a market_snapshots mentese NEM sikerult: %s: %s",
                      symbol, type(e).__name__, e)
        return result.inserted_id
