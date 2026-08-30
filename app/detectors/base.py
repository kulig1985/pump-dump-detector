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
                reasons, metrics, history, move_pct=None,
                stop_anchor=None, target_anchor=None):
    """A detektorok egysegesen ilyen dictet adnak vissza -- ez meg CANDIDATE.

    reasons       emberi allitasok arrol, miert erdekes ez a mozgas
    metrics       a hozzajuk tartozo mert szamok (Mongo-ba is ez kerul)
    move_pct      a latott armozgas nagysaga -- ehhez merjuk a spreadet
    stop_anchor   az az arszint, ami alatt/felett a tezis ervenyet veszti
    target_anchor ameddig a mozgas tarthat
                  A kettobol az app/plan.py szamol belepot / celt / stopot
                  es hozam-kockazat aranyt, minden detektornal egyformán.
    history       [(ts, ar), ...] a market_snapshots-hoz

    Nincs score: a dontest a kapuk hozzak, es minden allitas mogott konkret
    mert szam all.
    """
    return {
        "detector": detector,
        "configKey": config_key,
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "timestamp": ts,
        "movePct": move_pct,
        "reasons": list(reasons),
        "metrics": dict(metrics),
        "stopAnchor": stop_anchor,
        "targetAnchor": target_anchor,
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
