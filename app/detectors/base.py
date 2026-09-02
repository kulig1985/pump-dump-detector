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
                reasons, metrics, history, setup=None):
    """A detektorok egysegesen ilyen dictet adnak vissza -- ez meg CANDIDATE.

    reasons       emberi allitasok arrol, miert erdekes ez a mozgas
    metrics       a hozzajuk tartozo mert szamok (Mongo-ba is ez kerul)
    history       [(ts, ar), ...] a market_snapshots-hoz
    setup         a belepo tipusa, pl. LONG_CONTINUATION / SHORT_REVERSAL

    Nincs score es nincs kereskedelmi terv: a detektor annyit allit, hogy egy
    belepo setup megerositodott, es megmondja, mibol gondolja.
    """
    return {
        "detector": detector,
        "configKey": config_key,
        "setup": setup,
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "timestamp": ts,
        "reasons": list(reasons),
        "metrics": dict(metrics),
        "history": history,
    }


class Detector:
    """Amit egy detektornak tudnia kell. Az on_trade az egyetlen kotelezo resz."""

    name = "névtelen"
    config_key = "detector"

    def on_trade(self, trade):
        """Uj trade. Visszaad egy make_signal() CANDIDATE dictet, vagy None-t.

        A detektor nem kuld Telegramot, nem ir Mongo-ba es nem nyit poziciot --
        azt a SignalService intezi.
        """
        raise NotImplementedError

    def status_lines(self):
        """Opcionalis blokk az elo statusz tablahoz."""
        return []
