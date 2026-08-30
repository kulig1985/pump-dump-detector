"""MarketDataService -- Binance Futures aggTrade streamek.

Kombinalt stream endpoint:  wss://fstream.binance.com/stream?streams=a@aggTrade/b@aggTrade
A futures WS kapcsolatonkent max 200 subscription-t enged, ezert 150-es chunkokra bontjuk.
"""
import json
import time
import asyncio
import logging

import websockets

from datetime import datetime, timezone

from . import binance_rest
from .detector import MovementDetector

log = logging.getLogger("market")

WS_BASE = "wss://fstream.binance.com"
STREAMS_PER_CONNECTION = 150
STALL_SEC = 30          # ennyi nemasag utan halottnak tekintjuk a kapcsolatot


class MarketDataService:
    def __init__(self, cfg, db, on_trigger):
        self.cfg = cfg
        self.db = db
        self.on_trigger = on_trigger
        self.detector = MovementDetector(cfg)
        self.symbols = []
        self.started = time.time()
        self.connected = 0
        self.frames = 0          # nyers WS keret, barmi jott a droton
        self.ignored = 0         # keret, amit nem aggTrade-kent dobtunk el
        self.last_frame = 0.0

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
        url = f"{WS_BASE}/stream?streams=" + "/".join(f"{s.lower()}@aggTrade" for s in symbols)
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, ping_interval=20,
                                              ping_timeout=20) as ws:
                    self.connected += 1
                    log.info("WS #%d csatlakozva (%d stream)", index, len(symbols))
                    backoff = 1
                    first = True
                    try:
                        while True:
                            # ha STALL_SEC-ig egy keret sem jon, a kapcsolat halott:
                            # bontunk es ujracsatlakozunk, nem varunk vegtelenul
                            raw = await asyncio.wait_for(ws.recv(), timeout=STALL_SEC)
                            self.frames += 1
                            self.last_frame = time.time()
                            if first:
                                log.info("WS #%d elso keret megjott: %s", index, raw[:160])
                                first = False
                            self._handle(raw)
                    except asyncio.TimeoutError:
                        log.error("WS #%d %ds-ig egy keret sem erkezett -- ujracsatlakozas",
                                  index, STALL_SEC)
                    finally:
                        self.connected -= 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS #%d szakadas: %s -- ujracsatlakozas %ds mulva", index, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle(self, raw):
        if not self.cfg.detector["enabled"]:
            return
        msg = json.loads(raw)
        data = msg.get("data")
        if not data or data.get("e") != "aggTrade":
            self.ignored += 1
            if self.ignored <= 3:      # az elso parat mutassuk, hatha hibauzenet
                log.warning("Ismeretlen WS uzenet eldobva: %s", str(msg)[:200])
            return
        trigger = self.detector.on_price(data["s"], float(data["p"]), data["T"] / 1000.0)
        if trigger:
            # a reszletes elemzes lassu (order book + klines), nem blokkolhatja a stream olvasast
            asyncio.create_task(self.on_trigger(trigger))

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
            if self.frames == 0:
                baj = ("A Binance WebSocket kapcsolat all, de EGY KERET SEM erkezett.\n"
                       "  Ellenorizd a kimeno halozatot a fstream.binance.com:443 fele.")
            else:
                baj = (f"Erkezett {self.frames:,} keret, de egyik sem hasznalhato arfolyam\n"
                       f"  ({self.ignored:,} eldobva). A fenti 'Ismeretlen WS uzenet' sor mutatja, mi jott.")
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
                          "topMovers": snap["rows"]}},
                upsert=True)
        except Exception as e:
            log.warning("statusz mentese sikertelen: %s", e)

    def _chunk_count(self):
        return max(1, -(-len(self.symbols) // STREAMS_PER_CONNECTION))


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
