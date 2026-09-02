"""ScalpDetector -- impulzus utani belepo setupok 5-10 perces scalpre.

A regi logika (PUMP -> LONG, DUMP -> SHORT) azt feltetelezte, hogy a hirtelen
mozgas folytatodik. A meres szerint nem: onmagaban ermefeldobas. Itt az impulzus
NEM jelzes, hanem egy setup KEZDETE.

    IDLE -> IMPULSE_DETECTED -> WAITING_CONFIRMATION
         -> CONTINUATION_CONFIRMED | REVERSAL_CONFIRMED -> SIGNAL -> COOLDOWN -> IDLE

A ket ag ugyanabbol a ket szerkezeti szintbol dolgozik. Egy FELFELE impulzus utan:

    pivot     az impulzus csucsa, amint kialakult egy erdemi visszahuzas: ROGZUL
    counter   a visszahuzas melypontja (a pivot rogzitese ota mert legalacsonyabb ar)

    CONTINUATION (LONG)   az ar visszatori a pivot fole    -> folytatodik a mozgas
    REVERSAL     (SHORT)  az ar letori a counter szintet   -> megfordult

Lefele impulzusnal minden tukorkepe. Igy egyetlen, szimmetrikus allapotgep fedi
mindket esetet -- ez valtja ki a korabbi kulon ReversalDetectort is.

Minden meret az IMPULZUS-LAB (leg = |P1 - P0|) aranyaban ertendo, nem abszolut
szazalekban: igy ugyanaz a parameter mukodik egy 0.4%-os es egy 4%-os impulzusnal.
"""
import time
import logging
from collections import deque, defaultdict

from .. import events
from ..fmt import pad, price as fprice
from .base import Detector, make_signal
from .baseline import Baseline, RollingMedian

log = logging.getLogger("scalp")

IDLE = "IDLE"
IMPULSE_DETECTED = "IMPULSE_DETECTED"
WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
COOLDOWN = "COOLDOWN"

MAX_HISTORY_SEC = 180.0     # ennyi arelozmenyt tartunk a bizonyitekhoz


class Setup:
    """Egy symbol eppen fejlodo setupja egy impulzus utan."""

    __slots__ = ("up", "p0", "p1", "leg", "t1", "state", "pivot", "pivot_ts",
                 "counter", "counter_locked", "max_retrace", "impulse",
                 "break_ts", "break_level")

    def __init__(self, up, p0, p1, t1, impulse):
        self.up = up                    # felfele volt-e az impulzus
        self.p0 = p0                    # az impulzus kiindulopontja
        self.p1 = p1                    # az impulzus vege
        self.leg = abs(p1 - p0)         # az impulzus-lab arban -- MINDEN ehhez merodik
        self.t1 = t1
        self.impulse = impulse          # a mert impulzus-adatok (Mongo-ba is)
        self.state = IMPULSE_DETECTED
        self.pivot = p1                 # a szelsoertek; a visszahuzasig meg mozog
        self.pivot_ts = t1
        self.counter = p1               # a visszahuzas szelsoerteke (a fordulo szintje)
        self.counter_locked = False     # rogzult-e mar (volt ellen-visszahuzas)
        self.max_retrace = 0.0          # a legmelyebb visszahuzas a lab %-aban
        self.break_ts = None            # mikor tortent a REVERSAL attores
        self.break_level = None

    def retrace_pct(self, price):
        """A pivottol mert visszahuzas a lab szazalekaban."""
        if self.leg <= 0:
            return 0.0
        tav = (self.pivot - price) if self.up else (price - self.pivot)
        return max(0.0, tav) / self.leg * 100.0

    def kor(self, ts):
        return ts - self.t1


