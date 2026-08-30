# pump-dump-detector

Valós idejű pump/dump detektor a **Binance USDⓈ-M Futures** perpetual piacra.
Másodperces skálán észreveszi a hirtelen ármozgásokat (nem vár gyertyazárásra),
gyors order book + EMA elemzést végez, pontoz, **Telegramra** küld, és opcionálisan
(alapból **kikapcsolva**) pozíciót is nyit.

```
Binance WebSocket -> MarketDataService -> MovementDetector
   -> (trigger) -> OrderBookAnalyzer + TAAnalyzer -> scoring
   -> SignalService -> MongoDB -> TelegramNotifier -> [TradingService]
```

---

## Előfeltételek

| | macOS (Apple Silicon és Intel) | Ubuntu |
|---|---|---|
| Docker | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | `sudo apt install docker.io docker-compose-plugin` |
| ellenőrzés | `docker compose version` | `docker compose version` |

Az image-ek (`python:3.12-slim`, `mongo:7`) hivatalos multi-arch buildek, így
`arm64` (Apple Silicon) és `amd64` (Ubuntu) alatt is ugyanaz a parancs működik —
nem kell `--platform` kapcsoló.

> Ubuntun, ha a `docker` parancs `permission denied`-et ad:
> `sudo usermod -aG docker $USER`, majd be- és kijelentkezés.

---

## 1. Telegram bot létrehozása

