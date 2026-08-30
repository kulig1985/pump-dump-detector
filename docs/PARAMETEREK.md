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
  KERESKEDHETOSÉG      spread / white- és blacklist
        ↓              ha nem felel meg, a detektorokig el sem jut
  DetectorManager  →  PumpDumpDetector,  ReversalDetector      → CANDIDATE
        ↓
   SIGNAL   (order book és EMA információként hozzáfűzve)
        ↓
  MongoDB → Telegram → [TradingService]
```

Egy pár akkor esik ki, ha nem kereskedhető. Az elutasításnak **gépi neve** van (ez megy
a MongoDB-be, hogy aggregálható legyen) és **magyar szövege** (ez megy a logba):

| ok | mit jelent |
|---|---|
| `blacklisted` / `not_whitelisted` | kézi kizárás |
| `no_book_data` | még nem láttuk a pár order book tetejét |
| `spread_too_wide` | a spread szélesebb, mint `maxSpreadPct` |

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

Ha egyáltalán nem érkezik könyv-adat, a rendszer nem némul el: a spread szűrés
kikapcsol, és a `STATUS` sor kiírja, hogy `KONYV-ADAT NEM ERKEZIK`.

| kulcs | alap | mit csinál | ha növeled |
|---|---|---|---|
| `maxSpreadPct` | 0.05 | ennél szélesebb spreadnél a be- és kiszállás felemészti a mozgást | több pár fér be |

### Pump/dump

| kulcs | alap | mit csinál | ha növeled |
|---|---|---|---|
| `moveWindowSec` | 2.0 | ekkora **időablakban** mérjük az elmozdulást | lassabb, simább mozgásokat lát |
| `minTradesInWindow` | 10 | ennyi kötés kell bele | csak a forgalmasabb párok |
| `baselineMinutes` | 5 | ennyi perc visszatekintéssel épül a „normál" | stabilabb, lassabban alkalmazkodó normál |
| `baselineRatio` | 4.0 | **a fő kapcsoló:** a mozgás a normál ennyiszerese legyen | kevesebb, de rendkívülibb jelzés |
| `minMovePct` | 0.15 | abszolút padló, hogy halott páron se jelezzünk | |
| `maxSingleStepPct` | 50 | ha a mozgás ennél nagyobb részét **egyetlen árlépés** adta, nem jelzés: egy nagy kötés átsöpörte a könyvet, a többi kötés már az új áron nyomtat, és az ablak szép egyenletes mozgásnak látszik | több egy-kötéses ugrás fér be |
| `confirmSec` | 3.0 | a jelzés nem a mozgás pillanatában megy ki: ennyivel később megnézzük, megvan-e még | később, de biztosabban jelez |
| `confirmHoldPct` | 60 | és a látott mozgás ennyi százaléka legyen még meg. Ez választja el a valódi elindulást a pillanatnyi korrekciótól | szigorúbb: csak az marad, ami tényleg ottmarad |
| `symbolCooldownSec` | 60 | páronként ennyi szünet | |

A mozgást nem az első és utolsó ár különbsége adja, hanem az ablakra **illesztett egyenes
elmozdulása** — így egyetlen kiugró print nem tud jelzést csinálni.



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
| `wickSliceSec` | 0.5 | a szélsőértéket **nem** a nyers min/max adja, hanem ekkora szeletek középára. Enélkül egyetlen pillanat alatt beérkező pár print (egy nagy kötés, ami átsöpri a könyvet, majd az ár azonnal visszaáll) lett a „mozgás" kezdőpontja |
| `bounceOfMovePct` | 12 | ennyit kell visszapattannia a mozgásból |
| `pullbackOfBouncePct` | 30 | a visszapattanásból ennyi visszahúzás rögzíti a micro szintet |
| `breakOfMovePct` | 5 | az áttörés mérete |
| **`maxRetracementPct`** | **25** | **a legfontosabb:** ennél többet ne jöjjön vissza az ár, amikor jelzünk — efölött a kereskedhető rész elfogyott |
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
   kizarva 5: tul szeles a spread: 5
   spread   p10      0.004%  p50      0.011%  p90      0.048%   kuszob 0.050%  -> 2 par felette
   baseline kesz: 31/39 par | legkozelebbi jelolt: SOLUSDT 1.8x normal (kell 4.0x)
```

A percentilis sorból **adatból** állítható a küszöb, nem vaktában: látod az eloszlást,
a jelenlegi küszöböt, és hogy hány pár esik kívül. Az utolsó sor megmondja, hogy a
detektor egyáltalán mennyire van közel jelzéshez.

## Ha a DB-ben más van, mint az alapértelmezés

A config dokumentum a meglévő **értékeket sosem írja felül** (hogy ne törölje a
hangolásodat) — csak a hiányzó kulcsokat veszi fel. Ezért ha egy alapértelmezés
megváltozik, az nem jut el egy már létező dokumentumba. Induláskor kiírjuk, mi tér el:

```
INFO main  A DB-ben eltero beallitas: maxSpreadPct=0.1 (alap 0.05)
```

Ha át akarod venni az új alapértelmezést, egy `$unset` elég — a következő indításnál
visszakerül a friss defaulttal:

```js
db.config.updateOne({_id:"detector"}, {$unset:{maxSpreadPct:""}})
```

## Hangolási sorrend

1. **Előbb a kereskedhetőség.** A `STATUS` sor kiírja, hány pár esik ki és miért. Ha túl
   sok, lazíts a `maxSpreadPct` értéken.
2. **Aztán a `baselineRatio`.** Ez a fő érzékenység-kapcsoló: 4.0 → kevesebb és
   rendkívülibb, 3.0 → több.
3. **Végül a validáció.** A Mongo aggregációból látod, melyik ok dominál a
   `REJECTED`-ek között.
4. Egyszerre csak egyet állíts, hogy tudd, mi okozta a változást.
