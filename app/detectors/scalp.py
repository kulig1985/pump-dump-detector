"""ScalpDetector -- impulzus utani continuation belepo, 5-10 perces kezi scalpre.

EGYETLEN setup-tipus, egyetlen ut:

    IMPULSE  ->  PULLBACK  ->  FRISS BREAKOUT  ->  SIGNAL

Allapotgep symbolonkent, egyszerre EGY aktiv setup:

    IDLE -> IMPULSE -> WAIT_PULLBACK -> WAIT_BREAKOUT -> SIGNAL -> COOLDOWN -> IDLE

Amit a rendszer szandekosan NEM csinal: nincs reversal ag, nincs EMA a belepo
dontesben, nincs fal- es konyv-imbalance kapu. Egy jol ertheto setup, amit merni
lehet -- ha ez mukodik, arra lehet epiteni.

Minden meret az IMPULZUS-LAB (leg = |pivot - p0|) aranyaban ertendo, nem abszolut
szazalekban: igy ugyanaz a parameter mukodik egy 0.4%-os es egy 4%-os impulzusnal.
A leg a pivottal EGYUTT frissul, amig az ar uj szelsoerteket csinal.
"""
import logging
from collections import deque, defaultdict

from .. import events
from ..fmt import pad, price as fprice
from .base import Detector, make_signal
from .baseline import Baseline, RollingMedian

log = logging.getLogger("scalp")

IDLE = "IDLE"
IMPULSE = "IMPULSE"                 # az ar meg uj szelsoerteket csinal
WAIT_PULLBACK = "WAIT_PULLBACK"     # visszahuzodik, de meg nem eleget
WAIT_BREAKOUT = "WAIT_BREAKOUT"     # a pivot rogzult, a kitoresre varunk
COOLDOWN = "COOLDOWN"

MAX_HISTORY_SEC = 180.0     # ennyi arelozmenyt tartunk a bizonyitekhoz


class Setup:
    """Egy symbol eppen fejlodo setupja egy impulzus utan."""

    __slots__ = ("up", "p0", "pivot", "pivot_ts", "leg", "t1", "state", "extreme_back",
                 "max_retrace", "impulse", "breakout_ts", "breakout_level",
                 "wait_breakout_ts")

    def __init__(self, up, p0, pivot, pivot_ts, t1, impulse):
        self.up = up                    # felfele volt-e az impulzus
        self.p0 = p0                    # az impulzus kiindulopontja
        self.pivot = pivot              # az ablak TENYLEGES szelsoerteke
        self.pivot_ts = pivot_ts
        self.leg = abs(pivot - p0)      # a lab; a pivottal EGYUTT frissul
        self.t1 = t1
        self.impulse = impulse          # a mert impulzus-adatok (Mongo-ba is)
        self.state = IMPULSE
        self.extreme_back = pivot       # a visszahuzas szelsoerteke (kijelzeshez)
        self.max_retrace = 0.0          # a legmelyebb visszahuzas a lab %-aban
        self.breakout_ts = None         # mikor tortent a kitores (a keresztezes)
        self.breakout_level = None
        self.wait_breakout_ts = None    # mikor rogzult a pivot -- a flow innentol szamit

    def uj_szelsoertek(self, ar, ts):
        """Uj csucs/melypont: a pivot ES a lab is frissul.

        Ez volt a hiba korabban: a pivot elmozdult, de a leg a regi erteken maradt,
        igy a visszahuzas szazaleka rossz alaphoz merodott.
        """
        self.pivot = ar
        self.pivot_ts = ts
        self.leg = abs(ar - self.p0)
        self.extreme_back = ar
        self.max_retrace = 0.0

    def retrace_pct(self, ar):
        """A pivottol mert visszahuzas a lab szazalekaban."""
        if self.leg <= 0:
            return 0.0
        tav = (self.pivot - ar) if self.up else (ar - self.pivot)
        return max(0.0, tav) / self.leg * 100.0

    def extension_pct(self, ar):
        """Mennyivel ment tul az ar a kitoresi szinten, a lab szazalekaban."""
        if self.leg <= 0 or self.breakout_level is None:
            return 0.0
        tav = (ar - self.breakout_level) if self.up else (self.breakout_level - ar)
        return max(0.0, tav) / self.leg * 100.0

    def kor(self, ts):
        return ts - self.t1


