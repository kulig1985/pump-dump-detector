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

## `detector` — a pump/dump detektor és a közös beállítások

### Mikor jelez

| kulcs | alap | mit csinál | ha növeled | ha csökkented |
|---|---|---|---|---|
| `tradeWindow` | 30 | ennyi trade-re illesztjük az egyenest | simább, lassabb reakció | zajosabb, gyorsabb |
| `maxSpanSec` | 5.0 | ha az N trade ennél tovább tartott, nem hirtelen mozgás | lassú mozgásokat is befogad | csak a nagyon gyorsakat |
| `minSlopePctPerSec` | 0.15 | ennyi %/másodperc meredekség kell | kevesebb, erősebb jelzés | több, gyengébb |
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
| `qualityWindow` | 50 | ennyi trade-ből mérjük a mozgásminőséget |
| `minEfficiency` | 0.25 | **a szaggatott párok kizárása.** Hatékonysági arány = nettó elmozdulás / megtett út. Tiszta mozgás 0.7–1.0, normál pár 0.3–0.6, össze-vissza ugráló meme 0.0–0.2. `0` = kikapcsolva |

### Jelzés-minőség

| kulcs | alap | mit csinál |
|---|---|---|
| `minSignalScore` | 60 | ez alatt csak mentünk, nem küldünk |
| `minMoveToSpreadRatio` | 3.0 | a mozgás legyen legalább ennyiszer a spread. Ha nem, akkor nem mozgás történt, csak valaki átlépte a spreadet — az nem lekereskedhető |
| `stopBufferPct` | 0.05 | a stop ennyivel kerül a horgony túloldalára |
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
