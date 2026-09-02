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
from .detectors import DetectorManager, ScalpDetector
from .detectors.baseline import Baseline
from .eligibility import Eligibility
from .bookcache import BookCache
from . import ta
from .outcome import OutcomeTracker
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
    m, d, t = cfg.market, cfg.detector, cfg.trading
    eltero = []
    for defaults, akt in ((C.MARKET_DEFAULTS, m), (C.DETECTOR_DEFAULTS, d),
                          (C.TRADING_DEFAULTS, t)):
        for k, alap in defaults.items():
            if k != "_id" and akt.get(k) != alap:
                eltero.append(f"{k}={akt.get(k)} (alap {alap})")
    return ([f"A DB-ben eltero beallitas: {', '.join(eltero)}"] if eltero else []) + [
        f"Piac: {', '.join(m['quoteAssets'])} parok, forgalom >= "
        f"{m['minQuoteVolume24h']:,.0f}, max {m['maxSymbols']} par, "
        f"spread <= {m['maxSpreadPct']:.3f}%",
        f"Impulzus: a mozgas a par normaljanak {d['impulseBaselineRatio']:.1f}x-e "
        f"(min {d['minImpulsePct']:.2f}%), forgalom >= {d['minImpulseNotional']:,.0f} "
        f"USDT es a normal {d['notionalRatio']:.1f}x-e, "
        f"kotesaramlas >= {d['minImpulseImbalance']:.2f}  ({d['impulseWindowSec']:.0f} mp-es ablak)",
        f"Setup: {d['setupTimeoutSec']}s-ig el | FOLYTATAS: "
        f"{d['minPullbackPct']}-{d['maxPullbackPct']}% visszahuzas, majd "
        f"{d['breakoutOfLegPct']}%-os ujratores | FORDULO: {d['exhaustionSec']:.0f} mp "
        f"kifulladas, {d['reclaimOfLegPct']}%-os attores {d['reclaimHoldSec']:.0f} mp-ig tartva",
        f"A jelzest BEFOLYASOLJA a konyv (fal {d['wallBlockDistPct']:.2f}%-on belul, "
        f"imbalance {d['maxOpposingBookImbalance']:.2f}) es az EMA "
        f"(folytatas: {'igen' if d['requireTrendForContinuation'] else 'nem'}, "
        f"fordulo: {'igen' if d['requireTrendForReversal'] else 'nem'})",
        f"Eredmenymeres: {m['outcomeTrackSec'] // 60} percig, MFE/MAE + TP "
        f"{m['tpLevels']} / SL {m['slLevels']}",
        f"Telegram: {'BE -- minden SIGNAL azonnal megy' if cfg.telegram['enabled'] else 'KI'}"
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
    outcome = OutcomeTracker(cfg, db)
    book = BookCache(cfg)
    signals = SignalService(cfg, db, notifier, trader, outcome, book)

    baseline = Baseline(cfg)
    eligibility = Eligibility(cfg)

    # Uj detektor hozzaadasa: egy uj osztaly az app/detectors/ ala, es egy sor ide.
    # A konyv es az EMA a detektorba megy: ezek BEFOLYASOLJAK a dontest, es a
    # dontes pillanataban mar cache-bol jonnek, halozati varakozas nelkul.
    detectors = DetectorManager(cfg, [ScalpDetector(cfg, baseline, book, ta)],
                                eligibility)
    log.info("Detektorok: %s", ", ".join(
        f"{d.name} ({'BE' if detectors.enabled(d) else 'KI'})" for d in detectors.detectors))

    market = MarketDataService(cfg, db, detectors, eligibility,
                               on_signal=signals.handle_trigger)
    market.signal_service = signals
    market.outcome = outcome
    market.notifier = notifier
    market.book = book

    try:
        await asyncio.gather(cfg.refresh_loop(), market.run(), outcome.run(),
                             ta.refresh_loop(cfg, lambda: market.symbols))
    except Exception:
        # A docker azonnal ujrainditja a konteneret. Ha szorosan pergunk, minden
        # inditas lo egy exchangeInfo + egy ticker/24hr hivast (utobbi 40 sulyu),
        # es percek alatt osszejon a Binance 429 -> 418 IP tiltas. Ezert varunk.
        log.exception("Vegzetes hiba -- varakozas ujrainditas elott")
        await asyncio.sleep(60)
        raise
    finally:
        await notifier.close()
        await trader.close()
        await binance_rest.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Leallitas")
