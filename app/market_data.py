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


class MarketDataService:
    def __init__(self, cfg, db, on_trigger):
        self.cfg = cfg
        self.db = db
        self.on_trigger = on_trigger
        self.detector = MovementDetector(cfg)
        self.symbols = []
        self.started = time.time()
        self.connected = 0

    async def run(self):
        asyncio.create_task(self._heartbeat())
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
                async with websockets.connect(url, ping_interval=180) as ws:
                    self.connected += 1
                    log.info("WS #%d csatlakozva (%d stream)", index, len(symbols))
                    backoff = 1
                    try:
                        async for raw in ws:
                            self._handle(raw)
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
        data = json.loads(raw).get("data")
        if not data or data.get("e") != "aggTrade":
            return
        trigger = self.detector.on_price(data["s"], float(data["p"]), data["T"] / 1000.0)
        if trigger:
            # a reszletes elemzes lassu (order book + klines), nem blokkolhatja a stream olvasast
            asyncio.create_task(self.on_trigger(trigger))

    async def _heartbeat(self):
        """Periodikus eletjel: konzolra es a Mongo `status` collectionbe.

        Enelkul egy nyugodt piacon oraszamra nem irna semmit, es nem latszana,
        hogy egyaltalan el-e a stream.
        """
        while True:
            interval = self.cfg.detector["heartbeatSec"]
            await asyncio.sleep(interval)
            s = self.detector.take_stats()
            uptime = int(time.time() - self.started)

            if s["ticks"] == 0:
                log.error("STATUS  NEM ERKEZIK TICK az elmult %ds-ben! (%d/%d WS kapcsolat el)",
                          interval, self.connected, self._chunk_count())
            else:
                log.info("STATUS  uptime %s | %d tick (%.0f/s) | %d aktiv symbol | "
                         "%d/%d WS el | trigger: %d (osszesen %d)",
                         _hms(uptime), s["ticks"], s["ticks"] / interval, s["activeSymbols"],
                         self.connected, self._chunk_count(), s["triggers"], s["totalTriggers"])
                if s["topMovers"]:
                    log.info("STATUS  legnagyobb mozgas: %s", " | ".join(
                        f"{m['symbol']} {m['window']} {m['changePct']:+.2f}%"
                        for m in s["topMovers"]))

            try:
                await self.db.status.update_one(
                    {"_id": "detector"},
                    {"$set": {"lastHeartbeat": datetime.now(timezone.utc),
                              "uptimeSec": uptime,
                              "watchedSymbols": len(self.symbols),
                              "wsConnected": self.connected,
                              "wsTotal": self._chunk_count(),
                              "ticksPerSec": round(s["ticks"] / interval, 1),
                              **s}},
                    upsert=True)
            except Exception as e:
                log.warning("STATUS  heartbeat mentes sikertelen: %s", e)

    def _chunk_count(self):
        return max(1, -(-len(self.symbols) // STREAMS_PER_CONNECTION))


def _hms(sec):
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
