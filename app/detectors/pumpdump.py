"""PumpDumpDetector -- rendkivuli rovid tavu armozgas.

A kerdes nem az, hogy "mozdult-e 0.3%-ot", hanem hogy "SZOKATLAN-e ez a mozgas
EZEN a paron". Egy meme coinon 0.3% masodpercenkent tortenik, a BTC-n hetente.
Ezert a mercet a par sajat, futas kozben mert normalja adja (lasd baseline.py).

Harom feltetel, nem tobb:

    1. a mozgas a par normaljanak baselineRatio-szorosa (es legalabb minMovePct)
    2. a lepesek minConsistency hanyada egy iranyba mutat  -- alakzat, nem kapkodas
    3. az ablak forgalma legalabb a par atlaga             -- van mogotte penz

A likviditas (spread, melyseg, aktivitas) mar a detektor ELOTT elintezodik az
eligibility szuroben, ezert itt nem szerepel.
"""
import time
import logging
from collections import deque, defaultdict

from .. import events, binance_rest
from ..fmt import pad, price as fprice, money
from ..links import binance_url
from .base import Detector, make_signal
from .baseline import Baseline

log = logging.getLogger("pumpdump")


class PumpDumpDetector(Detector):
    name = "pump_dump"
    config_key = "detector"

    def __init__(self, cfg, baseline=None):
        self.cfg = cfg
        self.baseline = baseline or Baseline(cfg)
        self.history = defaultdict(deque)   # symbol -> deque[(ts, ar, quote mennyiseg)]
        self.last_trigger = {}
        self.latest = {}                    # symbol -> mert allapot (a DEBUG tablahoz)
        self.last_ts = 0.0
        self.ticks = 0
        self.total_candidates = 0

    # ------------------------------------------------------------------ fo utvonal

    def on_trade(self, trade):
        c = self.cfg.detector
        h = self.history[trade.symbol]
        h.append((trade.ts, trade.price, trade.price * trade.qty))
        while h and h[0][0] < trade.ts - c["moveWindowSec"] * 2:
            h.popleft()

        self.ticks += 1
        self.last_ts = trade.ts

        m = self._measure(h, trade.ts, c, trade.symbol)
        self.latest[trade.symbol] = m
        if m is None:
            return None

        # Eloszor a KORABBI normalhoz hasonlitunk, csak utana frissitunk -- kulonben
        # az eppen vizsgalt mozgas resze lenne annak, amihez merjuk. (300 mintas
        # mediannal a torzitas elhanyagolhato, de a sorrend igy elvileg tiszta.)
        arany = self.baseline.ratio(trade.symbol, m["movePct"])
        m["baseline"] = self.baseline.value(trade.symbol)
        m["baselineRatio"] = arany
        # a baseline MINDEN merheto ablakbol epul, nem csak a jelzesekbol
        self.baseline.add(trade.symbol, trade.ts, abs(m["movePct"]))

        kell = c["minMovePct"]
        if arany is not None:
            kell = max(kell, m["baseline"] * c["baselineRatio"])
        if abs(m["movePct"]) < kell:
            return None
        if m["consistency"] < c["minConsistency"]:
            return None
        if m["volume"] < m["expectedVolume"]:
            return None
        if trade.ts - self.last_trigger.get(trade.symbol, 0) < c["symbolCooldownSec"]:
            return None

        self.last_trigger[trade.symbol] = trade.ts
        self.total_candidates += 1
        direction = "LONG" if m["movePct"] > 0 else "SHORT"

        reasons = [
            f"move {m['movePct']:+.2f}% / {m['spanSec']:.1f}s",
            (f"{arany:.1f}x a par normaljahoz kepest "
             f"(normal {m['baseline']:.3f}%)" if arany is not None
             else f"nincs meg baseline, abszolut padlo {c['minMovePct']:.2f}%"),
            f"{m['consistency']:.0%} egy iranyba ({m['trades']} kotes)",
            f"forgalom {money(m['volume'])} USDT "
            f"({m['volume'] / m['expectedVolume']:.1f}x atlag)"
            if m["expectedVolume"] else f"forgalom {money(m['volume'])} USDT",
        ]
        log.info("CANDIDATE  %-14s %-5s move %+.2f%% / %.1fs  baseline %s",
                 trade.symbol, direction, m["movePct"], m["spanSec"],
                 f"{arany:.1f}x" if arany is not None else "n/a")
        events.add(f"{trade.symbol:<14} CANDIDATE {direction:<5} "
                   f"{m['movePct']:+.2f}% / {m['spanSec']:.1f}s")

        return make_signal(
            self.name, self.config_key, trade.symbol, direction, trade.price, trade.ts,
            move_pct=abs(m["movePct"]),
            reasons=reasons,
            metrics={k: v for k, v in m.items() if k != "origin"},
            # a stop az impulzus felenel: ha a mozgas felet visszaadja, a tezis halott
            stop_anchor=(m["origin"] + (trade.price - m["origin"])
                         * (1 - c["momentumStopRetracementPct"] / 100.0)),
            target_anchor=(trade.price + (trade.price - m["origin"])
                           * c["momentumTargetFactor"]),
            history=[(t, p) for t, p, _ in h],
        )

    # ------------------------------------------------------------------ meres

    @staticmethod
    def _measure(history, now, c, symbol):
        """Az utolso moveWindowSec masodperc elmozdulasa. None, ha nem merheto.

        IDOALAPU ablak, nem darabszam alapu: egy nagy paron 30 kotes akar 30
        milliszekundum alatt is beerkezik, es abbol ertelmetlen tempot szamolni.
        """
        start = now - c["moveWindowSec"]
        w = [(t, p, q) for t, p, q in history if t >= start]
        if len(w) < c["minTradesInWindow"]:
            return None
        span = w[-1][0] - w[0][0]
        if span < c["moveWindowSec"] / 2:      # egy pillanatnyi kotescsokor nem ablak
            return None

        ys = [p for _, p, _ in w]
        if ys[0] <= 0:
            return None
        volume = sum(q for _, _, q in w)
        vol24 = binance_rest.SYMBOL_VOLUME.get(symbol)
        elvart = vol24 / 86400.0 * span * c["minVolumeFactor"] if vol24 else 0.0

        # A mozgast NEM az elso es utolso ar kulonbsegebol szamoljuk, hanem az
        # ablakra illesztett egyenes elmozdulasabol. A vegpont-kulonbseget egyetlen
        # kiugro print is felviszi (mert az 0.45%-ot mutatna, aztan visszaesne);
        # az illesztett egyenest nem.
        xs = [t - w[0][0] for t, _, _ in w]
        n = len(w)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0 or mean_y <= 0:
            return None
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        move_pct = (sxy / sxx) * span / mean_y * 100.0

        lepesek = [ys[i + 1] - ys[i] for i in range(n - 1)]
        mozgott = [l for l in lepesek if l != 0]
        irany = 1 if move_pct >= 0 else -1
        consistency = (sum(1 for l in mozgott if l * irany > 0) / len(mozgott)
                       if mozgott else 0.0)

        return {
            "movePct": move_pct,
            "endpointPct": (ys[-1] - ys[0]) / ys[0] * 100.0,
            "spanSec": round(span, 2),
            "trades": len(w),
            "consistency": round(consistency, 3),
            "volume": round(volume, 2),
            "expectedVolume": round(elvart, 2),
            "origin": ys[0],
        }

    # ------------------------------------------------------------------ DEBUG tabla

    def readiness(self):
        """Hany parnak van mar normalja, es melyik van legkozelebb a jelzeshez.

        A nyers szamokat is mutatja: hidegindulaskor a median lehet ~0.001%, amitol
        egy jelentektelen mozgas is "266x"-nek latszana. A dontest ez nem rontja el
        (a minMovePct padlo dominal), de a kijelzes felrevezeto lenne.
        """
        c = self.cfg.detector
        keszek, legjobb = 0, None
        for symbol, m in self.latest.items():
            if m is None:
                continue
            alap = self.baseline.value(symbol)
            if alap is None or alap <= 0:
                continue
            keszek += 1
            kell = max(c["minMovePct"], alap * c["baselineRatio"])
            hanyad = abs(m["movePct"]) / kell
            if legjobb is None or hanyad > legjobb[0]:
                legjobb = (hanyad, symbol, abs(m["movePct"]), kell, alap)
        sor = f"normal kesz: {keszek}/{len(self.latest)} par"
        if legjobb:
            _, symbol, mozgas, kell, alap = legjobb
            sor += (f" | legkozelebb: {symbol} {mozgas:.3f}% "
                    f"(kell {kell:.3f}%, normalja {alap:.3f}%)")
        return sor

    def status_lines(self, top=10):
        """Reszletes paronkenti allapot -- csak DEBUG szinten kerul kiirasra."""
        c = self.cfg.detector
        sorok = []
        for symbol, m in self.latest.items():
            if m is None:
                continue
            alap = self.baseline.value(symbol)
            arany = abs(m["movePct"]) / alap if alap else None
            sorok.append((arany or 0, symbol, m, alap, arany))
        if not sorok:
            return ["  nincs merheto par"]
        sorok.sort(reverse=True, key=lambda x: x[0])

        out = [f"  {pad('par', 14)}{'arfolyam':>13}{'mozgas':>9}{'normal':>9}"
               f"{'arany':>8}{'egyirany':>10}{'kotes':>7}"]
        for _, symbol, m, alap, arany in sorok[:top]:
            out.append(f"  {pad(symbol, 14)}"
                       f"{fprice(m.get('price', 0)) if 'price' in m else '':>13}"
                       f"{m['movePct']:>8.2f}%"
                       f"{(f'{alap:.3f}%' if alap else '--'):>9}"
                       f"{(f'{arany:.1f}x' if arany else '--'):>8}"
                       f"{m['consistency']:>9.0%}{m['trades']:>7}")
        out.append(f"  jelzeshez: {c['baselineRatio']:.1f}x a normalhoz kepest "
                   f"(min {c['minMovePct']:.2f}%), {c['minConsistency']:.0%} egyiranyusag")
        return out
