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
from . import config as C
from .config import ConfigStore
from .market_data import MarketDataService
from .detectors import DetectorManager, PumpDumpDetector, ReversalDetector
from .detectors.baseline import Baseline
from .eligibility import Eligibility
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
    eltero = []
    for defaults, akt in ((C.DETECTOR_DEFAULTS, d), (C.REVERSAL_DEFAULTS, r),
                          (C.TRADING_DEFAULTS, t)):
        for k, alap in defaults.items():
            if k != "_id" and akt.get(k) != alap:
                eltero.append(f"{k}={akt.get(k)} (alap {alap})")
    return ([f"A DB-ben eltero beallitas: {', '.join(eltero)}"] if eltero else []) + [
        f"Kereskedhetoseg: forgalom >= {d['minQuoteVolume24h']:,.0f}, "
        f"spread <= {d['maxSpreadPct']:.3f}%, melyseg >= {d['minTopDepthUSDT']:,.0f} USDT, "
        f"legalabb {d['minTradesPerMinute']} kotes/perc",
        f"Pump/dump: a mozgas a par sajat normaljanak {d['baselineRatio']:.1f}x-e "
        f"({d['baselineMinutes']} perc visszatekintes, min {d['minMovePct']:.2f}%), "
        f"{d['minConsistency']:.0%} egyiranyusag, cooldown {d['symbolCooldownSec']}s",
        f"Reversal: elozetes mozgas a normal {r['baselineRatio']:.1f}x-e "
        f"(min {r['minMovePct']:.2f}%), belepes a mozgas {r['maxRetracementPct']:.0f}%-an "
        f"belul, max {r['maxExtremeAgeSec']:.0f} mp regi szelsoertekre",
        f"Validacio: mozgas >= {d['minMoveToSpreadRatio']:.0f}x spread, "
        f"nincs fal {d['wallBlockDistancePct']:.2f}%-on belul, "
        f"hozam/kockazat >= {d['minRewardRisk']:.1f}:1",
        f"Telegram: {'BE -- minden SIGNAL azonnal megy' if d['telegramEnabled'] else 'KI'}"
        f"   |   Eredmenymeres: {'BE' if d['outcomeEnabled'] else 'KI'}"
        f"   |   Auto trading: {'BE' if t['autoTradingEnabled'] else 'KI'} "
        f"({t['marginMode']}, {t['leverage']}x)",
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

    # A baseline-t a ket detektor megosztva hasznalja: ugyanaz a "mi normalis
    # ezen a paron" mertek all mindketto mogott.
    baseline = Baseline(cfg)
    eligibility = Eligibility(cfg)

    # Uj detektor hozzaadasa: egy uj osztaly az app/detectors/ ala, es egy sor ide.
    detectors = DetectorManager(cfg, [PumpDumpDetector(cfg, baseline),
                                      ReversalDetector(cfg, baseline)], eligibility)
    log.info("Detektorok: %s", ", ".join(
        f"{d.name} ({'BE' if detectors.enabled(d) else 'KI'})" for d in detectors.detectors))

    market = MarketDataService(cfg, db, detectors, eligibility,
                               on_signal=signals.handle_trigger)
    market.signal_service = signals

    try:
        await asyncio.gather(cfg.refresh_loop(), market.run(),
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
