# Paraméterek

Minden beállítás a MongoDB `config` collectionjében van, négy dokumentumban:
`detector`, `reversal`, `trading`, `telegram`. Módosítás után **30 másodpercen belül él**,
újraindítás nélkül. A dokumentumok magukat tartják karban: az új beállítások bekerülnek,
a már nem használtak törlődnek.

```js
use pump-dump
db.config.find().pretty()
db.config.updateOne({_id:"detector"}, {$set:{baselineRatio: 6}})
```

---

## Az elv: a pár saját normáljához mérünk

Nem az a kérdés, hogy „mozdult-e 0.3%-ot", hanem hogy **szokatlan-e ez a mozgás ezen a
páron**. Egy meme coinon 0.3% másodpercenként történik, a BTC-n hetente. Fix küszöbbel ez
nem kezelhető — ezért a rendszer futás közben, páronként méri, mi a normális.

```
  baseline = az utolso baselineMinutes percben mert 2 masodperces
             |elmozdulasok| MEDIANJA   (masodpercenkent egy minta)

  jelzeshez kell:  |mozgas|  >=  max( minMovePct ,  baselineRatio × baseline )
```

A medián azért jó, mert egyetlen kiugró érték nem viszi el. Amíg nincs elég minta
(legalább egy perc), az abszolút `minMovePct` padló dönt.

---

## A pipeline

```
Binance WebSocket  (aggTrade  +  !bookTicker)
        ↓
  KERESKEDHETOSÉG      spread / mélység / aktivitás / white- és blacklist
        ↓              ha nem felel meg, a detektorokig el sem jut
  DetectorManager  →  PumpDumpDetector,  ReversalDetector      → CANDIDATE
        ↓
  VALIDÁCIÓ            spread vs mozgás, fal az útban, hozam/kockázat
        ↓
   SIGNAL   vagy   REJECTED       (mindkettő okkal, mindkettő MongoDB-be)
        ↓
  MongoDB → Telegram → [TradingService]
```

Minden elutasításnak gépi neve van, hogy aggregálható legyen:

| ok | mit jelent |
|---|---|
| `blacklisted` / `not_whitelisted` | kézi kizárás |
| `no_book_data` | még nem láttuk a pár order book tetejét |
| `spread_too_wide` | a spread szélesebb, mint `maxSpreadPct` |
| `insufficient_depth` | a legjobb szinten kevesebb pénz van, mint `minTopDepthUSDT` |
| `low_activity` | kevesebb kötés percenként, mint `minTradesPerMinute` |
| `wall_immediately_ahead` | fal a mozgás irányában `wallBlockDistancePct`-en belül |
| `poor_reward_risk` | a hozam/kockázat `minRewardRisk` alatt |
| `no_usable_plan` | nem számítható értelmes belépő/cél/stop |

```js
// mi miert esett ki?
db.signals.aggregate([{$match:{status:"rejected"}},
                      {$group:{_id:"$reasons", db:{$sum:1}}}, {$sort:{db:-1}}])
```

---

## `detector`

### Melyik párokat nézzük

| kulcs | alap | mit csinál |
|---|---|---|
| `quoteAssets` | `["USDT","USDC"]` | melyik elszámoló devizás perpetualokat |
| `minQuoteVolume24h` | 50 000 000 | ez alatti 24h forgalom kizárva |
| `maxSymbols` | 200 | top N forgalom szerint |
| `symbolWhitelist` | `[]` | ha nem üres, **csak** ezeket figyeljük |
| `symbolBlacklist` | `[]` | névre kizárás |
| `symbolRefreshMinutes` | 60 | ilyen sűrűn építjük újra a listát |

### Realtime kereskedhetőség — a detektorok előtt szűr

Az adat egyetlen `!bookTicker` feliratkozásból jön, ami az egész piac legjobb bid/ask
árát és mennyiségét adja — **külön WebSocket kapcsolaton**. Az aggTrade a `market`, a
bookTicker a `public` csoportba tartozik, és ez az URL szegmensben is megjelenik:
`/market/stream` illetve `/public/stream`. Rossz szegmensen a Binance nyugtázza a
feliratkozást, de **nem küld adatot**.

Ha egyáltalán nem érkezik könyv-adat, a rendszer nem némul el: a spread/mélység szűrés
kikapcsol, és a `STATUS` sor kiírja, hogy `KONYV-ADAT NEM ERKEZIK`.

| kulcs | alap | mit csinál | ha növeled |
|---|---|---|---|
| `maxSpreadPct` | 0.05 | ennél szélesebb spreadnél a be- és kiszállás felemészti a mozgást | több pár fér be |
| `minTopDepthUSDT` | 5 000 | a legjobb szinten ennyi pénz legyen. **Ez egyetlen árszint, nem a teljes könyv:** BTC ~150e, SOL ~31e, egy 50M-os alt ~2e USDT | csak a vastag könyvű párok |
| `minTradesPerMinute` | 30 | ritka kereskedésnél nincs mit megfogni | csak az aktív párok |

