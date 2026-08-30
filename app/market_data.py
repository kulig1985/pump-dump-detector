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

from . import binance_rest, events
from .detectors.base import Trade
from .fmt import clock

log = logging.getLogger("market")

# a hivatalos spec servers listaja: eles + testnet
WS_HOST = ("wss://stream.binancefuture.com" if os.getenv("FUTURES_TESTNET") == "1"
           else "wss://fstream.binance.com")
# elso a dokumentalt utvonal, utana a regebbiek -- ha az elso nem kuld adatot,
# a kovetkezo ujracsatlakozas mar a kovetkezot probalja
# Az aggTrade a "market", a bookTicker a "public" csoportba tartozik, es ez az
# URL szegmensben is megjelenik -- ezert kell nekik KULON kapcsolat. A !bookTicker
# a /market/stream vegponton nem erkezik meg (a feliratkozast nyugtazza, de nem kuld).
WS_BASES = [f"{WS_HOST}/market/stream", f"{WS_HOST}/stream", f"{WS_HOST}/ws"]
BOOK_BASES = [f"{WS_HOST}/public/stream", f"{WS_HOST}/stream", f"{WS_HOST}/ws"]
STREAMS_PER_CONNECTION = 150
SILENCE_SEC = 15        # ennyi nemasag utan ujracsatlakozunk (esetleg mas utvonalon)


