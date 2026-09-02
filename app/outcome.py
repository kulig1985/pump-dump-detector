"""Mi lett a jelzesbol? -- a jelzes utani ARUT merese.

A poziciot KEZZEL nyitod, es nem zarod automatikusan egy -0.3%-os stopnal. Egy
trade lehet elobb minuszban, majd 10-20 perccel kesobb erdemi profitban. Ezert a
FO eredmeny idoalapu: mennyit mozdult az ar a jelzes iranyaba 1 / 3 / 5 / 10 /
15 / 20 perccel kesobb.

Iranyhelyes hozam (pozitiv = a jelzes iranyaba):

    LONG :  returnPct = (ar - signalPrice) / signalPrice * 100
    SHORT:  returnPct = (signalPrice - ar) / signalPrice * 100

Jelzesenkent elmentve:

    return1m .. return20m   az adott merespontban mert hozam
    mfePct / maePct         a legjobb es a legrosszabb pont
    timeToMfe / timeToMae   mikor erte el
    maxPrice / minPrice     a nyers szelsoertekek
    tp / sl                 DIAGNOSZTIKA: melyik szintet mikor erte el eloszor

A TP/SL first-touch megmarad, de NEM ez donti el, hogy egy jelzes "jo" volt-e --
kulon, egyertelmuen jelolt statisztikakent jelenik meg.
"""
import time
import asyncio
import logging
import statistics

from .fmt import pad, price as fprice

log = logging.getLogger("outcome")

FLUSH_SEC = 15.0        # ilyen suru a Mongo-ba iras
MEMORIA = 500           # ennyi legutobbi jelzest tartunk a kijelzeshez


def _kulcs(szint):
    return f"{szint:g}"


def _perc_nev(sec):
    """60 -> "1m", 1200 -> "20m" -- a mezonevekhez es a fejlechez egyarant."""
    return f"{int(round(float(sec) / 60))}m"


def _median(ertekek):
    return statistics.median(ertekek) if ertekek else None


class Tracker:
    """Egy jelzes elo merese."""

    __slots__ = ("id", "symbol", "setup", "direction", "signal_price", "t0",
                 "deadline", "mfe", "mae", "t_mfe", "t_mae", "max_price",
                 "min_price", "tp", "sl", "marks", "final", "done", "dirty",
                 "current_run")

    def __init__(self, sid, symbol, setup, direction, signal_price, t0, deadline,
                 tp_levels, sl_levels, mark_sec=(), current_run=True):
        self.id, self.symbol, self.setup = sid, symbol, setup
        self.direction = direction
        # Ma a meres a detektor altal adott jelzes-arbol indul. Kesobb ide johet
        # egy actualEntryPrice is (tenyleges belepo) -- az adatmodell keszen all ra.
        self.signal_price = signal_price
        self.t0 = t0
        self.deadline = deadline
        self.mfe = self.mae = 0.0
        self.t_mfe = self.t_mae = 0.0
        self.max_price = self.min_price = signal_price
        self.tp = {_kulcs(x): None for x in tp_levels}
        self.sl = {_kulcs(x): None for x in sl_levels}
        # merespontonkent: {"price": ..., "returnPct": ...}
        self.marks = {_kulcs(x): None for x in mark_sec}
        self.final = 0.0
        self.done = False
        self.dirty = True
        self.current_run = current_run      # ebben a futasban keletkezett-e

    # ---------------------------------------------------------------- meres

    def hozam(self, price):
        """Iranyhelyes hozam szazalekban: pozitiv = a jelzes iranyaba ment."""
        if self.signal_price <= 0:
            return 0.0
        r = (price - self.signal_price) / self.signal_price * 100.0
        return r if self.direction == "LONG" else -r

    def on_price(self, price, ts):
        r = self.hozam(price)
        eltelt = round(ts - self.t0, 1)
        self.final = round(r, 4)
        self.max_price = max(self.max_price, price)
        self.min_price = min(self.min_price, price)
        if r > self.mfe:
            self.mfe, self.t_mfe = r, eltelt
        if r < self.mae:
            self.mae, self.t_mae = r, eltelt
        for kulcs, mikor in self.tp.items():
            if mikor is None and r >= float(kulcs):
                self.tp[kulcs] = eltelt
        for kulcs, mikor in self.sl.items():
            if mikor is None and r <= -float(kulcs):
                self.sl[kulcs] = eltelt
        for kulcs, ertek in self.marks.items():
            if ertek is None and ts - self.t0 >= float(kulcs):
                self.marks[kulcs] = {"price": price, "returnPct": round(r, 4)}
        self.dirty = True

    def hozam_at(self, mark_sec):
        """A merespontban mert hozam, vagy None ha meg nincs meg."""
        e = self.marks.get(_kulcs(mark_sec))
        return e["returnPct"] if e else None

    def doc(self):
        d = {
            "signalPrice": self.signal_price, "setup": self.setup,
            "direction": self.direction,
            "mfePct": round(self.mfe, 4), "maePct": round(self.mae, 4),
            "timeToMfeSec": self.t_mfe, "timeToMaeSec": self.t_mae,
            "maxPrice": self.max_price, "minPrice": self.min_price,
            # DIAGNOSZTIKA, nem ez donti el, hogy jo volt-e a jelzes
            "tpFirstTouch": dict(self.tp), "slFirstTouch": dict(self.sl),
            "marks": dict(self.marks),
            "finalPct": self.final, "done": self.done,
        }
        # return1m .. return20m -- kereshetoen, kulon mezokent
        for kulcs, ertek in self.marks.items():
            d[f"return{_perc_nev(kulcs)}"] = ertek["returnPct"] if ertek else None
        return d

    def first_touch(self, tp, sl):
        """DIAGNOSZTIKA: a megadott TP/SL parbol melyiket erte el elobb."""
        t = self.tp.get(_kulcs(tp))
        s = self.sl.get(_kulcs(sl))
        if t is not None and (s is None or t < s):
            return "TP"
        if s is not None:
            return "SL"
        return "egyiket sem"


