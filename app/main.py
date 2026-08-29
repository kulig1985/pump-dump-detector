"""Belepesi pont.

Binance WebSocket -> MarketDataService -> MovementDetector -> (trigger)
  -> OrderBookAnalyzer + TAAnalyzer -> SignalService -> MongoDB
  -> TelegramNotifier -> opcionalisan TradingService
"""
import asyncio
import logging

from .db import Database
from .config import ConfigStore
from .market_data import MarketDataService
from .signals import SignalService
from .telegram import TelegramNotifier
from .trading import TradingService
from . import binance_rest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)-9s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger("main")


async def main():
    db = Database()
    await db.init()

    cfg = ConfigStore(db)
    await cfg.load()
    log.info("Kuszobok: 1s %.2f%% | 3s %.2f%% | 5s %.2f%% | min score %d | cooldown %ds",
             cfg.detector["priceChangeThreshold1s"], cfg.detector["priceChangeThreshold3s"],
             cfg.detector["priceChangeThreshold5s"], cfg.detector["minSignalScore"],
             cfg.detector["symbolCooldownSec"])
    log.info("Auto trading: %s", "BE" if cfg.trading["autoTradingEnabled"] else "KI")

    notifier = TelegramNotifier(cfg)
    trader = TradingService(cfg, db)
    signals = SignalService(cfg, db, notifier, trader)
    market = MarketDataService(cfg, on_trigger=signals.handle_trigger)

    try:
        await asyncio.gather(cfg.refresh_loop(), market.run())
    finally:
        await notifier.close()
        await trader.close()
        await binance_rest.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Leallitas")
