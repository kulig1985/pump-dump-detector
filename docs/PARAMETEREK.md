# Paraméterek

Minden beállítás a MongoDB `config` collectionjében van, négy dokumentumban. Módosítás
után **30 másodpercen belül él**, újraindítás nélkül. A dokumentumok magukat tartják
karban: az új beállítások bekerülnek, a már nem használtak törlődnek.

```js
use pump-dump
db.config.find().pretty()
db.config.updateOne({_id:"reversal"}, {$set:{maxRetracementPct: 20}})
```

---

## A fordulós alakzat anatómiája

Ez a rajz a `reversal` detektor összes méretét megmutatja. **Minden méret a mozgás
arányában értendő** (0–100%), nem abszolút százalékban — így egy 0.5%-os és egy 3%-os
mozgásnál ugyanaz a logika működik.

```
   csucs ──────────────────────────────────────────────  100%   ← innen indult az esés
                                                                   (a mozgás hossza:
                                                                    minMovePct legalább)

         ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─      61.8%  ← CÉL
                                                                   targetRetracementPct

         ══════════════════════════════════════════       25%    ← MEDDIG SZABAD BELÉPNI
                                                                   maxRetracementPct
              ╱╲          ╱                                        efölött a kereskedhető
             ╱  ╲   ╱╲   ╱  ← ÁTTÖRÉS = BELÉPŐ                     rész már elfogyott
            ╱    ╲ ╱  ╲ ╱      breakOfMovePct (5%) a micro fölött
      micro╱──────╳────╳────────────────────────────      17%    ← a rögzített micro-high
          ╱        ╲                                                (a visszapattanás csúcsa)
         ╱          ╲  ← VISSZAHÚZÁS                       12%    ← ide kell visszapattannia
        ╱            ╲    pullbackOfBouncePct (30%)                 bounceOfMovePct
       ╱              ╲   a visszapattanásból
      ╱                ╲
 melypont ─────────────────────────────────────────────    0%    ← a szélsőérték
                                                                    max maxExtremeAgeSec
         ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─      -5%    ← STOP
                                                                    stopBufferPct a mélypont alatt
```

A SHORT forduló ennek pontos tükörképe: emelkedés → csúcs → visszahúzás → micro-low →
lefelé áttörés.

**Ez volt a fő hiba a korábbi verzióban:** nem volt `maxRetracementPct`, ezért a rendszer
akkor is jelzett, amikor a visszapattanás 48%-a már megtörtént. Ott a kereskedhető rész
elfogyott, és az ár rendszeresen visszaesett.

---

## Miért időalapú az ablak?

A mérés korábban **darabszám** alapú volt (utolsó 30 trade). Egy nagy páron 30 aggTrade
akár 30 milliszekundum alatt is beérkezik — ilyenkor egy apró árváltozást apró
időtartammal osztva hatalmas hamis „meredekség" jön ki:

```
TRIGGER LONG | meredekseg +0.398%/mp | 30 trade 0.03 mp alatt | osszesen +0.02%
```

Ez nem mozgás, ez egy trade-csokor. Ráadásul a legnagyobb párokon (ETHUSDT, SOLUSDT) sok
aggTrade **azonos időbélyeggel** érkezik, ott a mérés egyáltalán nem működött. A
volatilitás-adaptáció pedig felszívta a hamis meredekségeket, és 126 %/mp-es küszöböket
állított be.

Most az ablak `slopeWindowSec` (2 mp) hosszú, és három feltétel van rá: legalább
`minTradesInWindow` trade essen bele, a tényleges időszakasz fedje le az ablak felét, és
a nettó elmozdulás érje el a `minTotalMovePct`-ot.

## Miért nem a „hatékonysági arány" szűri a meme coinokat?

Először az volt itt, hogy `nettó elmozdulás / megtett út` 50 trade-en. Ez **a pillanatnyi
állapotot mérte, nem a pár jellegét**, és két bajt okozott:

- Egy likvid páron 50 aggTrade ezredmásodperceket fed le, ott az ár a spreaden pattog.
  A BTCUSDT így 0.02-es „hatékonyságot" kapott, és kizárva maradt — pedig épp az ilyen
  párokon a legértékesebb a jelzés.
- A mérték simítva változik, tehát amikor egy lapos pár **végre megmozdult**, a szűrő még
  mindig a régi, lapos állapotot látta, és kizárta — vagyis pontosan a keresett eseményt.

Most a mérték a **tick zaj**: mekkorát mozdít az áron egyetlen kötés. Ez a pár jellemzője,
stabil, és pontosan azt fogja meg, ami zavaró: ha egy trade 0.5%-ot mozdít, ott hiába
akarsz 0.3%-os mozgást elkapni.

## Hogyan jön ki a score?

Öt rész, összesen 100 pont. A `minSignalScore` (60) alatt a jelzés mentődik, de nem megy ki.

