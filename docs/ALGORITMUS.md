# Mit figyel a scalp detektor — pontosan

`app/detectors/scalp.py`, az `app/config.py` alapértékeivel.
Paraméter-magyarázat: `docs/PARAMETEREK.md`.

## Egyetlen út

```
IMPULZUS  ->  PULLBACK  ->  FRISS KITÖRÉS  ->  JELZÉS
```

Állapotgép páronként, egyszerre **egy** aktív setup:

```
IDLE -> IMPULSE -> WAIT_PULLBACK -> WAIT_BREAKOUT -> SIGNAL -> COOLDOWN -> IDLE
```

Ha a setup érvénytelenné válik vagy lejár: `IDLE`.

**Nincs** reversal ág, **nincs** EMA a belépő döntésben, **nincs** fal- vagy
könyv-imbalance kapu. Ha ez az egy setup mérhetően működik, arra lehet építeni.

## Bemenet

A Binance `aggTrade` folyam a figyelt párokra: `price`, `qty`, `ts`,
`buy_taker` (az agresszor oldala, az `m` mezőből). Emellett folyamatosan:

- `!bookTicker` — legjobb bid/ask (spread, kizárásra)
- `<symbol>@depth20@500ms` — a könyv, memóriában (`app/bookcache.py`)

Mindkettőnek **időbélyege** van. `maxDataAgeSec`-nél régebbi adattal **nincs
jelzés** — nincs fail-open.

## 1. Impulzus

`impulseWindowSec` (3 mp) gördülő ablak. A mozgást az ablakra **illesztett
egyenes** adja, nem a végpontok különbsége.

```
movePct     az illesztett egyenes elmozdulása
notional    Σ (ár × mennyiség)
imbalance   (vételi − eladói notional) / összes            [-1, 1]
singleStep  a legnagyobb egyetlen árlépés a mozgás %-ában
```

Két normál (medián, `baselineMinutes` visszatekintéssel): a pár normál
**ármozgása** és normál **forgalma**.

```
1. mindket normal kesz
2. movePct >= max(minImpulsePct, arNormal × impulseBaselineRatio)
3. notional >= max(minImpulseNotional, forgalomNormal × notionalRatio)
4. |imbalance| >= minImpulseImbalance, a mozgas iranyaba
5. singleStep <= maxSingleStepPct
```

Rögzül: `p0` (az ablak eleje), `pivot` (az ablak vége), `leg = |pivot − p0|`.
**Az impulzus önmagában nem jelzés.**

## 2. Pullback

Amíg az ár új szélsőértéket csinál, a **`pivot` és a `leg` együtt frissül**
(`Setup.uj_szelsoertek`). Korábban itt volt egy hiba: a pivot elmozdult, de a
`leg` a régi értéken maradt, így a visszahúzás rossz alaphoz mérődött.

```
retrace = |pivot - ar| / leg * 100

retrace < minPullbackPct   ->  WAIT_PULLBACK, varunk
retrace >= minPullbackPct  ->  a PIVOT ROGZUL, jon a kitoresi szint
retrace > maxPullbackPct   ->  ervenytelen (WAIT_BREAKOUT allapotban)
```

Érvénytelenítés: `setupTimeoutSec` lejárt, vagy az ár `invalidateBeyondOriginPct`-tal
az impulzus kiindulópontja alá ment.

## 3. Friss kitörés

```
kitoresi szint = pivot ± breakoutOfLegPct/100 × leg
```

A jelzés **csak a keresztezés pillanatában** születhet:

```
LONG :  elozo_ar <= szint  ES  aktualis_ar > szint
SHORT:  elozo_ar >= szint  ES  aktualis_ar < szint
```

Nem elég, hogy az ár már korábban áttörte és még fölötte áll. A keresztezés
időpontja `breakoutTimestamp`-ként rögzül. Utána:

- ha `maxBreakoutAgeSec`-en belül nincs megerősítés → a setup eldobva
- ha az ár `maxEntryExtensionPct`-nél messzebb jár a szinttől → nincs belépő

## 4. Megerősítés — három dolog

```
1. kotesaramlas: flow >= minConfirmImbalance a belepo iranyaba (flowWindowSec)
2. friss konyv-adat (maxDataAgeSec)  -- FAIL-CLOSED
3. spread: az eligibility szuri, a jelzes kiadasanal
```

Ennyi. Se EMA, se fal, se könyv-imbalance.

## Eredménymérés (`app/outcome.py`)

A mérés **a jelzés létrejöttekor indul, még a Telegram HTTP hívás előtt**.
`outcomeTrackSec`-ig (10 perc) minden kötést figyel:

```
LONG :  r(t) = (p(t) - entry) / entry * 100
SHORT:  r(t) = (entry - p(t)) / entry * 100
```

Ebből: MFE/MAE (érték + mikor), `outcomeMarkSec` pontokban az ár (1/3/5/10 perc),
és minden TP/SL szinthez az **első** érintés ideje. Mivel minden kötést látunk,
utólag bármelyik TP/SL párra eldönthető, melyiket érte el előbb.

Ez **nem kapuz semmit** — tisztán megfigyelés.