class MarketDataService:
    def __init__(self, cfg, db, detectors, eligibility, on_signal):
        self.cfg = cfg
        self.db = db
        self.detectors = detectors      # DetectorManager
        self.eligibility = eligibility
        self.on_signal = on_signal
        self.signal_service = None      # a STATUS sorhoz, a main koti be
        self.symbols = []
        self.started = time.time()
        self.connected = 0
        self.messages = 0        # minden beerkezett WS uzenet
        self.ignored = 0         # uzenet, ami nem arfolyam volt
        self.cycle = 0           # hanyadik statusz tabla
        self.cycle_start = time.time()

    async def run(self):
        asyncio.create_task(self._status_loop())
        asyncio.create_task(self._book_stream())
        while True:
            c = self.cfg.market
            self.symbols = await binance_rest.load_symbols(
                c["minQuoteVolume24h"], c["maxSymbols"], c["symbolBlacklist"],
                c["quoteAssets"])
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

    async def _book_stream(self):
        """A teljes piac legjobb bid/ask ara es mennyisege, egyetlen feliratkozassal.

        Kulon kapcsolat, mert a bookTicker a "public" csoportba tartozik: a
        /market/stream vegponton a feliratkozas nyugtazva lesz, de adat nem jon.
        Ugyanaz az utvonal-visszalepes, mint a fo streamnel.
        """
        backoff, attempt = 1, 0
        while True:
            base = BOOK_BASES[attempt % len(BOOK_BASES)]
            attempt += 1
            try:
                async with websockets.connect(base, ping_interval=20,
                                              ping_timeout=20) as ws:
                    await ws.send(json.dumps({"method": "SUBSCRIBE",
                                              "params": ["!bookTicker"],
                                              "id": uuid.uuid4().hex}))
                    log.info("Order book stream csatlakozva: %s", base)
                    backoff = 1
                    kapott = False
                    try:
                        while True:
                            raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_SEC)
                            msg = json.loads(raw)
                            if "result" in msg or "error" in msg:
                                if msg.get("error"):
                                    log.error("A !bookTicker feliratkozast a Binance "
                                              "elutasitotta: %s", msg["error"])
                                continue
                            adat = msg.get("data", msg)
                            # a !bookTicker kombinalt streamen tombot is kuldhet
                            for x in (adat if isinstance(adat, list) else [adat]):
                                if isinstance(x, dict) and "b" in x and "a" in x:
                                    self.eligibility.on_book_ticker(x)
                                    if not kapott:
                                        kapott = True
                                        attempt -= 1        # ez az utvonal jo
                                        log.info("Order book adat erkezik innen: %s", base)
                    except asyncio.TimeoutError:
                        log.error("Order book stream: %s utvonalrol nem jott adat "
                                  "%ds alatt -- atvaltas a kovetkezore", base, SILENCE_SEC)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Order book stream szakadas: %s -- ujra %ds mulva", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle(self, raw):
        """True, ha ez egy feldolgozott arfolyam volt."""
        if not self.cfg.market["enabled"]:
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
        if isinstance(data, dict) and data.get("e") == "bookTicker":
            self.eligibility.on_book_ticker(data)
            return True
        if not data or data.get("e") != "aggTrade":
            self.ignored += 1
            if self.ignored <= 3:      # az elso parat mutassuk, hatha hibauzenet
                log.warning("Ismeretlen WS uzenet eldobva: %s", str(msg)[:200])
            return False
        # buy_taker: az "m" mezo azt mondja meg, a VEVO volt-e a maker.
        # m=true -> az agresszor az elado; m=false -> az agresszor a vevo.
        trade = Trade(symbol=data["s"], price=float(data["p"]), qty=float(data["q"]),
                      ts=data["T"] / 1000.0, buy_taker=not data["m"])
        for sig in self.detectors.on_trade(trade):
            # a reszletes elemzes lassu (order book + klines), nem blokkolhatja a stream olvasast
            asyncio.create_task(self.on_signal(sig))
        return True

    async def _status_loop(self):
        """Percenkent egy rovid allapotsor. A reszletes tabla csak DEBUG szinten.

        Az elso sor hamarabb jon, hogy indulas utan ne kelljen egy percet varni
        arra, hogy lassuk: fut a rendszer.
        """
        elso = True
        while True:
            interval = self.cfg.market["statusIntervalSec"]
            await asyncio.sleep(15 if elso else interval)
            elso = False
            ticks = self.detectors.take_ticks()
            self.cycle += 1
            svc = self.signal_service
            kizart = self.eligibility.summary()

            sorok = [
                f"STATUS     {len(self.symbols)} par | {ticks:,} tick/{interval}s | "
                f"{self.eligibility.book_status()} | "
                f"{self.detectors.total_candidates} candidate, "
                f"{svc.signals_today if svc else 0} jelzes, "
                f"{self.detectors.skipped} kihagyva (nem kereskedheto) | "
                f"Telegram: {'BE' if self.cfg.telegram['enabled'] else 'KI'}",
            ]
            sorok += [f"   {x}" for x in kizart]
            sorok += [f"   {x}" for x in
                      self.eligibility.distribution(self.symbols)]
            for d in self.detectors.detectors:
                if hasattr(d, "readiness"):
                    sorok.append(f"   {d.readiness()}")
            log.log(logging.ERROR if ticks == 0 else logging.INFO,
                    "%s", "\n".join(sorok))

            if ticks == 0:
                log.error("Nem erkezik arfolyam! %s",
                          "Meg a feliratkozas nyugtaja sem jott meg."
                          if self.messages == 0 else
                          "Erkeznek uzenetek, de nem arfolyamok -- rossz WS utvonal?")

            if log.isEnabledFor(logging.DEBUG):
                blokk = self._events_section() + self.detectors.debug_lines()
                log.debug("\n%s", "\n".join(blokk))
            else:
                events.drain()          # ne gyuljon vegtelenul

            await self._save_status(ticks, interval)

    @staticmethod
    def _events_section(limit=30):
        items = events.drain()
        if not items:
            return ["  az elozo statusz ota nem tortent semmi"]
        out = [f"  AZ ELOZO STATUSZ OTA ({len(items)}):"]
        for ts, text in items[-limit:]:
            out.append(f"    {clock(ts)}  {text}")
        return out

    async def _save_status(self, ticks, interval):
        try:
            await self.db.status.update_one(
                {"_id": "detector"},
                {"$set": {"lastUpdate": datetime.now(timezone.utc),
                          "uptimeSec": int(time.time() - self.started),
                          "watchedSymbols": len(self.symbols),
                          "wsConnected": self.connected,
                          "wsTotal": self._chunk_count(),
                          "ticksPerSec": round(ticks / interval, 1),
                          "candidates": self.detectors.total_candidates,
                          "skippedByEligibility": self.detectors.skipped,
                          "detectors": [d.name for d in self.detectors.detectors
                                        if self.detectors.enabled(d)],
                          "notEligible": self.eligibility.summary()}},
                upsert=True)
        except Exception as e:
            log.warning("statusz mentese sikertelen: %s", e)

    def _chunk_count(self):
        return max(1, -(-len(self.symbols) // STREAMS_PER_CONNECTION))
