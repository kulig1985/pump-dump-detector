# pump-dump-detect

Binance USDⓈ-M Futures perpetual piac valós idejű pump/dump detektora. Telegram
értesítést küld, és opcionálisan (alapból **kikapcsolva**) pozíciót is nyit.

## Futtatás

```bash
cp .env.example .env      # MONGO_URL, Telegram token + chat ID
docker compose up --build                        # saját, már futó MongoDB-vel
docker compose --profile local-mongo up --build  # ha nincs saját MongoDB-d
```

Vagy Docker nélkül: `pip install -r requirements.txt && python -m app.main`.
Részletek és a mongod `bindIp` buktató: `README.md`.

Teszt hálózat nélkül: `python tests/test_core.py`

## Adatfolyam

```
Binance WebSocket -> MarketDataService -> MovementDetector
   -> (trigger) -> OrderBookAnalyzer + TAAnalyzer -> scoring
   -> SignalService -> MongoDB -> TelegramNotifier -> [TradingService]
```

## Modulok

| Fájl | Felelősség |
|---|---|
| `app/main.py` | wiring, logging setup |
| `app/db.py` | Mongo kapcsolat, collectionök, indexek |
| `app/config.py` | config seed + betöltés Mongo-ból, 30 mp-enként újratöltve |
| `app/binance_rest.py` | exchangeInfo / ticker24hr / klines, aláírt REST (leverage, marginType) |
| `app/market_data.py` | aggTrade WS-ek 150-es chunkokban, reconnect |
| `app/detector.py` | 1s/3s/5s rolling ablak, trigger, per-symbol cooldown |
| `app/orderbook.py` | rövid életű depth20 WS + relatív wall detektálás |
| `app/ta.py` | 1m EMA9/EMA21 (cache-elve) |
| `app/scoring.py` | 0–100 score + indoklás — **itt hangolható a detektor ízlése** |
| `app/signals.py` | elemzés összefogása, mentés, továbbítás |
| `app/telegram.py` | Bot API sendMessage + üzenetformázás |
| `app/trading.py` | WS API `order.place`, TP/SL, pozíciólimitek |

## Konfiguráció

Minden a MongoDB `config` collectionben, három dokumentumban: `detector`, `trading`,
`telegram`. Első indulásnál a defaultok bekerülnek, utána **a DB az igazság** — menet
közbeni módosítás 30 mp-en belül életbe lép, újraindítás nélkül.

Az API kulcsok környezeti változóban maradnak, nem a DB-ben.

```js
// pl. érzékenyebb detektor, tesztre
db.config.updateOne({_id: "detector"}, {$set: {priceChangeThreshold1s: 0.1}})
// auto trading bekapcsolása (előbb testneten!)
db.config.updateOne({_id: "trading"}, {$set: {autoTradingEnabled: true}})
```

## Collectionök

- `config` — a fenti három dokumentum
- `signals` — minden detektált signal (score, EMA, order book összefoglaló, Telegram és trade státusz)
- `market_snapshots` — a trigger körüli nyers adat (ártörténet, 20 szintes könyv, score inputok), `signalId`-vel visszaköthető
- `orders` — a TradingService eredményei és hibái
- `status` — 5 mp-enként frissülő élő állapot (uptime, tick/s, WS kapcsolatok, top 10 mozgó pár)

## Binance API — mit használunk

WebSocket (`wss://fstream.binance.com`):
- `/market/stream` + `{"method":"SUBSCRIBE","params":["<sym>@aggTrade",...],"id":"<hex>"}`
  — árfolyam tickek. Az útvonalban lévő **`/market` szegmens kötelező**: a régi `/ws`
  végpont elfogadja a kapcsolatot és nyugtázza a feliratkozást, de nem küld adatot.
  Ha egy útvonalról 15 mp-en belül nem jön árfolyam, a kód a következőre vált.
  Max 200 feliratkozás / kapcsolat, ezért 150-esével bontjuk.
- `/public/ws/<sym>@depth20@100ms` — csak triggerkor, első üzenet után azonnal bontunk.
  A depth a doksiban a *public* csoportba tartozik, ezért `/public` a szegmense (az
  aggTrade-é `/market`).

WebSocket API (`wss://ws-fapi.binance.com/ws-fapi/v1`): `order.place`, `v2/account.position`.

REST (`https://fapi.binance.com`) — csak ahol nincs WS megfelelő:
- `/fapi/v1/exchangeInfo`, `/fapi/v1/ticker/24hr` — symbol univerzum + forgalmi szűrés
- `/fapi/v1/klines` — EMA
- `/fapi/v1/leverage`, `/fapi/v1/marginType` — **a WS API-ban nincs rájuk metódus**

Testnet: `FUTURES_TESTNET=1` — REST és WS API a `testnet.binancefuture.com`-ra,
a market stream a `stream.binancefuture.com`-ra vált (a hivatalos spec `servers` listája szerint).