class ScalpDetector(Detector):
    name = "scalp"
    config_key = "detector"

    def __init__(self, cfg, baseline=None, book=None, eligibility=None):
        self.cfg = cfg
        self.eligibility = eligibility  # a spread/lista ellenorzes a commit ELOTT
        self.baseline = baseline or Baseline(cfg)
        perc = cfg.detector["baselineMinutes"]
        # a forgalom normalja: ugyanaz a median-logika, de az idovel LINEARISAN
        # skalazodik, ezert nem hasznaljuk ra a Baseline gyok-skalazasat
        self.notional_baseline = RollingMedian(perc * 60, int(perc * 60 * 0.5))
        self.book = book                    # BookCache (frissesseg + snapshot)
        self.window = defaultdict(deque)    # symbol -> deque[(ts, ar, notional, buy, sell)]
        self.history = defaultdict(deque)   # symbol -> deque[(ts, ar)] a bizonyitekhoz
        self.setups = {}                    # symbol -> Setup (EGYSZERRE EGY)
        self.cooldown = {}                  # symbol -> eddig nincs uj setup
        self.prev_price = {}                # symbol -> az elozo kotes ara
        self.latest = {}                    # symbol -> utolso mert allapot (STATUS)
        self.last_ts = 0.0
        self.ticks = 0
        self.total_candidates = 0

    def reset(self, symbols=None):
        """Adatszakadas utan: a felepitett allapot NEM folytathato.

        Reconnect utan az elso kotes kulonben egy regen megtortent kitores "friss
        keresztezesenek" latszana (a prev_price a szakadas elottrol maradt volna).
        A baseline megmarad: az a par hosszu tavu normalja, nem setup-allapot.
        """
        celok = list(symbols) if symbols is not None else list(self.setups)
        for symbol in celok:
            self.setups.pop(symbol, None)
            self.prev_price.pop(symbol, None)
            self.window.pop(symbol, None)
        return len(celok)

    # ------------------------------------------------------------------ fo utvonal

    def on_trade(self, trade):
        c = self.cfg.detector
        self.ticks += 1
        self.last_ts = trade.ts
        elozo = self.prev_price.get(trade.symbol)
        self.prev_price[trade.symbol] = trade.price

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
            m["baseline"] = self.baseline.value(trade.symbol, trade.ts)
            m["notionalBaseline"] = self.notional_baseline.value(trade.symbol, trade.ts)
            self.baseline.add(trade.symbol, trade.ts, abs(m["movePct"]))
            self.notional_baseline.add(trade.symbol, trade.ts, m["notional"])

        setup = self.setups.get(trade.symbol)
        if setup is not None:
            return self._track(trade, setup, elozo, c)
        return self._detect_impulse(trade, m, c)

    # ------------------------------------------------------------------ 1. IMPULZUS

    def _detect_impulse(self, trade, m, c):
        """Eros rovid tavu mozgas: ar + forgalom + kotesaramlas. NEM jelzes."""
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
            return None                 # nem egyiranyu a kotesaramlas
        if c["maxSingleStepPct"] and m["singleStepPct"] > c["maxSingleStepPct"]:
            return None                 # egyetlen arlepes adta -> konyv-sopres

        # A pivot az ablak TENYLEGES szelsoerteke (UP-nal a high, DOWN-nal a low),
        # nem az utolso kotes ara -- kulonben hamis kitorest latnank.
        pivot = m["high"] if up else m["low"]
        pivot_ts = m["highTs"] if up else m["lowTs"]
        self.setups[trade.symbol] = Setup(up, m["startPrice"], pivot, pivot_ts,
                                          trade.ts, dict(m))
        log.info("IMPULSE %-4s %-14s ar %.8g  %+.2f%% / %.1fs  normal %.3f%%  "
                 "forgalom %s USDT (%.1fx)  flow %+.2f",
                 "UP" if up else "DOWN", trade.symbol, trade.price, m["movePct"],
                 m["spanSec"], m["baseline"], f"{m['notional']:,.0f}",
                 m["notional"] / m["notionalBaseline"] if m["notionalBaseline"] else 0,
                 m["imbalance"])
        events.add(f"{trade.symbol:<14} IMPULSE {'UP' if up else 'DOWN'} "
                   f"{m['movePct']:+.2f}%")
        return None

    # ------------------------------------------------------------------ 2-3. setup

    def _track(self, trade, setup, elozo, c):
        ar = trade.price

        # ---- ervenytelenites ----
        if setup.kor(trade.ts) > c["setupTimeoutSec"]:
            return self._eldob(trade, "lejart a setup ideje")
        tul = (setup.p0 - ar) if setup.up else (ar - setup.p0)
        if setup.leg > 0 and tul > setup.leg * c["invalidateBeyondOriginPct"] / 100.0:
            return self._eldob(trade, "az ar visszament az impulzus ala")

        # ---- IMPULSE / WAIT_PULLBACK: a pivot es a lab meg egyutt mozog ----
        if setup.state in (IMPULSE, WAIT_PULLBACK):
            if (ar > setup.pivot) if setup.up else (ar < setup.pivot):
                setup.uj_szelsoertek(ar, trade.ts)
                setup.state = IMPULSE
                return None
            visszahuzas = setup.retrace_pct(ar)
            setup.max_retrace = max(setup.max_retrace, visszahuzas)
            if (ar < setup.extreme_back) if setup.up else (ar > setup.extreme_back):
                setup.extreme_back = ar
            if visszahuzas < c["minPullbackPct"]:
                setup.state = WAIT_PULLBACK
                return None
            # eleg mely a visszahuzas: a pivot ROGZUL, jon a kitoresi szint
            setup.state = WAIT_BREAKOUT
            # A megerosito flow CSAK innentol szamit: az impulzus alatti regi
            # buy/sell aramlas ne erositse mesterségesen a kesobbi kitorest.
            setup.wait_breakout_ts = trade.ts
            setup.breakout_level = setup.pivot + (1 if setup.up else -1) \
                * setup.leg * c["breakoutOfLegPct"] / 100.0
            log.info("WAIT_BREAKOUT %-14s %-4s pivot %.8g  kitores %.8g  "
                     "visszahuzas %.0f%%", trade.symbol, "UP" if setup.up else "DOWN",
                     setup.pivot, setup.breakout_level, visszahuzas)
            return None

        # ---- WAIT_BREAKOUT ----
        setup.max_retrace = max(setup.max_retrace, setup.retrace_pct(ar))
        if setup.max_retrace > c["maxPullbackPct"]:
            return self._eldob(trade, "a visszahuzas tul melyre ment")

        szint = setup.breakout_level
        if setup.breakout_ts is not None:
            # A kitores mar megtortent, de meg megerositesre var. Ha kozben az ar
            # visszament a szint ROSSZ OLDALARA, ez a kitores mar nem ervenyes --
            # kulonben LONG jelzest adnank olyan aron, ami mar a szint ALATT van.
            # Csak egy UJ, valodi cross indithat uj megerositesi ablakot.
            if (ar <= szint) if setup.up else (ar >= szint):
                setup.breakout_ts = None
                log.info("KITORES VISSZA %-14s %-4s ar %.8g  vissza a %.8g szint "
                         "rossz oldalara", trade.symbol,
                         "UP" if setup.up else "DOWN", ar, szint)
                return None
            if trade.ts - setup.breakout_ts > c["maxBreakoutAgeSec"]:
                return self._eldob(trade, "a kitores megerosites nelkul elavult")
        else:
            # FRISS kitores: MOST kell keresztezni a szintet, nem eleg folotte allni
            if elozo is None:
                return None
            keresztezte = (elozo <= szint < ar) if setup.up else (elozo >= szint > ar)
            if not keresztezte:
                return None
            setup.breakout_ts = trade.ts
            log.info("BREAKOUT   %-14s %-4s ar %.8g  szint %.8g",
                     trade.symbol, "UP" if setup.up else "DOWN", ar, szint)

        # az ar mar tul messze jart a kitoresi szinttol -> nincs ertelme beszallni
        if setup.extension_pct(ar) > c["maxEntryExtensionPct"]:
            return self._eldob(trade, "az ar tul messze ment a kitoresi szinttol")

        return self._confirm(trade, setup, c)

    def _eldob(self, trade, ok):
        self.setups.pop(trade.symbol, None)
        log.info("IDLE       %-14s setup eldobva: %s", trade.symbol, ok)
        return None

    # ------------------------------------------------------------------ 4. megerosites

    def _confirm(self, trade, setup, c):
        """A kitores pillanataban: kotesaramlas + FRISS konyv-adat. Semmi mas.

        Ha barmelyik feltetel nem teljesul, a setup ELETBEN MARAD -- nem torlunk,
        es NEM inditunk cooldownt. Egy elutasitas nem "elhasznalt" jelzes.
        """
        # A flow ablaka nem nyulhat vissza a pivot rogzitese ele: az impulzus
        # alatti egyiranyu aramlas kulonben magatol "megerositene" a kitorest.
        eleje = max(trade.ts - c["flowWindowSec"], setup.wait_breakout_ts or 0.0)
        flow = self._flow(trade.symbol, eleje, c["minTradesInWindow"])
        if flow is None:
            return None                 # nincs eleg friss kotes -> NINCS jelzes
        if (flow if setup.up else -flow) < c["minConfirmImbalance"]:
            return None

        # FAIL-CLOSED: elavult vagy hianyzo konyv-adattal nem jelzunk
        if not (self.book and self.book.fresh(trade.symbol)):
            return None

        # Az eligibility (spread, white/blacklist) MEG A COMMIT ELOTT: kulonben
        # a setup torlodne es a cooldown elindulna egy olyan jelzes utan, amit a
        # manager amugy is eldob.
        if self.eligibility is not None:
            mehet, ok, _ = self.eligibility.check(trade.symbol)
            if not mehet:
                log.info("VAR        %-14s a kitores kesz, de %s -- a setup el tovabb",
                         trade.symbol, ok)
                return None

        return self._kiad(trade, setup, c, flow)

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
        # Az ablak TENYLEGES szelsoertekei. A pivot ezekbol lesz, nem az utolso
        # kotes arabol: kulonben ha az ar mar jart magasabban, egy kesobbi,
        # alacsonyabb pont lenne a pivot -- es a rendszer hamis kitorest latna.
        hi = max(w, key=lambda x: x[1])
        lo = min(w, key=lambda x: x[1])
        return {
            "movePct": round(move_pct, 4),
            "spanSec": round(span, 2),
            "trades": n,
            "notional": round(notional, 2),
            "delta": round(buy - sell, 2),
            # -1 .. +1, nincs vegtelen es szimmetrikus -- ezert nem aranyt hasznalunk
            "imbalance": round((buy - sell) / notional, 4) if notional > 0 else 0.0,
            "singleStepPct": round(min(egy_lepes, 100.0), 1),
            "startPrice": ys[0],
            "high": hi[1], "highTs": hi[0],
            "low": lo[1], "lowTs": lo[0],
        }

    def _flow(self, symbol, start, min_trades):
        """Kotesaramlas-imbalance a `start` ota erkezett kotesekbol: -1 .. +1.

        None, ha nincs eleg friss kotes -- ilyenkor NINCS jelzes (fail-closed).
        """
        buy = sell = 0.0
        db = 0
        for ts, _, _, b, s in self.window[symbol]:
            if ts < start:
                continue
            buy += b
            sell += s
            db += 1
        total = buy + sell
        if db < min_trades or total <= 0:
            return None
        return (buy - sell) / total

    # ------------------------------------------------------------------ jelzes

    def _kiad(self, trade, setup, c, flow):
        self.setups.pop(trade.symbol, None)
        self.cooldown[trade.symbol] = trade.ts + c["symbolCooldownSec"]
        self.total_candidates += 1
        irany = "LONG" if setup.up else "SHORT"
        imp = setup.impulse
        kitores_kor = trade.ts - setup.breakout_ts

        metrics = {
            "impulsePct": imp["movePct"],
            "impulseSec": imp["spanSec"],
            "impulseNotional": imp["notional"],
            "legPct": round(setup.leg / setup.p0 * 100.0, 4) if setup.p0 else None,
            "pullbackPct": round(setup.max_retrace, 1),
            "breakoutLevel": setup.breakout_level,
            "breakoutAgeSec": round(kitores_kor, 2),
            # a taker forgalom hany szazaleka volt veteli (0-100)
            "flowPct": round((1 + flow) / 2 * 100, 1),
            "entryExtensionPct": round(setup.extension_pct(trade.price), 1),
            "setupAgeSec": round(setup.kor(trade.ts), 1),
        }
        log.info("SIGNAL     %-14s %-5s ar %.8g  impulzus %+.2f%%  visszahuzas %.0f%%  "
                 "flow %.0f%%  kitores kora %.1f mp", trade.symbol, irany, trade.price,
                 imp["movePct"], setup.max_retrace, metrics["flowPct"], kitores_kor)
        events.add(f"{trade.symbol:<14} {irany} belepo")

        return make_signal(
            self.name, self.config_key, trade.symbol, irany, trade.price,
            trade.ts, reasons=[], metrics=metrics, setup=irany,
            history=_thin(self.history[trade.symbol]))

    # ------------------------------------------------------------------ statusz

    def allapotok(self):
        szamlalo = defaultdict(int)
        for s in self.setups.values():
            szamlalo[s.state] += 1
        szamlalo[COOLDOWN] = sum(1 for t in self.cooldown.values() if t > self.last_ts)
        return szamlalo

    def readiness(self):
        c = self.cfg.detector
        a = self.allapotok()
        sor = (f"setup: impulzus {a[IMPULSE]}, visszahuzas {a[WAIT_PULLBACK]}, "
               f"kitoresre var {a[WAIT_BREAKOUT]}, cooldown {a[COOLDOWN]} | "
               f"normal kesz: {self.baseline.kesz_parok(self.last_ts)}/"
               f"{len(self.latest)} par")
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
        out = [f"  {pad('par', 14)}{'allapot':<16}{'lab %':>8}{'visszahuzas':>13}"]
        for symbol, s in list(self.setups.items())[:top]:
            lab = s.leg / s.p0 * 100.0 if s.p0 else 0.0
            out.append(f"  {pad(symbol, 14)}{s.state:<16}{lab:>7.2f}%"
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
