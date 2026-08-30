"""Symbolonkenti mozgasminoseg.

A forgalmi kuszob nem fogja meg a szaggatott meme coinokat, mert nem a forgalommal
van baj: az arfolyam ossze-vissza ugral, es azon nem lehet fordulot vagy lendulet
kereskedni. A jo merőszam a HATEKONYSAGI ARANY:

    efficiency = |utolso ar - elso ar| / a megtett ut osszege

      tiszta, iranyos mozgas   ->  0.7 - 1.0
      normal par               ->  0.3 - 0.6
      ossze-vissza ugralo meme ->  0.0 - 0.2

Symbolonkent EWMA-val kovetjuk, es aki a kuszob alatt van, arra egyik detektor
sem jelez.
"""
from collections import deque, defaultdict

EWMA_ALPHA = 0.05      # ~20 ablaknyi emlekezet


class SymbolQuality:
    def __init__(self, cfg):
        self.cfg = cfg
        self.prices = defaultdict(deque)   # symbol -> utolso N ar
        self.eff = {}                      # symbol -> hatekonysagi arany (EWMA)
        self.blocked = set()               # amiket eppen kizarunk

    def on_trade(self, trade):
        c = self.cfg.detector
        n = c["qualityWindow"]
        w = self.prices[trade.symbol]
        w.append(trade.price)
        if len(w) > n:
            w.popleft()
        if len(w) < n:
            return
        # csak minden n/2. trade-nel szamolunk ujra -- eleg surun valtozik
        if len(w) % max(1, n // 2):
            return

        ut = sum(abs(w[i + 1] - w[i]) for i in range(len(w) - 1))
        nettó = abs(w[-1] - w[0])
        ertek = nettó / ut if ut > 0 else 0.0
        elozo = self.eff.get(trade.symbol)
        self.eff[trade.symbol] = (ertek if elozo is None
                                  else elozo + EWMA_ALPHA * (ertek - elozo))

    def efficiency(self, symbol):
        return self.eff.get(symbol)

    def tradeable(self, symbol):
        """(mehet-e, ok). Amig nincs eleg adat, atengedjuk."""
        e = self.eff.get(symbol)
        if e is None:
            return True, None
        kuszob = self.cfg.detector["minEfficiency"]
        if e < kuszob:
            self.blocked.add(symbol)
            return False, f"szaggatott mozgas (hatekonysag {e:.2f} < {kuszob:.2f})"
        self.blocked.discard(symbol)
        return True, None

    def blocked_summary(self, top=6):
        if not self.blocked:
            return []
        rendezett = sorted(self.blocked, key=lambda s: self.eff.get(s, 1.0))
        nevek = "  ".join(f"{s} ({self.eff[s]:.2f})" for s in rendezett[:top])
        tobb = f"  ... +{len(rendezett) - top}" if len(rendezett) > top else ""
        return [f"  KIZARVA szaggatott mozgas miatt ({len(rendezett)} par, "
                f"hatekonysag < {self.cfg.detector['minEfficiency']:.2f}):",
                f"    {nevek}{tobb}"]
