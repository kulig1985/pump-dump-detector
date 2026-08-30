# Paraméterek — mit állítasz, és mi történik tőle

Minden beállítás a MongoDB `config` collectionben van, négy dokumentumban:
`detector`, `reversal`, `trading`, `telegram`. Ez a leírás a két detektorról szól.

```js
use("pump-dump")
db.config.findOne({_id: "detector"})
db.config.findOne({_id: "reversal"})
```

Menet közbeni módosítás **30 másodpercen belül** életbe lép, újraindítás nélkül.

> **Fontos:** az induláskori seed a **meglévő értékeket sosem írja felül** — csak a
> hiányzó kulcsokat veszi fel. Ha egy alapértelmezés a kódban megváltozik, a te
> dokumentumodba **nem** jut el. Induláskor a log kiírja, mi tér el:
> `A DB-ben eltero beallitas: minMovePct=0.3 (alap 0.6)`.
> Az új alapértelmezés átvétele: `db.config.updateOne({_id:"reversal"}, {$unset:{minMovePct:""}})`
> és újraindítás — vagy egyszerűen `$set`-eld kézzel arra, amit akarsz.

---

## Hogyan dönt a rendszer

Mindkét detektor **a pár saját normáljához** méri a mozgást, nem fix százalékhoz.
A normál (`baseline`) futás közben épül: másodpercenként egy minta a 2 másodperces
ablak elmozdulásából, `baselineMinutes` perc visszatekintés, és ezek **mediánja**.

```
   jelzéshez kell:   |mozgás|  >=  max( minMovePct ,  baselineRatio × normál )
```

Ezért ugyanaz a +0.3% egy nyugodt páron rendkívüli, egy meme coinon a normál működés
része. Amíg egy párnak nincs elég mintája (kb. 1–2 perc), nincs jelzés — nem tippelünk.

---

## `detector` — pump / dump

### Melyik párokat nézzük egyáltalán

| kulcs | alap | mit jelent |
|---|---|---|
| `enabled` | true | a pump/dump detektor ki-/bekapcsolása |
| `telegramEnabled` | true | ha false, a jelzés a DB-be és a logba megy, Telegramra nem |
| `quoteAssets` | `["USDT","USDC"]` | melyik elszámoló devizás párokat figyeljük |
| `minQuoteVolume24h` | 50 000 000 | ennél kisebb 24 órás forgalmú párok kiesnek |
| `maxSymbols` | 200 | a forgalom szerinti lista első ennyi eleme |
| `symbolRefreshMinutes` | 60 | ennyi percenként frissül a figyelt lista |
| `symbolWhitelist` | `[]` | **ha nem üres, CSAK ezeket figyeljük** |
| `symbolBlacklist` | `[]` | ezeket sosem |
| `maxSpreadPct` | 0.05 | ennél szélesebb vétel–eladás résnél nincs jelzés: a be- és kiszállás felemésztené a mozgást |

### A mozgás mérése

| kulcs | alap | mit jelent | ha **növeled** |
|---|---|---|---|
| `moveWindowSec` | 2.0 | ekkora időablakban mérünk elmozdulást. Nem gyertya: **nem várunk zárásra** | lassabb, nagyobb mozgásokat keres |
| `minTradesInWindow` | 10 | ennyi kötés kell az ablakba, különben nem mérhető | kevesebb, forgalmasabb pár jelez |
| `baselineMinutes` | 5 | ennyi perc visszatekintésből épül a pár normálja | lassabban alkalmazkodik |

### Érzékenység — **ezeket állítsd**

| kulcs | alap | mit jelent | ha **növeled** |
|---|---|---|---|
| **`baselineRatio`** | **4.0** | **a fő kapcsoló:** a mozgás a pár normáljának ennyiszerese legyen | kevesebb, de rendkívülibb jelzés |
| `minMovePct` | 0.15 | abszolút padló: ennél kisebb mozgás sosem jelzés, akármilyen nyugodt a pár | kiszűröd az apró mozgásokat |
| `maxSingleStepPct` | 50 | ha a mozgás ennél nagyobb részét **egyetlen árlépés** adta, nem jelzés. Egy nagy kötés átsöpri a könyvet, a többi már az új áron nyomtat, és az ablak szép egyenletes mozgásnak látszik | több egykötéses ugrás fér be. `0` = kikapcsolva |
| `confirmSec` | 3.0 | a jelzés **nem** a mozgás pillanatában megy ki: ennyivel később megnézzük, megvan-e még | később, de biztosabban jelez |
| `confirmHoldPct` | 60 | és a látott mozgás ennyi %-a legyen még meg. **Ez választja el a valódi elindulást a pillanatnyi korrekciótól** | szigorúbb: csak az marad, ami tényleg ott is marad |
| `symbolCooldownSec` | 60 | páronként ennyi szünet két jelzés között | ritkább ismétlés |

