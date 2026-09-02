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

Rögzül: `p0` (az ablak eleje), `pivot` = **az ablak tényleges high-ja (UP) /
low-ja (DOWN)** — nem az utolsó kötés ára. Enélkül előfordulhatna, hogy az ár már
járt magasabban, de a rendszer egy későbbi, alacsonyabb pontot venne pivotnak, és
egy már bejárt szintet látna „kitörésnek". `leg = |pivot − p0|`.

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

- ha az ár **visszamegy a szint rossz oldalára** (LONG: `ár <= szint`), a kitörés
  **érvényét veszti** (`breakout_ts = None`) — csak egy **új, valódi cross**
  indíthat új megerősítési ablakot. A setup közben életben marad.
- ha `maxBreakoutAgeSec`-en belül nincs megerősítés → a setup eldobva
- ha az ár `maxEntryExtensionPct`-nél messzebb jár a szinttől → nincs belépő

## 4. Megerősítés — három dolog

```
1. kotesaramlas: flow >= minConfirmImbalance a belepo iranyaba
2. friss konyv-adat (maxDataAgeSec)  -- FAIL-CLOSED
3. eligibility: spread + white/blacklist
```

Ennyi. Se EMA, se fal, se könyv-imbalance.

**A flow csak a pivot rögzítése óta érkezett kötésekből számol**
(`Setup.wait_breakout_ts`). Az impulzus alatti erős egyirányú áramlás különben
magától „megerősítené" a későbbi kitörést.

**Az eligibility a commit ELŐTT fut.** Ha bármelyik feltétel nem teljesül, a setup
**életben marad** — nem törlődik, és **nem indul cooldown**. Cooldown kizárólag
ténylegesen kiküldött jelzés után indul.

## Adatszakadás

Ha az `aggTrade` kapcsolat megszakad, az érintett párok setupja és `prev_price`
értéke törlődik (`ScalpDetector.reset`). Reconnect után az első kötés különben egy
régen megtörtént kitörés „friss keresztezésének" látszana.

**A baseline NEM törlődik** — az a pár hosszú távú normálja, nem setup-állapot.
Helyette a `RollingMedian` mindig az **aktuális időhöz** viszonyít: a `value()` is
levágja a `now - baselineMinutes`-nél régebbi mintákat. Így egy rövid reconnect
után a még friss minták használhatók maradnak (nem kell újra 5 percet várni), egy
hosszabb kimaradás után viszont a régi baseline elavul, és nincs jelzés, amíg
újra fel nem épül.

## Eredménymérés (`app/outcome.py`)

A mérés **a jelzés létrejöttekor indul, még a Telegram HTTP hívás előtt**, és
`outcomeTrackSec`-ig (**20 perc**) minden kötést figyel. Iránnyal korrigált hozam:

```
LONG :  returnPct = (ar - signalPrice) / signalPrice * 100
SHORT:  returnPct = (signalPrice - ar) / signalPrice * 100
```

Pozitív érték **mindig** a jelzés irányába történő kedvező mozgás.

Jelzésenként mentve:

```
return1m .. return20m   az outcomeMarkSec pontokban mert hozam
mfePct / maePct         a legjobb es a legrosszabb pont
timeToMfeSec / MaeSec   mikor erte el
maxPrice / minPrice     a nyers szelsoertekek
tpFirstTouch / slFirstTouch   DIAGNOSZTIKA: melyik szintet mikor erte el eloszor
```

A mérés alapja a `signalPrice` (a detektor által adott ár). Az adatmodell készen
áll egy későbbi `actualEntryPrice` mezőre, ha egyszer a tényleges belépő árat is
vissza tudjuk küldeni — most nem szükséges.

**A TP/SL first-touch nem minősíti a jelzést.** Egy trade lehet előbb mínuszban,
majd 20 perccel később érdemi profitban; kézi kereskedésnél a `-0.3%` korábbi
érintése nem bukott trade. A heartbeat ezért külön, `TP/SL FIRST-TOUCH
(diagnosztika)` cím alatt mutatja.

**Current run és historical külön.** Az induláskor betöltött, korábbi futásokból
származó mérések `current_run = False` jelöléssel jönnek be, és külön táblában
(`EREDMENY -- HISTORICAL / ALL TIME`) jelennek meg — nem keverednek az aktuális
futás statisztikájába.

Ez **nem kapuz semmit** — tisztán megfigyelés.
