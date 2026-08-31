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

    def _fejlec(self, bal):
        """Oszlopfejlec: a bal oldali resz + egy oszlop merespontonkent."""
        return bal + "".join(f"{'+' + str(p) + 'p':>8}" for p in self._percek())

    def recent_lines(self, n=3):
        """TIPUSONKENT az utolso n jelzes tablazatban: mit jelzett mikor, es merre
        indult el az ar.

        Tipusonkent kulon valogatunk, kulonben egy sokat jelzo detektor kiszoritana
        a masikat a listarol. A tablazat idorendben, a legfrissebb elol.
        """
        kesz = []
        for det in {j["detector"] for j in self.jelzesek.values() if j["m"]}:
            sajat = [j for j in self.jelzesek.values()
                     if j["detector"] == det and j["m"]]
            kesz += sorted(sajat, key=lambda x: -x["ts"])[:n]
        if not kesz:
            return []
        sorok = [self._fejlec(f"{'ido':<6}{'par':<13}{'tipus':<6}{'irany':<6}")]
        for j in sorted(kesz, key=lambda x: -x["ts"]):
            tipus = "pump" if j["detector"] == "pump_dump" else "rev"
            sor = (f"{time.strftime('%H:%M', time.gmtime(j['ts'])):<6}"
                   f"{pad(j['symbol'], 13)}{pad(tipus, 6)}{pad(j['direction'], 6)}")
            for perc in self._percek():
                v = j["m"].get(perc)
                sor += f"{v:>+7.2f}%" if v is not None else f"{'...':>8}"
            sorok.append(sor)
        return sorok

    def summary_lines(self):
        """Osszesites TABLAZATBAN: soronkent egy tipus + egy merespont.

        Iranyhelyesen: pozitiv = LONG utan felfele ment az ar, vagy SHORT utan
        lefele. A talalat az, hogy a jelzesek hany szazaleka ilyen.
        """
        detektorok = sorted({j["detector"] for j in self.jelzesek.values() if j["m"]})
        if not detektorok:
            return []
        sorok = [f"{'tipus':<6}{'ido':>5}{'db':>5}{'atlag':>9}{'talalat':>9}"]
        for det in detektorok:
            tipus = "pump" if det == "pump_dump" else "rev"
            sajat = [j for j in self.jelzesek.values() if j["detector"] == det and j["m"]]
            for perc in self._percek():
                ertekek = [j["m"][perc] for j in sajat if perc in j["m"]]
                sor = f"{tipus:<6}{'+' + str(perc) + 'p':>5}{len(ertekek):>5}"
                if ertekek:
                    jo = sum(1 for v in ertekek if v > 0)
                    sor += (f"{sum(ertekek) / len(ertekek):>+8.2f}%"
                            f"{jo / len(ertekek) * 100:>8.0f}%")
                else:
                    sor += f"{'...':>9}{'...':>9}"
                sorok.append(sor)
        return sorok

    def per_symbol_lines(self, limit=10):
        """Paronkent: melyik paron mit hoztak a jelzesek, tipusonkent kulon.

        A tipusonkenti atlag elfedi, hogy egy-ket par viszi az egeszet -- ebbol
        latszik, melyik paron mukodik es melyiken nem.
        """
        csoport = {}
        for j in self.jelzesek.values():
            if not j["m"]:
                continue
            kulcs = (j["symbol"], j["detector"])
            csoport.setdefault(kulcs, []).append(j)
        if not csoport:
            return []
        sorok = [self._fejlec(f"{'par':<13}{'tipus':<6}{'db':>4}  ")]
        rendezett = sorted(csoport.items(), key=lambda x: (-len(x[1]), x[0][0]))
        for (symbol, det), jelzesek in rendezett[:limit]:
            tipus = "pump" if det == "pump_dump" else "rev"
            sor = f"{pad(symbol, 13)}{pad(tipus, 6)}{len(jelzesek):>4}  "
            for perc in self._percek():
                ertekek = [j["m"][perc] for j in jelzesek if perc in j["m"]]
                sor += (f"{sum(ertekek) / len(ertekek):>+7.2f}%" if ertekek
                        else f"{'...':>8}")
            sorok.append(sor)
        if len(rendezett) > limit:
            sorok.append(f"... es meg {len(rendezett) - limit} par")
        return sorok

    def status_lines(self):
        """A log STATUS blokkjahoz. Ures, amig nincs lemert jelzes."""
        osszes = self.summary_lines()
        if not osszes:
            return []
        return (["OSSZESITES  (+ = a jelzes iranyaba ment az ar)"]
                + [f"  {x}" for x in osszes]
                + ["PARONKENT"]
                + [f"  {x}" for x in self.per_symbol_lines()]
                + ["UTOLSO JELZESEK"]
                + [f"  {x}" for x in self.recent_lines()])