### Pump/dump

| kulcs | alap | mit csinál | ha növeled |
|---|---|---|---|
| `moveWindowSec` | 2.0 | ekkora **időablakban** mérjük az elmozdulást | lassabb, simább mozgásokat lát |
| `minTradesInWindow` | 10 | ennyi kötés kell bele | csak a forgalmasabb párok |
| `baselineMinutes` | 5 | ennyi perc visszatekintéssel épül a „normál" | stabilabb, lassabban alkalmazkodó normál |
| `baselineRatio` | 4.0 | **a fő kapcsoló:** a mozgás a normál ennyiszerese legyen | kevesebb, de rendkívülibb jelzés |
| `minMovePct` | 0.15 | abszolút padló, hogy halott páron se jelezzünk | |
| `minConsistency` | 0.70 | a lépések ekkora hányada mutasson egy irányba | csak a tiszta mozgások |
| `minVolumeFactor` | 1.0 | az ablak forgalma a pár átlagának ennyiszerese | valódi pénz kell mögé |
| `symbolCooldownSec` | 60 | páronként ennyi szünet | |

A mozgást nem az első és utolsó ár különbsége adja, hanem az ablakra **illesztett egyenes
elmozdulása** — így egyetlen kiugró print nem tud jelzést csinálni.

### Validáció

| kulcs | alap | mit csinál |
|---|---|---|
| `minMoveToSpreadRatio` | 3.0 | a mozgás legyen legalább ennyiszer a spread |
| `wallBlockDistancePct` | 0.15 | ennél közelebbi fal a mozgás irányában elutasít |
| `minRewardRisk` | 1.5 | ez alatt nem éri meg felvenni |
| `stopBufferOfDistancePct` | 10 | a stop ennyivel kerül a horgony mögé, a belépő–horgony **távolság arányában** |
| `momentumStopRetracementPct` | 50 | lendületnél a stop az impulzus felénél: ha a mozgás felét visszaadja, a tézis halott |
| `momentumTargetFactor` | 1.0 | a cél azonos méretű folytatás (mért mozgás) |

### Információ, nem kapu

`orderBookLevels` (20), `wallSensitivity` (3.0), `wallMaxDistancePct` (1.5) — a wall
detektáláshoz. `emaFast`/`emaSlow`/`emaInterval` (9/21/1m) — az EMA **csak a Telegram
üzenetben jelenik meg**, semmit nem kapuz.

### Eredménymérés és megjelenítés

| kulcs | alap | mit csinál |
|---|---|---|
| `outcomeEnabled` | `false` | **alapból kikapcsolva** — első körben nem mérünk |
| `outcomeMinutes` | 5 | ennyi ideig méri az árat a jelzés után (ha bekapcsolod) |
| `outcomeTargetPct` / `outcomeStopPct` | 0.3 / 0.3 | mikor számít jónak, illetve rossznak |
| `statusIntervalSec` | 60 | ilyen sűrűn egy rövid `STATUS` sor |
| `signalWindowMinutes` | 10 | ekkora visszatekintéssel számolja, hányadik a jelzés |
| `telegramEnabled` | `true` | **minden `SIGNAL` azonnal megy** — nincs más kapu előtte |

Az eredménymérés **alapból ki van kapcsolva**. Nem backteszt (a jelzés *után* nézi az
árat), de első körben csak zajt tenne a logba. Bekapcsolva 10 percenként egy `EREDMENYEK`
tábla mutatja, merre ment az ár a jelzések után — de akkor sem kapuz semmit:

```js
db.config.updateOne({_id:"detector"}, {$set:{outcomeEnabled:true}})
```

---

## `reversal`

Az eseménysor: lemozgás → lokális szélsőérték → visszapattanás → nincs új szélsőérték →
kötésáramlás fordul → micro-szint áttörés.

Az **előzetes mozgás nagyságát a baseline dönti el** (mint a pump/dump-nál), az alakzat
méretei pedig **a mozgás arányában** (0–100%) értendők:

```
   csucs ────────────────────────────────  100%   ← innen indult
         ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─      61.8%  ← CÉL
         ════════════════════════════        25%   ← MEDDIG SZABAD BELÉPNI
              ╱╲    ╱  ← áttörés = belépő    17%
       micro ╱──╳──╱                         12%   ← ide kell visszapattannia
            ╱    ╲  ← visszahúzás
   melypont ──────────────────────────────    0%   ← stop ez alá
```

