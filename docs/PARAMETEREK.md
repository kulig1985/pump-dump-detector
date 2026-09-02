# Paraméterek — mit jelent, és mi történik, ha átállítod

## Hol állítod: `app/config.py`

**Az alapértékek a kódban vannak, és hidegindítással minden beállítás felépül.**
A MongoDB `config` collection csak egy másolat, amit induláskor a kód hoz létre:

```
app/config.py            ->  config collection (MongoDB)
  MARKET_DEFAULTS              market     KÖZÖS: melyik párokat figyeljük, és
                                            hogyan mérjük az eredményt
  DETECTOR_DEFAULTS            detector   a scalp detektor (impulzus + setup)
  TRADING_DEFAULTS             trading    pozíciónyitás (alapból KI)
  TELEGRAM_DEFAULTS            telegram   a bot és az üzenet
```

A hangolás **mindig a kódban** történik:

```bash
# 1. atirod az erteket az app/config.py-ban
# 2. ujrainditas
git pull && docker compose up -d --build
```

A `config` collectiont bármikor törölheted: a következő induláskor **minden
beállítás újra létrejön** a kódban lévő alapértékekkel. Ezt teszt őrzi
(`test_cold_start_creates_every_setting`) — egyetlen beállítás sem maradhat ki
hidegindításnál, és semmi nem várhat kézi beavatkozásra.

> **MINDEN ÉRTÉK ITT KIINDULÁSI PARAMÉTER.** Nem "helyes" értékek — kezdőpontok,
> amelyeket az `EREDMENY` mérésből (MFE/MAE, TP/SL) kell hangolni. Ne találgass:
> nézd meg, mit mutat a mérés, és onnan indulj el.

---

## Az alapelv: impulzus ≠ jelzés

A korábbi rendszer minden hirtelen mozgásból automatikusan jelzést csinált
(PUMP→LONG, DUMP→SHORT). A mérés megmutatta: ez érmefeldobás — a mozgás
gyakran visszajön, mielőtt bármit tehetnél vele.

Most az **impulzus csak egy setup kezdete**. A jelzés csak azután megy ki, hogy
a szerkezet (visszahúzás + újratörés, vagy kifulladás + a szint visszavétele) és
a kötésáramlás **megerősítette**:

```
IDLE -> IMPULSE_DETECTED -> WAITING_CONFIRMATION
     -> CONTINUATION_CONFIRMED | REVERSAL_CONFIRMED -> SIGNAL -> COOLDOWN -> IDLE
```

Minden méret az **impulzus-láb** (`leg = |P1 − P0|`) arányában értendő, nem
abszolút százalékban — így ugyanaz a beállítás működik egy 0.4%-os és egy
4%-os impulzusnál is.

---

# `market` — közös piaci beállítások és eredménymérés

### `enabled` — alap: `true`
Az egész feldolgozás ki-/bekapcsolása.

### `quoteAssets` — alap: `["USDT", "USDC"]`
Milyen elszámoló devizás párokat figyelünk.

### `minQuoteVolume24h` — alap: `120 000 000`
Ennél kisebb 24 órás forgalmú párok kiesnek.

### `maxSymbols` — alap: `60`
A forgalom szerint rendezett lista első ennyi eleme.

### `symbolRefreshMinutes` — alap: `60`
Ennyi percenként frissül a figyelt lista.

### `symbolWhitelist` / `symbolBlacklist` — alap: `[]` / `[]`
Ha a whitelist nem üres, **kizárólag** azokat figyeljük. A blacklist mindig kizár.
> **Példa:** `symbolWhitelist: ["BTCUSDT","ETHUSDT"]` — teszteléshez, két páron.

### `maxSpreadPct` — alap: `0.05`
A legjobb vétel/eladás közti rés felső határa. Ennél szélesebb páron nincs jelzés.

