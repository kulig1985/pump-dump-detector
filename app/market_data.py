"""MarketDataService -- Binance Futures aggTrade streamek.

A hivatalos dokumentacio szerint (Futures USDS-M WebSocket Market Streams, Aggregate
Trade Streams / Request) az aggTrade harom modon erheto el:

    Raw stream:              wss://fstream.binance.com/market/ws/<sym>@aggTrade
    Combined stream URL-bol: wss://fstream.binance.com/market/stream?streams=<sym>@aggTrade
    Combined stream keressel: wss://fstream.binance.com/market/stream  + SUBSCRIBE uzenet

Az utolsot hasznaljuk: a stream nevek az uzenet torzseben mennek (nem az URL query
stringjeben, amit egy proxy levaghat), es nyugtat is kapunk a feliratkozasrol.

FONTOS: az utvonalban benne van a  /market  szegmens. A regi  /ws  vegpont elfogadja
a kapcsolatot es meg a SUBSCRIBE-ot is nyugtazza, de nem kuld adatot -- ezert a
WS_BASES listaban a dokumentalt utvonal az elso, es csak utana probaljuk a regieket.

Egy kapcsolat max 200 feliratkozast enged, ezert 150-es csoportokra bontjuk.
A Binance idonkent bontja a kapcsolatot (kb. 24 orankent, illetve halozati hiba
eseten) -- a _stream ciklus ezt automatikusan ujraepiti.
"""
import os
import json
import time
import uuid
import asyncio
import logging

import websockets

from datetime import datetime, timezone

from . import binance_rest
from .detector import MovementDetector

log = logging.getLogger("market")

# a hivatalos spec servers listaja: eles + testnet
WS_HOST = ("wss://stream.binancefuture.com" if os.getenv("FUTURES_TESTNET") == "1"
           else "wss://fstream.binance.com")
# elso a dokumentalt utvonal, utana a regebbiek -- ha az elso nem kuld adatot,
# a kovetkezo ujracsatlakozas mar a kovetkezot probalja
WS_BASES = [f"{WS_HOST}/market/stream", f"{WS_HOST}/stream", f"{WS_HOST}/ws"]
STREAMS_PER_CONNECTION = 150
SILENCE_SEC = 15        # ennyi nemasag utan ujracsatlakozunk (esetleg mas utvonalon)


