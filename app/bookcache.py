"""BookCache -- a partial book depth pillanatkepek memoriaban, symbolonkent.

A jelzes PILLANATABAN nem kerunk le semmit: a konyv folyamatosan streamel
(<symbol>@depth20@500ms), tehat a dontesnel mar keszen all.

FAIL-CLOSED: ha nincs friss adat, a detektor NEM jelez. A "nincs adat, hat akkor
atengedjuk" viselkedes korabban orakon at rossz jelzeseket eredmenyezett.

A hivatalos spec szerint a Partial Book Depth Stream a "public" csoportba tartozik,
az esemeny neve depthUpdate, a mezok b (bid) es a (ask): [[ar, mennyiseg], ...].
"""
import time
import logging

log = logging.getLogger("bookcache")


class BookCache:
    def __init__(self, cfg):
        self.cfg = cfg
        self.books = {}         # symbol -> (ts, bids, asks)
        self.messages = 0

    def on_depth(self, data):
        """Egy depthUpdate uzenet feldolgozasa."""
        try:
            symbol = data["s"]
            bids = [(float(p), float(q)) for p, q in data.get("b", [])]
            asks = [(float(p), float(q)) for p, q in data.get("a", [])]
        except (KeyError, TypeError, ValueError):
            return
        if not bids or not asks:
            return
        self.books[symbol] = (time.time(), bids, asks)
        self.messages += 1

    def fresh(self, symbol):
        """Van-e friss konyv-adat errol a parrol? Ez a belepo egyik feltetele."""
        b = self.books.get(symbol)
        return bool(b) and time.time() - b[0] <= self.cfg.detector["maxDataAgeSec"]

    def snapshot(self, symbol):
        """A nyers konyv (a market_snapshots-hoz), vagy None ha elavult."""
        if not self.fresh(symbol):
            return None
        _, bids, asks = self.books[symbol]
        return {"bids": bids, "asks": asks}

    def status(self):
        if not self.books:
            return "konyv-melyseg: nincs adat"
        friss = sum(1 for s in self.books if self.fresh(s))
        return f"konyv-melyseg: {friss}/{len(self.books)} par friss"
