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
import itertools
import time
import asyncio
import logging

import websockets

from collections import deque
from datetime import datetime, timezone

from . import binance_rest, events, telegram
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

# A SUBSCRIBE/UNSUBSCRIBE kerelmek azonositoja: egyszeru, novekvo unsigned int.
# (A hivatalos WSS leiras unsigned INT-et ir; az OpenAPI spec stringet mutat --
# a Binance mindkettot elfogadja, de az int a dokumentalt alak.)
_req_id = itertools.count(1)


def next_request_id():
    return next(_req_id)
SILENCE_SEC = 15        # ennyi nemasag utan ujracsatlakozunk (esetleg mas utvonalon)

# A Binance az IP-t tiltja ki (429 -> 418), ha tul suru a kapcsolodasi kiserlet.
# Egy szakadozo halozat (VPN / alagut a VPS fele) mellett a naiv "azonnal probald
# ujra" logika percek alatt osszehozza ezt -- ezert van globalis korlat MINDEN
# ujracsatlakozasra, es minden bontas utan varunk.
MIN_CONNECT_GAP = 2.0       # ket kapcsolodasi kiserlet kozott legalabb ennyi
MAX_CONNECTS_5MIN = 40      # osszesen ennyi kiserlet 5 percenkent
MAX_BACKOFF = 120.0
EGESZSEGES_SEC = 60.0       # ennyi ideig elo kapcsolat utan nullazzuk a backoffot


class ConnectLimiter:
    """Globalis korlat az ujracsatlakozasokra -- az OSSZES kapcsolatra egyutt.

    Nem per-kapcsolat: a tiltas az IP-re szol, tehat a 6 stream + a konyv-stream
    egyutt szamit. Ha a keret elfogy, varunk, amig az 5 perces ablak felszabadul.
    """

    def __init__(self, min_gap=MIN_CONNECT_GAP, max_per_5min=MAX_CONNECTS_5MIN):
        self.min_gap = min_gap
        self.max_per_5min = max_per_5min
        self.kiserletek = deque()       # a legutobbi kapcsolodasok idopontjai
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            while True:
                most = time.time()
                while self.kiserletek and self.kiserletek[0] < most - 300:
                    self.kiserletek.popleft()
                keses = 0.0
                if self.kiserletek:
                    keses = max(0.0, self.kiserletek[-1] + self.min_gap - most)
                if len(self.kiserletek) >= self.max_per_5min:
                    keses = max(keses, self.kiserletek[0] + 300 - most)
                    log.warning("Tul sok ujracsatlakozas (%d / 5 perc) -- varakozas "
                                "%.0f mp, hogy a Binance ne tiltsa ki az IP-t",
                                len(self.kiserletek), keses)
                if keses <= 0:
                    self.kiserletek.append(most)
                    return
                await asyncio.sleep(keses)

    def utolso_5_perc(self):
        most = time.time()
        return sum(1 for t in self.kiserletek if t > most - 300)