class ScalpDetector(Detector):
    name = "scalp"
    config_key = "detector"

    def __init__(self, cfg, baseline=None, book=None, trend=None):
        self.cfg = cfg
        self.baseline = baseline or Baseline(cfg)
        perc = cfg.detector["baselineMinutes"]
        # a forgalom normalja: ugyanaz a median-logika, de az idovel LINEARISAN
        # skalazodik, ezert nem hasznaljuk ra a Baseline gyok-skalazasat
        self.notional_baseline = RollingMedian(perc * 60, min(60, int(perc * 60 / 2)))
        self.book = book                # BookCache vagy None
        self.trend = trend              # ta modul (get(symbol)) vagy None
        self.window = defaultdict(deque)    # symbol -> deque[(ts, ar, notional, buy, sell)]
        self.history = defaultdict(deque)   # symbol -> deque[(ts, ar)] a bizonyitekhoz
        self.setups = {}                    # symbol -> Setup
        self.cooldown = {}                  # symbol -> eddig nincs uj setup
        self.latest = {}                    # symbol -> utolso mert allapot (STATUS)
        self.last_ts = 0.0
        self.ticks = 0
        self.total_candidates = 0

    # ------------------------------------------------------------------ fo utvonal

    def on_trade(self, trade):
        c = self.cfg.detector
        self.ticks += 1
        self.last_ts = trade.ts

        notional = trade.price * trade.qty
        w = self.window[trade.symbol]
        w.append((trade.ts, trade.price, notional,
                  notional if trade.buy_taker else 0.0,
                  0.0 if trade.buy_taker else notional))
        tart = max(c["impulseWindowSec"], c["flowWindowSec"]) * 2
        while w and w[0][0] < trade.ts - tart:
            w.popleft()

        h = self.history[trade.symbol]
        h.append((trade.ts, trade.price))
        while h and h[0][0] < trade.ts - MAX_HISTORY_SEC:
            h.popleft()

        m = self._measure(w, trade.ts, c)
        self.latest[trade.symbol] = m
        if m is not None:
            # eloszor hasonlitunk, csak utana frissitjuk a normalt
            m["baseline"] = self.baseline.value(trade.symbol)
            m["notionalBaseline"] = self.notional_baseline.value(trade.symbol)
            self.baseline.add(trade.symbol, trade.ts, abs(m["movePct"]))
            self.notional_baseline.add(trade.symbol, trade.ts, m["notional"])

        setup = self.setups.get(trade.symbol)
        if setup is not None:
            return self._track(trade, setup, m, c)
        return self._detect_impulse(trade, m, c)

    # ------------------------------------------------------------------ 1. impulzus

    def _detect_impulse(self, trade, m, c):
        """Az impulzus NEM jelzes: csak setupot indit."""
        if m is None:
            return None
        if trade.ts < self.cooldown.get(trade.symbol, 0):
            return None
        if m["baseline"] is None or m["notionalBaseline"] is None:
            return None                 # meg nem tudjuk, mi normalis ezen a paron

        kell_mozgas = max(c["minImpulsePct"], m["baseline"] * c["impulseBaselineRatio"])
        if abs(m["movePct"]) < kell_mozgas:
            return None
        kell_forgalom = max(c["minImpulseNotional"],
                            m["notionalBaseline"] * c["notionalRatio"])
        if m["notional"] < kell_forgalom:
            return None
        up = m["movePct"] > 0
        if (m["imbalance"] if up else -m["imbalance"]) < c["minImpulseImbalance"]:
            return None                 # nem egyiranyu a kotesaramlas -> nem impulzus
        if c["maxSingleStepPct"] and m["singleStepPct"] > c["maxSingleStepPct"]:
            return None                 # egyetlen arlepes adta -> konyv-sopres

        self.setups[trade.symbol] = Setup(up, m["startPrice"], trade.price,
                                          trade.ts, dict(m))
        log.info("IMPULSE_%-4s %-14s ar %.8g  %+.2f%% / %.1fs  normal %.3f%%  "
                 "forgalom %s USDT (%.1fx)  flow %+.2f",
                 "UP" if up else "DOWN", trade.symbol, trade.price, m["movePct"],
                 m["spanSec"], m["baseline"], f"{m['notional']:,.0f}",
                 m["notional"] / m["notionalBaseline"] if m["notionalBaseline"] else 0,
                 m["imbalance"])
        events.add(f"{trade.symbol:<14} IMPULSE {'UP' if up else 'DOWN'} "
                   f"{m['movePct']:+.2f}%")
        return None

    # ------------------------------------------------------------------ 2. setup

    def _track(self, trade, setup, m, c):
        """A setup kovetese: szerkezet + megerosites, vagy ervenytelenites."""
        ar = trade.price

        # ---- ervenytelenites ----
        if setup.kor(trade.ts) > c["setupTimeoutSec"]:
            return self._eldob(trade, setup, "lejart a setup ideje")
        tul = (setup.p0 - ar) if setup.up else (ar - setup.p0)
        if setup.leg > 0 and tul > setup.leg * c["invalidateBeyondOriginPct"] / 100.0:
            return self._eldob(trade, setup, "az ar visszament az impulzus ala")

        # ---- a szerkezet karbantartasa ----
        if setup.state == IMPULSE_DETECTED:
            # amig nincs erdemi visszahuzas, a pivot meg kovetheti az uj szelsoerteket
            if (ar > setup.pivot) if setup.up else (ar < setup.pivot):
                setup.pivot, setup.pivot_ts, setup.counter = ar, trade.ts, ar
            visszahuzas = setup.retrace_pct(ar)
            setup.max_retrace = max(setup.max_retrace, visszahuzas)
            if visszahuzas >= c["minPullbackPct"]:
                setup.state = WAITING_CONFIRMATION   # a pivot innentol ROGZITETT
                setup.counter = ar
                setup.counter_locked = False
                log.info("WAITING    %-14s %-5s pivot %.8g  visszahuzas %.0f%% a labbol",
                         trade.symbol, "UP" if setup.up else "DOWN", setup.pivot,
                         visszahuzas)
            return None

        # WAITING_CONFIRMATION: a pivot fix. A counter a visszahuzas szelsoerteke --
        # de a fordulohoz ROGZULNIE kell, kulonben nincs mit attorni: amig az ar
        # egyfolytaban tovabb megy a visszahuzas iranyaba, a szint vele csuszna.
        # A rogzites feltetele egy ellen-visszahuzas (counterPullbackPct), pontosan
        # ugy, ahogy egy csucsbol swing-csucs lesz.
        if not setup.counter_locked:
            if (ar < setup.counter) if setup.up else (ar > setup.counter):
                setup.counter = ar
            else:
                bounce = abs(setup.counter - setup.pivot)
                vissza = abs(setup.counter - ar)
                if bounce > 0 and vissza >= bounce * c["counterPullbackPct"] / 100.0:
                    setup.counter_locked = True
                    log.info("SZINT      %-14s a fordulo szintje rogzult: %.8g",
                             trade.symbol, setup.counter)
        setup.max_retrace = max(setup.max_retrace, setup.retrace_pct(ar))

        jel = self._continuation(trade, setup, c) or self._reversal(trade, setup, c)
        return jel

    def _eldob(self, trade, setup, ok):
        self.setups.pop(trade.symbol, None)
        log.info("IDLE       %-14s setup eldobva: %s", trade.symbol, ok)
        return None

    # ------------------------------------------------------------------ 3a. folytatas

    def _continuation(self, trade, setup, c):
        """Sekely visszahuzas utan a pivot ujratorese, valtozatlan kotesaramlassal."""
        if setup.max_retrace > c["maxPullbackPct"]:
            return None                 # tul melyre jott vissza -> ez mar nem folytatas
        kuszob = setup.pivot + (1 if setup.up else -1) * setup.leg \
            * c["breakoutOfLegPct"] / 100.0
        if (trade.price < kuszob) if setup.up else (trade.price > kuszob):
            return None

        irany = "LONG" if setup.up else "SHORT"
        flow = self._flow(trade.symbol, trade.ts, c["flowWindowSec"])
        if flow is None:
            return None
        if (flow if setup.up else -flow) < c["minConfirmImbalance"]:
            return None
        if not self._book_ok(trade.symbol, irany, c):
            return None
        if c["requireTrendForContinuation"] and not self._trend_ok(trade.symbol, irany):
            return None

        return self._kiad(trade, setup, f"{irany}_CONTINUATION", irany, c, flow,
                          extra=[f"visszahuzas a lab {setup.max_retrace:.0f}%-aig, "
                                 f"majd a {fprice(setup.pivot)} pivot ujratorese"])

    # ------------------------------------------------------------------ 3b. fordulo

    def _reversal(self, trade, setup, c):
        """Az impulzus kifullad, a kotesaramlas fordul, es letorik a counter szint."""
        if trade.ts - setup.pivot_ts < c["exhaustionSec"]:
            return None                 # meg friss a szelsoertek -> nincs kifulladas

        if not setup.counter_locked:
            return None                 # meg nincs mit attorni

        irany = "SHORT" if setup.up else "LONG"
        kuszob = setup.counter - (1 if setup.up else -1) * setup.leg \
            * c["reclaimOfLegPct"] / 100.0
        atment = (trade.price < kuszob) if setup.up else (trade.price > kuszob)

        # az attoresnek TARTANIA kell -- egy azonnal visszaveszett szint nem fordulo
        if not atment:
            setup.break_ts = None
            return None
        if setup.break_ts is None:
            setup.break_ts = trade.ts
            setup.break_level = kuszob
            return None
        if trade.ts - setup.break_ts < c["reclaimHoldSec"]:
            return None

        flow = self._flow(trade.symbol, trade.ts, c["flowWindowSec"])
        if flow is None:
            return None
        if (-flow if setup.up else flow) < c["minReversalImbalance"]:
            return None                 # nem fordult meg a kotesaramlas
        if setup.retrace_pct(trade.price) > c["maxEntryRetracePct"]:
            return None                 # a mozgas nagy resze mar lefutott
        if not self._book_ok(trade.symbol, irany, c):
            return None
        if c["requireTrendForReversal"] and not self._trend_ok(trade.symbol, irany):
            return None

        return self._kiad(trade, setup, f"{irany}_REVERSAL", irany, c, flow,
                          extra=[f"az impulzus kifulladt ({trade.ts - setup.pivot_ts:.0f} mp "
                                 f"ota nincs uj szelsoertek)",
                                 f"a {fprice(setup.counter)} szint "
                                 f"{'letorve' if setup.up else 'visszaveve'}, es "
                                 f"{c['reclaimHoldSec']:.0f} mp-ig tartotta is"])

    # ------------------------------------------------------------------ meresek

    @staticmethod
    def _measure(window, now, c):
        """Az impulzus-ablak mutatoi. None, ha nem merheto.

        IDOALAPU ablak: egy nagy paron 30 kotes 30 ezredmasodperc alatt is beerkezik,
        abbol ertelmetlen tempot szamolni.
        """
        start = now - c["impulseWindowSec"]
        w = [x for x in window if x[0] >= start]
        if len(w) < c["minTradesInWindow"]:
            return None
        span = w[-1][0] - w[0][0]
        if span < c["impulseWindowSec"] / 2:
            return None

        ys = [x[1] for x in w]
        if ys[0] <= 0:
            return None
        # A mozgast az ablakra ILLESZTETT EGYENES adja, nem a vegpontok kulonbsege:
        # igy egyetlen kiugro print nem csinal impulzust, es a furészfog ~nulla.
        xs = [x[0] - w[0][0] for x in w]
        n = len(w)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0 or mean_y <= 0:
            return None
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        move_pct = (sxy / sxx) * span / mean_y * 100.0

        elmozdulas = abs(sxy / sxx) * span
        legnagyobb = max((abs(b - a) for a, b in zip(ys, ys[1:])), default=0.0)
        egy_lepes = (legnagyobb / elmozdulas * 100.0) if elmozdulas > 0 else 100.0

        notional = sum(x[2] for x in w)
        buy = sum(x[3] for x in w)
        sell = sum(x[4] for x in w)
        return {
            "movePct": round(move_pct, 4),
            "spanSec": round(span, 2),
            "trades": n,
            "notional": round(notional, 2),
            "buyNotional": round(buy, 2),
            "sellNotional": round(sell, 2),
            "delta": round(buy - sell, 2),
            # -1 .. +1, nincs vegtelen es szimmetrikus -- ezert nem aranyt hasznalunk
            "imbalance": round((buy - sell) / notional, 4) if notional > 0 else 0.0,
            "singleStepPct": round(min(egy_lepes, 100.0), 1),
            "startPrice": ys[0],
        }

    def _flow(self, symbol, now, seconds):
        """Kotesaramlas-imbalance az utolso par masodpercben: -1 .. +1."""
        start = now - seconds
        buy = sell = 0.0
        db = 0
        for ts, _, _, b, s in self.window[symbol]:
            if ts < start:
                continue
            buy += b
            sell += s
            db += 1
        total = buy + sell
        if db < self.cfg.detector["minTradesInWindow"] or total <= 0:
            return None
        return (buy - sell) / total

    def _book_ok(self, symbol, direction, c):
        """A konyv ne alljon ellen: se a legjobb szint tulsulya, se egy kozeli fal."""
        ctx = self.book.context(symbol) if self.book else None
        if not ctx:
            return True                 # nincs konyv-adat -> nem nemitunk el mindent
        imb = ctx.get("topImbalance")
        if imb is not None:
            ellen = -imb if direction == "LONG" else imb
            if ellen > c["maxOpposingBookImbalance"]:
                return False
        fal = ctx.get("wallAsk") if direction == "LONG" else ctx.get("wallBid")
        if fal and fal["distancePct"] <= c["wallBlockDistPct"]:
            return False
        return True

    def _trend_ok(self, symbol, direction):
        t = self.trend.get(symbol) if self.trend else None
        if not t:
            return True                 # nincs adat -> nem kapuz
        return t["trend"] == ("bullish" if direction == "LONG" else "bearish")

    # ------------------------------------------------------------------ jelzes

    def _kiad(self, trade, setup, setup_nev, direction, c, flow, extra):
        self.setups.pop(trade.symbol, None)
        self.cooldown[trade.symbol] = trade.ts + c["symbolCooldownSec"]
        self.total_candidates += 1
        imp = setup.impulse
        ctx = (self.book.context(trade.symbol) if self.book else None) or {}
        t = (self.trend.get(trade.symbol) if self.trend else None) or {}

        metrics = {
            "setup": setup_nev,
            "impulsePct": imp["movePct"],
            "impulseSec": imp["spanSec"],
            "impulseFrom": setup.p0,
            "impulseTo": setup.p1,
            "impulseNotional": imp["notional"],
            "impulseImbalance": imp["imbalance"],
            "impulseBaseline": imp["baseline"],
            # hanyszorosa a par szokasos ablak-forgalmanak -- ez mondja meg, hogy
            # valodi penz hajtotta-e, vagy csak vekony konyvon csuszott at az ar
            "notionalRatio": round(imp["notional"] / imp["notionalBaseline"], 1)
                             if imp.get("notionalBaseline") else None,
            "legPct": round(setup.leg / setup.p0 * 100.0, 4) if setup.p0 else None,
            "pivot": setup.pivot,
            "counter": setup.counter,
            "maxRetracePct": round(setup.max_retrace, 1),
            "exhaustionSec": round(trade.ts - setup.pivot_ts, 1),
            "setupAgeSec": round(setup.kor(trade.ts), 1),
            "confirmImbalance": round(flow, 4),
            "spreadPct": ctx.get("spreadPct"),
            "bookImbalance": ctx.get("topImbalance"),
            "trend": t.get("trend"),
        }
        reasons = [
            f"impulzus {imp['movePct']:+.2f}% / {imp['spanSec']:.1f}s "
            f"({fprice(setup.p0)} -> {fprice(setup.p1)}), "
            f"{imp['notional']:,.0f} USDT forgalom, flow {imp['imbalance']:+.2f}",
        ] + extra + [
            f"kotesaramlas a belepo iranyaba {flow:+.2f} "
            f"({c['flowWindowSec']:.0f} mp)",
            f"setup kora {setup.kor(trade.ts):.0f} mp",
        ]
        if ctx.get("spreadPct") is not None:
            reasons.append(f"spread {ctx['spreadPct']:.3f}%, konyv-imbalance "
                           f"{ctx.get('topImbalance', 0):+.2f}")
        if t.get("trend"):
            reasons.append(f"EMA trend {t['trend']}")

        log.info("SETUP OK   %-14s %-18s ar %.8g  lab %.2f%%  visszahuzas %.0f%%  "
                 "flow %+.2f  kor %.0f mp", trade.symbol, setup_nev, trade.price,
                 metrics["legPct"] or 0, setup.max_retrace, flow,
                 setup.kor(trade.ts))
        events.add(f"{trade.symbol:<14} {setup_nev}")

        return make_signal(
            self.name, self.config_key, trade.symbol, direction, trade.price,
            trade.ts, reasons=reasons, metrics=metrics, setup=setup_nev,
            history=_thin(self.history[trade.symbol]))

    # ------------------------------------------------------------------ statusz

    def allapotok(self):
        szamlalo = defaultdict(int)
        for s in self.setups.values():
            szamlalo[s.state] += 1
        most = self.last_ts
        szamlalo[COOLDOWN] = sum(1 for t in self.cooldown.values() if t > most)
        return szamlalo

    def readiness(self):
        c = self.cfg.detector
        a = self.allapotok()
        sor = (f"setup: impulzus {a[IMPULSE_DETECTED]}, megerositesre var "
               f"{a[WAITING_CONFIRMATION]}, cooldown {a[COOLDOWN]} | "
               f"normal kesz: {self.baseline.kesz_parok()}/{len(self.latest)} par")
        legjobb = None
        for symbol, m in self.latest.items():
            if not m or m.get("baseline") is None:
                continue
            kell = max(c["minImpulsePct"], m["baseline"] * c["impulseBaselineRatio"])
            hanyad = abs(m["movePct"]) / kell if kell else 0
            if legjobb is None or hanyad > legjobb[0]:
                legjobb = (hanyad, symbol, abs(m["movePct"]), kell)
        if legjobb:
            _, symbol, mozgas, kell = legjobb
            sor += f" | legkozelebb: {symbol} {mozgas:.3f}% (kell {kell:.3f}%)"
        return sor

    def status_lines(self, top=10):
        """Reszletes allapot -- csak DEBUG szinten."""
        if not self.setups:
            return ["  eppen egy setup sem epul"]
        out = [f"  {pad('par', 14)}{'allapot':<22}{'lab %':>8}{'visszahuzas':>13}"]
        for symbol, s in list(self.setups.items())[:top]:
            lab = s.leg / s.p0 * 100.0 if s.p0 else 0.0
            out.append(f"  {pad(symbol, 14)}{s.state:<22}{lab:>7.2f}%"
                       f"{s.max_retrace:>12.0f}%")
        return out


def _thin(points, bucket_sec=1.0):
    """Ar-elozmeny ritkitasa: masodpercenkent az elso / min / max / utolso pont.

    Egy 60 masodperces setup alatt tobb ezer kotes erkezhet (a BTRUSDT-nel 9477 volt
    64 masodperc alatt). A teljes lista feleslegesen nagy Mongo dokumentum, a puszta
    vagas viszont eltuntetne a szelsoertekeket -- epp azt, amit latni akarunk.
    """
    if not points:
        return []
    vodrok = {}
    for ts, ar in points:
        vodrok.setdefault(int(ts / bucket_sec), []).append((ts, ar))
    out = []
    for k in sorted(vodrok):
        cs = vodrok[k]
        kivalasztott = {cs[0], cs[-1], min(cs, key=lambda x: x[1]),
                        max(cs, key=lambda x: x[1])}
        out.extend(sorted(kivalasztott))
    return out
