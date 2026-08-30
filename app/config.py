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

    # ---- melyik parokat nezzuk egyaltalan ----
    "quoteAssets": ["USDT", "USDC"],
    "minQuoteVolume24h": 50_000_000,
    "maxSymbols": 200,
    "symbolRefreshMinutes": 60,
    "symbolWhitelist": [],             # ha nem ures, CSAK ezeket figyeljuk
    "symbolBlacklist": [],

    # ---- realtime kereskedhetoseg (a detektorok elott szur) ----
    "maxSpreadPct": 0.05,

    # ---- pump/dump: rendkivuli-e a mozgas EZEN a paron ----
    "moveWindowSec": 2.0,              # ekkora idoablakban merjuk az elmozdulast
    "minTradesInWindow": 10,           # ennyi kotes kell bele, kulonben nem merheto
    "baselineMinutes": 5,              # ennyi perc visszatekintessel epul a "normal"
    "baselineRatio": 4.0,              # a mozgas a par normaljanak ennyiszerese legyen
    "minMovePct": 0.15,                # abszolut padlo
    "symbolCooldownSec": 60,

    # ---- order book es EMA: CSAK INFORMACIO a jelzesben, semmit nem kapuznak ----
    "orderBookLevels": 20,
    "wallSensitivity": 3.0,
    "wallMaxDistancePct": 1.5,
    "emaFast": 9,
    "emaSlow": 21,
    "emaInterval": "1m",

    # ---- megjelenites ----
    "statusIntervalSec": 60,
    "signalWindowMinutes": 10,         # ennyi visszatekintessel: hanyadik jelzes ez
}

REVERSAL_DEFAULTS = {
    "_id": "reversal",
    "enabled": True,
    "cooldownSec": 120,

    # ---- mekkora elozetes mozgas utan keresunk fordulot ----
    "baselineRatio": 4.0,              # a par normaljanak ennyiszerese
    "minMovePct": 0.30,                # abszolut padlo

    # ---- az alakzat merete, MINDIG a mozgas aranyaban (0-100%) ----
    #
    #   csucs  ─────────────────────  100%
    #                                  25%    <- max belepo (maxRetracementPct)
    #                                  12%    <- ide kell visszapattannia
    #   melypont ───────────────────   0%     <- stop ez ala
    #
    "bounceOfMovePct": 12,
    "pullbackOfBouncePct": 30,         # a visszapattanasbol ennyi visszahuzas -> micro szint
    "breakOfMovePct": 5,               # az attores merete
    "maxRetracementPct": 25,           # ennel tobb mar ne jojjon vissza, amikor jelzunk
    "newExtremeOfMovePct": 2,

    # ---- idozites es kotesaramlas ----
    "windowSeconds": 20,
    "maxExtremeAgeSec": 8,
    "flowWindowSeconds": 3,
    "minFlowRatio": 1.6,
    "minTradesInFlowWindow": 5,
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
}

TELEGRAM_DEFAULTS = {
    "_id": "telegram",
    "botToken": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "chatId": os.getenv("TELEGRAM_CHAT_ID", ""),
    # Ha kulon csatornara akarod a ket detektort, ide irj chat ID-t.
    # Ures ertek eseten a fenti kozos chatId-re megy.
    "chatIds": {"pump_dump": "", "reversal": ""},
    # Extra link az uzenet aljara, {symbol} helyettesitessel. Sima szovegkent
    # kerul bele (nem kattinthato hivatkozaskent), mert a Telegram Bot API csak
    # http/https/tg semat fogad el <a href>-ben -- egy bnc:// anchor hibaval
    # elszallna. Sok kliens a sima szoveges semat is felismeri es atadja az appnak.
    # Pelda: "bnc://app.binance.com/futures/{symbol}"
    "appLinkTemplate": "",
}


class ConfigStore:
    def __init__(self, db):
        self.db = db
        self.detector = dict(DETECTOR_DEFAULTS)
        self.reversal = dict(REVERSAL_DEFAULTS)
        self.trading = dict(TRADING_DEFAULTS)
        self.telegram = dict(TELEGRAM_DEFAULTS)

    DOCS = (
        (DETECTOR_DEFAULTS, "detector"),
        (REVERSAL_DEFAULTS, "reversal"),
        (TRADING_DEFAULTS, "trading"),
        (TELEGRAM_DEFAULTS, "telegram"),
    )

    async def load(self):
        for defaults, attr in self.DOCS:
            doc = await self.db.config.find_one({"_id": defaults["_id"]})
            if doc is None:
                await self.db.config.insert_one(dict(defaults))
                doc = dict(defaults)
                log.info("Config letrehozva defaultokkal: %s", defaults["_id"])
            else:
                doc = await self._sync(defaults, doc)

            merged = {**defaults, **doc}
            # ures ertekre az env meg mindig ervenyes -- kulonben a kesobb kitoltott
            # .env sose jutna ervenyre, mert a seed mar berakta az ures stringet
            merged = {k: (defaults[k] if v == "" and defaults.get(k) else v)
                      for k, v in merged.items()}
            setattr(self, attr, merged)

    async def _sync(self, defaults, doc):
        """A DB dokumentum tukrozze a jelenlegi beallitas-keszletet.

        Enelkul egy uj beallitas sosem jelenne meg a DB-ben (csak a memoriaban
        letezne), a mar nem hasznalt regiek pedig bent maradnanak, es ugy nezne ki,
        mintha hatnanak valamire. A meglevo ERTEKEKHEZ nem nyulunk.
        """
        hianyzo = {k: v for k, v in defaults.items() if k not in doc}
        felesleges = [k for k in doc if k not in defaults and k != "_id"]
        if not hianyzo and not felesleges:
            return doc

        update = {}
        if hianyzo:
            update["$set"] = hianyzo
            log.info("Config '%s': %d uj beallitas felveve -> %s",
                     defaults["_id"], len(hianyzo), ", ".join(sorted(hianyzo)))
        if felesleges:
            update["$unset"] = {k: "" for k in felesleges}
            log.warning("Config '%s': %d mar nem hasznalt beallitas torolve -> %s",
                        defaults["_id"], len(felesleges), ", ".join(sorted(felesleges)))
        await self.db.config.update_one({"_id": defaults["_id"]}, update)

        doc = {k: v for k, v in doc.items() if k not in felesleges}
        doc.update(hianyzo)
        return doc

    async def refresh_loop(self, interval=30):
        while True:
            await asyncio.sleep(interval)
            try:
                await self.load()
            except Exception as e:
                log.warning("Config ujratoltes sikertelen: %s", e)
