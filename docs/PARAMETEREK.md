# Paraméterek — mit jelent, és mi történik, ha átállítod

## Hol állítod: `app/config.py`

**Az alapértékek a kódban vannak, és hidegindítással minden beállítás felépül.**
A MongoDB `config` collection csak egy másolat, amit induláskor a kód hoz létre:

```
app/config.py            ->  config collection (MongoDB)
  MARKET_DEFAULTS              market     melyik párokat figyeljük + eredménymérés
  DETECTOR_DEFAULTS            detector   a scalp detektor
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
beállítás újra létrejön**. Ezt teszt őrzi (`test_cold_start_creates_every_setting`).

> **MINDEN ÉRTÉK ITT KIINDULÁSI PARAMÉTER.** Nem "helyes" értékek — kezdőpontok,
> amelyeket az `EREDMENY` mérésből (MFE/MAE, TP/SL) kell hangolni.

---

## Az algoritmus: egyetlen út

```
IMPULZUS  ->  PULLBACK  ->  FRISS KITÖRÉS  ->  JELZÉS
```

Állapotgép páronként, egyszerre **egy** aktív setup:

```
IDLE -> IMPULSE -> WAIT_PULLBACK -> WAIT_BREAKOUT -> SIGNAL -> COOLDOWN -> IDLE
```

Nincs reversal ág, nincs EMA a belépő döntésben, nincs fal- vagy
könyv-imbalance kapu. Egy jól érthető setup, amit mérni lehet.

Minden méret az **impulzus-láb** (`leg = |pivot − p0|`) arányában értendő — így
ugyanaz a beállítás működik egy 0.4%-os és egy 4%-os impulzusnál. A `leg` a
pivottal **együtt frissül**, amíg az ár új szélsőértéket csinál.

---

# `market` — melyik párokat figyeljük, és hogyan mérjük az eredményt

### `enabled` — alap: `true`
Az egész feldolgozás ki-/bekapcsolása.

### `quoteAssets` — alap: `["USDT", "USDC"]`
### `minQuoteVolume24h` — alap: `120 000 000`
Ennél kisebb 24 órás forgalmú párok kiesnek.

### `maxSymbols` — alap: `60`
A forgalom szerint rendezett lista első ennyi eleme.

### `symbolRefreshMinutes` — alap: `60`
### `symbolWhitelist` / `symbolBlacklist` — alap: `[]` / `[]`
Ha a whitelist nem üres, **kizárólag** azokat figyeljük.

### `maxSpreadPct` — alap: `0.05`
Ennél szélesebb vétel/eladás résnél nincs jelzés.

### `outcomeTrackSec` — alap: `600`
A jelzés után ennyi másodpercig **minden kötést** figyelünk (10 perc).

### `outcomeMarkSec` — alap: `[60, 180, 300, 600]`
Ezeknél a pontoknál rögzítjük az árat — 1 / 3 / 5 / 10 perc.
> **Példa:** `marks["300"] = {price: 100.62, pct: +0.62}` — 5 perccel a jelzés
> után az ár 100.62 volt, ami a jelzés irányában +0.62%.

### `tpLevels` — alap: `[0.3, 0.5, 0.8, 1.0]`
### `slLevels` — alap: `[0.2, 0.3, 0.5]`
Ezekre a szintekre mérjük, **mikor** érte el először a jelzés irányában, illetve
ellene. Mivel minden kötést látunk, utólag bármelyik TP/SL párra eldönthető,
melyiket érte el előbb.

### `reportTp` / `reportSl` — alap: `0.5` / `0.3`
Az összesítésben ez a TP/SL pár szerepel.

### `statusIntervalSec` — alap: `60`

---

# `detector` — a scalp detektor

### `enabled` — alap: `true`

## 1. Impulzus — ár + forgalom + kötésáramlás

Az impulzus **önmagában nem jelzés**, csak egy setup kezdete.

### `impulseWindowSec` — alap: `3.0`
Ekkora időablakban mérünk. A mozgást az ablakra **illesztett egyenes** adja, nem
a végpontok különbsége — egyetlen kiugró print nem tud impulzust csinálni.

### `minTradesInWindow` — alap: `10`
### `baselineMinutes` — alap: `5`
Ennyi perc visszatekintéssel épül a pár normálja (ár **és** forgalom). A normál
akkor „kész", ha a minták **tényleg lefedik** az ablak ~90%-át — indulás/restart
után tehát valóban kb. 5 percet vár, nem 1-et.

### `minImpulsePct` — alap: `0.40`  ⭐
Abszolút padló a mozgásra.

### `impulseBaselineRatio` — alap: `6.0`  ⭐
És a pár saját normáljának ennyiszerese.

### `minImpulseNotional` — alap: `50 000`
Abszolút padló az agresszív (taker) forgalomra USDT-ben.

### `notionalRatio` — alap: `3.0`  ⭐
És a pár normál ablak-forgalmának ennyiszerese. Ez fogja meg azt, amikor egy
nagy kötés átsöpri a könyvet kevés valódi pénzből.

### `minImpulseImbalance` — alap: `0.25`  ⭐
A taker oldal ennyire legyen egyirányú, `-1..1` skálán
(`(vétel − eladás) / összes`). `0.25` = a forgalom legalább 62.5%-a egy oldalra.

### `maxSingleStepPct` — alap: `35`
Ha a mozgás ennél nagyobb részét egyetlen árlépés adta, nincs impulzus.

## 2. Pullback

```
   pivot  ─────────────────────────  100%   ← az impulzus csúcsa (a leggel EGYÜTT frissül)
                                      62%   ← ennél mélyebb -> érvénytelen (maxPullbackPct)
                                      15%   ← eddig kell visszahúznia (minPullbackPct)
   p0     ─────────────────────────    0%   ← az impulzus kiindulópontja