### `outcomeTrackSec` — alap: `600`
A jelzés után ennyi másodpercig **folyamatosan** figyeljük az árat — minden
kötést, nem csak pár mérési pontot. Ebből épül fel az MFE/MAE és a TP/SL mérés.
> **Példa:** `600` = 10 perc. Ez illeszkedik a rendszer céljához: 5–10 perces
> scalp belépők.

### `tpLevels` — alap: `[0.3, 0.5, 0.8, 1.0]`
Ezekre a take-profit szintekre (százalék) mérjük, **mikor** érte el először a
jelzés irányában az árfolyam.
> **Példa:** egy LONG jelzés 100.00-nál. Ha az ár eléri a 100.50-et 40
> másodperc múlva, akkor `tp["0.5"] = 40`.

### `slLevels` — alap: `[0.2, 0.3, 0.5]`
Ugyanez stop-loss szintekre, ellenkező irányban.

### `reportTp` / `reportSl` — alap: `0.5` / `0.3`
Az összesítésben (`EREDMENY` blokk) ez a TP/SL pár szerepel: melyiket érte el
előbb. Mivel minden kötést látunk, ez utólag **bármelyik** TP/SL párra
kiszámítható a `tp`/`sl` mezőkből — ez a kettő csak a megjelenítéshez kell.
> **Példa:** `reportTp: 0.8, reportSl: 0.3` — szigorúbb kiértékelés (nagyobb
> nyereségcél a kis stophoz képest).

### `statusIntervalSec` — alap: `60`
Ennyi másodpercenként egy STATUS sor a logba.

---

# `detector` — a scalp detektor

### `enabled` — alap: `true`

## 1. Impulzus — rendkívüli-e a mozgás ÉS a mögötte álló pénz

A régi rendszer csak az árat nézte. Ez az **egyetlen bemenete a kötés méretét
(`qty`) és az agresszor oldalát (`buy_taker`) is használja** — ez a fő különbség.

### `impulseWindowSec` — alap: `3.0`
Ekkora időablakban mérünk. A mozgást az ablakra **illesztett egyenes** adja, nem
a végpontok különbsége — egyetlen kiugró print nem tud impulzust csinálni.

### `minTradesInWindow` — alap: `10`
Ennyi kötés kell az ablakba, különben nem mérhető.

### `baselineMinutes` — alap: `5`
Ennyi perc visszatekintéssel épül a pár normálja (ár **és** forgalom).

### `minImpulsePct` — alap: `0.40`
Abszolút padló a mozgásra.

### `impulseBaselineRatio` — alap: `6.0`  ⭐
A mozgás a pár normáljának ennyiszerese legyen.
> **Példa:** a pár normálja 0.05% → `6.0` mellett 0.30% kell (vagy a padló, ha az nagyobb).

### `minImpulseNotional` — alap: `50 000`
Abszolút padló az agresszív (taker) forgalomra USDT-ben.

### `notionalRatio` — alap: `3.0`  ⭐
És a pár normál ablak-forgalmának ennyiszerese.
> **Példa (a ZECUSDT eset, amivel a régi rendszer megbukott):** egy nagy kötés
> átsöpörte a könyvet kevés valódi forgalommal — ez a szűrő pont ezt fogja meg:
> ha a mozgáshoz **nem** kellett a normálishoz képest sok pénz, nincs impulzus.

### `minImpulseImbalance` — alap: `0.25`  ⭐
A taker oldal ennyire legyen egyirányú, `-1..1` skálán
(`(vétel − eladás) / összes`). `0.25` = legalább 62.5%-a a forgalomnak egy oldalra.
> **Példa:** +0.6%-os mozgás, de a taker forgalom fele-fele vétel/eladás →
> `imbalance ≈ 0` → **nincs impulzus**, mert nem egyirányú a kötésáramlás.

### `maxSingleStepPct` — alap: `35`
Ha a mozgás ennél nagyobb részét egyetlen árlépés adta (könyv-söprés), nincs impulzus.

## 2. Setup — az impulzus utáni szerkezet követése

