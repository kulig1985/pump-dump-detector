"""Konfiguracio MongoDB-bol. Ot dokumentum:

    market      KOZOS: melyik parokat figyeljuk, es hogyan merjuk az eredmenyt
    detector    a scalp detektor (impulzus + setup) parameterei
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

    # ---- eredmenymeres: a jelzes UTAN folyamatosan kovetjuk az arat ----
    # Nem kapuz semmit, nem backteszt. Ebbol derul ki, melyik setup mukodik.
    "outcomeTrackSec": 600,            # ennyi ideig kovetunk minden jelzest
    "tpLevels": [0.3, 0.5, 0.8, 1.0],  # ezeket a TP szinteket merjuk (%)
    "slLevels": [0.2, 0.3, 0.5],       # es ezeket a SL szinteket (%)
    "reportTp": 0.5,                   # az osszesitesben ez a TP/SL par szerepel
    "reportSl": 0.3,

    # ---- megjelenites ----
    "statusIntervalSec": 60,
}

# ==========================================================================
#  A SCALP DETEKTOR PARAMETEREI
#
#  MINDEN ERTEK ITT KIINDULASI PARAMETER. Nem "helyes" ertekek: ezek olyan
#  kezdopontok, amelyeket az outcome meres (MFE/MAE, TP/SL) adataibol kell
#  hangolni. A hangolas a KODBAN tortenik, nem a DB-ben -- lasd docs/PARAMETEREK.md.
# ==========================================================================
DETECTOR_DEFAULTS = {
    "_id": "detector",
    "enabled": True,

    # ---- 1. IMPULZUS: rendkivuli-e a mozgas ES a mogotte allo penz ----
    "impulseWindowSec": 3.0,           # ekkora idoablakban merunk
    "minTradesInWindow": 10,           # ennyi kotes kell bele, kulonben nem merheto
    "baselineMinutes": 5,              # ennyi perc visszatekintessel epul a "normal"
    "minImpulsePct": 0.40,             # abszolut padlo a mozgasra
    "impulseBaselineRatio": 6.0,       # es a par sajat normaljanak ennyiszerese
    "minImpulseNotional": 50_000,      # abszolut padlo az agressziv forgalomra (USDT)
    "notionalRatio": 3.0,              # es a par normal ablak-forgalmanak ennyiszerese
    "minImpulseImbalance": 0.25,       # a taker oldal ennyire legyen egyiranyu (0-1)
    "maxSingleStepPct": 35,            # egyetlen arlepes ne adja a mozgas tobbet

    # ---- 2. SETUP: az impulzus utani szerkezet kovetese ----
    "setupTimeoutSec": 90,             # ennyi ido utan eldobjuk a setupot
    "invalidateBeyondOriginPct": 20,   # ha az ar ennyivel az impulzus ala megy, vege
    "flowWindowSec": 5.0,              # a megerosito kotesaramlas ablaka

    # ---- 3a. FOLYTATAS: sekely visszahuzas, majd a pivot ujratorese ----
    "minPullbackPct": 15,              # ennyi visszahuzas kell (a lab %-aban)
    "maxPullbackPct": 62,              # ennel melyebb visszahuzas utan mar nem folytatas
    "breakoutOfLegPct": 5,             # ekkora attores kell a pivot folott
    "minConfirmImbalance": 0.15,       # es ennyi kotesaramlas a belepo iranyaba

    # ---- 3b. FORDULO: kifulladas, majd a counter szint letorese ----
    "exhaustionSec": 10.0,             # ennyi ideje nincs uj szelsoertek
    "minReversalImbalance": 0.20,      # a kotesaramlas ennyire fordult meg
    "counterPullbackPct": 30,          # ennyi ellen-visszahuzas rogziti a fordulo szintjet
    "reclaimOfLegPct": 5,              # ekkora attores kell a counter szinten
    "reclaimHoldSec": 3.0,             # es ennyi ideig tartania is kell
    "maxEntryRetracePct": 50,          # ennel tobb mar ne jojjon vissza a belepoig

    # ---- 4. KONYV es TREND: ezek BEFOLYASOLJAK a dontest ----
    "maxOpposingBookImbalance": 0.40,  # ennyi ellentetes konyv-tulsuly meg elfogadhato
    "wallBlockDistPct": 0.15,          # ilyen kozeli fal a mozgas iranyaban -> nincs jelzes
    "depthLevels": 20,                 # a partial book depth stream szintjei (5/10/20)
    "depthUpdateSpeed": "500ms",       # frissitesi sebesseg (100ms/500ms)
    "wallSensitivity": 3.0,            # fal = a tobbi szint medianjanak ennyiszerese
    "wallMaxDistancePct": 1.5,         # ennel tavolabbi falat figyelmen kivul hagyunk
    "requireTrendForContinuation": True,   # a folytatas egyezzen az EMA iranyaval
    "requireTrendForReversal": False,      # a fordulo szandekosan szembe megy
    "emaFast": 9,
    "emaSlow": 21,
    "emaInterval": "1m",
    "emaRefreshSec": 60,               # ennyi idonkent frissul minden par EMA-ja

    # ---- 5. KIMENET ----
    "symbolCooldownSec": 600,          # paronkent ennyi szunet ket jelzes kozott
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
    "statusEveryMinutes": 20,          # idoszakos eletjel Telegramra (0 = nincs)
    "statusRecentSignals": 3,          # TIPUSONKENT ennyi legutobbi jelzes az eletjelben
    "botToken": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "chatId": os.getenv("TELEGRAM_CHAT_ID", ""),
    # Ha kulon csatornara akarod a folytatas- es a fordulo-jelzeseket, ide irj
    # chat ID-t. Ures ertek eseten a fenti kozos chatId-re megy.
    "chatIds": {"scalp": ""},
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
        self.trading = dict(TRADING_DEFAULTS)
        self.telegram = dict(TELEGRAM_DEFAULTS)

    DOCS = (
        (MARKET_DEFAULTS, "market"),
        (DETECTOR_DEFAULTS, "detector"),
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
        ("detector", "telegram", {"telegramEnabled": "enabled"}),
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