| rész | max | mit mér | hogyan |
|---|---|---|---|
| `movement` | 30 | mennyivel lépte túl a saját küszöbét | 1× küszöb = 15 pont, 2× = 30 (itt megáll) |
| `acceleration` | 10 | gyorsul-e / erős-e az áttörés | igen = 10, nem = 0 |
| `ema` | 20 | támogatja-e a trend | momentumnál: EMA az irányba = 20 (ha az ár is a jó oldalon), ellentétes = 0. Fordulónál: az ár visszavette-e az EMA9-et |
| `orderbook` | 20 | van-e hely elmozdulni | nincs wall előttünk = 20; minél közelebb a wall, annál kevesebb. Fordulónál a támasz is számít |
| `rewardRisk` | 20 | **megéri-e egyáltalán felvenni** | 1.0:1 alatt 0 pont, 3.0:1 felett 20, közte arányosan |

Nincs adat (order book / EMA / terv nem elérhető) → az adott rész a maximum 40%-át kapja,
hogy egy hiányzó információ ne büntessen úgy, mint egy rossz információ.

### Példa egy valós jelzésre

```
[BTRUSDT] pump_dump  mozgas 3.3x kuszob, EMA bearish, wall 0.13%-ra, hozam/kockazat 0.8:1

  movement       30   (3.3x küszöb, a plafonon)
  acceleration    0   (nem gyorsult)
  ema            20   (bearish trend, SHORT irány -> támogatja)
  orderbook       2   (a wall 0.13%-ra van, gyakorlatilag azonnal útban)
  rewardRisk      0   (0.8:1 -- a cél közelebb van, mint a stop)
  ────────────────
  összesen       52   -> a 60-as küszöb alatt, NEM megy ki
```

Ugyanez a jelzés a `rewardRisk` rész nélkül 67 pontot kapott volna, és kiment volna —
miközben a terv szerint nem érte meg felvenni. Az „erősen mozog" önmagában nem jelzés,
csak akkor az, ha van hova mennie.

---

## `detector` — a pump/dump detektor és a közös beállítások

### Mikor jelez

| kulcs | alap | mit csinál | ha növeled | ha csökkented |
|---|---|---|---|---|
| `slopeWindowSec` | 2.0 | **időalapú** ablak: ekkora szakaszra illesztjük az egyenest | simább, lassabb reakció | zajosabb, gyorsabb |
| `minTradesInWindow` | 10 | ennyi trade kell az ablakba, különben nem mérhető | csak a forgalmasabb párok | kevés adatból is mér |
| `minTotalMovePct` | 0.15 | ekkora nettó elmozdulás kell az ablakban | csak az érdemi mozgások | apró rezdülésekre is jelez |
| `minSlopePctPerSec` | 0.15 | ennyi %/másodperc tempó kell | kevesebb, erősebb jelzés | több, gyengébb |
| `maxThresholdFactor` | 10 | a volatilitáshoz igazított küszöb legfeljebb ennyiszerese az alapnak | | |
| `minConsistency` | 0.70 | a lépések ekkora hányada mutasson egy irányba | csak a tiszta mozgások | fűrészfogat is befogad |
| `minVolumeFactor` | 1.0 | az ablakban legalább ennyiszer a pár átlagos forgalma | valódi pénz kell mögé | apró kötések is elmennek |
| `volatilityMultiplier` | 4.0 | a küszöb a pár saját zajszintjéhez igazodik. **A configban megadott érték a padló**, ez alá sosem megy | a zajos párokon sokkal magasabb küszöb | `0` = kikapcsolva |
| `symbolCooldownSec` | 60 | páronként ennyi ideig nincs újabb jelzés | ritkább jelzés | sűrűbb |

### Symbol univerzum

| kulcs | alap | mit csinál |
|---|---|---|
| `quoteAssets` | `["USDT","USDC"]` | melyik elszámoló devizás perpetualokat figyeljük |
| `minQuoteVolume24h` | 50 000 000 | ez alatti 24h forgalmú párokat kihagyjuk |
| `maxSymbols` | 200 | top N pár forgalom szerint |
| `excludeSymbols` | `[]` | névre kizárt párok |
| `maxTickNoisePct` | 0.08 | **a szaggatott párok kizárása.** Ha *egyetlen kötés* átlagosan ennél többet mozdít az áron, ott nem lehet 0.2–0.5%-os mozgást megfogni. BTC/ETH: 0.000x%, normál alt: 0.00x–0.0x%, össze-vissza ugráló: 0.1% felett. A táblázat `tick zaj` oszlopa mutatja a mért értéket. `0` = kikapcsolva |

### Jelzés-minőség

