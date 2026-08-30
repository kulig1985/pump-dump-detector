"""Mi lett a jelzesbol? -- a jelzes UTANI ar feljegyzese.

Ez NEM backteszt es NEM jelzo: semmit nem kapuz, nem nyit poziciot, nem valtoztat
a detektorokon. Annyit csinal, hogy a mar kikuldott jelzes utan par perccel
megnezi, hol all az ar, es beirja a signal dokumentumba.

Enelkul senki -- sem te, sem en -- nem tudja megmondani, hogy egy fordulo vagy egy
dump tartos-e. Ezzel egy het mulva ez a mondat all elo:

    reversal   63 jelzes | +5 perc: 41% jo iranyba, median -0.05%

Az arat a mar futo aggTrade folyambol vesszuk (utolso ar paronkent), tehat nincs
egyetlen extra halozati keres sem.
"""
import time
import asyncio
import logging
import statistics
from collections import defaultdict

log = logging.getLogger("outcome")

ELLENORZES_SEC = 5      # ilyen suru a lejart merespontok kiertekelese


class OutcomeTracker:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.last_price = {}            # symbol -> utolso latott ar
        self.varolista = []             # meg le nem jart merespontok
        self.eredmenyek = defaultdict(lambda: defaultdict(list))  # detektor -> perc -> [%]

    # ---------------------------------------------------------------- adatgyujtes

    def on_trade(self, trade):
        self.last_price[trade.symbol] = trade.price

    def track(self, signal_id, symbol, detector, direction, price):
        """Egy kikuldott jelzes meresre jelolese."""
        if not signal_id or not price:
            return
        most = time.time()
        for perc in self.cfg.market["outcomeMinutes"]:
            self.varolista.append({
                "id": signal_id, "symbol": symbol, "detector": detector,
                "direction": direction, "price": price, "perc": perc,
                "esedekes": most + perc * 60,
            })

    # ---------------------------------------------------------------- kiertekeles

    async def run(self):
        while True:
            await asyncio.sleep(ELLENORZES_SEC)
            try:
                await self._kiertekel()
            except Exception as e:
                log.warning("eredmenymeres hiba: %s", e)

    async def _kiertekel(self):
        most = time.time()
        lejart = [x for x in self.varolista if x["esedekes"] <= most]
        if not lejart:
            return
        self.varolista = [x for x in self.varolista if x["esedekes"] > most]

        for x in lejart:
            ar = self.last_price.get(x["symbol"])
            if not ar:
                continue            # nem erkezik kotes errol a parrol -- nem talalunk ki adatot
            valt = (ar - x["price"]) / x["price"] * 100.0
            if x["direction"] == "SHORT":
                valt = -valt        # elojelhelyesen: pozitiv = a jelzes iranyaba ment
            self.eredmenyek[x["detector"]][x["perc"]].append(valt)
            await self.db.signals.update_one(
                {"_id": x["id"]},
                {"$set": {f"outcome.m{x['perc']}": {"price": ar, "pct": round(valt, 4)}}})

    # ---------------------------------------------------------------- kijelzes

    def status_lines(self):
        """Osszegzes a STATUS blokkhoz. Ures, amig nincs lemert jelzes."""
        sorok = []
        for detektor, per_perc in sorted(self.eredmenyek.items()):
            for perc, ertekek in sorted(per_perc.items()):
                if not ertekek:
                    continue
                jo = sum(1 for v in ertekek if v > 0)
                sorok.append(
                    f"EREDMENY  {detektor:<10} +{perc:>2} perc: {len(ertekek):>3} merve, "
                    f"{jo / len(ertekek) * 100:>3.0f}% jo iranyba, "
                    f"median {statistics.median(ertekek):+.2f}%, "
                    f"legjobb {max(ertekek):+.2f}%, legrosszabb {min(ertekek):+.2f}%")
        return sorok
