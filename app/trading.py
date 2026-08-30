"""TradingService -- opcionalis automatikus poziciónyitas.

A megbizasok a Futures WebSocket API-n mennek:
    wss://ws-fapi.binance.com/ws-fapi/v1
    { "id": "...", "method": "order.place", "params": { ...alairt parameterek... } }

Kivetel: a leverage es a marginType allitasara a WS API-ban NINCS metodus,
azt ketto REST hivas vegzi (symbolonkent egyszer, utana cache-elve).

Ha autoTradingEnabled = false (ez az alapertelmezes), ez a modul azonnal visszater,
a detector tole fuggetlenul teljes ertekuen mukodik.
"""
import os
import json
import time
import hmac
import uuid
import hashlib
import asyncio
import logging
from urllib.parse import urlencode
from datetime import datetime, timezone

import websockets

from . import binance_rest

log = logging.getLogger("trading")

TESTNET = os.getenv("FUTURES_TESTNET", "0") == "1"
WS_API_URL = ("wss://testnet.binancefuture.com/ws-fapi/v1" if TESTNET
              else "wss://ws-fapi.binance.com/ws-fapi/v1")
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

SKIPPED = {"executed": False, "orderId": None, "error": None}


class TradingService:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.ws = None
        self.lock = asyncio.Lock()
        self.prepared = set()      # symbolok, ahol a leverage/marginType mar be van allitva

    # ---------- WebSocket API ----------

    async def _request(self, method, params=None):
        """Egy alairt WS API hivas. A lock miatt egyszerre csak egy kerés van a dróton,
        igy eleg a valaszt id alapjan megvarni -- nem kell kulon reader task."""
        params = dict(params or {})
        params["apiKey"] = API_KEY
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(sorted(params.items()))
        params["signature"] = hmac.new(API_SECRET.encode(), query.encode(),
                                       hashlib.sha256).hexdigest()
        req_id = str(uuid.uuid4())

        async with self.lock:      # ponytail: globalis lock, par megbizas/perc mellett boven eleg
            for attempt in (1, 2):
                try:
                    if self.ws is None or self.ws.close_code is not None:
                        self.ws = await websockets.connect(WS_API_URL, ping_interval=180)
                        log.info("WS API kapcsolat felepult (%s)", WS_API_URL)
                    await self.ws.send(json.dumps({"id": req_id, "method": method,
                                                   "params": params}))
                    while True:
                        resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
                        if resp.get("id") == req_id:
                            break
                except Exception as e:
                    self.ws = None
                    if attempt == 2:
                        raise
                    log.warning("WS API hiba (%s), ujraprobalas: %s", method, e)
                    continue
                break

        if resp.get("status") != 200:
            err = resp.get("error", {})
            raise RuntimeError(f"{method}: {err.get('code')} {err.get('msg')}")
        return resp["result"]

    async def _open_positions(self):
        positions = await self._request("v2/account.position")
        return [p for p in positions if float(p["positionAmt"]) != 0]

    # ---------- fo belepesi pont ----------

    async def maybe_open(self, signal):
        """Visszaad {"executed", "orderId", "error"}. Sose dob kivetelt."""
        symbol, direction = signal["symbol"], signal["direction"]
        t = self.cfg.trading

        if not t["autoTradingEnabled"]:
            log.info("[%s] auto trading KI -- nincs megbizas", symbol)
            return SKIPPED
        if direction == "LONG" and not t["longEnabled"]:
            log.info("[%s] LONG letiltva", symbol)
            return SKIPPED
        if direction == "SHORT" and not t["shortEnabled"]:
            log.info("[%s] SHORT letiltva", symbol)
            return SKIPPED

        try:
            return await self._open(signal)
        except Exception as e:
            log.error("[%s] megbizas sikertelen: %s", symbol, e)
            await self.db.orders.insert_one({
                "timestamp": datetime.now(timezone.utc),
                "symbol": symbol, "side": None, "error": str(e),
            })
            return {"executed": False, "orderId": None, "error": str(e)}

    async def _open(self, signal):
        symbol, direction = signal["symbol"], signal["direction"]
        t = self.cfg.trading

        open_positions = await self._open_positions()
        if any(p["symbol"] == symbol for p in open_positions):
            log.info("[%s] mar van nyitott pozicio -- kihagyva", symbol)
            return SKIPPED
        if len(open_positions) >= t["maxOpenPositions"]:
            log.info("[%s] elertuk a max %d nyitott poziciot -- kihagyva",
                     symbol, t["maxOpenPositions"])
            return SKIPPED

        await self._prepare_symbol(symbol, t)

        price = signal["price"]
        qty = binance_rest.round_qty(symbol, t["positionSizeUSDT"] / price)
        min_qty = binance_rest.SYMBOL_FILTERS.get(symbol, {}).get("minQty", 0)
        if qty < min_qty or qty <= 0:
            log.warning("[%s] a %s USDT meret a minimum alatt van (qty %s < %s)",
                        symbol, t["positionSizeUSDT"], qty, min_qty)
            return {"executed": False, "orderId": None, "error": "quantity below minQty"}

        side = "BUY" if direction == "LONG" else "SELL"
        log.info("[%s] MARKET %s nyitas | qty %s | ~%.2f USDT | lev %dx",
                 symbol, side, qty, qty * price, t["leverage"])
        entry = await self._request("order.place", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": str(qty), "newOrderRespType": "RESULT",
        })

        fill_price = float(entry.get("avgPrice") or 0) or price
        close_side = "SELL" if direction == "LONG" else "BUY"
        sign = 1 if direction == "LONG" else -1
        tp_price = binance_rest.round_price(symbol, fill_price * (1 + sign * t["takeProfitPct"] / 100))
        sl_price = binance_rest.round_price(symbol, fill_price * (1 - sign * t["stopLossPct"] / 100))

        tp = await self._exit_order(symbol, close_side, "TAKE_PROFIT_MARKET", tp_price)
        sl = await self._exit_order(symbol, close_side, "STOP_MARKET", sl_price)

        log.info("[%s] pozicio nyitva @ %.8g | TP %.8g | SL %.8g | orderId %s",
                 symbol, fill_price, tp_price, sl_price, entry["orderId"])
        await self.db.orders.insert_one({
            "timestamp": datetime.now(timezone.utc),
            "symbol": symbol, "side": side, "qty": qty, "fillPrice": fill_price,
            "entryOrder": entry, "tpOrder": tp, "slOrder": sl, "error": None,
        })
        return {"executed": True, "orderId": entry["orderId"], "error": None}

    async def _exit_order(self, symbol, side, order_type, stop_price):
        """TP/SL a teljes poziciora. closePosition=true mellett nem kell quantity."""
        try:
            return await self._request("order.place", {
                "symbol": symbol, "side": side, "type": order_type,
                "stopPrice": str(stop_price), "closePosition": "true",
                "workingType": "MARK_PRICE",
            })
        except Exception as e:
            # a pozicio mar nyitva van, ezert ezt nem engedjuk felszallni -- de hangosan logoljuk
            log.error("[%s] %s felvetel SIKERTELEN: %s -- a pozicio vedelem nelkul all!",
                      symbol, order_type, e)
            return {"error": str(e)}

    async def _prepare_symbol(self, symbol, t):
        """Leverage + margin mode. REST, mert a WS API-ban nincs ra metodus."""
        if symbol in self.prepared:
            return
        try:
            await binance_rest.set_margin_type(symbol, t["marginMode"])
        except Exception as e:
            # -4046 "No need to change margin type" -- normalis, ha mar jo
            log.info("[%s] margin mode valtoztatas kihagyva: %s", symbol, e)
        await binance_rest.set_leverage(symbol, t["leverage"])
        self.prepared.add(symbol)
        log.info("[%s] beallitva: %s, %dx tokeattetel", symbol, t["marginMode"], t["leverage"])

    async def close(self):
        if self.ws is not None and self.ws.close_code is None:
            await self.ws.close()