1. Telegramban írj a [@BotFather](https://t.me/BotFather)-nek: `/newbot`, adj neki nevet.
   A válaszban kapott token lesz a `TELEGRAM_BOT_TOKEN`.
2. Írj egy üzenetet az új botodnak (különben nem tud neked küldeni).
3. A chat ID-ért nyisd meg böngészőben:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → a `result[0].message.chat.id`
   érték lesz a `TELEGRAM_CHAT_ID`.

Csoportba küldéshez add hozzá a botot a csoporthoz, és a chat ID negatív szám lesz.

## 2. Konfiguráció

```bash
git clone https://github.com/kulig1985/pump-dump-detector.git
cd pump-dump-detector
cp .env.example .env
```

Szerkeszd a `.env` fájlt:

```bash
MONGO_URL=mongodb://host.docker.internal:27017
MONGO_DB=pumpdump

TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789

# csak akkor kell, ha később bekapcsolod az auto tradinget
BINANCE_API_KEY=
BINANCE_API_SECRET=
FUTURES_TESTNET=0
```

A `.env` a `.gitignore`-ban van, nem kerül a repóba.

## 3. Indítás

### a) Van már saját MongoDB-d (alapértelmezés)

```bash
docker compose up --build
```

A `MONGO_URL` alapból a gazdagépre mutat (`host.docker.internal`). Ehhez a mongod-nak
elérhetőnek kell lennie a Docker bridge felől — a **mongod alapból csak a
`127.0.0.1`-re hallgat**, ami a konténerből nem érhető el. Két megoldás:

1. **Nyisd meg a mongod-ot a bridge felé** (`/etc/mongod.conf`):
   ```yaml
   net:
     bindIp: 127.0.0.1,172.17.0.1     # a docker0 interfész címe
   ```
   `sudo systemctl restart mongod`. A `172.17.0.1`-et az `ip addr show docker0` mutatja meg.
   Ne írj ide `0.0.0.0`-t tűzfal nélkül.

2. **Vagy futtasd a detectort a host hálózaton** (csak Linuxon) — nem kell mongod-ot
   piszkálni. A `docker-compose.yml`-ben a `detector` alá:
   ```yaml
       network_mode: host
   ```
   és a `.env`-ben `MONGO_URL=mongodb://127.0.0.1:27017`.

Ellenőrzés, hogy a konténer eléri-e:
```bash
docker compose run --rm detector python -c \
  "import socket;socket.create_connection(('host.docker.internal',27017),3);print('elerheto')"
```

### b) Nincs saját MongoDB-d

Indítsd a projekttel együtt — ilyenkor `MONGO_URL=mongodb://mongo:27017` kell a `.env`-be:

```bash
docker compose --profile local-mongo up --build
```

### Várt kimenet

Utána a logban ezt kell látnod:

```
12:04:11 INFO  db        MongoDB kapcsolat kesz: mongodb://mongo:27017/pumpdump
12:04:11 INFO  config    Config letrehozva defaultokkal: detector
12:04:11 INFO  main      Kuszobok: 1s 0.30% | 3s 0.60% | 5s 0.90% | min score 60 | cooldown 60s
12:04:11 INFO  main      Auto trading: KI
12:04:12 INFO  rest      Perpetual USDT parok: 412 | forgalom >= 50,000,000 USDT: 187 | figyelunk: 187
12:04:12 INFO  market    Indul 2 WebSocket kapcsolat, osszesen 187 symbol
12:04:13 INFO  market    WS #1 csatlakozva (150 stream)
12:04:13 INFO  market    WS #2 csatlakozva (37 stream)
```

Innentől **5 másodpercenként kiírja, mi történik éppen az árakkal** — a 10 legmozgékonyabb
párt, és hogy miért nincs (még) jelzés:

```
07:56:38 INFO  market
  ──────────────────────────────────────────────────────────────────────────────
  MI TORTENIK MOST   187 par figyelese   jelzes indulas ota: 1
  az elmult 5 masodpercben 2,061 arvaltozas erkezett   (2/2 kapcsolat el)
  jelzes kell hozza: 1 mp alatt 0.30%, 3 mp alatt 0.60%, 5 mp alatt 0.90%
  ──────────────────────────────────────────────────────────────────────────────
  par               arfolyam     1 mp     3 mp     5 mp   mi van vele
  WIFUSDT         0.85230384   +0.21%   +0.60%   +1.02%   jelzes mar elment, varakozas a kovetkezoig
  PEPEUSDT        0.00000932   -0.14%   -0.40%   -0.68%   erosen esik, meg 0.22% hianyzik a jelzeshez
  SUIUSDT             3.1350   +0.06%   +0.16%   +0.27%   emelkedik, de meg messze van a jelzestol
  BTCUSDT          61,013.42   +0.00%   +0.01%   +0.02%   alig mozdul
  ...
  ──────────────────────────────────────────────────────────────────────────────
```

Ha nem érkezik adat, `ERROR` szintű sort kapsz helyette. Ugyanez a Mongo `status`
collectionben is frissül, tehát kívülről is monitorozható:

```js
db.status.findOne({_id:"detector"})
```

A gyakoriságot és a tábla méretét a `statusIntervalSec` állítja.

Ha van mozgás:

```
12:05:22 WARN  detector  [PEPEUSDT] TRIGGER LONG | 1s +0.34% | 3s +0.71% | 5s +1.02%
12:05:22 INFO  orderbook [PEPEUSDT] 20 szint | akadaly LONG iranyban: 0.83% tavolsagra (4.2x atlag)
12:05:22 INFO  ta        [PEPEUSDT] EMA9 0.0000124 > EMA21 0.0000123 -> bullish (ar van EMA9 felett)
12:05:22 WARN  signal    [PEPEUSDT] SCORE 78/100 | mozgas 1.1x kuszob, gyorsulo, EMA bullish, wall 0.83%-ra
12:05:23 INFO  telegram  [PEPEUSDT] ertesites elkuldve
12:05:23 INFO  trading   [PEPEUSDT] auto trading KI -- nincs megbizas
```

Háttérben: `docker compose up -d --build`, log: `docker compose logs -f detector`,
leállítás: `docker compose down`.

> A lenti `docker compose exec mongo ...` parancsok a `local-mongo` profilra vonatkoznak.
> Saját Mongo esetén simán `mongosh pumpdump` a gazdagépen.

### Működik-e? — gyors próba

Nyugodt piacon órákig nem jön jelzés. Ideiglenesen vedd le a küszöböt:

```bash
docker compose exec mongo mongosh pumpdump --eval \
  'db.config.updateOne({_id:"detector"},{$set:{priceChangeThreshold1s:0.05}})'
```

Percen belül jönnie kell triggernek. A config 30 másodpercen belül magától újratöltődik,
**nem kell újraindítani**. Utána állítsd vissza `0.30`-ra.

### Docker nélkül

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URL=mongodb://localhost:27017      # kell egy futó MongoDB
python -m app.main
```

Teszt hálózat és Mongo nélkül: `python3 tests/test_core.py`

---

## Hangolás

Minden beállítás a MongoDB `config` collectionjében van, három dokumentumban.
**A DB az igazság** — módosítás után 30 másodpercen belül él, újraindítás nélkül.

```bash
docker compose exec mongo mongosh pumpdump
```

```js
db.config.find().pretty()

// érzékenyebb detektor
db.config.updateOne({_id:"detector"}, {$set:{priceChangeThreshold1s:0.20, minSignalScore:50}})

// az utolsó 5 jelzés
db.signals.find().sort({timestamp:-1}).limit(5)
```

### `detector`

| kulcs | default | mit csinál |
|---|---|---|
| `enabled` | `true` | fő kapcsoló |
| `telegramEnabled` | `true` | értesítés küldése |
| `minQuoteVolume24h` | `50000000` | ez alatti 24h forgalmú párokat kihagyjuk |
| `maxSymbols` | `200` | top N pár forgalom szerint |
| `priceChangeThreshold1s/3s/5s` | `0.30 / 0.60 / 0.90` | trigger küszöb %-ban |
| `minSignalScore` | `60` | ez alatt csak mentünk, nem küldünk |
| `symbolCooldownSec` | `60` | ugyanarra a párra ennyi ideig nincs új jelzés |
| `statusIntervalSec` | `5` | ilyen sűrűn írja ki, mi történik az árakkal |
| `signalWindowMinutes` | `10` | ekkora visszatekintéssel számolja, hányadik a jelzés |
| `orderBookLevels` | `20` | vizsgált árszintek (5 / 10 / 20) |
| `wallSensitivity` | `3.0` | wall = szint ≥ 3× az oldal átlaga |
| `wallMaxDistancePct` | `1.5` | ennél távolabbi wall már nem érdekes |
| `emaFast` / `emaSlow` / `emaInterval` | `9 / 21 / 1m` | trendfilter |

### `trading` — alapból kikapcsolva

| kulcs | default |
|---|---|
| `autoTradingEnabled` | **`false`** |
| `positionSizeUSDT` | `20` (notional, nem margin) |
| `leverage` / `marginMode` | `5` / `CROSSED` (EU-ban az ISOLATED nem elérhető) |
| `takeProfitPct` / `stopLossPct` | `1.5` / `0.8` |
| `maxOpenPositions` | `3` |
| `longEnabled` / `shortEnabled` | `true` / `true` |
| `minScoreForTrade` | `75` |

**Bekapcsolás előtt testneten próbáld:** `FUTURES_TESTNET=1` a `.env`-ben,
[testnet kulcsok](https://testnet.binancefuture.com/) beírása, majd:

```js
db.config.updateOne({_id:"trading"}, {$set:{autoTradingEnabled:true}})
```

A Binance API kulcson engedélyezni kell a Futures kereskedést, és érdemes
IP-korlátozást beállítani.

---

## Collectionök

Egyet sem kell kézzel létrehozni, az alkalmazás megcsinálja.

- `config` — a fenti három dokumentum
- `signals` — minden detektált jelzés (score, EMA, order book összefoglaló, Telegram/trade státusz)
- `market_snapshots` — a trigger körüli nyers adat (ártörténet, 20 szintes könyv, score inputok), `signalId`-vel visszaköthető
- `orders` — a TradingService eredményei és hibái
- `status` — egyetlen dokumentum (`_id: "detector"`), 5 másodpercenként frissül:
  uptime, tick/s, élő WS kapcsolatok, trigger számláló, a 10 legmozgékonyabb pár

## Binance API — mit használunk

WebSocket (`wss://fstream.binance.com`):
- `/market/stream` + `SUBSCRIBE` üzenet — árfolyam tickek. A `/market` szegmens
  kötelező (lásd `app/market_data.py` fejlécét). Max 200 feliratkozás / kapcsolat.
- `/public/ws/<sym>@depth20@100ms` — csak triggerkor, első üzenet után azonnal bontunk.
  A depth a doksiban a *public* csoportba tartozik, ezért `/public` a szegmense (az
  aggTrade-é `/market`).

WebSocket API (`wss://ws-fapi.binance.com/ws-fapi/v1`): `order.place`, `v2/account.position`.

REST (`https://fapi.binance.com`) — csak ahol nincs WS megfelelő:
`/fapi/v1/exchangeInfo`, `/fapi/v1/ticker/24hr` (symbol univerzum), `/fapi/v1/klines` (EMA),
`/fapi/v1/leverage`, `/fapi/v1/marginType` (a WS API-ban nincs rájuk metódus).

## Hibakeresés

| tünet | ok / megoldás |
|---|---|
| `TimeoutError` a `rest` loggerben | a Binance API nem érhető el a hálózatodról (tűzfal, régiókorlát) |
| a detector csendben áll, nincs `MongoDB kapcsolat kesz` sor | nem éri el a Mongo-t — lásd a 3/a pont `bindIp` részét |
| `hianyzik a Telegram token vagy chatId` | töltsd ki a `.env`-et és `docker compose up -d --force-recreate` — az üres DB-értéket felülírja az env. Vagy közvetlenül: `db.config.updateOne({_id:"telegram"},{$set:{botToken:"...",chatId:"..."}})` |
| nincs jelzés órák óta | normális nyugodt piacon — a `MI TORTENIK MOST` tábla `mi van vele` oszlopa megmondja, mennyi hiányzik a jelzéshez. Ha tartósan „alig mozdul", vedd lejjebb a küszöböt |
| `EGY symbol sem felel meg a ... forgalmi kuszobnek` | vedd lejjebb a `minQuoteVolume24h` értéket |
| kevés symbolt figyel | a `Legnagyobb / legkisebb bevalasztott` log sor mutatja, hol húz a szűrő |
| `WS #1 szakadas ... ujracsatlakozas` | átmeneti hálózati hiba, magától visszaáll (exponenciális backoff) |

## Figyelmeztetés

Ez egy jelzésdetektor, nem befektetési tanács. Az automatikus kereskedés valódi pénzt
kockáztat — csak testneten letesztelve, saját felelősségre kapcsold be.
