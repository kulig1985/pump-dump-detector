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
import statistics
from collections import deque, defaultdict

log = logging.getLogger("eligibility")

# gepi kulcs -> emberi szoveg. A kulcs megy a Mongo-ba (hogy aggregalhato legyen),
# a szoveg a logba.
OKOK = {
    "blacklisted":        "kezzel kizarva",
    "not_whitelisted":    "nincs a figyelt listan",
    "no_book_data":       "meg nem lattuk a konyvet",
    "spread_too_wide":    "tul szeles a spread",
    "insufficient_depth": "keves penz all a legjobb szinten",
    "low_activity":       "tul keves kotes percenkent",
}


def szoveg(ok):
    return OKOK.get(ok, ok)

AKTIVITAS_ABLAK_SEC = 60.0
DEPTH_MINTA = 40            # ennyi friss megfigyeles medianja adja a melyseget


class Eligibility:
    def __init__(self, cfg):
        self.cfg = cfg
        self.book = {}                          # symbol -> (bid, bidQty, ask, askQty)
        self.depth = defaultdict(deque)         # symbol -> friss melyseg-megfigyelesek
        self.book_messages = 0                  # kaptunk-e egyaltalan konyv-adatot
        self.trades = defaultdict(deque)        # symbol -> trade idobelyegek
        self.rejected = {}                      # symbol -> ok (a percenkenti osszesitohoz)

    # ---------------------------------------------------------------- adatgyujtes

    def on_book_ticker(self, data):
        """Egy !bookTicker uzenet feldolgozasa."""
        try:
            bid, bid_qty = float(data["b"]), float(data["B"])
            ask, ask_qty = float(data["a"]), float(data["A"])
            self.book[data["s"]] = (bid, bid_qty, ask, ask_qty)
            self.book_messages += 1
            # A legjobb szinten allo mennyiseg masodpercenkent kiurul es ujratoltodik.
            # A pillanatnyi ertek ezert ide-oda ugral (11 USDT es 30,000 kozott is) --
            # a friss megfigyelesek medianjat hasznaljuk helyette.
            w = self.depth[data["s"]]
            w.append(min(bid * bid_qty, ask * ask_qty))
            if len(w) > DEPTH_MINTA:
                w.popleft()
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
                minta = self.depth.get(symbol)
                m["topDepthUSDT"] = round(statistics.median(minta), 2) if minta else 0.0
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

        if "spreadPct" not in m:
            # Ha SEMMILYEN konyv-adat nem erkezik, az rendszerszintu baj (rossz WS
            # utvonal), nem a paron mulik. Ilyenkor nem nemitjuk el az egesz
            # rendszert: atengedunk, es a STATUS sor hangosan szol rola.
            if self.book_messages == 0:
                return True, None, m
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

    def book_status(self):
        """Rovid allapot a konyv-adatrol a STATUS sorhoz."""
        if self.book_messages == 0:
            return "KONYV-ADAT NEM ERKEZIK -- a spread/melyseg szures kikapcsolva!"
        return f"konyv: {len(self.book)} par"

    def summary(self):
        """Okonkent osszesitve, hany par esik ki -- nem soronkent."""
        if not self.rejected:
            return []
        szamlalo = defaultdict(int)
        for ok in self.rejected.values():
            szamlalo[ok] += 1
        reszek = ", ".join(f"{szoveg(ok)}: {n}" for ok, n in sorted(szamlalo.items(),
                                                                    key=lambda x: -x[1]))
        return [f"kizarva {len(self.rejected)}: {reszek}"]

    def distribution(self, symbols):
        """Spread es melyseg eloszlas a FIGYELT parokra, a kuszobokkel egyutt.

        Enelkul a kuszoboket vaktaban kellene allitgatni: ebbol egy pillantassal
        latszik, hol huznak, es hany par esik alattuk.
        """
        c = self.cfg.detector
        spreadek, melysegek = [], []
        for sym in symbols:
            m = self.metrics(sym)
            if "spreadPct" in m:
                spreadek.append(m["spreadPct"])
                melysegek.append(m["topDepthUSDT"])
        if not spreadek:
            return []

        def p(ertekek, szazalek):
            rendezett = sorted(ertekek)
            i = min(len(rendezett) - 1, int(len(rendezett) * szazalek / 100))
            return rendezett[i]

        alatta = sum(1 for x in melysegek if x < c["minTopDepthUSDT"])
        felette = sum(1 for x in spreadek if x > c["maxSpreadPct"])
        return [
            f"melyseg  p10 {p(melysegek, 10):>10,.0f}  p50 {p(melysegek, 50):>10,.0f}  "
            f"p90 {p(melysegek, 90):>10,.0f} USDT   kuszob {c['minTopDepthUSDT']:,.0f}"
            f"  -> {alatta} par alatta",
            f"spread   p10 {p(spreadek, 10):>10.3f}%  p50 {p(spreadek, 50):>10.3f}%  "
            f"p90 {p(spreadek, 90):>10.3f}%   kuszob {c['maxSpreadPct']:.3f}%"
            f"  -> {felette} par felette",
        ]