class MarketDataService:
    def __init__(self, cfg, db, on_trigger):
        self.cfg = cfg
        self.db = db
        self.on_trigger = on_trigger
        self.detector = MovementDetector(cfg)
        self.symbols = []
        self.started = time.time()
        self.connected = 0
        self.messages = 0        # minden beerkezett WS uzenet
        self.ignored = 0         # uzenet, ami nem arfolyam volt

    async def run(self):
        asyncio.create_task(self._status_loop())
        while True:
            c = self.cfg.detector
            self.symbols = await binance_rest.load_symbols(
                c["minQuoteVolume24h"], c["maxSymbols"])
            chunks = [self.symbols[i:i + STREAMS_PER_CONNECTION]
                      for i in range(0, len(self.symbols), STREAMS_PER_CONNECTION)]
            log.info("Indul %d WebSocket kapcsolat, osszesen %d symbol",
                     len(chunks), len(self.symbols))

            self.connected = 0
            tasks = [asyncio.create_task(self._stream(i + 1, ch))
                     for i, ch in enumerate(chunks)]
            try:
                # a symbol univerzumot idonkent ujraepitjuk (uj listing, kiszaradt par)
                await asyncio.sleep(c["symbolRefreshMinutes"] * 60)
                log.info("Symbol lista frissitese...")
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _stream(self, index, symbols):
        streams = [f"{s.lower()}@aggTrade" for s in symbols]
        backoff = 1
        attempt = 0
        while True:
            base = WS_BASES[attempt % len(WS_BASES)]
            attempt += 1
            try:
                async with websockets.connect(base, ping_interval=20,
                                              ping_timeout=20) as ws:
                    # a dokumentacio szerint az id kotelezo es string tipusu
                    req_id = uuid.uuid4().hex
                    await ws.send(json.dumps({"method": "SUBSCRIBE",
                                              "params": streams, "id": req_id}))
                    self.connected += 1
                    log.info("WS #%d csatlakozva: %s | %d stream feliratkozva",
                             index, base, len(streams))
                    backoff = 1
                    got_price = False
                    try:
                        while True:
                            # ha SILENCE_SEC-ig egy uzenet sem jon, a kapcsolat halott
                            raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_SEC)
                            self.messages += 1
                            if self._handle(raw) and not got_price:
                                got_price = True
                                log.info("WS #%d elso arfolyam megjott innen: %s", index, base)
                                attempt -= 1        # ez az utvonal jo, maradjunk rajta
                    except asyncio.TimeoutError:
                        if got_price:
                            log.warning("WS #%d %ds-ig nema -- ujracsatlakozas",
                                        index, SILENCE_SEC)
                        else:
                            log.error("WS #%d: a(z) %s utvonalrol nem jott arfolyam %ds alatt "
                                      "-- atvaltas a kovetkezo utvonalra", index, base, SILENCE_SEC)
                    finally:
                        self.connected -= 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS #%d szakadas: %s -- ujracsatlakozas %ds mulva", index, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle(self, raw):
        """True, ha ez egy feldolgozott arfolyam volt."""
        if not self.cfg.detector["enabled"]:
            return False
        msg = json.loads(raw)
        if "result" in msg or "error" in msg:
            # {"result": null, "id": "..."} = nyugta, {"error": {...}} = elutasitas
            if msg.get("error"):
                log.error("A Binance ELUTASITOTTA a feliratkozast: %s", msg["error"])
            else:
                log.info("Feliratkozas nyugtazva (id %s)", str(msg.get("id"))[:8])
            return False
        # /ws vegponton a payload csupaszon jon, /stream eseten "data" ala csomagolva
        data = msg.get("data", msg)
        if not data or data.get("e") != "aggTrade":
            self.ignored += 1
            if self.ignored <= 3:      # az elso parat mutassuk, hatha hibauzenet
                log.warning("Ismeretlen WS uzenet eldobva: %s", str(msg)[:200])
            return False
        trigger = self.detector.on_price(data["s"], float(data["p"]), data["T"] / 1000.0)
        if trigger:
            # a reszletes elemzes lassu (order book + klines), nem blokkolhatja a stream olvasast
            asyncio.create_task(self.on_trigger(trigger))
        return True

    async def _status_loop(self):
        """5 masodpercenkent kiirja, mi tortenik eppen az arfolyamokkal.

        Ez nem technikai eletjel: az a celja, hogy ranezesre lehessen latni,
        mit csinalnak az arak, es miert nincs (meg) jelzes.
        """
        while True:
            interval = self.cfg.detector["statusIntervalSec"]
            await asyncio.sleep(interval)
            snap = self.detector.snapshot()
            level = logging.ERROR if snap["ticks"] == 0 else logging.INFO
            log.log(level, "\n%s", self._render(snap, interval))
            await self._save_status(snap, interval)

    def _render(self, snap, interval):
        th = self.detector.thresholds()
        head = (f"  {'─' * 78}\n"
                f"  MI TORTENIK MOST   {len(self.symbols)} par figyelese   "
                f"jelzes indulas ota: {snap['totalTriggers']}\n")

        if snap["ticks"] == 0:
            if self.messages == 0:
                baj = ("A Binance kapcsolat all, de EGYETLEN UZENET SEM erkezett.\n"
                       "  Meg a feliratkozas nyugtaja sem -- ellenorizd a kimeno halozatot\n"
                       "  a fstream.binance.com:443 fele.")
            else:
                baj = (f"Erkezett {self.messages:,} uzenet, de egyetlen arfolyam sem.\n"
                       f"  Valoszinuleg rossz WebSocket utvonalon vagyunk -- a program\n"
                       f"  a kovetkezo ujracsatlakozaskor masikkal probalkozik.")
            return head + f"  {baj}\n  {'─' * 78}"

        head += (f"  az elmult {interval} masodpercben {snap['ticks']:,} arvaltozas erkezett   "
                 f"({self.connected}/{self._chunk_count()} kapcsolat el)\n"
                 f"  jelzes kell hozza: 1 mp alatt {th[1]:.2f}%, 3 mp alatt {th[3]:.2f}%, "
                 f"5 mp alatt {th[5]:.2f}%\n"
                 f"  {'─' * 78}\n"
                 f"  {'par':<13}{'arfolyam':>13}{'1 mp':>9}{'3 mp':>9}{'5 mp':>9}   mi van vele\n")

        lines = []
        for r in snap["rows"]:
            c = r["changes"]
            lines.append(f"  {r['symbol']:<13}{_price(r['price']):>13}"
                         f"{_pct(c[1]):>9}{_pct(c[3]):>9}{_pct(c[5]):>9}   {_verdict(r)}")
        return head + "\n".join(lines) + f"\n  {'─' * 78}"

    async def _save_status(self, snap, interval):
        try:
            await self.db.status.update_one(
                {"_id": "detector"},
                {"$set": {"lastUpdate": datetime.now(timezone.utc),
                          "uptimeSec": int(time.time() - self.started),
                          "watchedSymbols": len(self.symbols),
                          "wsConnected": self.connected,
                          "wsTotal": self._chunk_count(),
                          "ticksPerSec": round(snap["ticks"] / interval, 1),
                          "totalTriggers": snap["totalTriggers"],
                          "topMovers": [_mongo_row(r) for r in snap["rows"]]}},
                upsert=True)
        except Exception as e:
            log.warning("statusz mentese sikertelen: %s", e)

    def _chunk_count(self):
        return max(1, -(-len(self.symbols) // STREAMS_PER_CONNECTION))


def _mongo_row(r):
    """A changes kulcsai egesz szamok (1/3/5 mp) -- a Mongo csak string kulcsot fogad."""
    return {**r, "changes": {f"s{w}": v for w, v in r["changes"].items()}}


def _pct(v):
    return "  --  " if v is None else f"{v:+.2f}%"


def _price(p):
    """Olvashato arformatum: a nagy arak ket tizedessel, a torpek teljes hosszban."""
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def _verdict(r):
    """Emberi nyelven: mi van ezzel a parral."""
    if r["window"] is None:
        return "meg gyulik rola az adat"
    irany = "emelkedik" if r["rising"] else "esik"
    if r["missing"] <= 0:
        # a kuszobot mar atlepte -- vagy most ment el a jelzes, vagy varakozunk
        return (f"jelzes mar elment, varakozas a kovetkezoig" if r["cooling"]
                else f"kuszob atlepve, {irany}")
    hiany = f"{r['missing']:.2f}%"
    if r["ratio"] >= 0.9:
        return f"MINDJART JELZES! {irany}, meg {hiany} hianyzik"
    if r["ratio"] >= 0.6:
        return f"erosen {irany}, meg {hiany} hianyzik a jelzeshez"
    if r["ratio"] >= 0.3:
        return f"{irany}, de meg messze van a jelzestol"
    return "alig mozdul"
