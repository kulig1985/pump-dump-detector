"""Symbolonkenti "normal" ertekek gordulo medianja.

A kerdes, amire valaszol: "az adott paron SZOKATLAN-e ez a mozgas / ez a forgalom?"
Egy meme coinon 0.3% masodpercenkent tortenik, a BTC-n hetente; egy 50,000 USDT-s
ablak az egyiken semmi, a masikon rendkivuli. Fix kuszobbel ez nem kezelheto.

Nem historikus adatbazis: futas kozben, memoriaban gyujtjuk. Symbolonkent
masodpercenkent egy mintat veszunk, es a MEDIANT tekintjuk normalnak -- a median
nem viheto el egy kiugro ertekkel.
"""
import math
import statistics
from collections import deque, defaultdict

MINTA_SURUSEG_SEC = 1.0     # ennel surubben nem mintavetelezunk


class RollingMedian:
    """Symbolonkent gyujtott mintak medianja, idoablakkal.

    A "kesz" allapot IDOTARTAM-alapu: ha a normalt 5 percre kertek, akkor tenyleg
    kell kb. 5 percnyi elozmeny. Korabban 60 minta (kb. 1 perc) mar keszne
    nyilvanitotta az 5 perces baseline-t -- restart utan igy percekkel korabban
    jelezhetett a rendszer, hianyos elozmenybol.

    A mintak MINDIG az aktualis idohoz kepest ertendok: a value() is levagja a
    now - window_sec elottieket. Enelkul egy hosszu adatkimaradas utan a regi,
    elavult mintak medianjat adnank vissza -- az add() ugyanis csak akkor vag,
    amikor uj minta erkezik. Rovid reconnect utan viszont a meg friss mintak
    ervenyben maradnak, tehat nem kell feleslegesen ujra 5 percet varni.
    """

    KESZ_ARANY = 0.9        # a mintaknak az ablak ennyi reszet le kell fedniuk

    def __init__(self, window_sec, min_samples):
        self.window_sec = window_sec
        self._min_samples = min_samples
        self.samples = defaultdict(deque)      # symbol -> deque[(ts, ertek)]
        self.last_sample = {}

    def _trim(self, w, now):
        hatar = now - self.window_sec
        while w and w[0][0] < hatar:
            w.popleft()

    def add(self, symbol, ts, ertek):
        if ts - self.last_sample.get(symbol, 0) < MINTA_SURUSEG_SEC:
            return
        self.last_sample[symbol] = ts
        w = self.samples[symbol]
        w.append((ts, ertek))
        self._trim(w, ts)

    def value(self, symbol, now=None):
        """A par normalja, vagy None ha nincs eleg FRISS elozmeny.

        Harom feltetel: eleg SOK minta, eleg HOSSZU idoszak lefedese, es --
        a `now` megadasaval -- hogy a mintak az aktualis idohoz kepest frissek
        legyenek. A trimmeles utan a maradek ablak a [now - window_sec, now]
        szakaszra esik, tehat a lefedettsegi feltetel egyben azt is kikenyszeriti,
        hogy az utolso minta friss legyen.

        FIGYELEM: a `now` megadasa TRIMMEL, tehat mellekhatasa van. Ez szandekos --
        a kiesett mintak sosem valnak ujra relevanssa, az ido elore megy.
        """
        w = self.samples.get(symbol)
        if not w:
            return None
        if now is not None:
            self._trim(w, now)
        if len(w) < self._min_samples:
            return None
        if w[-1][0] - w[0][0] < self.window_sec * self.KESZ_ARANY:
            return None
        return statistics.median(v for _, v in w)

    def ready(self, symbol, now=None):
        return self.value(symbol, now) is not None

    def kesz_parok(self, now=None):
        return sum(1 for s in list(self.samples) if self.ready(s, now))


class Baseline:
    """A par normal rovid tavu ARMOZGASA (szazalekban).

    Kulon osztaly a RollingMedian folott, mert az armozgasnak van egy sajatossaga:
    mas idotavra atskalazhato. Bolyongasnal az elmozdulas az ido GYOKEVEL no, tehat
    egy 4x hosszabb ablakban a normal mozgas ~2x akkora.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        perc = cfg.detector["baselineMinutes"]
        # legalabb a masodpercek felenyi minta, ES az ablak 90%-anak lefedese
        self.median = RollingMedian(perc * 60, int(perc * 60 * 0.5))

    def add(self, symbol, ts, abs_move_pct):
        self.median.add(symbol, ts, abs_move_pct)

    def value(self, symbol, now=None):
        return self.median.value(symbol, now)

    def kesz_parok(self, now=None):
        return self.median.kesz_parok(now)

    def value_for(self, symbol, seconds, now=None):
        """A normal mozgas EGY MASIK idotavra atskalazva (gyok-skalazas)."""
        alap = self.value(symbol, now)
        if alap is None:
            return None
        ablak = self.cfg.detector["impulseWindowSec"]
        if not seconds or seconds <= 0 or ablak <= 0:
            return alap
        return alap * math.sqrt(seconds / ablak)

    def ratio(self, symbol, move_pct, now=None):
        """Hanyszorosa a mozgas a par normaljanak. None, ha meg nem tudjuk."""
        alap = self.value(symbol, now)
        if alap is None or alap <= 0:
            return None
        return abs(move_pct) / alap
