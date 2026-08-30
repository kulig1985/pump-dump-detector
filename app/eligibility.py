"""Realtime kereskedhetoseg: melyik paron van ertelme egyaltalan jelezni?

A 24 oras forgalom onmagaban keves: atenged szeles spreadu, vekony konyvu
parokat, ahol egy kis, nagy tokeattetes scalp nem hajthato vegre.

Az adat egyetlen  !bookTicker  feliratkozasbol jon, ami az EGESZ piac legjobb
bid/ask arat es mennyiseget adja (a hivatalos spec szerinti b / B / a / A mezok).
Ez egy feliratkozas az osszes parra, tehat nincs erdemi tobbletterheles.

Ha egy symbol nem felel meg, mar a detektorok ELOTT kiesik.
"""
import time
import logging
from collections import deque, defaultdict

log = logging.getLogger("eligibility")

AKTIVITAS_ABLAK_SEC = 60.0


class Eligibility:
    def __init__(self, cfg):
        self.cfg = cfg
        self.book = {}                          # symbol -> (bid, bidQty, ask, askQty)
        self.trades = defaultdict(deque)        # symbol -> trade idobelyegek
        self.rejected = {}                      # symbol -> ok (a percenkenti osszesitohoz)

    # ---------------------------------------------------------------- adatgyujtes

    def on_book_ticker(self, data):
        """Egy !bookTicker uzenet feldolgozasa."""
        try:
            self.book[data["s"]] = (float(data["b"]), float(data["B"]),
                                    float(data["a"]), float(data["A"]))
        except (KeyError, TypeError, ValueError):
            pass

    def on_trade(self, trade):
        w = self.trades[trade.symbol]
        w.append(trade.ts)
        hatar = trade.ts - AKTIVITAS_ABLAK_SEC
        while w and w[0] < hatar:
            w.popleft()

    # ---------------------------------------------------------------- dontes

    def metrics(self, symbol):
        """A parra jellemzo pillanatnyi likviditasi szamok."""
        b = self.book.get(symbol)
        m = {"tradesPerMinute": len(self.trades.get(symbol, ()))}
        if b:
            bid, bid_qty, ask, ask_qty = b
            kozep = (bid + ask) / 2
            if kozep > 0:
                m["spreadPct"] = round((ask - bid) / kozep * 100, 5)
                m["topDepthUSDT"] = round(min(bid * bid_qty, ask * ask_qty), 2)
        return m

    def check(self, symbol):
        """(mehet-e, ok, mert szamok). Az ok gepi nev, hogy aggregalhato legyen."""
        c = self.cfg.detector
        m = self.metrics(symbol)

        if symbol in set(c["symbolBlacklist"]):
            return self._nem(symbol, "blacklisted", m)
        feher = set(c["symbolWhitelist"])
        if feher and symbol not in feher:
            return self._nem(symbol, "not_whitelisted", m)

        # amig nem lattuk a konyvet, nem itelunk -- de nem is engedunk at
        if "spreadPct" not in m:
            return self._nem(symbol, "no_book_data", m)

        if c["maxSpreadPct"] and m["spreadPct"] > c["maxSpreadPct"]:
            return self._nem(symbol, "spread_too_wide", m)
        if c["minTopDepthUSDT"] and m["topDepthUSDT"] < c["minTopDepthUSDT"]:
            return self._nem(symbol, "insufficient_depth", m)
        if c["minTradesPerMinute"] and m["tradesPerMinute"] < c["minTradesPerMinute"]:
            return self._nem(symbol, "low_activity", m)

        self.rejected.pop(symbol, None)
        return True, None, m

    def _nem(self, symbol, ok, m):
        self.rejected[symbol] = ok
        return False, ok, m

    # ---------------------------------------------------------------- kijelzes

    def summary(self):
        """Okonkent osszesitve, hany par esik ki -- nem soronkent."""
        if not self.rejected:
            return []
        szamlalo = defaultdict(int)
        for ok in self.rejected.values():
            szamlalo[ok] += 1
        reszek = "  ".join(f"{ok}: {n}" for ok, n in sorted(szamlalo.items(),
                                                            key=lambda x: -x[1]))
        return [f"kizarva {len(self.rejected)} par ({reszek})"]
