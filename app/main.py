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


def startup_summary(cfg):
    """A beallitasok osszefoglalasa indulaskor.

    Kulon fuggveny, hogy tesztelheto legyen: itt korabban egy atnevezett config
    kulcs miatt indulaskor elhasalt az egesz alkalmazas.
    """
    d, r, t = cfg.detector, cfg.reversal, cfg.trading
    return [
        f"Pump/dump: legalabb {d['minTotalMovePct']:.2f}% mozgas "
        f"{d['slopeWindowSec']:.0f} mp-en belul, {d['minSlopePctPerSec']:.3f}%/mp tempoval, "
        f"{d['minConsistency']:.0%} egyiranyusaggal | min score {d['minSignalScore']} "
        f"| cooldown {d['symbolCooldownSec']}s",
        f"Reversal: {r['minMovePct']:.2f}% elozetes mozgas, belepes a mozgas "
        f"{r['maxRetracementPct']:.0f}%-an belul, max {r['maxExtremeAgeSec']:.0f} mp regi "
        f"szelsoertekre | min score {r['minSignalScore']} | cooldown {r['cooldownSec']}s",
        f"Szures: forgalom >= {d['minQuoteVolume24h']:,.0f}, "
        f"egy kotes max {d['maxTickNoisePct']:.3f}%-ot mozdit, "
        f"mozgas >= {d['minMoveToSpreadRatio']:.0f}x spread",
        f"Telegram: pump_dump={d['telegramMode']}, reversal={r['telegramMode']} "
        f"(auto = csak {d['shadowMinSamples']} lemert jelzes es "
        f"{d['shadowMinHitRate']:.0%} talalat utan)",
        f"Auto trading: {'BE' if t['autoTradingEnabled'] else 'KI'} | "
        f"margin: {t['marginMode']} | {t['leverage']}x",
    ]


async def main():
    log.info("Kod ujjlenyomat: %s", code_fingerprint())
    db = Database()
    await db.init()

    cfg = ConfigStore(db)
    await cfg.load()
    for sor in startup_summary(cfg):
        log.info("%s", sor)

    notifier = TelegramNotifier(cfg)
    trader = TradingService(cfg, db)
    signals = SignalService(cfg, db, notifier, trader)

    # Uj detektor hozzaadasa: egy uj osztaly az app/detectors/ ala, es egy sor ide.
    detectors = DetectorManager(cfg, [PumpDumpDetector(cfg), ReversalDetector(cfg)])
    log.info("Detektorok: %s", ", ".join(
        f"{d.name} ({'BE' if detectors.enabled(d) else 'KI'})" for d in detectors.detectors))

    market = MarketDataService(cfg, db, detectors, on_signal=signals.handle_trigger)
    market.telegram_status = signals.telegram_status_lines

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
