"""Symbolonkenti rovid tavu normal mozgas.

A kerdes, amire valaszol: "az adott paron SZOKATLAN-e egy 0.3%-os mozgas?"
Egy meme coinon 0.3% masodpercenkent tortenik, a BTC-n hetente. Fix kuszobbel
ez nem kezelheto.

Nem historikus adatbazis: futas kozben, memoriaban gyujtjuk. Symbolonkent
masodpercenkent egy mintat veszunk az aktualis rovid ablak |elmozdulasabol|,
es a MEDIANT tekintjuk normalnak -- a median nem viheto el egy kiugro ertekkel.
"""
import math
import statistics
from collections import deque, defaultdict

MINTA_SURUSEG_SEC = 1.0     # ennel surubben nem mintavetelezunk


class Baseline:
    def __init__(self, cfg):
        self.cfg = cfg
        self.samples = defaultdict(deque)      # symbol -> deque[(ts, |mozgas %|)]
        self.last_sample = {}

    def add(self, symbol, ts, abs_move_pct):
        if ts - self.last_sample.get(symbol, 0) < MINTA_SURUSEG_SEC:
            return
        self.last_sample[symbol] = ts
        w = self.samples[symbol]
        w.append((ts, abs_move_pct))
        hatar = ts - self.cfg.detector["baselineMinutes"] * 60
        while w and w[0][0] < hatar:
            w.popleft()

    def value(self, symbol):
        """A par normal rovid mozgasa, vagy None ha meg nincs eleg minta."""
        w = self.samples.get(symbol)
        if not w or len(w) < self.min_samples():
            return None
        return statistics.median(m for _, m in w)

    def min_samples(self):
        # legalabb egy percnyi minta kell, mielott barmit allitanank a parrol
        return min(60, int(self.cfg.detector["baselineMinutes"] * 60 / 2))

    def value_for(self, symbol, seconds):
        """A normal mozgas EGY MASIK idotavra atskalazva.

        A baseline egy moveWindowSec (2 mp) hosszu ablakbol keszul, de a reversal
        akar 20 masodperces mozgast is mer. Egy 20 mp-es mozgas termeszetesen
        nagyobb: bolyongasnal az elmozdulas az ido gyokevel no. Skalazas nelkul
        egy 20 mp-es normal kuszas "rendkivulinek" latszana.
        """
        alap = self.value(symbol)
        if alap is None:
            return None
        ablak = self.cfg.detector["moveWindowSec"]
        if not seconds or seconds <= 0 or ablak <= 0:
            return alap
        return alap * math.sqrt(seconds / ablak)

    def ratio(self, symbol, move_pct):
        """Hanyszorosa az aktualis mozgas a par normaljanak. None, ha meg nem tudjuk."""
        alap = self.value(symbol)
        if alap is None or alap <= 0:
            return None
        return abs(move_pct) / alap
