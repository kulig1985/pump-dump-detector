# pump-dump-detector

Valós idejű pump/dump detektor a **Binance USDⓈ-M Futures** perpetual piacra.
Másodperces skálán észreveszi a hirtelen ármozgásokat (nem vár gyertyazárásra),
gyors order book + EMA elemzést végez, pontoz, **Telegramra** küld, és opcionálisan
(alapból **kikapcsolva**) pozíciót is nyit.

```
Binance WebSocket  (aggTrade + !bookTicker)
        ↓
  KERESKEDHETOSÉG      spread / mélység / aktivitás / white- és blacklist
        ↓
  DetectorManager  →  PumpDumpDetector,  ReversalDetector
        ↓
   SIGNAL            (order book és EMA információként hozzáfűzve)
        ↓
  MongoDB → Telegram → [TradingService]
```

Két detektor fut párhuzamosan ugyanazon a trade-folyamon, külön konfigurációval:

| detektor | mit keres | config dokumentum |
|---|---|---|
| `pump_dump` | hirtelen, egyirányú ármozgás — az utolsó N trade meredeksége | `detector` |
| `reversal` | rövid távú lokális árforduló (mélypont → visszapattanás → micro-high áttörés) | `reversal` |

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

### Tényleg az új kód fut?

Induláskor az első sor a kód ujjlenyomata:

```
INFO  main      Kod ujjlenyomat: af176a05d9
```

Ugyanez kiszámolható a gazdagépen — ha a kettő egyezik, a konténerben az van, ami a
munkakönyvtáradban:

```bash
git pull
python3 -c "import hashlib,pathlib;h=hashlib.sha256()
[h.update(f.read_bytes()) for f in sorted(pathlib.Path('app').rglob('*.py'))]
print(h.hexdigest()[:10])"

docker compose logs detector | grep "Kod ujjlenyomat" | tail -1
```

Ha nem egyezik, tiszta újraépítés:

```bash
docker compose down
docker compose build --no-cache detector
docker compose up -d
```

A `git pull` sikerét is érdemes ellenőrizni (`git log --oneline -3`) — ha helyi
módosításod van, a pull elhasalhat, és akkor a régi forrásból épül az image.

### Futtatás a háttérben (detached)

```bash
docker compose up -d --build          # indítás, a terminál visszaadja a promptot
docker compose logs -f detector       # élő log, Ctrl+C csak a nézést állítja le
docker compose ps                     # fut-e
docker compose restart detector       # újraindítás (config változáshoz NEM kell)
docker compose down                   # leállítás
```

A `logs -f` **nem** állítja le a konténert, csak a kiírást. Ha kilépsz az SSH-ból, a
detector fut tovább (`restart: unless-stopped`).

Hasznos log-parancsok:

```bash
docker compose logs -f --tail 100 detector          # az utolsó 100 sortól élőben
docker compose logs --since 10m detector            # az elmúlt 10 perc
docker compose logs -f detector | grep -E "TRIGGER|FORDULO|SCORE"   # csak a jelzések
docker compose logs -f detector | grep "EREDMENYEK" -A 8            # csak az összesítő
docker compose logs detector > detector.log         # mentés fájlba
```

A log a Docker journalba megy. Ha sokáig fut, korlátozd a méretét — a `detector`
szolgáltatás alá a `docker-compose.yml`-ben:

```yaml
    logging:
      driver: json-file
      options: {max-size: "50m", max-file: "3"}
```

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

## Hogyan dönt a rendszer

**Nem fix küszöbbel.** A kérdés nem az, hogy „mozdult-e 0.3%-ot", hanem hogy *szokatlan-e
ez a mozgás ezen a páron*. Futás közben, páronként mérjük, mi a normális:

```
baseline = az utolso 5 percben mert 2 masodperces |elmozdulasok| medianja
jelzeshez kell:  |mozgas| >= max( minMovePct , baselineRatio × baseline )
```

Ehhez jön két megerősítés — egyirányúság (a lépések 70%-a egy felé) és forgalom (az
ablakban legalább a pár átlaga) —, majd a validáció: spread, fal az útban, hozam/kockázat.

**Nincs 0-100 score.** Minden jelzés egy indoklás-listát és a hozzá tartozó mért számokat
viszi, és minden elutasításnak gépi neve van:

```
CANDIDATE  SOLUSDT  LONG  move +0.32% / 2.1s  baseline 4.1x
REJECTED   SOLUSDT  LONG  spread_too_wide
SIGNAL     BTCUSDT  LONG  move +0.24% / 2.1s  rr 2.4:1  https://www.binance.com/en/futures/BTCUSDT
STATUS     136 par (kizarva 24 par: spread_too_wide: 18  low_activity: 6) | 1,932 tick/60s | 12 candidate, 3 jelzes, 9 elutasitva | Telegram: BE
```

A `REJECTED` dokumentumok is a `signals` collectionbe kerülnek, így egy lekérdezéssel
látszik, mi miért esik ki:

```js
db.signals.aggregate([{$match:{status:"rejected"}},
                      {$group:{_id:"$reasons", db:{$sum:1}}}, {$sort:{db:-1}}])
```

Részletes detektor-állapot: `LOG_LEVEL=DEBUG`.

**Minden `SIGNAL` azonnal megy Telegramra** — nincs mérési előfeltétel. Az eredménymérés
fut és 10 percenként összesít, de semmit nem kapuz.

Minden beállítás leírása: **[docs/PARAMETEREK.md](docs/PARAMETEREK.md)**

## Collectionök

Egyet sem kell kézzel létrehozni, az alkalmazás megcsinálja.

- `config` — a fenti három dokumentum
- `signals` — minden detektált jelzés (score, EMA, order book összefoglaló, Telegram/trade státusz)
- `market_snapshots` — a trigger körüli nyers adat (ártörténet, 20 szintes könyv, score inputok), `signalId`-vel visszaköthető
- `orders` — a TradingService eredményei és hibái
- `status` — egyetlen dokumentum (`_id: "detector"`), 5 másodpercenként frissül:
  uptime, tick/s, élő WS kapcsolatok, jelzésszámláló, az aktív detektorok és a
  konzolon látható státusz sorok

## Binance API — mit használunk

WebSocket (`wss://fstream.binance.com`):
- `/market/stream` + `SUBSCRIBE` — árfolyam tickek (`<sym>@aggTrade`). Max 200
  feliratkozás / kapcsolat, ezért 150-esével bontjuk.
- `/public/stream` + `SUBSCRIBE ["!bookTicker"]` — **külön kapcsolat**, egyetlen
  feliratkozás az egész piac legjobb bid/ask árára és mennyiségére. Az aggTrade a
  `market`, a bookTicker a `public` csoportba tartozik, és ez az URL szegmensben is
  megjelenik — rossz szegmensen a Binance nyugtáz, de nem küld adatot.
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
