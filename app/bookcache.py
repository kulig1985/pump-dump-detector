"""BookCache -- a partial book depth pillanatkepek memoriaban, symbolonkent.

Korabban a jelzes PILLANATABAN nyitottunk egy rovid eletu WS kapcsolatot a
konyvre, es megvartuk az elso uzenetet. Ez keslelteti a jelzest, es el is bukhat.
Most a konyv folyamatosan streamel (<symbol>@depth20@500ms), tehat a dontes
pillanataban mar keszen all.

A hivatalos spec szerint a Partial Book Depth Stream a "public" csoportba tartozik,
az esemeny neve depthUpdate, a mezok b (bid) es a (ask): [[ar, mennyiseg], ...].
"""
import time
import logging

from .orderbook import find_wall

log = logging.getLogger("bookcache")

ELAVUL_SEC = 30.0       # ennel regebbi pillanatkepet nem hasznalunk dontesre


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

    def snapshot(self, symbol):
        """A nyers konyv (a market_snapshots-hoz), vagy None."""
        b = self.books.get(symbol)
        if not b or time.time() - b[0] > ELAVUL_SEC:
            return None
        return {"bids": b[1], "asks": b[2]}

    def context(self, symbol):
        """A dontesehez szukseges szamok. None, ha nincs friss konyv-adat."""
        b = self.books.get(symbol)
        if not b or time.time() - b[0] > ELAVUL_SEC:
            return None
        _, bids, asks = b
        c = self.cfg.detector
        kozep = (bids[0][0] + asks[0][0]) / 2
        if kozep <= 0:
            return None
        bid_n = bids[0][0] * bids[0][1]
        ask_n = asks[0][0] * asks[0][1]
        osszeg = bid_n + ask_n
        return {
            "spreadPct": round((asks[0][0] - bids[0][0]) / kozep * 100, 5),
            # -1 .. +1: pozitiv = a vetel oldalan all tobb penz a legjobb szinten
            "topImbalance": round((bid_n - ask_n) / osszeg, 4) if osszeg > 0 else 0.0,
            "wallBid": find_wall(bids, kozep, c["wallSensitivity"],
                                 c["wallMaxDistancePct"]),
            "wallAsk": find_wall(asks, kozep, c["wallSensitivity"],
                                 c["wallMaxDistancePct"]),
            "levels": len(bids),
        }

    def status(self):
        if not self.books:
            return "konyv-melyseg: nincs adat"
        return f"konyv-melyseg: {len(self.books)} par"
