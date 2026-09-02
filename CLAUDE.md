# pump-dump-detect

Binance USDⓈ-M Futures perpetual piac valós idejű scalp belépő-detektora. Egy
hirtelen impulzus (ár + agresszív forgalom + kötésáramlás) után figyeli a
szerkezetet, és csak akkor jelez, ha az FOLYTATÓDIK vagy MEGFORDUL — nem a puszta
mozgásra. Telegram értesítést küld, és opcionálisan (alapból **kikapcsolva**)
pozíciót is nyit.

## Futtatás

```bash
cp .env.example .env      # MONGO_URL, Telegram token + chat ID
docker compose up -d --build                     # saját, már futó MongoDB-vel, háttérben
docker compose logs -f detector                  # élő log
docker compose --profile local-mongo up -d --build  # ha nincs saját MongoDB-d
```

Vagy Docker nélkül: `pip install -r requirements.txt && python -m app.main`.
Részletek és a mongod `bindIp` buktató: `README.md`.

Teszt hálózat nélkül: `python tests/test_core.py`

## Adatfolyam

```
Binance WebSocket (aggTrade + !bookTicker + depth20, folyamatosan)
   → eligibility (spread, white/blacklist)
   → ScalpDetector: IMPULZUS -> SetupTracker (folytatas/fordulo) -> SIGNAL
     (a konyv es az EMA MAR a dontesben szamit, cache-bol, varakozas nelkul)
   → MongoDB → Telegram → [TradingService]
   → OutcomeTracker: MFE/MAE + TP/SL meres a jelzes utan
```

Új detektor = új fájl az `app/detectors/` alá + egy `*_DEFAULTS` a `config.py`-ban +
egy sor a `main.py`-ban. A többi réteget nem kell módosítani.

## Modulok

| Fájl | Felelősség |
|---|---|
| `app/main.py` | wiring, logging setup |
| `app/db.py` | Mongo kapcsolat, collectionök, indexek |
| `app/config.py` | config seed + betöltés Mongo-ból, 30 mp-enként újratöltve |
| `app/binance_rest.py` | exchangeInfo / ticker24hr / klines, aláírt REST (leverage, marginType) |
| `app/market_data.py` | aggTrade WS-ek 150-es chunkokban, reconnect |
| `app/detectors/base.py` | közös `Trade` / `Signal` alak, `Detector` interfész |
| `app/detectors/manager.py` | fan-out a detektorokra, detektoronkénti hibakezelés |
| `app/detectors/scalp.py` | IMPULZUS-detektálás + `SetupTracker` állapotgép (folytatás/fordulás) |
| `app/detectors/baseline.py` | páronkénti normál (ár ÉS forgalom), `RollingMedian` |
| `app/bookcache.py` | a partial book depth pillanatképek memóriában, folyamatosan |
| `app/orderbook.py` | tiszta függvények a fal/likviditás számításához (a `BookCache` hívja) |
| `app/ta.py` | 1m EMA9/EMA21, háttérben frissítve, cache-ből olvasva |
| `app/eligibility.py` | realtime kereskedhetőség (spread, white/blacklist) |
| `app/fmt.py` | közös formázók a logoláshoz |
| `app/signals.py` | mentés, Telegram, trade indítás — halózati várakozás NÉLKÜL |
| `app/outcome.py` | MFE/MAE + TP/SL mérés a jelzés után, folyamatosan |
| `app/telegram.py` | Bot API sendMessage + üzenetformázás (jelzés és időszakos életjel) |
| `app/trading.py` | WS API `order.place`, TP/SL, pozíciólimitek |

Paraméterek részletes leírása: `docs/PARAMETEREK.md`

**Ami szándékosan NINCS a rendszerben:** score, kereskedelmi terv, díjszámítás,
backteszt. A detektor annyit csinál, hogy egy impulzus utáni setupot lát
megerősödni (folytatás vagy fordulás), és megmondja, miből gondolja. Az
eredménymérés (MFE/MAE, TP/SL) **van**, de nem kapuz semmit — csak megmutatja
utólag, melyik setup működik.

## Konfiguráció

Az alapértékek `app/config.py`-ban, a MongoDB `config` collection ezek másolata,
négy dokumentumban: `market` (közös: melyik párokat figyeljük, és hogyan mérjük
az eredményt), `detector` (a scalp detektor — impulzus + setup, MINDEN érték
kiindulási paraméter), `trading`, `telegram`. Induláskor a defaultok bekerülnek
(hidegindítással is teljesen), és a futó rendszer 30 mp-enként újratölti őket a
DB-ből.

Az API kulcsok környezeti változóban maradnak, nem a DB-ben.

**A hangolás a kódban történik**, nem a DB-ben: az érték `app/config.py`-ban, utána
`docker compose up -d --build`. A `config` collection bármikor törölhető — induláskor
minden beállítás újra létrejön (`test_cold_start_creates_every_setting` őrzi).
Soha ne adj a felhasználónak `db.config.updateOne` parancsot: minden indulásnál törli
a configot, tehát a DB-be írt érték elveszne.

```python
# app/config.py -- pl. erzekenyebb impulzus-kuszob (reszletek: docs/PARAMETEREK.md)
"impulseBaselineRatio": 4.0,    # DETECTOR_DEFAULTS
"minImpulsePct": 0.20,
"autoTradingEnabled": True,     # TRADING_DEFAULTS, elobb testneten!
```

## Collectionök

- `config` — a fenti négy dokumentum
- `signals` — minden jelzés (setup típusa, indoklás, mért számok, Telegram és trade
  státusz, plusz az `outcome` mezőben a folyamatos MFE/MAE + TP/SL mérés)
- `market_snapshots` — a setup körüli nyers adat (ártörténet, order book), `signalId`-vel visszaköthető
- `orders` — a TradingService eredményei és hibái
- `status` — élő állapot (uptime, tick/s, WS kapcsolatok, symbol lista cache)

## Binance API — mit használunk

WebSocket (`wss://fstream.binance.com`):
- `/market/stream` + `{"method":"SUBSCRIBE","params":["<sym>@aggTrade",...],"id":"<hex>"}`
  — árfolyam tickek. Az útvonalban lévő **`/market` szegmens kötelező**: a régi `/ws`
  végpont elfogadja a kapcsolatot és nyugtázza a feliratkozást, de nem küld adatot.
  Ha egy útvonalról 15 mp-en belül nem jön árfolyam, a kód a következőre vált.
  Max 200 feliratkozás / kapcsolat, ezért 150-esével bontjuk.
- `/public/stream` + `<sym>@depth20@500ms` — **folyamatosan**, minden figyelt párra
  (nem csak triggerkor). A partial book depth a doksiban a *public* csoportba
  tartozik, ezért `/public` a szegmense (az aggTrade-é `/market`).

WebSocket API (`wss://ws-fapi.binance.com/ws-fapi/v1`): `order.place`, `v2/account.position`.

REST (`https://fapi.binance.com`) — csak ahol nincs WS megfelelő:
- `/fapi/v1/exchangeInfo`, `/fapi/v1/ticker/24hr` — symbol univerzum + forgalmi szűrés
- `/fapi/v1/klines` — EMA
- `/fapi/v1/leverage`, `/fapi/v1/marginType` — **a WS API-ban nincs rájuk metódus**

Testnet: `FUTURES_TESTNET=1` — REST és WS API a `testnet.binancefuture.com`-ra,
a market stream a `stream.binancefuture.com`-ra vált (a hivatalos spec `servers` listája szerint).
