"""Symbolonkenti mozgasminoseg: mekkorat ugrik az ar EGYETLEN kotesre?

Egy paron akkor nem lehet 0.2-0.5%-os mozgast megfogni, ha mar egy darab trade is
tized szazalekokat mozdit az aron. Ilyenkor a be- es kiszallas maga is a jelzes
merteteben mozgatja az arat.

    tick zaj = |ar valtozasa az elozo trade ota| szazalekban, EWMA-val simitva

      BTCUSDT, ETHUSDT     ->  0.000x %      (egy tick a spread toredeke)
      normal altcoin       ->  0.00x - 0.0x %
      ossze-vissza ugralo  ->  0.1 % folott   <- ezeket zarjuk ki

FONTOS: ez a par JELLEGET meri, nem a pillanatnyi allapotat. Korabban a
"hatekonysagi arany" (netto elmozdulas / megtett ut) volt itt, de az allapotot mert:
egy lapos BTCUSDT-n 0.02 lett (csak bid/ask pattogas), es mire a BTC tenyleg
megmozdult, a simitott mertek meg mindig a regi allapotot mutatta -- vagyis
pontosan a keresett esemenyt zarta ki.
"""
import logging

log = logging.getLogger("quality")

EWMA_ALPHA = 0.02          # ~50 trade emlekezet
MIN_MINTA = 30             # ennyi trade elott nem itelunk


class SymbolQuality:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_price = {}
        self.noise = {}        # symbol -> atlagos tick-ugras szazalekban
        self.samples = {}
        self.blocked = set()

    def on_trade(self, trade):
        elozo = self.last_price.get(trade.symbol)
        self.last_price[trade.symbol] = trade.price
        if not elozo or elozo <= 0:
            return
        ugras = abs(trade.price - elozo) / elozo * 100.0
        prev = self.noise.get(trade.symbol)
        self.noise[trade.symbol] = (ugras if prev is None
                                    else prev + EWMA_ALPHA * (ugras - prev))
        self.samples[trade.symbol] = self.samples.get(trade.symbol, 0) + 1

    def tick_noise(self, symbol):
        return self.noise.get(symbol)

    def tradeable(self, symbol):
        """(mehet-e, ok). Amig nincs eleg minta, atengedjuk."""
        kuszob = self.cfg.detector["maxTickNoisePct"]
        n = self.noise.get(symbol)
        if not kuszob or n is None or self.samples.get(symbol, 0) < MIN_MINTA:
            return True, None
        if n > kuszob:
            self.blocked.add(symbol)
            return False, f"egy kotes atlagosan {n:.3f}%-ot mozdit (max {kuszob:.3f}%)"
        self.blocked.discard(symbol)
        return True, None

    def blocked_summary(self, top=6):
        if not self.blocked:
            return []
        rendezett = sorted(self.blocked, key=lambda s: -self.noise.get(s, 0))
        nevek = "  ".join(f"{s} ({self.noise[s]:.3f}%)" for s in rendezett[:top])
        tobb = f"  ... +{len(rendezett) - top}" if len(rendezett) > top else ""
        return [f"  KIZARVA -- egy kotes tul sokat mozdit "
                f"({len(rendezett)} par, max {self.cfg.detector['maxTickNoisePct']:.3f}%):",
                f"    {nevek}{tobb}"]