```

### `minPullbackPct` — alap: `15`  ⭐
Legalább ennyi visszahúzás kell a lábból. Ekkor a **pivot rögzül**, és megszületik
a kitörési szint.

### `maxPullbackPct` — alap: `62`  ⭐
Ennél mélyebb visszahúzás után a setup érvénytelen.

### `setupTimeoutSec` — alap: `90`
Ennyi idő után eldobjuk a setupot.

### `invalidateBeyondOriginPct` — alap: `20`
Ha az ár a láb ennyi %-ával az impulzus kiindulópontja alá megy, azonnal vége.

## 3. Friss kitörés

**A jelzés CSAK a keresztezés pillanatában születhet.** Nem elég, hogy az ár
valamikor korábban áttörte a szintet és még mindig fölötte áll:

```
LONG :  előző_ár <= szint  ÉS  aktuális_ár > szint
SHORT:  előző_ár >= szint  ÉS  aktuális_ár < szint
```

### `breakoutOfLegPct` — alap: `5`
Ekkora áttörés kell a pivot fölött (a láb %-ában).

### `maxBreakoutAgeSec` — alap: `3.0`  ⭐
Ha a kitörés megtörtént, de a megerősítés nem jött össze ennyi időn belül, a
setupot eldobjuk. Nem szállunk be egy réges-régi kitörésre.

### `maxEntryExtensionPct` — alap: `25`  ⭐
Ha az ár már ennyivel (a láb %-ában) túl van a kitörési szinten, **nincs jelzés** —
a belépő már nem éri meg.

## 4. Megerősítés — csak ez a három

A kitörés pillanatában semmi más nem számít:

### `flowWindowSec` — alap: `5.0`
### `minConfirmImbalance` — alap: `0.15`  ⭐
A kötésáramlásnak ennyire kell a belépő irányába mutatnia. **Csak a pivot
rögzítése óta érkezett kötésekből számol** — az impulzus alatti áramlás nem
erősítheti meg a későbbi kitörést. LONG-nál a vételi,
SHORT-nál az eladói taker oldalnak kell dominálnia.

### `maxDataAgeSec` — alap: `5.0`  ⭐
**FAIL-CLOSED:** ennél régebbi order book / bookTicker adattal **nincs jelzés**.
Nincs „nincs adat, hát akkor átengedjük" viselkedés.

## 5. Kimenet

### `symbolCooldownSec` — alap: `600`
### `depthLevels` — alap: `20`   (`5` / `10` / `20`)
### `depthUpdateSpeed` — alap: `"500ms"`   (`100ms` / `500ms`)

---

# `telegram`

### `enabled` — alap: `true`
### `statusEveryMinutes` — alap: `20`
### `statusRecentSignals` — alap: `3`
### `botToken` / `chatId` / `chatIds` / `appLinkTemplate`

---

# Így néz ki egy jelzés

```
🟢 LONG HEMIUSDT
Entry: 0.01318400
Impulse: +1.12%
Pullback: 28%
Buy flow: 67%
Breakout age: 0.8s
https://www.binance.com/en/futures/HEMIUSDT
```

# Elutasítási okok

| ok | mit jelent |
|---|---|
| `blacklisted` / `not_whitelisted` | kézi kizárás a `market` dokumentumban |
| `no_book_data` | még nem láttuk a pár könyvét |
| `stale_book_data` | a könyv-adat régebbi, mint `maxDataAgeSec` |
| `spread_too_wide` | a spread szélesebb, mint `maxSpreadPct` |
