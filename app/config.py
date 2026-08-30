"""Konfiguracio MongoDB-bol. Ket dokumentum: 'detector' es 'trading'.

Elso indulaskor a default ertekek bekerulnek a DB-be, utana onnan olvasunk.
A configot 30 masodpercenkent ujratoltjuk, igy DB-ben modositva menet kozben hat.
"""
import os
import asyncio
import logging

log = logging.getLogger("config")

DETECTOR_DEFAULTS = {
    "_id": "detector",
    "enabled": True,
    "telegramEnabled": True,
    # --- symbol szures ---
    "minQuoteVolume24h": 50_000_000,   # USDT, 24h forgalom minimum
    "maxSymbols": 200,                 # top N forgalom szerint
    "symbolRefreshMinutes": 60,
    # --- trigger: az utolso N trade-re illesztett egyenes meredeksege ---
    "tradeWindow": 30,                 # ennyi trade-bol szamolunk meredekseget
    "maxSpanSec": 5.0,                 # ha ez a N trade ennel hosszabb ido alatt tortent,
                                       # akkor nem hirtelen mozgas -- nem erdekel
    "minSlopePctPerSec": 0.15,         # ennyi %/masodperc kell a jelzeshez
    "minConsistency": 0.70,            # a lepesek ekkora hanyada mutasson egy iranyba
    "volatilityMultiplier": 4.0,       # 0 = ki. A meredekseg-kuszob sose megy a fenti
                                       # ertek ala, de zajos parokon feljebb megy
    # --- csak a tablazatban mutatott 1/3/5 mp-es szamokhoz ---
    "minTicksInWindow": 3,             # ennyi trade-nek kell lennie az ablakban
    "maxRefAgeFactor": 1.5,            # a viszonyitasi pont max ennyiszer regebbi az ablaknal
    # --- signal ---
    "minSignalScore": 60,
    "symbolCooldownSec": 60,
    "statusIntervalSec": 5,           # ilyen surun irja ki, mi tortenik az arakkal
    "signalWindowMinutes": 10,        # ennyi idore visszamenoleg szamoljuk a jelzeseket
    # --- order book ---
    "orderBookLevels": 20,             # 5 / 10 / 20 (Binance partial depth stream)
    "wallSensitivity": 3.0,            # szint >= N * a tobbi szint atlaga => wall
    "wallMaxDistancePct": 1.5,         # ennel tavolabbi wall mar nem erdekes
    # --- TA ---
    "emaFast": 9,
    "emaSlow": 21,
    "emaInterval": "1m",
}

REVERSAL_DEFAULTS = {
    "_id": "reversal",
    "enabled": True,
    "minSignalScore": 60,              # sajat kuszob, fuggetlen a pump/dump-etol
    "cooldownSec": 120,
    # --- rolling trade ablak ---
    "windowSeconds": 20,
    "minTradesInFlowWindow": 5,
    "maxSetupAgeSec": 30,              # ennyi ido utan elavul egy alakzat
    # --- alakzat ---
    "minMovePct": 0.40,                # a fordulo elotti mozgas merteke
    "bouncePct": 0.15,                 # ennyit kell eltavolodni a szelsoertektol
    "pullbackPct": 0.08,               # a micro szint rogzitesehez szukseges visszahuzas
    "newExtremeTolerancePct": 0.05,    # ennel melyebb minimum = uj alakzat
    "breakTolerancePct": 0.02,         # ennyivel kell atutni a micro szintet
    # --- trade flow ---
    "flowWindowSeconds": 3,
    "minFlowRatio": 1.6,               # buy/sell (vagy sell/buy) arany
}

TRADING_DEFAULTS = {
    "_id": "trading",
    "autoTradingEnabled": False,       # ALAPERTELMEZETTEN KIKAPCSOLVA
    "positionSizeUSDT": 20.0,          # notional (nem margin)
    "leverage": 5,
    "marginMode": "CROSSED",           # CROSSED | ISOLATED (EU-ban az ISOLATED nem elerheto)
    "takeProfitPct": 1.5,
    "stopLossPct": 0.8,
    "maxOpenPositions": 3,
    "longEnabled": True,
    "shortEnabled": True,
    "minScoreForTrade": 75,
}

TELEGRAM_DEFAULTS = {
    "_id": "telegram",
    "botToken": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "chatId": os.getenv("TELEGRAM_CHAT_ID", ""),
}


class ConfigStore:
    def __init__(self, db):
        self.db = db
        self.detector = dict(DETECTOR_DEFAULTS)
        self.reversal = dict(REVERSAL_DEFAULTS)
        self.trading = dict(TRADING_DEFAULTS)
        self.telegram = dict(TELEGRAM_DEFAULTS)

    async def load(self):
        for defaults, attr in (
            (DETECTOR_DEFAULTS, "detector"),
            (REVERSAL_DEFAULTS, "reversal"),
            (TRADING_DEFAULTS, "trading"),
            (TELEGRAM_DEFAULTS, "telegram"),
        ):
            doc = await self.db.config.find_one({"_id": defaults["_id"]})
            if doc is None:
                await self.db.config.insert_one(dict(defaults))
                doc = dict(defaults)
                log.info("Config letrehozva defaultokkal: %s", defaults["_id"])
            # hianyzo kulcsok kiegeszitese, hogy uj mezo ne torje el a futast
            merged = {**defaults, **doc}
            # ures ertekre az env meg mindig ervenyes -- kulonben a kesobb kitoltott
            # .env sose jutna ervenyre, mert a seed mar berakta az ures stringet
            merged = {k: (defaults[k] if v == "" and defaults.get(k) else v)
                      for k, v in merged.items()}
            setattr(self, attr, merged)
            self._warn_legacy(defaults, doc)

    _warned = set()
    LEGACY = {
        "priceChangeThreshold1s": "minSlopePctPerSec",
        "priceChangeThreshold3s": "minSlopePctPerSec",
        "priceChangeThreshold5s": "minSlopePctPerSec",
    }

    def _warn_legacy(self, defaults, doc):
        """Szoljunk, ha a DB-ben olyan kulcs van, amit mar senki nem olvas.

        Kulonben csendben lehet allitgatni egy erteket, aminek semmi hatasa."""
        for key, helyette in self.LEGACY.items():
            if key in doc and key not in defaults and key not in self._warned:
                self._warned.add(key)
                log.warning("A config.%s mar NEM hasznalt (regi ido-ablakos trigger). "
                            "Helyette: %s. Nyugodtan torolheto a DB-bol.",
                            key, helyette)

    async def refresh_loop(self, interval=30):
        while True:
            await asyncio.sleep(interval)
            try:
                await self.load()
            except Exception as e:
                log.warning("Config ujratoltes sikertelen: %s", e)
