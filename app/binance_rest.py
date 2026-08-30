"""Binance USDS-M Futures REST hivasok.

Csak ott hasznalunk REST-et, ahol nincs WebSocket megfeleloje:
  - GET  /fapi/v1/exchangeInfo   symbol lista + lot/tick filterek (nincs WS)
  - GET  /fapi/v1/ticker/24hr    24h forgalom a szureshez (indulaskor, orankent)
  - GET  /fapi/v1/klines         EMA szamitashoz (a feladat ezt kifejezetten engedi)
  - POST /fapi/v1/leverage       a Futures WS API-ban NINCS ilyen metodus
  - POST /fapi/v1/marginType     a Futures WS API-ban NINCS ilyen metodus
"""
import os
import time
import hmac
import hashlib
import logging
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("rest")

TESTNET = os.getenv("FUTURES_TESTNET", "0") == "1"
BASE_URL = "https://testnet.binancefuture.com" if TESTNET else "https://fapi.binance.com"
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# symbol -> {"stepSize": float, "tickSize": float}
SYMBOL_FILTERS = {}
# symbol -> 24 oras forgalom USDT-ben
SYMBOL_VOLUME = {}

_session = None


async def session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _session


async def close():
    if _session and not _session.closed:
        await _session.close()


async def _get(path, params=None):
    s = await session()
    async with s.get(BASE_URL + path, params=params) as r:
        r.raise_for_status()
        return await r.json()


async def _signed_post(path, params):
    """HMAC SHA256 alairt POST. Csak leverage / marginType megy erre."""
    params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
    query = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    s = await session()
    url = f"{BASE_URL}{path}?{query}&signature={sig}"
    async with s.post(url, headers={"X-MBX-APIKEY": API_KEY}) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError(f"{path} hiba {r.status}: {body}")
        return body


async def load_symbols(min_quote_volume, max_symbols, exclude=(), quotes=("USDT",)):
    """Perpetual parok a megadott elszamolo devizakban, forgalom szerint szurve.

    Mellekhatas: feltolti a SYMBOL_FILTERS cache-t (kerekiteshez kell a tradinghez).
    """
    quotes = {q.upper() for q in quotes}
    info = await _get("/fapi/v1/exchangeInfo")
    tradable = set()
    for s in info["symbols"]:
        if (s["contractType"] == "PERPETUAL" and s["quoteAsset"] in quotes
                and s["status"] == "TRADING"):
            tradable.add(s["symbol"])
            f = {x["filterType"]: x for x in s["filters"]}
            SYMBOL_FILTERS[s["symbol"]] = {
                "stepSize": float(f["LOT_SIZE"]["stepSize"]),
                "tickSize": float(f["PRICE_FILTER"]["tickSize"]),
                "minQty": float(f["LOT_SIZE"]["minQty"]),
            }

    kizart = {s.upper() for s in exclude}
    tickers = await _get("/fapi/v1/ticker/24hr")
    liquid = [(t["symbol"], float(t["quoteVolume"])) for t in tickers
              if t["symbol"] in tradable and t["symbol"] not in kizart
              and float(t["quoteVolume"]) >= min_quote_volume]
    liquid.sort(key=lambda x: x[1], reverse=True)
    symbols = [s for s, _ in liquid[:max_symbols]]
    SYMBOL_VOLUME.clear()
    SYMBOL_VOLUME.update(dict(liquid))

    log.info("Perpetual %s parok: %d | forgalom >= %s: %d | figyelunk: %d%s",
             "/".join(sorted(quotes)), len(tradable), f"{min_quote_volume:,.0f}",
             len(liquid), len(symbols),
             f" | kizarva: {', '.join(sorted(kizart))}" if kizart else "")
    if symbols:
        bontas = {}
        for sym in symbols:
            q = next((q for q in sorted(quotes, key=len, reverse=True)
                      if sym.endswith(q)), "?")
            bontas[q] = bontas.get(q, 0) + 1
        log.info("Elszamolo deviza szerint: %s",
                 "  ".join(f"{q}: {n}" for q, n in sorted(bontas.items())))
    if liquid:
        # latszodjon, hol huz a szuro -- ha keves symbol jon at, itt derul ki, hogy miert
        log.info("Figyelt parok forgalom szerint csokkeno sorrendben:")
        for i in range(0, len(symbols), 5):
            log.info("  %s", "  ".join(f"{s} ({SYMBOL_VOLUME[s] / 1e6:,.0f}M)"
                                       for s in symbols[i:i + 5]))
    else:
        log.error("EGY symbol sem felel meg a %s USDT forgalmi kuszobnek -- "
                  "vedd lejjebb a minQuoteVolume24h erteket!", f"{min_quote_volume:,.0f}")
    return symbols


async def get_closes(symbol, interval, limit):
    """1m gyertyak zaroarai EMA-hoz. A kline mezo indexe 4 = close."""
    raw = await _get("/fapi/v1/klines",
                     {"symbol": symbol, "interval": interval, "limit": limit})
    return [float(k[4]) for k in raw]


async def set_leverage(symbol, leverage):
    return await _signed_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})


async def set_margin_type(symbol, margin_type):
    return await _signed_post("/fapi/v1/marginType",
                              {"symbol": symbol, "marginType": margin_type})


def _round_step(value, step):
    """Lefele kerekites a step tobbszorosere -- sose lepjuk tul a szandekolt meretet."""
    if step <= 0:
        return value
    d = Decimal(str(step))
    return float((Decimal(str(value)) / d).to_integral_value(ROUND_DOWN) * d)


def round_qty(symbol, qty):
    f = SYMBOL_FILTERS.get(symbol)
    return _round_step(qty, f["stepSize"]) if f else qty


def round_price(symbol, price):
    f = SYMBOL_FILTERS.get(symbol)
    return _round_step(price, f["tickSize"]) if f else price
