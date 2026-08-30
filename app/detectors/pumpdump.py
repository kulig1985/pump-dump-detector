"""PumpDumpDetector -- rendkivuli rovid tavu armozgas.

A kerdes nem az, hogy "mozdult-e 0.3%-ot", hanem hogy "SZOKATLAN-e ez a mozgas
EZEN a paron". Egy meme coinon 0.3% masodpercenkent tortenik, a BTC-n hetente.
Ezert a mercet a par sajat, futas kozben mert normalja adja (lasd baseline.py).

Harom feltetel:

    1. a mozgas a par normaljanak baselineRatio-szorosa (es legalabb minMovePct)
    2. NEM egyetlen nagy kotes vitte el az arat: a legnagyobb egyetlen arlepes a
       mozgasnak legfeljebb maxSingleStepPct szazaleka
    3. MEGTARTJA: confirmSec masodperccel kesobb is megvan a mozgas
       confirmHoldPct szazaleka -- egy pillanatnyi korrekcio visszaesik, egy
       valodi elindulas nem

A likviditas (spread, melyseg, aktivitas) mar a detektor ELOTT elintezodik az
eligibility szuroben. A mozgast az ablakra ILLESZTETT EGYENES adja, nem a vegpontok
kulonbsege -- igy sem egyetlen kiugro print, sem fureszfog nem tud jelzest csinalni
(fureszfognal az illesztett meredekseg ~nulla).
"""
import time
import logging
from collections import deque, defaultdict