| kulcs | alap | mit csinál |
|---|---|---|
| `baselineRatio` / `minMovePct` | 4.0 / 0.30 | mekkora előzetes mozgás után keresünk fordulót. A normál a mozgás **tényleges hosszára** skálázódik (bolyongásnál az elmozdulás az idő gyökével nő), így egy 20 mp-es kúszás nem számít rendkívülinek |
| `bounceOfMovePct` | 12 | ennyit kell visszapattannia a mozgásból |
| `pullbackOfBouncePct` | 30 | a visszapattanásból ennyi visszahúzás rögzíti a micro szintet |
| `breakOfMovePct` | 5 | az áttörés mérete |
| **`maxRetracementPct`** | **25** | **a legfontosabb:** ennél többet ne jöjjön vissza az ár, amikor jelzünk — efölött a kereskedhető rész elfogyott |
| `targetRetracementPct` | 61.8 | a cél a mozgás ennyi %-ánál |
| `maxExtremeAgeSec` | 8 | a szélsőérték ennél frissebb legyen |
| `newExtremeOfMovePct` | 2 | ennyivel mélyebb minimum indít új alakzatot |
| `windowSeconds` | 20 | a rolling trade ablak |
| `flowWindowSeconds` / `minFlowRatio` / `minTradesInFlowWindow` | 3 / 1.6 / 5 | a kötésáramlás fordulása |
| `cooldownSec` | 120 | páronkénti szünet |

---

## `trading` és `telegram`

Az auto trading **alapból kikapcsolva** (`autoTradingEnabled: false`), lásd a README-t.

A `telegram` dokumentum:

| kulcs | mit csinál |
|---|---|
| `botToken` / `chatId` | a bot és a cél chat |
| `chatIds` | detektoronként külön csatorna, pl. `{"pump_dump":"-100…","reversal":"-100…"}` |
| `appLinkTemplate` | extra link az üzenet aljára, `{symbol}` helyettesítéssel |

### Megnyitás a Binance appban

A `https://www.binance.com/en/futures/BTCUSDT` **már universal link**: telefonon, ha fent
van az app, az OS elvileg annak adja át. A tényleges akadály az, hogy **a Telegram a saját
beépített böngészőjében nyitja meg**, így az OS nem is jut szóhoz.

A megbízható megoldás a Telegramban: **Beállítások → Adatok és tárhely → „Beépített
böngésző" kikapcsolása** (vagy hosszan nyomni a linket → *Megnyitás böngészőben*).

Ha séma-alapú linket akarsz kipróbálni, az `appLinkTemplate`-be írhatod. **Sima
szövegként** kerül az üzenetbe, nem kattintható hivatkozásként — a Telegram Bot API csak
`http`, `https` és `tg` sémát fogad el `<a href>`-ben, egy `bnc://` anchor
`Bad Request: unsupported URL protocol` hibával elszállna. Sok kliens a szöveges sémát is
felismeri és átadja az appnak.

```js
db.config.updateOne({_id:"telegram"},
                    {$set:{appLinkTemplate:"bnc://app.binance.com/futures/{symbol}"}})
```

> A pontos Binance séma-formátumot nem tudom hitelesen megerősíteni, ezért nem is
> égettem be — próbáld ki a telefonodon, és ha találsz működőt, ez a mező várja.

---

## Mit mutat a STATUS sor

```
STATUS  39 par | 14,113 tick/60s | konyv: 752 par | 0 candidate, 0 jelzes, 0 elutasitva | Telegram: BE
   kizarva 35: insufficient_depth 30, spread_too_wide 5
   melyseg  p10        900  p50      3,200  p90     48,000 USDT   kuszob 5,000  -> 12 par alatta
   spread   p10      0.004%  p50      0.011%  p90      0.048%   kuszob 0.050%  -> 2 par felette
   baseline kesz: 31/39 par | legkozelebbi jelolt: SOLUSDT 1.8x normal (kell 4.0x)
```

A percentilis sorokból **adatból** állítható a küszöb, nem vaktában: látod az eloszlást,
a jelenlegi küszöböt, és hogy hány pár esik kívül. Az utolsó sor megmondja, hogy a
detektor egyáltalán mennyire van közel jelzéshez.

## Hangolási sorrend

1. **Előbb a kereskedhetőség.** A `STATUS` sor kiírja, hány pár esik ki és miért. Ha túl
   sok, lazíts a `maxSpreadPct` / `minTopDepthUSDT` értéken.
2. **Aztán a `baselineRatio`.** Ez a fő érzékenység-kapcsoló: 4.0 → kevesebb és
   rendkívülibb, 3.0 → több.
3. **Végül a validáció.** A Mongo aggregációból látod, melyik ok dominál a
   `REJECTED`-ek között.
4. Egyszerre csak egyet állíts, hogy tudd, mi okozta a változást.
