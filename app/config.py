"""Konfiguracio MongoDB-bol. Ot dokumentum:

    market      KOZOS: melyik parokat figyeljuk egyaltalan (mindket detektorra hat)
    detector    CSAK a pump/dump detektor parameterei
    reversal    CSAK a fordulo detektor parameterei
    trading     a TradingService
    telegram    a Bot API es az uzenet


Elso indulaskor a default ertekek bekerulnek a DB-be, utana onnan olvasunk.
A configot 30 masodpercenkent ujratoltjuk, igy DB-ben modositva menet kozben hat.
"""
import os
import asyncio
import logging

log = logging.getLogger("config")

# KOZOS beallitasok: ezek dontik el, melyik parokra iratkozunk fel egyaltalan --
# onnantol MINDKET detektor pontosan ugyanazt a kotesfolyamot kapja. Ezert nincs
# kulon peldanyuk a detektoroknal: egy helyen allitod, mindenhol hat.
MARKET_DEFAULTS = {
    "_id": "market",
    "enabled": True,                   # az egesz feldolgozas ki-/bekapcsolasa

    # ---- melyik parokat nezzuk egyaltalan ----
    "quoteAssets": ["USDT", "USDC"],
    "minQuoteVolume24h": 120_000_000,
    "maxSymbols": 60,
    "symbolRefreshMinutes": 60,
    "symbolWhitelist": [],             # ha nem ures, CSAK ezeket figyeljuk
    "symbolBlacklist": [],

    # ---- realtime kereskedhetoseg (a jelzes kiadasanal szur) ----
    "maxSpreadPct": 0.05,

    # ---- eredmenymeres: a jelzes UTAN ennyi perccel jegyezzuk fel az arat ----
    # Nem kapuz semmit, nem backteszt. Ebbol derul ki, tartos-e egy jelzes.
    "outcomeMinutes": [1, 5, 15],

    # ---- megjelenites ----
    "statusIntervalSec": 60,
}

DETECTOR_DEFAULTS = {
    "_id": "detector",
    "enabled": True,                   # CSAK a pump/dump detektor

    # ---- pump/dump: rendkivuli-e a mozgas EZEN a paron ----
    "moveWindowSec": 2.0,              # ekkora idoablakban merjuk az elmozdulast
    "minTradesInWindow": 10,           # ennyi kotes kell bele, kulonben nem merheto
    "baselineMinutes": 5,              # ennyi perc visszatekintessel epul a "normal"
    "baselineRatio": 8.0,              # a mozgas a par normaljanak ennyiszerese legyen
    "minMovePct": 0.80,                # abszolut padlo
    "maxSingleStepPct": 35,            # ennel nagyobb reszt egyetlen arlepes ne adjon
    "confirmSec": 60.0,                # ennyi ideig VEGIG tartania kell a mozgasnak
    "confirmHoldPct": 80,              # es a mozgas ennyi szazaleka legyen meg
    "symbolCooldownSec": 900,

    # ---- order book es EMA: CSAK INFORMACIO a jelzesben, semmit nem kapuznak ----
    "orderBookLevels": 20,
    "wallSensitivity": 3.0,
    "wallMaxDistancePct": 1.5,
    "emaFast": 9,
    "emaSlow": 21,
    "emaInterval": "1m",
}

REVERSAL_DEFAULTS = {
    "_id": "reversal",
    "enabled": True,
    "cooldownSec": 1800,

    # ---- mekkora elozetes mozgas utan keresunk fordulot ----
    "baselineRatio": 8.0,              # a par normaljanak ennyiszerese
    "minMovePct": 2.00,                # abszolut padlo
    "wickSliceSec": 0.5,               # ekkora szeletek kozeparan keressuk a szelsoerteket

    # ---- az alakzat merete, MINDIG a mozgas aranyaban (0-100%) ----
    #
    #   csucs  ─────────────────────  100%
    #                                  25%    <- max belepo (maxRetracementPct)
    #                                  12%    <- ide kell visszapattannia
    #   melypont ───────────────────   0%     <- stop ez ala
    #
    "bounceOfMovePct": 12,
    "pullbackOfBouncePct": 30,         # a visszapattanasbol ennyi visszahuzas -> micro szint
    "breakOfMovePct": 5,               # az attores merete (bounce + break < maxRetracement!)
    "maxRetracementPct": 25,           # ennel tobb mar ne jojjon vissza, amikor jelzunk
    "newExtremeOfMovePct": 2,

    # ---- idozites es kotesaramlas ----
    "windowSeconds": 20,
    "maxExtremeAgeSec": 6,
    "confirmSec": 30.0,                # ennyi ideig VEGIG tartania kell az attoresnek
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
    "enabled": True,                   # ha false: log + DB igen, Telegram nem
    "signalWindowMinutes": 10,         # ennyi visszatekintessel: hanyadik jelzes ez
    "statusEveryMinutes": 20,          # idoszakos eletjel Telegramra (0 = nincs)
    "statusRecentSignals": 5,          # ennyi legutobbi jelzes eredmenye az eletjelben
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
        self.market = dict(MARKET_DEFAULTS)
        self.detector = dict(DETECTOR_DEFAULTS)
        self.reversal = dict(REVERSAL_DEFAULTS)
        self.trading = dict(TRADING_DEFAULTS)
        self.telegram = dict(TELEGRAM_DEFAULTS)

    DOCS = (
        (MARKET_DEFAULTS, "market"),
        (DETECTOR_DEFAULTS, "detector"),
        (REVERSAL_DEFAULTS, "reversal"),
        (TRADING_DEFAULTS, "trading"),
        (TELEGRAM_DEFAULTS, "telegram"),
    )

    # Regen a 'detector' dokumentum tartalmazta a kozos beallitasokat is. Ezeket
    # atkoltoztetjuk oda, ahova valok -- a MAR BEALLITOTT ERTEKEIDDEL egyutt,
    # kulonben a szetvalasztas csendben visszaallitana mindent alapertelmezettre.
    KOLTOZES = (
        ("detector", "market", {k: k for k in (
            "quoteAssets", "minQuoteVolume24h", "maxSymbols", "symbolRefreshMinutes",
            "symbolWhitelist", "symbolBlacklist", "maxSpreadPct", "statusIntervalSec")}),
        ("detector", "telegram", {"telegramEnabled": "enabled",
                                  "signalWindowMinutes": "signalWindowMinutes"}),
    )

    async def _migrate(self):
        for honnan, hova, kulcsok in self.KOLTOZES:
            forras = await self.db.config.find_one({"_id": honnan})
            if not forras:
                continue
            atveendo = {uj: forras[regi] for regi, uj in kulcsok.items()
                        if regi in forras}
            if not atveendo:
                continue
            cel = await self.db.config.find_one({"_id": hova}) or {}
            # amit a celdokumentum mar tartalmaz, azt nem irjuk felul
            atveendo = {k: v for k, v in atveendo.items() if k not in cel}
            if not atveendo:
                continue
            await self.db.config.update_one({"_id": hova}, {"$set": atveendo},
                                            upsert=True)
            log.warning("Config koltoztetes '%s' -> '%s': %s (a te ertekeiddel)",
                        honnan, hova, ", ".join(sorted(atveendo)))

    async def load(self):
        await self._migrate()
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