A logban végigkövethető:

```
MOZGAS      SKRUSDT  LONG  ar 0.015717  +0.31% / 1.8s  normal 0.049% (6.3x)  -- 3 mp megerositesre var
VISSZAESETT SKRUSDT  LONG  ar 0.015702  a +0.31%-bol +0.04% maradt (13%, kell 60%) -- pillanatnyi korrekcio volt
CANDIDATE   PROMUSDT LONG  ar 0.2841    mozgas +0.44% / 2.0s  normal 0.041% (10.7x)  megtartott 118%
KIHAGYVA    BTRUSDT  ar 0.0912  a mozgas 78%-at EGYETLEN arlepes adta (max 50%)
```

### Csak információ a jelzésben (semmit nem kapuznak)

| kulcs | alap | mit jelent |
|---|---|---|
| `orderBookLevels` | 20 | ennyi szintet kérünk le triggerkor |
| `wallSensitivity` | 3.0 | fal = a többi szint **mediánjának** ennyiszerese |
| `wallMaxDistancePct` | 1.5 | ennél távolabbi falat nem említünk |
| `emaFast` / `emaSlow` / `emaInterval` | 9 / 21 / `1m` | EMA az üzenetbe. **Sosem trigger, sosem szűrő** |
| `statusIntervalSec` | 60 | ennyi másodpercenként egy STATUS sor |
| `signalWindowMinutes` | 10 | „hányadik jelzés ebbe az irányba" — ekkora visszatekintéssel |

---

## `reversal` — lokális forduló

Az alakzat, LONG esetben (a SHORT ennek a tükörképe):

```
   origin  ─────────────────────────────  100%   ← innen indult a lemozgás
                                           25%   ← max belépő (maxRetracementPct)
                                           12%   ← ide kell visszapattannia (bounceOfMovePct)
   szélsőérték ─────────────────────────    0%
```

`LEMOZGÁS → MÉLYPONT → VISSZAPATTANÁS → MICRO-HIGH RÖGZÜL → VÉTELI FLOW → ÁTTÖRÉS`

Minden méret **a mozgás arányában** van, nem abszolút százalékban — így egy 0.6%-os és
egy 3%-os mozgásnál ugyanaz a logika működik.

### Érzékenység — **ezeket állítsd**

| kulcs | alap | mit jelent | ha **növeled** |
|---|---|---|---|
| **`minMovePct`** | **0.60** | mekkora előzetes mozgás után van értelme fordulót keresni. **A legerősebb szűrő:** egy 0.3%-os hullámzásból nincs mit kifordulni | jóval kevesebb, de nagyobb jelzés |
| **`baselineRatio`** | **5.0** | és ugyanez a pár normáljához mérve. A normál a mozgás **tényleges hosszára** skálázódik (bolyongásnál az elmozdulás az idő gyökével nő), így egy 20 mp-es kúszás nem számít rendkívülinek | kevesebb, rendkívülibb |
| `confirmSec` | 3.0 | az áttörés pillanata még nem forduló: ennyivel később az árnak **még mindig** a micro szint túloldalán kell lennie | később, de biztosabban |
| `cooldownSec` | 300 | páronként ennyi szünet | ritkább ismétlés |
| `maxExtremeAgeSec` | 8 | a szélsőérték ennél frissebb legyen — egy 15 mp-es mélypontra már késő beszállni | régebbi fordulókra is jelez |
| **`maxRetracementPct`** | **25** | a jelzés pillanatáig a mozgásnak legfeljebb ennyi %-a jöhetett vissza. Efölött a kereskedhető rész elfogyott | későbbi, rosszabb belépők is átmennek |

### Az alakzat geometriája (ritkán kell hozzányúlni)

| kulcs | alap | mit jelent |
|---|---|---|
| `wickSliceSec` | 0.5 | a szélsőértéket **nem** a nyers min/max adja, hanem ekkora szeletek középára. Enélkül egyetlen pillanat alatt beérkező pár print (egy nagy kötés, ami átsöpri a könyvet, majd az ár azonnal visszaáll) lett a „mozgás" kezdőpontja. `0` = kikapcsolva, nyers min/max |
| `bounceOfMovePct` | 12 | ennyit kell visszapattannia a mozgásból, hogy egyáltalán fordulóról beszéljünk |
| `pullbackOfBouncePct` | 30 | a visszapattanásból ennyi visszahúzás rögzíti a micro szintet (ez teszi swing-csúccsá) |
| `breakOfMovePct` | 5 | ekkora áttörés kell a micro szinten, a mozgás arányában |
| `newExtremeOfMovePct` | 2 | ennyivel mélyebb új minimum indítja újra az alakzatot |
| `windowSeconds` | 20 | ekkora rolling kötés-ablakban keressük az alakzatot |

