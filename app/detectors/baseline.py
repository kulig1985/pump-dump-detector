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
    """

    KESZ_ARANY = 0.9        # a mintaknak az ablak ennyi reszet le kell fedniuk

    def __init__(self, window_sec, min_samples):
        self.window_sec = window_sec
        self._min_samples = min_samples
        self.samples = defaultdict(deque)      # symbol -> deque[(ts, ertek)]
        self.last_sample = {}

    def add(self, symbol, ts, ertek):
        if ts - self.last_sample.get(symbol, 0) < MINTA_SURUSEG_SEC:
            return
        self.last_sample[symbol] = ts
        w = self.samples[symbol]
        w.append((ts, ertek))
        hatar = ts - self.window_sec
        while w and w[0][0] < hatar:
            w.popleft()

    def value(self, symbol):
        """A par normalja, vagy None ha meg nincs eleg elozmeny.

        Ketto kell hozza: eleg SOK minta, ES eleg HOSSZU idoszak lefedese.
        """
        w = self.samples.get(symbol)
        if not w or len(w) < self._min_samples:
            return None
        if w[-1][0] - w[0][0] < self.window_sec * self.KESZ_ARANY:
            return None
        return statistics.median(v for _, v in w)

    def ready(self, symbol):
        return self.value(symbol) is not None

    def kesz_parok(self):
        return sum(1 for s in self.samples if self.ready(s))


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

    def value(self, symbol):
        return self.median.value(symbol)

    def kesz_parok(self):
        return self.median.kesz_parok()

    def value_for(self, symbol, seconds):
        """A normal mozgas EGY MASIK idotavra atskalazva (gyok-skalazas)."""
        alap = self.value(symbol)
        if alap is None:
            return None
        ablak = self.cfg.detector["impulseWindowSec"]
        if not seconds or seconds <= 0 or ablak <= 0:
            return alap
        return alap * math.sqrt(seconds / ablak)

    def ratio(self, symbol, move_pct):
        """Hanyszorosa a mozgas a par normaljanak. None, ha meg nem tudjuk."""
        alap = self.value(symbol)
        if alap is None or alap <= 0:
            return None
        return abs(move_pct) / alap
