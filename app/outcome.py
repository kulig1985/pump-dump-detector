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

from .fmt import pad, price as fprice

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

    async def load_history(self):
        """A korabbi lemert jelzesek betoltese indulaskor.

        Enelkul az osszesites minden ujrainditasnal nullarol kezdodik, es epp
        akkor mutat mast, amikor eldontened, mukodik-e a rendszer.
        """
        try:
            kurzor = self.db.signals.find(
                {"outcome": {"$exists": True}}).sort("timestamp", -1).limit(MEMORIA)
            betoltve = 0
            for doc in reversed(await kurzor.to_list(length=MEMORIA)):
                m = {}
                for kulcs, e in (doc.get("outcome") or {}).items():
                    if not kulcs.startswith("m") or "price" not in e:
                        continue
                    valt = e.get("changePct")
                    if valt is None:            # regi alak: iranyhelyes szazalek
                        valt = e.get("pct", 0.0)
                        if doc["direction"] == "SHORT":
                            valt = -valt
                    nyero = e.get("win")
                    if nyero is None:
                        nyero = valt > 0 if doc["direction"] == "LONG" else valt < 0
                    m[int(kulcs[1:])] = {"ar": e["price"], "valt": valt, "nyero": nyero}
                if m:
                    self.jelzesek[doc["_id"]] = {
                        "ts": doc["timestamp"].timestamp(), "symbol": doc["symbol"],
                        "detector": doc["detector"], "direction": doc["direction"],
                        "price": doc["price"], "m": m}
                    betoltve += 1
            if betoltve:
                log.info("%d korabbi lemert jelzes betoltve az osszesiteshez", betoltve)
        except Exception as e:
            log.warning("a korabbi eredmenyek betoltese nem sikerult: %s", e)

    async def run(self):
        await self.load_history()
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
            valt = (ar - j["price"]) / j["price"] * 100.0     # NYERS arvaltozas
            nyero = valt > 0 if j["direction"] == "LONG" else valt < 0
            j["m"][x["perc"]] = {"ar": ar, "valt": valt, "nyero": nyero}
            await self.db.signals.update_one(
                {"_id": x["id"]},
                {"$set": {f"outcome.m{x['perc']}": {"price": ar,
                                                    "changePct": round(valt, 4),
                                                    "win": nyero}}})

    # ---------------------------------------------------------------- kijelzes

    def _percek(self):
        return sorted(self.cfg.market["outcomeMinutes"])

    def recent_lines(self, n=3):
        """Tipusonkent az utolso n jelzes: mit jelzett, milyen aron, es hol allt
        az ar 1 / 5 / 15 perccel kesobb -- konkret arral es szazalekos valtozassal.

        A szazalek a NYERS arvaltozas: felfele + , lefele - , fuggetlenul attol,
        hogy LONG vagy SHORT volt a jelzes.
        """
        out = []
        for det, cim in (("pump_dump", "PUMP/DUMP"), ("reversal", "FORDULO")):
            sajat = [j for j in self.jelzesek.values()
                     if j["detector"] == det and j["m"]]
            if not sajat:
                continue
            out.append(f"UTOLSO {min(n, len(sajat))} {cim} JELZES")
            for j in sorted(sajat, key=lambda x: -x["ts"])[:n]:
                ma = time.strftime('%Y-%m-%d', time.gmtime())
                nap = time.strftime('%Y-%m-%d', time.gmtime(j["ts"]))
                mikor = (time.strftime('%H:%M', time.gmtime(j["ts"])) if nap == ma
                         else time.strftime('%m-%d %H:%M', time.gmtime(j["ts"])))
                out.append(f"{pad(j['symbol'], 12)}{pad(j['direction'], 6)}"
                           f"{pad(mikor, 12)}jelzes aron {fprice(j['price'])}")
                for perc in self._percek():
                    e = j["m"].get(perc)
                    if e:
                        out.append(f"   +{perc:>2} perc   ar {pad(fprice(e['ar']), 12)}"
                                   f"{e['valt']:>+7.2f}%")
                    else:
                        out.append(f"   +{perc:>2} perc   meg nincs lemerve")
            out.append("")
        return out[:-1] if out else []

    def summary_lines(self):
        """Kategoriankent: hany jelzes lett volna NYERO es hany BUKO.

        Nyero = LONG jelzes utan feljebb, SHORT jelzes utan lejjebb allt az ar.
        """
        detektorok = [d for d in ("pump_dump", "reversal")
                      if any(j["detector"] == d and j["m"]
                             for j in self.jelzesek.values())]
        if not detektorok:
            return []
        sorok = [f"{'tipus':<7}{'ido':>5}{'nyero':>8}{'buko':>7}{'arany':>8}"]
        for det in detektorok:
            tipus = "pump" if det == "pump_dump" else "rev"
            sajat = [j for j in self.jelzesek.values()
                     if j["detector"] == det and j["m"]]
            for perc in self._percek():
                merve = [j["m"][perc] for j in sajat if perc in j["m"]]
                if not merve:
                    continue
                nyero = sum(1 for e in merve if e["nyero"])
                sorok.append(f"{tipus:<7}{'+' + str(perc) + 'p':>5}{nyero:>8}"
                             f"{len(merve) - nyero:>7}"
                             f"{nyero / len(merve) * 100:>7.0f}%")
        return sorok

    def status_lines(self):
        """A log STATUS blokkjahoz. Ures, amig nincs lemert jelzes."""
        osszes = self.summary_lines()
        if not osszes:
            return []
        return (["NYERO / BUKO JELZESEK"] + [f"  {x}" for x in osszes]
                + [f"  {x}" for x in self.recent_lines()])
