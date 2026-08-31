"""Mi lett a jelzesbol? -- a jelzes UTANI ar feljegyzese.

Ez NEM backteszt es NEM jelzo: semmit nem kapuz, nem nyit poziciot, nem valtoztat
a detektorokon. Annyit csinal, hogy a mar kikuldott jelzes utan par perccel
megnezi, hol all az ar, es beirja a signal dokumentumba.

Ket kerdesre valaszol:

  1. AZ UTOLSO NEHANY JELZESSEL mi tortent? (jelzesenkent egy sor)
  2. OSSZESSEGEBEN mennyi a talalati arany? LONG utan felfele ment-e az ar,
     SHORT utan lefele?

Az arat a mar futo aggTrade folyambol vesszuk (utolso ar paronkent), tehat nincs
egyetlen extra halozati keres sem.
"""
import time
import asyncio
import logging

from .fmt import pad

log = logging.getLogger("outcome")

ELLENORZES_SEC = 5      # ilyen suru a lejart merespontok kiertekelese
MEMORIA = 50            # ennyi legutobbi jelzest tartunk meg a kijelzeshez


class OutcomeTracker:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.last_price = {}            # symbol -> utolso latott ar
        self.varolista = []             # meg le nem jart merespontok
        self.jelzesek = {}              # signal id -> {..., "m": {perc: valtozas%}}

    # ---------------------------------------------------------------- adatgyujtes

    def on_trade(self, trade):
        self.last_price[trade.symbol] = trade.price

    def track(self, signal_id, symbol, detector, direction, price):
        """Egy kikuldott jelzes meresre jelolese."""
        if not signal_id or not price:
            return
        most = time.time()
        self.jelzesek[signal_id] = {"ts": most, "symbol": symbol, "detector": detector,
                                    "direction": direction, "price": price, "m": {}}
        while len(self.jelzesek) > MEMORIA:
            self.jelzesek.pop(next(iter(self.jelzesek)))
        for perc in self.cfg.market["outcomeMinutes"]:
            self.varolista.append({"id": signal_id, "perc": perc,
                                   "esedekes": most + perc * 60})

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
            j = self.jelzesek.get(x["id"])
            if not j:
                continue
            ar = self.last_price.get(j["symbol"])
            if not ar:
                continue            # nem erkezik kotes errol a parrol -- nem talalunk ki adatot
            valt = (ar - j["price"]) / j["price"] * 100.0
            if j["direction"] == "SHORT":
                valt = -valt        # elojelhelyesen: pozitiv = a jelzes iranyaba ment
            j["m"][x["perc"]] = valt
            await self.db.signals.update_one(
                {"_id": x["id"]},
                {"$set": {f"outcome.m{x['perc']}": {"price": ar, "pct": round(valt, 4)}}})

    # ---------------------------------------------------------------- kijelzes

    def _percek(self):
        return sorted(self.cfg.market["outcomeMinutes"])

    def recent_lines(self, n=5):
        """Az utolso n jelzes: mi tortent VOLNA, ha beszallsz. Legfrissebb elol."""
        kesz = [j for j in self.jelzesek.values() if j["m"]]
        sorok = []
        for j in sorted(kesz, key=lambda x: -x["ts"])[:n]:
            reszek = []
            for perc in self._percek():
                v = j["m"].get(perc)
                reszek.append(f"+{perc}p {v:+6.2f}%" if v is not None else f"+{perc}p    ...")
            sorok.append(f"{time.strftime('%H:%M', time.gmtime(j['ts']))} "
                         f"{pad(j['symbol'], 12)} {pad(j['direction'], 5)} "
                         + "  ".join(reszek))
        return sorok

    def summary_lines(self):
        """Talalati arany: LONG utan felfele ment-e, SHORT utan lefele.

        Detektoronkent es merespontonkent: hany szazalekban ment a jelzes iranyaba.
        """
        percek = self._percek()
        detektorok = sorted({j["detector"] for j in self.jelzesek.values() if j["m"]})
        sorok = []
        for det in detektorok:
            reszek = []
            for perc in percek:
                ertekek = [j["m"][perc] for j in self.jelzesek.values()
                           if j["detector"] == det and perc in j["m"]]
                if not ertekek:
                    continue
                jo = sum(1 for v in ertekek if v > 0)
                reszek.append(f"+{perc}p {jo / len(ertekek) * 100:>3.0f}% ({jo}/{len(ertekek)})")
            if reszek:
                sorok.append(f"{pad(det, 10)} " + "   ".join(reszek))
        return sorok

    def status_lines(self):
        """A log STATUS blokkjahoz. Ures, amig nincs lemert jelzes."""
        osszes = self.summary_lines()
        if not osszes:
            return []
        return (["TALALATI ARANY (a jelzes iranyaba ment-e az ar)"]
                + [f"  {x}" for x in osszes]
                + ["UTOLSO JELZESEK"]
                + [f"  {x}" for x in self.recent_lines()])