from .. import events, binance_rest
from ..fmt import pad, price as fprice
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
        self.pending = {}                   # symbol -> megerositesre varo jelzes
        self.latest = {}                    # symbol -> mert allapot (a DEBUG tablahoz)
        self.last_ts = 0.0
        self.ticks = 0
        self.total_candidates = 0

    # ------------------------------------------------------------------ fo utvonal

    def on_trade(self, trade):
        c = self.cfg.detector
        h = self.history[trade.symbol]
        h.append((trade.ts, trade.price, 0.0))
        while h and h[0][0] < trade.ts - c["moveWindowSec"] * 2:
            h.popleft()

        self.ticks += 1
        self.last_ts = trade.ts

        # Van megerositesre varo jelzes? Amig le nem jart az ideje, nem keresunk ujat.
        if trade.symbol in self.pending:
            return self._resolve_pending(trade, h)

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

        # Baseline nelkul nem tudjuk megmondani, hogy a mozgas RENDKIVULI-e ezen a
        # paron -- ilyenkor a rendszer csak egy fix kuszob lenne, epp az, amitol el
        # akartunk jutni. Inkabb varunk, amig felepul a normal (kb. 1-2 perc).
        if arany is None:
            return None
        kell = max(c["minMovePct"], m["baseline"] * c["baselineRatio"])
        if abs(m["movePct"]) < kell:
            return None
        if trade.ts - self.last_trigger.get(trade.symbol, 0) < c["symbolCooldownSec"]:
            return None

        # Egyetlen nagy kotes is elviheti az arat: atsopri a konyv par szintjet, a
        # tobbi kotes mar az uj aron nyomtat, es az ablak szep egyenletes mozgasnak
        # latszik. Az ilyen ar rendszerint visszaesik. Ha a mozgast egyetlen arlepes
        # adja, ez nem elindulas.
        if (c["maxSingleStepPct"]
                and m["singleStepPct"] > c["maxSingleStepPct"]):
            log.info("KIHAGYVA   %-14s ar %.8g  a mozgas %.0f%%-at EGYETLEN arlepes "
                     "adta (max %.0f%%)", trade.symbol, trade.price,
                     m["singleStepPct"], c["maxSingleStepPct"])
            return None

        self.last_trigger[trade.symbol] = trade.ts
        direction = "LONG" if m["movePct"] > 0 else "SHORT"

        log.info("MOZGAS     %-14s %-5s ar %.8g  %+.2f%% / %.1fs  normal %.3f%% "
                 "(%.1fx)  -- %.0f mp megerositesre var",
                 trade.symbol, direction, trade.price, m["movePct"], m["spanSec"],
                 m["baseline"], arany, c["confirmSec"])

        self.pending[trade.symbol] = {
            "direction": direction,
            "startPrice": m["startPrice"],
            "triggerPrice": trade.price,
            "deadline": trade.ts + c["confirmSec"],
            "metrics": dict(m),
            "arany": arany,
        }
        return None

    # -------------------------------------------------------------- megerosites

    def _resolve_pending(self, trade, h):
        """Megtartotta-e az ar a mozgast? Ez valasztja el az elindulast a korrekciotol."""
        c = self.cfg.detector
        p = self.pending[trade.symbol]
        if trade.ts < p["deadline"]:
            return None
        del self.pending[trade.symbol]

        m = p["metrics"]
        # A mozgas, amit a jelzes pillanataban LATTUNK: az ablak elejetol a
        # trigger araig. Ebbol mennyi van meg most? 100% = az ar ott maradt,
        # 0% = teljesen visszajott oda, ahonnan indult.
        teljes = p["triggerPrice"] - p["startPrice"]
        maradt = trade.price - p["startPrice"]
        hanyad = (maradt / teljes * 100.0) if teljes else 0.0
        tartott = maradt / p["startPrice"] * 100.0 if p["startPrice"] else 0.0

        if hanyad < c["confirmHoldPct"]:
            log.info("VISSZAESETT %-13s %-5s ar %.8g  a %+.2f%%-bol %+.2f%% maradt "
                     "(%.0f%%, kell %.0f%%) -- pillanatnyi korrekcio volt",
                     trade.symbol, p["direction"], trade.price, m["movePct"],
                     tartott, hanyad, c["confirmHoldPct"])
            events.add(f"{trade.symbol:<14} visszaesett: a {m['movePct']:+.2f}%-bol "
                       f"{tartott:+.2f}% maradt")
            return None

        self.total_candidates += 1
        m = dict(m, heldPct=round(tartott, 4), heldOfMovePct=round(hanyad, 1),
                 confirmSec=c["confirmSec"])
        reasons = [
            f"move {m['movePct']:+.2f}% / {m['spanSec']:.1f}s",
            f"{p['arany']:.1f}x a par normaljahoz kepest (normal {m['baseline']:.3f}%)",
            f"{m['trades']} kotes az ablakban, a legnagyobb egyetlen arlepes "
            f"a mozgas {m['singleStepPct']:.0f}%-a",
            f"{c['confirmSec']:.0f} mp-el kesobb is megvan a mozgas {hanyad:.0f}%-a "
            f"({tartott:+.2f}%)",
        ]
        log.info("CANDIDATE  %-14s %-5s ar %.8g  mozgas %+.2f%% / %.1fs  "
                 "normal %.3f%% (%.1fx)  megtartott %.0f%%",
                 trade.symbol, p["direction"], trade.price, m["movePct"], m["spanSec"],
                 m["baseline"], p["arany"], hanyad)
        events.add(f"{trade.symbol:<14} CANDIDATE {p['direction']:<5} "
                   f"{m['movePct']:+.2f}% / {m['spanSec']:.1f}s, megtartva {hanyad:.0f}%")

        return make_signal(
            self.name, self.config_key, trade.symbol, p["direction"], trade.price,
            trade.ts,
            reasons=reasons,
            metrics=m,
            history=[(t, pr) for t, pr, _ in h],
        )

    # ------------------------------------------------------------------ meres

    @staticmethod
    def _measure(history, now, c, symbol):
        """Az utolso moveWindowSec masodperc elmozdulasa. None, ha nem merheto.

        IDOALAPU ablak, nem darabszam alapu: egy nagy paron 30 kotes akar 30
        milliszekundum alatt is beerkezik, es abbol ertelmetlen tempot szamolni.
        """
        start = now - c["moveWindowSec"]
        w = [(t, p) for t, p, _ in history if t >= start]
        if len(w) < c["minTradesInWindow"]:
            return None
        span = w[-1][0] - w[0][0]
        if span < c["moveWindowSec"] / 2:      # egy pillanatnyi kotescsokor nem ablak
            return None

        ys = [p for _, p in w]
        if ys[0] <= 0:
            return None
        # A mozgast NEM az elso es utolso ar kulonbsegebol szamoljuk, hanem az
        # ablakra illesztett egyenes elmozdulasabol. A vegpont-kulonbseget egyetlen
        # kiugro print is felviszi (mert az 0.45%-ot mutatna, aztan visszaesne);
        # az illesztett egyenest nem.
        xs = [t - w[0][0] for t, _ in w]
        n = len(w)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0 or mean_y <= 0:
            return None
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        move_pct = (sxy / sxx) * span / mean_y * 100.0

        # A legnagyobb egyetlen arlepes a mozgas hany szazaleka? (Egy kotes, ami
        # atsopri a konyvet, ITT latszik: egy lepcso, nem sok kis lepes.)
        elmozdulas = abs(sxy / sxx) * span
        legnagyobb = max((abs(b - a) for a, b in zip(ys, ys[1:])), default=0.0)
        egy_lepes = (legnagyobb / elmozdulas * 100.0) if elmozdulas > 0 else 100.0

        return {
            "movePct": round(move_pct, 4),
            "spanSec": round(span, 2),
            "trades": n,
            "singleStepPct": round(min(egy_lepes, 100.0), 1),
            "startPrice": ys[0],
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

    def top_movers(self, top=5):
        """A legmozgekonyabb parok EPPEN MOST: (par, "mozgas / kell") parok.

        Nem a legnagyobb abszolut mozgas, hanem ami a sajat kuszobehez legkozelebb
        van -- ez mondja meg, hol tart a mezony a jelzeshez kepest.
        """
        c = self.cfg.detector
        sorok = []
        for symbol, m in self.latest.items():
            if m is None:
                continue
            alap = self.baseline.value(symbol)
            if not alap:
                continue
            kell = max(c["minMovePct"], alap * c["baselineRatio"])
            sorok.append((abs(m["movePct"]) / kell, symbol, abs(m["movePct"]), kell))
        sorok.sort(reverse=True)
        return [(sym, f"{mozgas:.2f}% / kell {kell:.2f}%")
                for _, sym, mozgas, kell in sorok[:top]]

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

        out = [f"  {pad('par', 14)}{'mozgas':>9}{'normal':>9}{'arany':>8}{'kotes':>7}"]
        for _, symbol, m, alap, arany in sorok[:top]:
            out.append(f"  {pad(symbol, 14)}{m['movePct']:>8.2f}%"
                       f"{(f'{alap:.3f}%' if alap else '--'):>9}"
                       f"{(f'{arany:.1f}x' if arany else '--'):>8}{m['trades']:>7}")
        out.append(f"  jelzeshez: {c['baselineRatio']:.1f}x a normalhoz kepest "
                   f"(min {c['minMovePct']:.2f}%)")
        return out
