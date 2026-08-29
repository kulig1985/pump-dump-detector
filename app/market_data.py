"""MarketDataService -- Binance Futures aggTrade streamek.

Kombinalt stream endpoint:  wss://fstream.binance.com/stream?streams=a@aggTrade/b@aggTrade
A futures WS kapcsolatonkent max 200 subscription-t enged, ezert 150-es chunkokra bontjuk.
"""
import json
import asyncio
import logging

import websockets

from . import binance_rest
from .detector import MovementDetector

log = logging.getLogger("market")

WS_BASE = "wss://fstream.binance.com"
STREAMS_PER_CONNECTION = 150


class MarketDataService:
    def __init__(self, cfg, on_trigger):
        self.cfg = cfg
        self.on_trigger = on_trigger
        self.detector = MovementDetector(cfg)
        self.symbols = []

    async def run(self):
        while True:
            c = self.cfg.detector
            self.symbols = await binance_rest.load_symbols(
                c["minQuoteVolume24h"], c["maxSymbols"])
            chunks = [self.symbols[i:i + STREAMS_PER_CONNECTION]
                      for i in range(0, len(self.symbols), STREAMS_PER_CONNECTION)]
            log.info("Indul %d WebSocket kapcsolat, osszesen %d symbol",
                     len(chunks), len(self.symbols))

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
                    log.info("WS #%d csatlakozva (%d stream)", index, len(symbols))
                    backoff = 1
                    async for raw in ws:
                        self._handle(raw)
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
