# pump-dump-detector

Valós idejű **scalp belépő-detektor** a **Binance USDⓈ-M Futures** perpetual piacra.
Egy hirtelen impulzus (ár + agresszív forgalom + kötésáramlás) önmagában NEM jelzés
— csak egy setup kezdete. A rendszer utána figyeli a szerkezetet, és csak akkor
jelez, ha az **folytatódik** (sekély visszahúzás, majd újratörés) vagy **megfordul**
(kifulladás, majd a szint visszavétele). **Telegramra** küld, és opcionálisan
(alapból **kikapcsolva**) pozíciót is nyit.

```
Binance WebSocket  (aggTrade + !bookTicker + depth20, FOLYAMATOSAN)
        ↓
  KERESKEDHETOSÉG      spread / white- és blacklist
        ↓
  ScalpDetector  →  IMPULZUS  →  SetupTracker  →  folytatás / fordulás
        ↓                         (a könyv és az EMA MÁR itt számít, cache-ből)
   SIGNAL
        ↓
  MongoDB → Telegram → [TradingService]
        ↓
  OutcomeTracker  →  MFE/MAE + TP/SL mérés, folyamatosan
```

| setup típusa | mit keres |
|---|---|
| `LONG_CONTINUATION` / `SHORT_CONTINUATION` | impulzus, sekély visszahúzás, majd a csúcs/mélypont újratörése |
| `LONG_REVERSAL` / `SHORT_REVERSAL` | impulzus kifullad, a kötésáramlás fordul, a visszahúzás szintje visszavéve |

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
12:04:11 INFO  config    Config letrehozva defaultokkal: market
12:04:11 INFO  main      Piac: USDT, USDC parok, forgalom >= 120,000,000, max 60 par, spread <= 0.050%
12:04:11 INFO  main      Impulzus: a mozgas a par normaljanak 6.0x-e (min 0.40%), forgalom >= 50,000 USDT es a normal 3.0x-e, ...
12:04:11 INFO  main      Telegram: BE -- minden SIGNAL azonnal megy   |   Auto trading: KI (CROSSED, 5x)
12:04:12 INFO  rest      Perpetual USDT/USDC parok: 412 | forgalom >= 120,000,000 USDT: 60 | figyelunk: 60
12:04:12 INFO  market    Indul 1 arfolyam- es 1 konyv-kapcsolat, osszesen 60 symbol
12:04:13 INFO  market    WS #1 csatlakozva (60 stream)
```

Percenként egy STATUS sor mutatja, mi történik éppen:

```
STATUS  60 par | 1,932 tick/60s | konyv-melyseg: 60 par | 0 candidate, 0 jelzes | ujracsatlakozas 0/5perc
   normal kesz: 55/60 par | legkozelebb: SOLUSDT 0.31% (kell 0.80%)
```

Amint egy impulzus elindul, majd megerősödik, ez látszik:

```
IMPULSE_UP SOLUSDT   ar 184.21  +0.62% / 1.9s  normal 0.041%  forgalom 560,000 USDT (28.0x)  flow +0.80
WAITING    SOLUSDT   pivot 184.40  visszahuzas 22% a labbol
SETUP OK   SOLUSDT   LONG_CONTINUATION  ar 184.55  lab 0.62%  visszahuzas 22%  flow +0.60  kor 14 mp
SIGNAL     SOLUSDT   LONG_CONTINUATION  ar 184.55  https://www.binance.com/en/futures/SOLUSDT
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
docker compose logs -f detector | grep -E "IMPULSE|SETUP OK|SIGNAL"  # csak a jelzések
docker compose logs -f detector | grep "EREDMENY" -A 8              # csak az osszesito
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
# app/config.py -> DETECTOR_DEFAULTS: vedd le ideiglenesen a kuszoboket
"minImpulsePct": 0.10,
"impulseBaselineRatio": 2.0,
# majd:
git pull && docker compose up -d --build
```

Percen belül jönnie kell egy `IMPULSE_...` sornak a logban. **Ez a hangolás mindig a
kódban történik** — a `config` collectiont a rendszer minden indításkor törli, tehát
egy mongo shell parancs itt nem maradna meg. Utána állítsd vissza az eredeti értékre.

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
ez a mozgás ÉS a mögötte álló pénz ezen a páron*. Futás közben, páronként mérjük, mi a
normális — ár ÉS forgalom is:

```
jelzeshez kell:  |mozgas| >= max( minImpulsePct , priceBaseline × impulseBaselineRatio )
             ES  forgalom >= max( minImpulseNotional , notionalBaseline × notionalRatio )
             ES  a kotesaramlas egyiranyu (imbalance >= minImpulseImbalance)
