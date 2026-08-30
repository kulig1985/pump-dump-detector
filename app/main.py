"""Belepesi pont.

Binance WebSocket -> MarketDataService -> MovementDetector -> (trigger)
  -> OrderBookAnalyzer + TAAnalyzer -> SignalService -> MongoDB
  -> TelegramNotifier -> opcionalisan TradingService
"""
import asyncio
import hashlib
import logging
import pathlib

from .db import Database
from .config import ConfigStore
from .market_data import MarketDataService
from .detectors import DetectorManager, PumpDumpDetector, ReversalDetector
from .signals import SignalService
from .telegram import TelegramNotifier
from .trading import TradingService
from . import binance_rest, outcome

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)-9s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger("main")


def code_fingerprint():
    """A futo kod ujjlenyomata.

    Enelkul nem lehet biztosra tudni, hogy a konteneben tenyleg az uj kod van-e.
    Ugyanez a szam kiszamolhato a gazdagepen is (lasd README).
    """
    h = hashlib.sha256()
    for f in sorted(pathlib.Path(__file__).parent.rglob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


async def main():
    log.info("Kod ujjlenyomat: %s", code_fingerprint())
    db = Database()
    await db.init()

    cfg = ConfigStore(db)
    await cfg.load()
    log.info("Pump/dump trigger: %.3f%%/mp meredekseg + %.0f%% egyiranyusag "
             "%d trade-en (max %.0f mp) | min score %d | cooldown %ds",
             cfg.detector["minSlopePctPerSec"], cfg.detector["minConsistency"] * 100,
             cfg.detector["tradeWindow"], cfg.detector["maxSpanSec"],
             cfg.detector["minSignalScore"], cfg.detector["symbolCooldownSec"])
    log.info("Auto trading: %s | margin: %s | %dx",
         "BE" if cfg.trading["autoTradingEnabled"] else "KI",
         cfg.trading["marginMode"], cfg.trading["leverage"])

    notifier = TelegramNotifier(cfg)
    trader = TradingService(cfg, db)
    signals = SignalService(cfg, db, notifier, trader)

    # Uj detektor hozzaadasa: egy uj osztaly az app/detectors/ ala, es egy sor ide.
    detectors = DetectorManager(cfg, [PumpDumpDetector(cfg), ReversalDetector(cfg)])
    log.info("Detektorok: %s", ", ".join(
        f"{d.name} ({'BE' if detectors.enabled(d) else 'KI'})" for d in detectors.detectors))

    market = MarketDataService(cfg, db, detectors, on_signal=signals.handle_trigger)

    try:
        await asyncio.gather(cfg.refresh_loop(), market.run(),
                             signals.refresh_hit_rates(),
                             outcome.summary_loop(db, cfg.detector))
    finally:
        await notifier.close()
        await trader.close()
        await binance_rest.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Leallitas")
