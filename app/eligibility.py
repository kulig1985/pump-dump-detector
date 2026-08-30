"""Realtime kereskedhetoseg: melyik paron van ertelme egyaltalan jelezni?

Egyetlen szempont: ne legyen tul szeles a spread. Plusz a kezi white/blacklist.

Az adat egyetlen  !bookTicker  feliratkozasbol jon, ami az EGESZ piac legjobb
bid/ask arat es mennyiseget adja (a hivatalos spec szerinti b / B / a / A mezok).
Ez egy feliratkozas az osszes parra, tehat nincs erdemi tobbletterheles.
"""
import logging
from collections import defaultdict

log = logging.getLogger("eligibility")

# gepi kulcs -> emberi szoveg. A kulcs megy a Mongo-ba (hogy aggregalhato legyen),
# a szoveg a logba.
OKOK = {
    "blacklisted":     "kezzel kizarva",
    "not_whitelisted": "nincs a figyelt listan",
    "no_book_data":    "meg nem lattuk a konyvet",
    "spread_too_wide": "tul szeles a spread",
}


def szoveg(ok):
    return OKOK.get(ok, ok)


class Eligibility:
    def __init__(self, cfg):
        self.cfg = cfg
        self.book = {}              # symbol -> (bid, bidQty, ask, askQty)
        self.book_messages = 0      # kaptunk-e egyaltalan konyv-adatot
        self.rejected = {}          # symbol -> ok (a percenkenti osszesitohoz)

    # ---------------------------------------------------------------- adatgyujtes

    def on_book_ticker(self, data):
        """Egy !bookTicker uzenet feldolgozasa."""
        try:
            bid, bid_qty = float(data["b"]), float(data["B"])
            ask, ask_qty = float(data["a"]), float(data["A"])
            self.book[data["s"]] = (bid, bid_qty, ask, ask_qty)
            self.book_messages += 1
        except (KeyError, TypeError, ValueError):
            pass

    # ---------------------------------------------------------------- dontes

    def metrics(self, symbol):
        """A parra jellemzo pillanatnyi spread."""
        b = self.book.get(symbol)
        m = {}
        if b:
            bid, _, ask, _ = b
            kozep = (bid + ask) / 2
            if kozep > 0:
                m["spreadPct"] = round((ask - bid) / kozep * 100, 5)
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

        self.rejected.pop(symbol, None)
        return True, None, m

    def _nem(self, symbol, ok, m):
        self.rejected[symbol] = ok
        return False, ok, m

    # ---------------------------------------------------------------- kijelzes

    def book_status(self):
        """Rovid allapot a konyv-adatrol a STATUS sorhoz."""
        if self.book_messages == 0:
            return "KONYV-ADAT NEM ERKEZIK -- a spread szures kikapcsolva!"
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
        """Spread eloszlas a FIGYELT parokra, a kuszobbel egyutt.

        Enelkul a kuszobot vaktaban kellene allitgatni: ebbol egy pillantassal
        latszik, hol huz, es hany par esik folotte.
        """
        c = self.cfg.detector
        spreadek = [m["spreadPct"] for m in (self.metrics(s) for s in symbols)
                    if "spreadPct" in m]
        if not spreadek:
            return []

        def p(ertekek, szazalek):
            rendezett = sorted(ertekek)
            i = min(len(rendezett) - 1, int(len(rendezett) * szazalek / 100))
            return rendezett[i]

        felette = sum(1 for x in spreadek if x > c["maxSpreadPct"])
        return [
            f"spread   p10 {p(spreadek, 10):>10.3f}%  p50 {p(spreadek, 50):>10.3f}%  "
            f"p90 {p(spreadek, 90):>10.3f}%   kuszob {c['maxSpreadPct']:.3f}%"
            f"  -> {felette} par felette",
        ]