| kulcs | alap | mit csinál |
|---|---|---|
| `minSignalScore` | 60 | ez alatt csak mentünk, nem küldünk |
| `minMoveToSpreadRatio` | 3.0 | a mozgás legyen legalább ennyiszer a spread. Ha nem, akkor nem mozgás történt, csak valaki átlépte a spreadet — az nem lekereskedhető |
| `stopBufferPct` | 0.05 | a stop ennyivel kerül a horgony túloldalára |
| `momentumStopRetracementPct` | 50 | lendületnél a stop az impulzus ennyi %-ánál. Ha a mozgás felét visszaadja, a tézis halott — a teljes impulzust kockáztatni ugyanakkora célért szerkezetileg 1:1 arányt adna |
| `momentumTargetFactor` | 1.0 | a cél az impulzussal azonos méretű folytatás (mért mozgás) |
| `minRewardRisk` | 1.5 | ez alatt „gyenge arányú"-nak **jelöljük** a jelzést (nem dobjuk el) |
| `orderBookLevels` | 20 | vizsgált árszintek (5 / 10 / 20) |
| `wallSensitivity` | 3.0 | wall = egy szint ≥ 3× az oldal átlaga |
| `wallMaxDistancePct` | 1.5 | ennél távolabbi wall már nem érdekes |
| `emaFast` / `emaSlow` / `emaInterval` | 9 / 21 / 1m | trendfilter (soha nem trigger, csak kontextus) |

### Eredménymérés és árnyék mód

| kulcs | alap | mit csinál |
|---|---|---|
| `outcomeMinutes` | 5 | ennyi ideig méri az árat a jelzés után |
| `outcomeTargetPct` / `outcomeStopPct` | 0.3 / 0.3 | mikor számít jónak, illetve rossznak |
| `shadowMinSamples` | 50 | ennyi lemért jelzés kell egy score sávhoz, mielőtt Telegramra menne |
| `shadowMinHitRate` | 0.55 | és ekkora találati arány |
| `telegramMode` | `"auto"` | `auto` = csak bizonyított sávok; `always` = mindig; `never` = soha |
| `signalWindowMinutes` | 10 | ekkora visszatekintéssel számolja, hányadik a jelzés |
| `statusIntervalSec` | 5 | ilyen sűrűn írja ki, mi történik az árakkal |

---

## `reversal` — a fordulós detektor

Lásd a fenti anatómia-rajzot. A `telegramMode`, `minSignalScore` és `cooldownSec` itt
**saját**, független a pump/dump-étól.

| kulcs | alap | mit csinál | ha növeled |
|---|---|---|---|
| `minMovePct` | 0.40 | mekkora mozgás kell a forduló előtt (abszolút %) | csak nagyobb mozgások fordulóit keresi |
| `bounceOfMovePct` | 12 | ennyit kell visszapattannia a mozgásból | későbbi, megerősítettebb, de kevesebb hely marad |
| `pullbackOfBouncePct` | 30 | a visszapattanás ennyit húzzon vissza — ekkor rögzül a micro szint | mélyebb visszahúzást vár, ritkább jelzés |
| `breakOfMovePct` | 5 | az áttörés legyen a mozgás ennyi %-a | határozottabb áttörést vár |
| **`maxRetracementPct`** | **25** | **ennél többet ne jöjjön vissza az ár, amikor jelzünk.** Ez a legfontosabb kapcsoló: efölött a kereskedhető rész elfogyott | több jelzés, de későbbi belépés |
| `targetRetracementPct` | 61.8 | a cél a mozgás ennyi %-ánál van | távolabbi cél, jobb arány, ritkábban éri el |
| `maxExtremeAgeSec` | 8 | a szélsőérték ennél frissebb legyen | régebbi alakzatokat is elfogad |
| `newExtremeOfMovePct` | 2 | ennyivel mélyebb minimum indít új alakzatot | ritkábban indul újra |
| `windowSeconds` | 20 | a rolling trade ablak hossza | hosszabb szerkezetet lát |
| `maxSetupAgeSec` | 20 | ennyi idő után elavul egy alakzat | |
| `flowWindowSeconds` | 3 | ekkora ablakon mérjük a trade flow-t | |
| `minFlowRatio` | 1.6 | buy/sell (vagy sell/buy) arány | határozottabb fordulást vár |
| `minFlowVolumeFactor` | 1.0 | a flow ablakban legalább ennyiszer a pár átlagos forgalma | valódi pénz kell mögé |
| `minTradesInFlowWindow` | 5 | ennyi trade kell az ablakba | |

---

## `trading` — automatikus pozíciónyitás

**Alapból kikapcsolva.** Lásd a README `trading` szakaszát.

## `telegram`

| kulcs | mit csinál |
|---|---|
| `botToken` / `chatId` | a bot és a cél chat |
| `chatIds` | detektoronként külön chat, pl. `{"pump_dump":"-100...","reversal":"-100..."}`. Üres érték esetén a közös `chatId`-re megy |

---

## Hangolási sorrend

1. **Ne állíts semmit az első fél napban.** Az árnyék mód méri a találati arányt, és
   nem küld Telegramra. A `EREDMENYEK` tábla 10 percenként megjelenik a logban.
2. Amikor van 50+ mért jelzés egy sávban, a tábla megmondja, hol van értelmes találati
   arány. Ehhez igazítsd a `minSignalScore`-t.
3. Csak ezután nyúlj a detektor paramétereihez — és egyszerre csak egyhez, hogy tudd,
   mi okozta a változást.
