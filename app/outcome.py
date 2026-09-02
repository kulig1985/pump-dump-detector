"""Mi lett a jelzesbol? -- MFE / MAE es TP/SL merese a jelzes utan.

Ez NEM backteszt es NEM jelzo: semmit nem kapuz, nem nyit poziciot. A jelzes utan
`outcomeTrackSec` ideig MINDEN kotest figyel az adott paron, es feljegyzi:

    mfePct / maePct     a legjobb es a legrosszabb pont, iranyhelyesen
    timeToMfe / Mae     mikor erte el
    tp / sl             minden szinthez: HANYADIK masodpercben erte el eloszor

Az utolso a lenyeg: mivel minden kotest latunk, es a TP es az SL elso erintese
ugyanabbol az idorendbol jon, barmely TP/SL parra utolag eldontheto, MELYIKET
erte el elobb -- kulon meres nelkul.

Iranyhelyes hozam:   LONG:  r(t) = (p − entry) / entry × 100
                     SHORT: r(t) = (entry − p) / entry × 100
"""
import time
import asyncio
import logging
import statistics

from .fmt import pad, price as fprice

log = logging.getLogger("outcome")

FLUSH_SEC = 15.0        # ilyen suru a Mongo-ba iras
MEMORIA = 200           # ennyi legutobbi jelzest tartunk a kijelzeshez


def _kulcs(szint):
    return f"{szint:g}"


class Tracker:
    """Egy jelzes elo merese."""

    __slots__ = ("id", "symbol", "setup", "direction", "entry", "t0", "deadline",
                 "mfe", "mae", "t_mfe", "t_mae", "max_price", "min_price",
                 "tp", "sl", "marks", "final", "done", "dirty")

    def __init__(self, sid, symbol, setup, direction, entry, t0, deadline,
                 tp_levels, sl_levels, mark_sec=()):
        self.id, self.symbol, self.setup = sid, symbol, setup
        self.direction, self.entry, self.t0 = direction, entry, t0
        self.deadline = deadline
        self.mfe = self.mae = 0.0
        self.t_mfe = self.t_mae = 0.0
        self.max_price = self.min_price = entry
        self.tp = {_kulcs(x): None for x in tp_levels}
        self.sl = {_kulcs(x): None for x in sl_levels}
        # 1 / 3 / 5 / 10 perces ar -- az elso kotes, ami a merespont utan erkezik
        self.marks = {_kulcs(x): None for x in mark_sec}
        self.final = 0.0
        self.done = False
        self.dirty = True

    def hozam(self, price):
        """Iranyhelyes hozam szazalekban: pozitiv = a jelzes iranyaba ment."""
        if self.entry <= 0:
            return 0.0
        r = (price - self.entry) / self.entry * 100.0
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
                self.marks[kulcs] = {"price": price, "pct": round(r, 4)}
        self.dirty = True

    def doc(self):
        return {
            "entry": self.entry, "setup": self.setup,
            "mfePct": round(self.mfe, 4), "maePct": round(self.mae, 4),
            "timeToMfeSec": self.t_mfe, "timeToMaeSec": self.t_mae,
            "maxPrice": self.max_price, "minPrice": self.min_price,
            "tp": dict(self.tp), "sl": dict(self.sl), "marks": dict(self.marks),
            "finalPct": self.final, "done": self.done,
        }

    def eredmeny(self, tp, sl):
        """NYERO / BUKO / NYITOTT a megadott TP/SL parra -- melyiket erte el elobb."""
        t = self.tp.get(_kulcs(tp))
        s = self.sl.get(_kulcs(sl))
        if t is not None and (s is None or t < s):
            return "nyero"
        if s is not None:
            return "buko"
        return "nyitott"


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
                    c["outcomeMarkSec"])
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
        """A korabbi lezart meresek betoltese, hogy az osszesites ne kezdjen nullarol."""
        try:
            kurzor = self.db.signals.find(
                {"outcome.done": True}).sort("timestamp", -1).limit(MEMORIA)
            for doc in reversed(await kurzor.to_list(length=MEMORIA)):
                o = doc["outcome"]
                t = Tracker(doc["_id"], doc["symbol"],
                            o.get("setup") or doc.get("setup") or doc["detector"],
                            doc["direction"], o.get("entry") or doc["price"],
                            doc["timestamp"].timestamp(), 0, [], [])
                t.marks = o.get("marks", {})
                t.mfe, t.mae = o.get("mfePct", 0.0), o.get("maePct", 0.0)
                t.t_mfe, t.t_mae = o.get("timeToMfeSec", 0), o.get("timeToMaeSec", 0)
                t.tp, t.sl = o.get("tp", {}), o.get("sl", {})
                t.final, t.done = o.get("finalPct", 0.0), True
                self.keszek.append(t)
            if self.keszek:
                log.info("%d korabbi lemert jelzes betoltve", len(self.keszek))
        except Exception as e:
            log.warning("a korabbi eredmenyek betoltese nem sikerult: %s", e)

    # ---------------------------------------------------------------- kijelzes

    def _mind(self):
        return self.keszek + [t for lista in self.aktiv.values() for t in lista]

    def summary_lines(self):
        """Setup-tipusonkent: hany jelzes lett volna nyero es hany buko."""
        c = self.cfg.market
        tp, sl = c["reportTp"], c["reportSl"]
        mind = [t for t in self._mind() if t.tp or t.sl]
        if not mind:
            return []
        tipusok = sorted({t.setup for t in mind})
        sorok = [f"TP +{tp:g}% / SL -{sl:g}%",
                 f"{'setup':<20}{'db':>4}{'nyero':>7}{'buko':>6}{'nyitott':>9}"
                 f"{'arany':>7}{'atlag MFE':>11}{'atlag MAE':>11}"]
        for tipus in tipusok:
            cs = [t for t in mind if t.setup == tipus]
            ny = sum(1 for t in cs if t.eredmeny(tp, sl) == "nyero")
            bu = sum(1 for t in cs if t.eredmeny(tp, sl) == "buko")
            ny_bu = ny + bu
            sorok.append(
                f"{pad(tipus, 20)}{len(cs):>4}{ny:>7}{bu:>6}{len(cs) - ny_bu:>9}"
                f"{(ny / ny_bu * 100 if ny_bu else 0):>6.0f}%"
                f"{statistics.mean(t.mfe for t in cs):>+10.2f}%"
                f"{statistics.mean(t.mae for t in cs):>+10.2f}%")
        return sorok

    def recent_lines(self, n=3):
        """Setup-tipusonkent az utolso n jelzes: mi tortent vele."""
        c = self.cfg.market
        tp, sl = c["reportTp"], c["reportSl"]
        mind = self._mind()
        out = []
        for tipus in sorted({t.setup for t in mind}):
            cs = sorted([t for t in mind if t.setup == tipus],
                        key=lambda x: -x.t0)[:n]
            if not cs:
                continue
            out.append(f"UTOLSO {len(cs)} {tipus}")
            for t in cs:
                out.append(
                    f"{pad(t.symbol, 12)}{pad(time.strftime('%m-%d %H:%M', time.gmtime(t.t0)), 12)}"
                    f"belepo {pad(fprice(t.entry), 12)}"
                    f"MFE {t.mfe:>+6.2f}%  MAE {t.mae:>+6.2f}%  -> {t.eredmeny(tp, sl)}")
            out.append("")
        return out[:-1] if out else []

    def status_lines(self):
        osszes = self.summary_lines()
        if not osszes:
            return []
        return (["EREDMENY"] + [f"  {x}" for x in osszes]
                + [f"  {x}" for x in self.recent_lines()])
