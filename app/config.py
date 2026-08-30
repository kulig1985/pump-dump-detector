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
    "telegramMode": "auto",            # auto | always | never  (lasd arnyek mod)
    # --- symbol szures ---
    "quoteAssets": ["USDT", "USDC"],   # melyik elszamolo devizas perpetualokat nezzuk
    "minQuoteVolume24h": 50_000_000,   # 24h forgalom minimum (az elszamolo devizaban)
    "maxSymbols": 200,                 # top N forgalom szerint
    "excludeSymbols": [],              # pl. ["1000PEPEUSDT", "TRUMPUSDT"] -- ezeket kihagyjuk
    "symbolRefreshMinutes": 60,
    # --- trigger: az utolso par masodperc trade-jeire illesztett egyenes ---
    #
    # IDOALAPU ablak, nem darabszam alapu. Egy nagy paron 30 aggTrade akar 30
    # milliszekundum alatt is beerkezhet: ilyenkor egy apro arvaltozast egy apro
    # idotartammal osztva hatalmas hamis "meredekseg" jon ki, a legnagyobb parokon
    # pedig (azonos idobelyegu trade-ek miatt) egyaltalan nem lehetne merni.
    "slopeWindowSec": 2.0,             # ekkora idoablakra illesztjuk az egyenest
    "minTradesInWindow": 10,           # ennyi trade kell bele, kulonben nem merheto
    "minTotalMovePct": 0.15,           # az ablakban ekkora nettó elmozdulas kell
    "minSlopePctPerSec": 0.15,         # ennyi %/masodperc kell a jelzeshez
    "minConsistency": 0.70,            # a lepesek ekkora hanyada mutasson egy iranyba
    "minVolumeFactor": 1.0,            # az ablakban legalabb ennyiszer annyi forgalom
                                       # legyen, mint a par atlaga ugyanennyi ido alatt
    "minMoveToSpreadRatio": 3.0,       # a mozgas legyen legalabb ennyiszer a spread --
                                       # kulonben nem mozgas, csak a spread atlepese
    "volatilityMultiplier": 4.0,       # 0 = ki. A meredekseg-kuszob sose megy a fenti
                                       # ertek ala, de zajos parokon feljebb megy
    "maxThresholdFactor": 10,          # a volatilitashoz igazitott kuszob legfeljebb
                                       # ennyiszerese lehet az alapertelmezettnek
    # --- signal ---
    "minSignalScore": 60,
    "symbolCooldownSec": 60,
    "statusIntervalSec": 5,           # ilyen surun irja ki, mi tortenik az arakkal
    "signalWindowMinutes": 10,        # ennyi idore visszamenoleg szamoljuk a jelzeseket
    # --- mozgasminoseg: a szaggatott parokra nem lehet jelezni ---
    "maxTickNoisePct": 0.08,          # ha EGY kotes atlagosan ennel tobbet mozdit az
                                      # aron, a par nem kereskedheto (0 = kikapcsolva).
                                      # BTC/ETH: 0.000x%  normal alt: 0.00x-0.0x%
                                      # ossze-vissza ugralo meme: 0.1% folott
    # --- kereskedelmi terv ---
    "stopBufferPct": 0.05,            # a stop ennyivel kerul a horgony tuloldalara
    "momentumStopRetracementPct": 50, # lendulet: a stop az impulzus ennyi %-anal.
                                      # Ha az impulzus felet visszaadja, a tezis halott --
                                      # nem kell megvarni, hogy a teljes mozgas visszajojjon.
    "momentumTargetFactor": 1.0,      # a cel az impulzussal azonos meretu folytatas
    "minRewardRisk": 1.5,             # ez alatt "gyenge aranyu"-nak jeloljuk a jelzest
    # --- eredmenymeres: mi tortent a jelzes utan ---
    "outcomeMinutes": 5,              # ennyi ideig kovetjuk az arat a jelzes utan
    "outcomeTargetPct": 0.3,          # ezt elerve szamit jonak a jelzes
    "outcomeStopPct": 0.3,            # ezt elerve szamit rossznak
    # --- arnyek mod: Telegram csak bizonyitott score savokra ---
    "shadowMinSamples": 50,           # ennyi lemert jelzes kell egy savhoz
    "shadowMinHitRate": 0.55,         # es ekkora talalati arany
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
    "telegramMode": "auto",            # auto | always | never
    "minSignalScore": 60,              # sajat kuszob, fuggetlen a pump/dump-etol
    "cooldownSec": 120,
    # --- rolling trade ablak ---
    "windowSeconds": 20,
    "minTradesInFlowWindow": 5,
    "maxSetupAgeSec": 20,              # ennyi ido utan elavul egy alakzat
    # --- alakzat ---
    "minMovePct": 0.40,                # a fordulo elotti mozgas merteke, szazalekban
    # Az alakzat tobbi merete a MOZGAS ARANYABAN ertendo (0-100%), nem abszolut
    # szazalekban -- igy egy 0.5%-os es egy 3%-os mozgasnal ugyanaz a logika mukodik.
    #
    #   csucs  ─────────────────────────────  100%
    #                                          61.8%  <- cel (targetRetracementPct)
    #                                          25%    <- max belepo (maxRetracementPct)
    #                                          12%    <- ide kell visszapattannia
    #   melypont ───────────────────────────  0%      <- stop ez ala
    #
    "bounceOfMovePct": 12,             # ennyit kell visszapattannia a mozgasbol
    "pullbackOfBouncePct": 30,         # a visszapattanas ennyit huzzon vissza -> micro szint
    "breakOfMovePct": 5,               # az attores legyen a mozgas ennyi %-a
    "maxRetracementPct": 25,           # EZ A LENYEG: ennel tobb mar ne jojjon vissza,
                                       # kulonben a kereskedheto resz elfogyott
    "targetRetracementPct": 61.8,      # a cel a mozgas ennyi %-anal van
    "maxExtremeAgeSec": 8,             # a szelsoertek ennel frissebb legyen
    "newExtremeOfMovePct": 2,          # ennyivel melyebb minimum mar uj alakzatot indit
    # --- trade flow ---
    "flowWindowSeconds": 3,
    "minFlowRatio": 1.6,               # buy/sell (vagy sell/buy) arany
    "minFlowVolumeFactor": 1.0,        # a flow ablakban legalabb ennyiszer annyi forgalom
                                       # legyen, mint a par atlaga ugyanennyi ido alatt.
                                       # Enelkul par szaz USDT-bol is kijon egy 1.9x arany.
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
    # Ha kulon csatornara akarod a ket detektort, ide irj chat ID-t.
    # Ures ertek eseten a fenti kozos chatId-re megy.
    "chatIds": {"pump_dump": "", "reversal": ""},
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