```

Ez az **impulzus** — de önmagában még nem jelzés, csak egy setup kezdete. A setup
csak akkor válik jelzéssé, ha a szerkezet (visszahúzás + újratörés, vagy kifulladás +
a szint visszavétele) és a könyv/EMA is megerősíti:

```
IMPULSE_UP SOLUSDT   ar 184.21  +0.62% / 1.9s  forgalom 560,000 USDT (28.0x)  flow +0.80
WAITING    SOLUSDT   pivot 184.40  visszahuzas 22% a labbol
SETUP OK   SOLUSDT   LONG_CONTINUATION  ar 184.55  lab 0.62%  visszahuzas 22%  flow +0.60
SIGNAL     SOLUSDT   LONG_CONTINUATION  ar 184.55  https://www.binance.com/en/futures/SOLUSDT
```

Az elutasítási okoknak gépi nevük van, a `signals` collectionbe kerülnek:

```js
db.signals.aggregate([{$group:{_id:"$setup", db:{$sum:1}}}, {$sort:{db:-1}}])
```

Részletes detektor-állapot: `LOG_LEVEL=DEBUG`.

**Minden jelzés azonnal megy Telegramra.** Az eredménymérés (MFE/MAE, TP/SL) a jelzés
után folyamatosan fut, de semmit nem kapuz — csak utólag mutatja meg, melyik setup
működik.

Minden beállítás leírása: **[docs/PARAMETEREK.md](docs/PARAMETEREK.md)**

## Collectionök

Egyet sem kell kézzel létrehozni, az alkalmazás megcsinálja.

- `config` — a fenti négy dokumentum (`market`, `detector`, `trading`, `telegram`)
- `signals` — minden jelzés (setup típusa, indoklás, mért számok, Telegram/trade státusz,
  plusz az `outcome` mezőben a folyamatos MFE/MAE + TP/SL mérés)
- `market_snapshots` — a setup körüli nyers adat (ártörténet, order book), `signalId`-vel visszaköthető
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
| `A Binance 418 valaszt adott ... AZ IP-T IDEIGLENESEN KITILTOTTA` | rate limit miatti IP-tiltás. A rendszer **nem áll le**: a legutóbb mentett symbol listával fut tovább, és a `Retry-After` szerint próbálkozik újra. Két gyakori oka van: **crash-loop** (a konténer újraindul, és minden indulás lő egy `exchangeInfo` + `ticker/24hr` hívást), vagy **újracsatlakozási vihar** (szakadozó hálózat esetén sűrű WS reconnect). Mindkettőre van fék; a STATUS sor és az életjel kiírja az `ujracsatlakozas N/5perc` értéket — ha ez tartósan magas, a hálózat szakadozik |
| `hianyzik a Telegram token vagy chatId` | töltsd ki a `.env`-et és `docker compose up -d --force-recreate` — az üres DB-értéket felülírja az env. |
| nincs jelzés órák óta | normális nyugodt piacon — a `MI TORTENIK MOST` tábla `mi van vele` oszlopa megmondja, mennyi hiányzik a jelzéshez. Ha tartósan „alig mozdul", vedd lejjebb a küszöböt |
| `EGY symbol sem felel meg a ... forgalmi kuszobnek` | vedd lejjebb a `minQuoteVolume24h` értéket |
| kevés symbolt figyel | a `Legnagyobb / legkisebb bevalasztott` log sor mutatja, hol húz a szűrő |
| `WS #1 szakadas ... ujracsatlakozas` | átmeneti hálózati hiba, magától visszaáll (exponenciális backoff) |

## Figyelmeztetés

Ez egy jelzésdetektor, nem befektetési tanács. Az automatikus kereskedés valódi pénzt
kockáztat — csak testneten letesztelve, saját felelősségre kapcsold be.
