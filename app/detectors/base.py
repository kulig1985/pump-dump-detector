"""A detektorok kozos alakja.

Egy detektor annyit csinal, hogy trade-eket kap es jelzest ad vissza. Nem kuld
Telegramot, nem ir Mongo-ba, nem nyit poziciot -- azt a SignalService intezi.

Uj detektor hozzaadasa: uj fajl ebben a csomagban egy osztallyal, ami tudja a lenti
negy dolgot, plusz egy sor a main.py-ban. A tobbi reteget nem kell modositani.
"""
from collections import namedtuple

# buy_taker: az agresszor a vevo volt-e.
# A Binance aggTrade "m" mezoje azt mondja meg, hogy a VEVO volt-e a maker.
# m = true  -> a vevo a maker, tehat az agresszor az elado  -> buy_taker = False
# m = false -> az agresszor a vevo                          -> buy_taker = True
Trade = namedtuple("Trade", "symbol price qty ts buy_taker")


def make_signal(detector, config_key, symbol, direction, price, ts, *,
                strength, accelerating, context_mode, detail, lines, history):
    """A detektorok egysegesen ilyen dictet adnak vissza.

    strength      1.0 = pont a sajat kuszoben; ebbol jon a score mozgas-resze
    accelerating  igaz, ha a mozgas gyorsul (extra pont)
    context_mode  "momentum" vagy "reversal" -- hogyan kell olvasni az EMA-t
                  es az order bookot (lasd scoring.py)
    detail        detektor-specifikus adat, valtozatlanul Mongo-ba kerul
    lines         [(cimke, ertek), ...] -- a Telegram uzenet reszletezo blokkja,
                  igy a formazo egymas ala tudja igazitani az ertekeket
    history       [(ts, ar), ...] a market_snapshots-hoz
    """
    return {
        "detector": detector,
        "configKey": config_key,
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "timestamp": ts,
        "strength": strength,
        "accelerating": accelerating,
        "contextMode": context_mode,
        "detail": detail,
        "lines": lines,
        "history": history,
    }


class Detector:
    """Amit egy detektornak tudnia kell. Az on_trade az egyetlen kotelezo resz."""

    name = "névtelen"
    config_key = "detector"

    def on_trade(self, trade):
        """Uj trade. Visszaad egy make_signal() dictet, vagy None-t."""
        raise NotImplementedError

    def status_lines(self):
        """Opcionalis blokk az elo statusz tablahoz."""
        return []