### `setupTimeoutSec` — alap: `90`
Ennyi idő után a setupot eldobjuk, ha nem konfirmálódott. Ez teszi lehetővé,
hogy 30–90 másodperces szerkezetet is végigkövessünk, ne csak a másodperces V-t.

### `invalidateBeyondOriginPct` — alap: `20`
Ha az ár az impulzus **lábának** ennyi %-ával az impulzus kiindulópontja alá megy,
a setup azonnal érvénytelen — a mozgás megfordult, nincs mit folytatni vagy fordítani.

### `flowWindowSec` — alap: `5.0`
A megerősítő kötésáramlás mérési ablaka a döntés pillanatában.

## 3a. Folytatás — sekély visszahúzás, majd a pivot újratörése

```
   pivot  ─────────────────────────  100%   ← az impulzus csúcsa
                                      62%   ← eddig még folytatás (maxPullbackPct)
                                      15%   ← eddig kell visszahúznia (minPullbackPct)
   origin ─────────────────────────    0%   ← az impulzus kiindulópontja
```

### `minPullbackPct` — alap: `15`
Legalább ennyi visszahúzás kell a lábból, hogy a pivot **rögzüljön** — enélkül a
pivot folyamatosan követné az árat, és sosem lenne mit áttörni.

### `maxPullbackPct` — alap: `62`  ⭐
Ha a visszahúzás ennél mélyebbre ment, ez már nem "sekély" — inkább fordulóra
utal, nem folytatásra.

### `breakoutOfLegPct` — alap: `5`
Ekkora áttörés kell a pivot fölött (a láb %-ában), hogy ne legyen zaj.

### `minConfirmImbalance` — alap: `0.15`
Az újratörés pillanatában a kötésáramlásnak ennyire kell a belépő irányába
mutatnia.

## 3b. Fordulás — kifulladás, majd a szint visszavétele

```
   pivot (szélsőérték) ─────────────  100%
   counter (a fordulás szintje)         *   ← ez rögzül, ha van ellen-visszahúzás
   maxEntryRetracePct-ig lehet belépni  ↑
   origin ─────────────────────────    0%
```

### `exhaustionSec` — alap: `10.0`  ⭐
Ennyi ideje nem volt új szélsőérték — enélkül egy még élő mozgás közepén
próbálnánk fordulót jelezni.

### `minReversalImbalance` — alap: `0.20`  ⭐
A kötésáramlásnak ennyire meg kell fordulnia a belépő irányába.

### `counterPullbackPct` — alap: `30`
A fordulás szintje (`counter`) csak akkor **rögzül**, ha az ártól legalább
ennyi ellen-visszahúzás történt — pontosan úgy, ahogy egy csúcsból swing-csúcs
lesz. Enélkül a szint folyamatosan az árral csúszna, és sosem lenne mit áttörni.

### `reclaimOfLegPct` — alap: `5`
Ekkora áttörés kell a rögzült szinten.

### `reclaimHoldSec` — alap: `3.0`  ⭐
Az áttörésnek **ennyi ideig tartania is kell** — minden kötésnél ellenőrizve.
Ha visszaesik a szint mögé, a jelzés elmarad (de a setup nem áll le, újra
várakozik).

### `maxEntryRetracePct` — alap: `50`
A belépő pillanatáig a mozgásnak legfeljebb ennyi %-a jöhetett vissza — efölött
a kereskedhető rész már elfogyott.

## 4. Könyv és trend — ezek BEFOLYÁSOLJÁK a döntést

A régi rendszerben az order book és az EMA csak az üzenetbe került, döntésre nem
hatott. Most **folyamatosan streamel** (`<symbol>@depth20@500ms`), és a döntés
pillanatában már készen áll.

### `maxOpposingBookImbalance` — alap: `0.40`
Ha a legjobb szinten ennél nagyobb túlsúly áll a **belépő ellen**, nincs jelzés.

### `wallBlockDistPct` — alap: `0.15`
Ha a belépő irányában ilyen közeli falat találunk, nincs jelzés.