class MarketDataService:
    def __init__(self, cfg, db, detectors, eligibility, on_signal):
        self.cfg = cfg
        self.db = db
        self.detectors = detectors      # DetectorManager
        self.eligibility = eligibility
        self.on_signal = on_signal
        self.signal_service = None      # a STATUS sorhoz, a main koti be
        self.outcome = None             # OutcomeTracker, a main koti be
        self.book = None                # BookCache, a main koti be
        self.notifier = None            # TelegramNotifier az idoszakos eletjelhez
        self.symbols = []
        self.started = time.time()
        self.connected = 0
        self.messages = 0        # minden beerkezett WS uzenet
        self.ignored = 0         # uzenet, ami nem arfolyam volt
        self.cycle = 0           # hanyadik statusz tabla
        self.cycle_start = time.time()
        self.stale_symbols = False   # mentett listaval futunk-e (REST nem elerheto)
        self.limiter = ConnectLimiter()
        self.reconnects = 0

    async def run(self):
        asyncio.create_task(self._status_loop())
        asyncio.create_task(self._telegram_status_loop())
        asyncio.create_task(self._book_stream())
        while True:
            c = self.cfg.market
            self.symbols = await self._load_symbols()
            if not self.symbols:
                await asyncio.sleep(30)
                continue
            chunks = [self.symbols[i:i + STREAMS_PER_CONNECTION]
                      for i in range(0, len(self.symbols), STREAMS_PER_CONNECTION)]
            log.info("Indul %d arfolyam- es %d konyv-kapcsolat, osszesen %d symbol",
                     len(chunks), len(chunks), len(self.symbols))

            self.connected = 0
            tasks = [asyncio.create_task(self._stream(i + 1, ch))
                     for i, ch in enumerate(chunks)]
            # a konyv-melyseg FOLYAMATOSAN streamel, hogy a dontes pillanataban
            # mar keszen alljon -- korabban a jelzeskor kertuk le, es az varakozas volt
            tasks += [asyncio.create_task(self._depth_stream(i + 1, ch))
                      for i, ch in enumerate(chunks)]
            try:
                # a symbol univerzumot idonkent ujraepitjuk (uj listing, kiszaradt par)
                # -- ha epp mentett listaval futunk, hamarabb probaljuk ujra
                await asyncio.sleep(600 if self.stale_symbols
                                    else c["symbolRefreshMinutes"] * 60)
                log.info("Symbol lista frissitese...")
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _load_symbols(self):
        """Symbol lista REST-bol. HIBA ESETEN SEM DOBUNK KIVETELT.

        Ha a folyamat elszall, a docker ujrainditja, es minden inditas lo egy
        exchangeInfo + egy ticker/24hr hivast (utobbi 40 sulyu). Egy crash-loop
        igy percek alatt osszehozza a 429-et, majd a 418-as IP tiltast -- pontosan
        ezert nem szabad kivetellel kilepni innen.

        Amig a REST nem elerheto, a legutobb ELMENTETT listaval dolgozunk: a
        WebSocket folyam nincs tiltva, tehat a detektor tovabb tud futni.
        """
        c = self.cfg.market
        varakozas = 5.0
        while True:
            try:
                symbols = await binance_rest.load_symbols(
                    c["minQuoteVolume24h"], c["maxSymbols"], c["symbolBlacklist"],
                    c["quoteAssets"])
                await self._save_symbols(symbols)
                self.stale_symbols = False
                return symbols
            except binance_rest.RateLimited as e:
                varakozas = max(varakozas, e.retry_after)
            except Exception as e:
                log.error("A symbol lista lekerese nem sikerult: %s: %s",
                          type(e).__name__, e)

            mentett = await self._cached_symbols()
            if mentett:
                self.stale_symbols = True
                log.warning("A legutobb mentett %d symbollal futunk tovabb, "
                            "ujraprobalas %.0f mp mulva.", len(mentett), varakozas)
                return mentett
            log.warning("Nincs mentett symbol lista, varakozas %.0f mp.", varakozas)
            await asyncio.sleep(varakozas)
            varakozas = min(varakozas * 2, binance_rest.MAX_VARAKOZAS)

    async def _save_symbols(self, symbols):
        try:
            await self.db.status.update_one(
                {"_id": "symbols"},
                {"$set": {"symbols": symbols, "updated": datetime.now(timezone.utc)}},
                upsert=True)
        except Exception as e:
            log.warning("a symbol lista mentese nem sikerult: %s", e)

    async def _cached_symbols(self):
        try:
            doc = await self.db.status.find_one({"_id": "symbols"})
            return (doc or {}).get("symbols") or []
        except Exception as e:
            log.warning("a mentett symbol lista olvasasa nem sikerult: %s", e)
            return []

    async def _stream(self, index, symbols):
        streams = [f"{s.lower()}@aggTrade" for s in symbols]
        backoff = 1.0
        attempt = 0
        while True:
            await self.limiter.wait()
            base = WS_BASES[attempt % len(WS_BASES)]
            attempt += 1
            self.reconnects += 1
            nyitva = time.time()
            try:
                async with websockets.connect(base, ping_interval=20,
                                              ping_timeout=20) as ws:
                    # a dokumentacio szerint az id kotelezo es string tipusu
                    req_id = next_request_id()
                    await ws.send(json.dumps({"method": "SUBSCRIBE",
                                              "params": streams, "id": req_id}))
                    self.connected += 1
                    log.info("WS #%d csatlakozva: %s | %d stream feliratkozva",
                             index, base, len(streams))
                    got_price = False
                    try:
                        while True:
                            # ha SILENCE_SEC-ig egy uzenet sem jon, a kapcsolat halott
                            raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_SEC)
                            self.messages += 1
                            if self._handle(raw) and not got_price:
                                got_price = True
                                log.info("WS #%d elso arfolyam megjott innen: %s",
                                         index, base)
                                attempt -= 1        # ez az utvonal jo, maradjunk rajta
                    except asyncio.TimeoutError:
                        if got_price:
                            log.warning("WS #%d %ds-ig nema -- ujracsatlakozas",
                                        index, SILENCE_SEC)
                        else:
                            log.error("WS #%d: a(z) %s utvonalrol nem jott arfolyam "
                                      "%ds alatt -- atvaltas a kovetkezo utvonalra",
                                      index, base, SILENCE_SEC)
                    finally:
                        self.connected -= 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS #%d szakadas: %s", index, e)

            # ADATSZAKADAS: az erintett parok felepitett setupjai NEM folytathatok.
            # Reconnect utan az elso kotes kulonben egy regen megtortent kitores
            # "friss keresztezesenek" latszana.
            self.detectors.reset(symbols)

            # MINDEN bontas utan varunk -- a nemasag-timeout utan is. Enelkul egy
            # szakadozo alagut mellett 15 masodpercenkent ujracsatlakoznank, ami
            # egyenes ut a 418-as IP tiltashoz.
            if time.time() - nyitva >= EGESZSEGES_SEC:
                backoff = 1.0                       # a kapcsolat elt, tiszta lappal
            log.info("WS #%d ujracsatlakozas %.0f mp mulva", index, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _depth_stream(self, index, symbols):
        """Partial book depth minden figyelt parra, folyamatosan.

        A hivatalos spec szerint ez a "public" csoport: <symbol>@depth<5|10|20>@
        <100ms|500ms>, az esemeny neve depthUpdate, a mezok b (bid) es a (ask).
        """
        c = self.cfg.detector
        streams = [f"{s.lower()}@depth{c['depthLevels']}@{c['depthUpdateSpeed']}"
                   for s in symbols]
        backoff, attempt = 1.0, 0
        while True:
            await self.limiter.wait()
            base = BOOK_BASES[attempt % len(BOOK_BASES)]
            attempt += 1
            self.reconnects += 1
            nyitva = time.time()
            try:
                async with websockets.connect(base, ping_interval=20,
                                              ping_timeout=20) as ws:
                    await ws.send(json.dumps({"method": "SUBSCRIBE",
                                              "params": streams,
                                              "id": next_request_id()}))
                    log.info("Konyv-melyseg #%d csatlakozva: %s | %d stream",
                             index, base, len(streams))
                    kapott = False
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_SEC)
                        msg = json.loads(raw)
                        if "result" in msg or "error" in msg:
                            if msg.get("error"):
                                log.error("A depth feliratkozast a Binance "
                                          "elutasitotta: %s", msg["error"])
                            continue
                        adat = msg.get("data", msg)
                        if isinstance(adat, dict) and self.book:
                            self.book.on_depth(adat)
                            if not kapott:
                                kapott = True
                                attempt -= 1        # ez az utvonal jo
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                log.warning("Konyv-melyseg #%d %ds-ig nema -- ujracsatlakozas",
                            index, SILENCE_SEC)
            except Exception as e:
                log.warning("Konyv-melyseg #%d szakadas: %s", index, e)

            if time.time() - nyitva >= EGESZSEGES_SEC:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _book_stream(self):
        """A teljes piac legjobb bid/ask ara es mennyisege, egyetlen feliratkozassal.

        Kulon kapcsolat, mert a bookTicker a "public" csoportba tartozik: a
        /market/stream vegponton a feliratkozas nyugtazva lesz, de adat nem jon.
        Ugyanaz az utvonal-visszalepes, mint a fo streamnel.
        """
        backoff, attempt = 1.0, 0
        while True:
            await self.limiter.wait()
            base = BOOK_BASES[attempt % len(BOOK_BASES)]
            attempt += 1
            self.reconnects += 1
            nyitva = time.time()
            try:
                async with websockets.connect(base, ping_interval=20,
                                              ping_timeout=20) as ws:
                    await ws.send(json.dumps({"method": "SUBSCRIBE",
                                              "params": ["!bookTicker"],
                                              "id": next_request_id()}))
                    log.info("Order book stream csatlakozva: %s", base)
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
                log.warning("Order book stream szakadas: %s", e)

            # ugyanaz, mint a fo streamnel: minden bontas utan varunk
            if time.time() - nyitva >= EGESZSEGES_SEC:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

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
        if self.outcome:
            self.outcome.on_trade(trade)
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
                f"{self.book.status() if self.book else 'nincs melyseg'} | "
                f"ujracsatlakozas {self.limiter.utolso_5_perc()}/5perc | "
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
            if self.outcome:
                sorok += [f"   {x}" for x in self.outcome.status_lines()]
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

    async def _telegram_status_loop(self):
        """Idoszakos eletjel Telegramra: fut-e meg, es mit nez eppen.

        Kulon a log STATUS sorotol: azt percenkent irjuk, ez ritkabb es rovidebb.
        """
        while True:
            perc = self.cfg.telegram.get("statusEveryMinutes", 0)
            if not perc or not self.notifier:
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(perc * 60)
            try:
                await self.notifier.send("STATUS", telegram.format_status(self._status_info()))
            except Exception as e:
                log.warning("eletjel kuldese sikertelen: %s", e)

    def _status_info(self):
        det = next((d for d in self.detectors.detectors
                    if hasattr(d, "allapotok")), None)
        allapot = det.allapotok() if det else {}
        eltelt = max(1, int(time.time() - self.started))
        return {
            "ido": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "uptime": f"{eltelt // 3600}h {eltelt % 3600 // 60}p",
            "symbols": len(self.symbols),
            "wsConnected": self.connected,
            "wsTotal": self._chunk_count(),
            "reconnects5min": self.limiter.utolso_5_perc(),
            "ticksPerMin": self.detectors.osszes_tick / max(1, eltelt / 60),
            "signals": self.signal_service.signals_today if self.signal_service else 0,
            "kizarva": (self.eligibility.summary() or [""])[0],
            "setups": [(k, str(v)) for k, v in sorted(allapot.items()) if v],
            "kozel": det.readiness() if det else "",
            "hozam": self.outcome.return_lines(True) if self.outcome else [],
            "kilenges": self.outcome.excursion_lines(True) if self.outcome else [],
            "firstTouch": self.outcome.first_touch_lines(True) if self.outcome else [],
            "hozamAllTime": (self.outcome.return_lines(False)
                             if self.outcome and any(not t.current_run
                                                     for t in self.outcome._mind())
                             else []),
            "utolso": self.outcome.recent_lines(
                self.cfg.telegram.get("statusRecentSignals", 3)) if self.outcome else [],
        }

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