> ⚠️ `bounceOfMovePct + breakOfMovePct` **maradjon jóval `maxRetracementPct` alatt**,
> különben a belépő matematikailag mindig a maximális visszahúzáson túlra esik, és
> soha nem lesz jelzés. Alapon: 12 + 5 = 17 < 25.

### Kötésáramlás

| kulcs | alap | mit jelent |
|---|---|---|
| `flowWindowSeconds` | 3 | ekkora ablakban nézzük a vevő/eladó arányt |
| `minFlowRatio` | 1.6 | a fordulat irányába ekkora túlsúly kell (USDT-ben mérve) |
| `minTradesInFlowWindow` | 5 | ennyi kötés kell bele. A domináns oldalnak **kötésszámban is** vezetnie kell — egyetlen bálna-print nem csinál fordulást |

---

## Recept: kevesebb és jobb jelzés

Sorrendben, **egyszerre csak egyet** állíts, hogy tudd, mi okozta a változást.

```js
// 1. PUMP/DUMP: csak a rendkívüli mozgás
db.config.updateOne({_id:"detector"}, {$set:{baselineRatio: 6.0, minMovePct: 0.30}})

// 2. PUMP/DUMP: csak az, ami meg is marad
db.config.updateOne({_id:"detector"}, {$set:{confirmSec: 5.0, confirmHoldPct: 75}})

// 3. REVERSAL: csak nagy mozgás után keressünk fordulót
db.config.updateOne({_id:"reversal"}, {$set:{minMovePct: 1.0, baselineRatio: 6.0}})

// 4. REVERSAL: korai belépő, friss szélsőérték
db.config.updateOne({_id:"reversal"}, {$set:{maxRetracementPct: 20, maxExtremeAgeSec: 5}})

// 5. ritkábban ugyanarról a párról
db.config.updateOne({_id:"detector"}, {$set:{symbolCooldownSec: 300}})
db.config.updateOne({_id:"reversal"}, {$set:{cooldownSec: 600}})

// 6. csak a legforgalmasabb párok
db.config.updateOne({_id:"detector"}, {$set:{minQuoteVolume24h: 200000000, maxSymbols: 60}})
```

Több jelzés kell? Ugyanezek lefelé: `baselineRatio` 3.0, `minMovePct` kisebb,
`confirmHoldPct` 40, `cooldownSec` rövidebb.

Az egyik detektort teljesen kikapcsolni:

```js
db.config.updateOne({_id:"reversal"}, {$set:{enabled: false}})
```

Csak néhány páron tesztelni:

```js
db.config.updateOne({_id:"detector"}, {$set:{symbolWhitelist: ["BTCUSDT","ETHUSDT"]}})
```

---

## Mit mutat a STATUS sor

```
STATUS  40 par | 14,113 tick/60s | konyv: 752 par | 3 candidate, 3 jelzes, 2 kihagyva | Telegram: BE
   kizarva 2: tul szeles a spread: 2
   spread   p10 0.004%  p50 0.016%  p90 0.042%   kuszob 0.050%  -> 2 par felette
   normal kesz: 34/40 par | legkozelebb: SKRUSDT 0.283% (kell 0.154%, normalja 0.038%)
```

- **`normal kesz`** — hány párnak épült már fel a normálja. Amíg nem kész, az a pár nem jelezhet.
- **`legkozelebb`** — a mezőny legjobbja épp mennyire van a küszöbtől. Ha itt tartósan
  „kell 0.154%" mellett 0.05%-os mozgások vannak, a piac áll — nem a beállítás rossz.
- **`spread` percentilisek** — a küszöb ebből állítható adat alapján, nem vaktában.

## Elutasítási okok

Gépi név megy a MongoDB-be (hogy aggregálható legyen), magyar szöveg a logba.

| ok | mit jelent |
|---|---|
| `blacklisted` / `not_whitelisted` | kézi kizárás |
| `no_book_data` | még nem láttuk a pár order book tetejét |
| `spread_too_wide` | a spread szélesebb, mint `maxSpreadPct` |

```js
// mi miért esett ki?
db.signals.aggregate([{$group:{_id:"$rejectedReason", n:{$sum:1}}}, {$sort:{n:-1}}])
```