### `depthLevels` — alap: `20`
A partial book depth stream szintjei: `5`, `10` vagy `20` lehet (Binance limit).

### `depthUpdateSpeed` — alap: `"500ms"`
`100ms` vagy `500ms` lehet (Binance limit).

### `wallSensitivity` — alap: `3.0`
Fal = a többi szint mediánjának ennyiszerese.

### `wallMaxDistancePct` — alap: `1.5`
Ennél távolabbi falat figyelmen kívül hagyunk.

### `requireTrendForContinuation` — alap: `true`
A folytatás egyezzen az 1 perces EMA trend irányával.

### `requireTrendForReversal` — alap: `false`
A forduló **szándékosan szembe megy** a rövid távú trenddel — épp azt keresi,
amikor a trend megfordul.

### `emaFast` / `emaSlow` / `emaInterval` — alap: `9` / `21` / `"1m"`

### `emaRefreshSec` — alap: `60`
Ennyi időnként frissül minden figyelt pár EMA-ja, egyenletesen elosztva (nem
egyszerre) — így egy frissítési kör sem üti meg a rate limitet.

## 5. Kimenet

### `symbolCooldownSec` — alap: `600`
Páronként ennyi szünet két jelzés között.

---

# `telegram`

### `enabled` — alap: `true`
### `statusEveryMinutes` — alap: `20`
Ennyi percenként egy életjel Telegramra. `0` = nincs.

### `statusRecentSignals` — alap: `3`
Az életjel táblázatában **típusonként** (setup-onként) ennyi legutóbbi jelzés
jelenik meg.

### `botToken` / `chatId` / `chatIds` / `appLinkTemplate`
Mint korábban — a `chatIds` most `{"scalp": ""}` alakú, ha külön csatornára
akarod vinni a scalp jelzéseket.

---

# Mit mutat a STATUS sor és az életjel

```
🟦 ELETJEL
14:20:03 UTC  ·  3h 12p ota fut

ALLAPOT
  figyelt par          58 db
  WS kapcsolat         1/1
  kotes / perc         1,932
  jelzes indulas ota   7 db
  kizarva              kizarva 2: tul szeles a spread: 2

ELO SETUPOK
  IMPULSE_DETECTED       3
  WAITING_CONFIRMATION   1
  COOLDOWN               4

DETEKTOR ALLAPOT
  • setup: impulzus 3, megerositesre var 1, cooldown 4 | normal kesz: 55/58 par
    | legkozelebb: SOLUSDT 0.31% (kell 0.80%)

NYERO / BUKO JELZESEK
tipus              db nyero  buko nyitott arany atlag MFE atlag MAE
LONG_CONTINUATION   7     4     2       1   67%    +0.60%    -0.35%
LONG_REVERSAL       3     2     0       1   100%    +0.90%    -0.15%

UTOLSO 3 LONG_CONTINUATION
SOLUSDT     09-01 04:02  belepo 184.21        MFE  +0.60%  MAE  -0.20%  -> nyero
```

- **`ELO SETUPOK`** — hány setup van épp az egyes állapotokban.
- **`NYERO / BUKO JELZESEK`** — a `reportTp`/`reportSl` pár alapján: melyiket
  érte el előbb a jelzés. `nyitott` = egyiket sem érte még el ebben a mérésben.
- **`atlag MFE` / `atlag MAE`** — a legjobb, illetve legrosszabb pont átlaga a
  mérés alatt, a jelzés irányában (a `reportTp`/`reportSl`-től függetlenül).
- **`UTOLSO ... JELZES`** — setup-típusonként a legutóbbi jelzések, a tényleges
  belépő árral és a mért MFE/MAE-vel.

## Elutasítási okok

| ok | mit jelent |
|---|---|
| `blacklisted` / `not_whitelisted` | kézi kizárás a `market` dokumentumban |
| `no_book_data` | még nem láttuk a pár order book tetejét |
| `spread_too_wide` | a spread szélesebb, mint `maxSpreadPct` |
