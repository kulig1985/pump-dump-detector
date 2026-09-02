# Mit figyel a scalp detektor — pontosan

A jelenlegi kód (`app/detectors/scalp.py`) alapján, az `app/config.py`
alapértékeivel. Részletes paraméter-magyarázat és példák: `docs/PARAMETEREK.md`.

## Az alapelv

Egy hirtelen impulzus **önmagában nem jelzés** — csak egy setup kezdete. A régi
rendszer (PUMP→LONG, DUMP→SHORT) mérése szerint ez érmefeldobás volt: a mozgás
gyakran visszajön, mielőtt bármit tehetnél vele. A jelenlegi rendszer megvárja,
amíg a szerkezet **megerősíti** az irányt.

```
IDLE -> IMPULSE_DETECTED -> WAITING_CONFIRMATION
     -> CONTINUATION_CONFIRMED | REVERSAL_CONFIRMED -> SIGNAL -> COOLDOWN -> IDLE
```

## Amit a rendszer lát

Egyetlen adatforrás: a Binance `aggTrade` folyam a figyelt párokra —
`price`, `qty`, `ts`, `buy_taker` (az agresszor oldala, az `m` mezőből).
Emellett **folyamatosan** streamel:

- `!bookTicker` — a legjobb bid/ask ár és mennyiség (spread, kizárásra)
- `<symbol>@depth20@500ms` — a könyv 20 szintje, memóriában cache-elve
  (`app/bookcache.py`) — a fal és a könyv-imbalance innen jön, a döntés
  pillanatában már készen áll
- 1 perces gyertyák (EMA9/EMA21), háttérben frissítve (`app/ta.py`)

**A könyv és az EMA ITT MÁR BEFOLYÁSOLJA a döntést** — nem csak az üzenetbe
kerül információként, mint a korábbi verzióban.

## 1. Impulzus

Páronként egy gördülő ablak (`impulseWindowSec` = 3 mp). A mozgást **az ablakra
illesztett egyenes** adja, nem a végpontok különbsége — egyetlen kiugró print
nem tud impulzust csinálni.

Az ablakra számolt mérőszámok:

```
movePct     az illesztett egyenes elmozdulása
notional    Σ (ár × mennyiség) az ablakban
imbalance   (vételi notional − eladói notional) / összes notional   [-1, 1]
singleStep  a legnagyobb egyetlen árlépés a mozgás %-ában
```

**Két normál** (medián, `baselineMinutes` = 5 perc visszatekintéssel):
- a pár normál **ár**mozgása (mint korábban)
- a pár normál **forgalma** ugyanabban az ablakméretben — ÚJ, ez fogja meg azt
  az esetet, amikor egy nagy kötés átsöpri a könyvet kevés valódi pénzből

**IMPULSE_UP** akkor áll fenn, ha mind teljesül (IMPULSE_DOWN a tükörképe):

```
1. mindket normal kesz (>= minta)
2. movePct >= max(minImpulsePct, priceBaseline × impulseBaselineRatio)
3. notional >= max(minImpulseNotional, notionalBaseline × notionalRatio)
4. imbalance a mozgas iranyaba mutat, es |imbalance| >= minImpulseImbalance
5. singleStep <= maxSingleStepPct
6. a par kereskedheto (spread, white/blacklist)
```

Ekkor rögzül: `P0` (az ablak eleje), `P1` (az ablak vége), `leg = |P1 − P0|`.
**Ettől kezdve minden méret a `leg` arányában értendő.**

## 2. Setup követése

Az impulzus után egy `Setup` figyeli a szerkezetet:

```
pivot     a P1 óta elért szélsőérték (amíg nincs érdemi visszahúzás, KÖVETI az árat)
counter   a visszahúzás szélsőértéke (a fordulás potenciális szintje)
```

**Érvénytelenítés → IDLE:**
- `setupTimeoutSec` (90 mp) letelt, VAGY
- az ár túlment az impulzus kiindulópontján `invalidateBeyondOriginPct`-nál
  (a `leg` arányában) — a mozgás megfordult, nincs mit folytatni vagy fordítani

Amint a visszahúzás eléri a `minPullbackPct`-ot, a **pivot rögzül**, és az
állapot `WAITING_CONFIRMATION`-re vált.

### 3a. Folytatás

```
LONG_CONTINUATION, ha:
  1. a visszahuzas SOHA nem lepte tul a maxPullbackPct-ot
  2. az ar attori a pivotot + breakoutOfLegPct
  3. a kotesaramlas (flowWindowSec) a belepo iranyaba mutat (>= minConfirmImbalance)
  4. a konyv nem all ellen (topImbalance, kozeli fal)
  5. (alapbol) az EMA trend is egyezik
```

### 3b. Fordulás

A `counter` szint csak akkor **rögzül**, ha attól legalább `counterPullbackPct`
ellen-visszahúzás történt — pontosan úgy, ahogy egy csúcsból swing-csúcs lesz.
Enélkül a szint folyamatosan az árral csúszna.

```
LONG_REVERSAL, ha:
  1. exhaustionSec ota nincs uj szelsoertek (a mozgas kifullad)
  2. a counter szint mar rogzult
  3. az ar attori a counter szintet + reclaimOfLegPct
  4. az attores reclaimHoldSec-ig TART (folyamatosan ellenorizve --
     ha visszaesik, a jelzes elmarad, de a setup nem all le)
  5. a kotesaramlas megfordult (>= minReversalImbalance)
  6. a belepoig a mozgas legfeljebb maxEntryRetracePct-at jott vissza
  7. a konyv nem all ellen. Az EMA trend SZANDEKOSAN NEM felteteltel --
     a fordulo epp azt keresi, amikor a trend megfordul
```

## 3. Kimenet

Cooldown: páronként `symbolCooldownSec` (10 perc) két jelzés között.

## Az eredménymérés (`app/outcome.py`)

A jelzés után `outcomeTrackSec`-ig (10 perc) **minden kötést** figyel a párról,
és iránnyal korrigált hozamot számol:

```
LONG :  r(t) = (p(t) - entry) / entry * 100
SHORT:  r(t) = (entry - p(t)) / entry * 100
```

Ebből: MFE/MAE (a legjobb és legrosszabb pont, és mikor érte el), és minden
`tpLevels`/`slLevels` szinthez az ELSŐ pillanat, amikor elérte. Mivel minden
kötést látunk, utólag **bármelyik** TP/SL párra eldönthető, melyiket érte el
előbb — külön mérés nélkül.

Ez **nem kapuz semmit** — a detektor döntésétől független, tisztán megfigyelés.
