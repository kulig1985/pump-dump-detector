"""TAAnalyzer -- egyszeru 1 perces EMA trendfilter.

Ez az egyetlen hely, ahol az arfolyam-adat REST-bol jon (a feladat kifejezetten
engedi az indikatoroknal). Symbolonkent cache-elunk, hogy egy pumphullam alatt
ne verjuk szet a rate limitet.
"""
import time
import logging

from . import binance_rest

log = logging.getLogger("ta")

CACHE_TTL = 30.0
_cache = {}   # symbol -> (ts, result)


async def analyze(symbol, price, cfg):
    fast_n, slow_n = cfg["emaFast"], cfg["emaSlow"]
    cached = _cache.get(symbol)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        closes = await binance_rest.get_closes(symbol, cfg["emaInterval"], slow_n * 5)
    except Exception as e:
        log.warning("[%s] klines lekeres sikertelen: %s", symbol, e)
        return None
    if len(closes) < slow_n:
        return None

    fast = ema(closes, fast_n)
    slow = ema(closes, slow_n)
    if fast > slow:
        trend = "bullish"
    elif fast < slow:
        trend = "bearish"
    else:
        trend = "neutral"

    result = {"fast": fast, "slow": slow, "trend": trend, "aboveFast": price > fast}
    _cache[symbol] = (time.time(), result)
    log.info("[%s] EMA%d %.8g %s EMA%d %.8g -> %s (ar %s EMA%d felett)",
             symbol, fast_n, fast, ">" if fast > slow else "<", slow_n, slow, trend,
             "van" if result["aboveFast"] else "nincs", fast_n)
    return result


def ema(values, period):
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period      # SMA az inditashoz
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e
