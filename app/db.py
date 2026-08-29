"""MongoDB kapcsolat es collection-ok."""
import os
import logging

from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger("db")

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "pumpdump")


class Database:
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGO_URL)
        self.db = self.client[MONGO_DB]
        self.config = self.db["config"]
        self.signals = self.db["signals"]
        self.snapshots = self.db["market_snapshots"]
        self.orders = self.db["orders"]

    async def init(self):
        await self.signals.create_index([("timestamp", -1)])
        await self.signals.create_index([("symbol", 1), ("timestamp", -1)])
        await self.snapshots.create_index([("timestamp", -1)])
        await self.orders.create_index([("timestamp", -1)])
        log.info("MongoDB kapcsolat kesz: %s/%s", MONGO_URL, MONGO_DB)