class OutcomeTracker:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.aktiv = {}         # symbol -> [Tracker]
        self.keszek = []        # lezart Trackerek (a kijelzeshez)

    # ---------------------------------------------------------------- adatgyujtes

    def on_trade(self, trade):
        for t in self.aktiv.get(trade.symbol, ()):
            t.on_price(trade.price, time.time())

    def track(self, signal_id, symbol, setup, direction, price):
        if not signal_id or not price:
            return
        c = self.cfg.market
        most = time.time()
        t = Tracker(signal_id, symbol, setup, direction, price, most,
                    most + c["outcomeTrackSec"], c["tpLevels"], c["slLevels"],
                    c["outcomeMarkSec"], current_run=True)
        self.aktiv.setdefault(symbol, []).append(t)

    # ---------------------------------------------------------------- kiertekeles

    async def run(self):
        await self.load_history()
        while True:
            await asyncio.sleep(FLUSH_SEC)
            try:
                await self._flush()
            except Exception as e:
                log.warning("eredmenymeres hiba: %s", e)

    async def _flush(self):
        most = time.time()
        for symbol, lista in list(self.aktiv.items()):
            marad = []
            for t in lista:
                if most >= t.deadline:
                    t.done = True
                if t.dirty:
                    t.dirty = False
                    await self.db.signals.update_one({"_id": t.id},
                                                     {"$set": {"outcome": t.doc()}})
                if t.done:
                    self._lezar(t)
                else:
                    marad.append(t)
            if marad:
                self.aktiv[symbol] = marad
            else:
                self.aktiv.pop(symbol, None)

    def _lezar(self, t):
        self.keszek.append(t)
        while len(self.keszek) > MEMORIA:
            self.keszek.pop(0)

    async def load_history(self):
        """Korabbi FUTASOK lemert jelzesei -- kulon jelolve, hogy ne keveredjenek."""
        try:
            kurzor = self.db.signals.find(
                {"outcome.done": True}).sort("timestamp", -1).limit(MEMORIA)
            betoltve = 0
            for doc in reversed(await kurzor.to_list(length=MEMORIA)):
                o = doc["outcome"]
                t = Tracker(doc["_id"], doc["symbol"],
                            o.get("setup") or doc.get("setup") or doc["detector"],
                            o.get("direction") or doc["direction"],
                            o.get("signalPrice") or doc["price"],
                            doc["timestamp"].timestamp(), 0, [], [],
                            current_run=False)
                t.marks = o.get("marks", {})
                t.mfe, t.mae = o.get("mfePct", 0.0), o.get("maePct", 0.0)
                t.t_mfe, t.t_mae = o.get("timeToMfeSec", 0), o.get("timeToMaeSec", 0)
                t.tp = o.get("tpFirstTouch", {})
                t.sl = o.get("slFirstTouch", {})
                t.final, t.done = o.get("finalPct", 0.0), True
                self.keszek.append(t)
                betoltve += 1
            if betoltve:
                log.info("%d korabbi futasbol szarmazo lemert jelzes betoltve",
                         betoltve)
        except Exception as e:
            log.warning("a korabbi eredmenyek betoltese nem sikerult: %s", e)

    # ---------------------------------------------------------------- kijelzes

    def _mind(self, current_run_only=False):
        osszes = self.keszek + [t for lista in self.aktiv.values() for t in lista]
        return [t for t in osszes if t.current_run] if current_run_only else osszes

    def _markok(self):
        return sorted(float(x) for x in self.cfg.market["outcomeMarkSec"])

    def return_lines(self, current_run_only=True):
        """A FO tabla: iranyonkent az atlagos hozam es a pozitiv arany horizontonkent."""
        mind = [t for t in self._mind(current_run_only) if t.marks]
        if not mind:
            return []
        markok = self._markok()
        fej = f"{'irany':<8}{'db':>4}" + "".join(f"{_perc_nev(x):>9}" for x in markok)
        sorok = ["SIGNAL UTANI ARMOZGAS  (atlag, iranyhelyesen)", fej]
        aranyok = []
        for irany in ("LONG", "SHORT"):
            cs = [t for t in mind if t.direction == irany]
            if not cs:
                continue
            sor = f"{irany:<8}{len(cs):>4}"
            arany_sor = f"{irany:<8}    "
            van_adat = False
            for mark in markok:
                ertekek = [r for r in (t.hozam_at(mark) for t in cs) if r is not None]
                if ertekek:
                    van_adat = True
                    sor += f"{statistics.mean(ertekek):>+8.2f}%"
                    pozitiv = sum(1 for r in ertekek if r > 0) / len(ertekek) * 100
                    arany_sor += f"{pozitiv:>8.0f}%"
                else:
                    sor += f"{'...':>9}"
                    arany_sor += f"{'...':>9}"
            if van_adat:
                sorok.append(sor)
                aranyok.append(arany_sor)
        if len(sorok) <= 2:
            return []
        sorok.append("")
        sorok.append("POZITIV ARANY  (returnPct > 0)")
        sorok.append(fej.replace("db", "  "))
        sorok += aranyok
        return sorok

    def excursion_lines(self, current_run_only=True):
        """MFE / MAE atlag ES median -- a median azert, hogy egy-ket extrem
        pump/dump ne torzitsa el a kepet."""
        mind = self._mind(current_run_only)
        if not mind:
            return []
        sorok = ["MFE / MAE",
                 f"{'irany':<8}{'db':>4}{'atlag MFE':>11}{'med MFE':>10}"
                 f"{'atlag MAE':>11}{'med MAE':>10}{'med t-MFE':>11}{'med t-MAE':>11}"]
        van = False
        for irany in ("LONG", "SHORT"):
            cs = [t for t in mind if t.direction == irany]
            if not cs:
                continue
            van = True
            sorok.append(
                f"{irany:<8}{len(cs):>4}"
                f"{statistics.mean(t.mfe for t in cs):>+10.2f}%"
                f"{_median([t.mfe for t in cs]):>+9.2f}%"
                f"{statistics.mean(t.mae for t in cs):>+10.2f}%"
                f"{_median([t.mae for t in cs]):>+9.2f}%"
                f"{_median([t.t_mfe for t in cs]):>10.0f}s"
                f"{_median([t.t_mae for t in cs]):>10.0f}s")
        return sorok if van else []

    def first_touch_lines(self, current_run_only=True):
        """DIAGNOSZTIKA: melyik szintet erte el elobb. NEM ez a jelzes minositese --
        a poziciot kezzel kezeled, nem automatikus stoppal."""
        c = self.cfg.market
        tp, sl = c["reportTp"], c["reportSl"]
        mind = [t for t in self._mind(current_run_only) if t.tp or t.sl]
        if not mind:
            return []
        sorok = [f"TP/SL FIRST-TOUCH  (diagnosztika: TP +{tp:g}% / SL -{sl:g}%)",
                 f"{'irany':<8}{'db':>4}{'TP elobb':>10}{'SL elobb':>10}{'egyik sem':>11}"]
        van = False
        for irany in ("LONG", "SHORT"):
            cs = [t for t in mind if t.direction == irany]
            if not cs:
                continue
            van = True
            e = [t.first_touch(tp, sl) for t in cs]
            sorok.append(f"{irany:<8}{len(cs):>4}"
                         f"{e.count('TP'):>10}{e.count('SL'):>10}"
                         f"{e.count('egyiket sem'):>11}")
        return sorok if van else []

    def recent_lines(self, n=3):
        """Iranyonkent az utolso n jelzes teljes arutja."""
        mind = self._mind()
        markok = self._markok()
        out = []
        for irany in ("LONG", "SHORT"):
            cs = sorted([t for t in mind if t.direction == irany],
                        key=lambda x: -x.t0)[:n]
            if not cs:
                continue
            out.append(f"UTOLSO {len(cs)} {irany}")
            for t in cs:
                mikor = time.strftime("%m-%d %H:%M", time.gmtime(t.t0))
                jeloles = "" if t.current_run else "  (korabbi futas)"
                out.append(f"{pad(t.symbol, 12)}{mikor}  jelzes aron "
                           f"{fprice(t.signal_price)}{jeloles}")
                reszek = []
                for mark in markok:
                    r = t.hozam_at(mark)
                    reszek.append(f"{_perc_nev(mark)} "
                                  + (f"{r:+.2f}%" if r is not None else "..."))
                out.append("   " + "  ".join(reszek))
                out.append(f"   MFE {t.mfe:+.2f}%  MAE {t.mae:+.2f}%")
            out.append("")
        return out[:-1] if out else []

    def status_lines(self):
        """A log STATUS blokkjahoz."""
        fo = self.return_lines(current_run_only=True)
        if not fo:
            return []
        out = ["EREDMENY -- CURRENT RUN"] + [f"  {x}" for x in fo]
        for blokk in (self.excursion_lines(True), self.first_touch_lines(True)):
            if blokk:
                out += [""] + [f"  {x}" for x in blokk]
        korabbi = self.return_lines(current_run_only=False)
        if korabbi and any(not t.current_run for t in self._mind()):
            out += ["", "EREDMENY -- HISTORICAL / ALL TIME"] \
                + [f"  {x}" for x in korabbi]
        out += [""] + [f"  {x}" for x in self.recent_lines()]
        return out
