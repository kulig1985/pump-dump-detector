"""EMA trend-kontextus, folyamatosan frissitve.

Ez az egyetlen hely, ahol az arfolyam-adat REST-bol jon. Korabban a jelzes
pillanataban kertuk le a klines-t -- vagyis epp akkor vartunk halozatra, amikor
sietni kellett volna. Most egy hatterciklus frissiti a figyelt parokat, a detektor
pedig a memoriabol olvas (get()).
"""
import time
import asyncio
import logging

from . import binance_rest

log = logging.getLogger("ta")

_cache = {}   # symbol -> {"fast","slow","trend","ts"}


def get(symbol):
    """A par EMA-kontextusa a cache-bol, halozat nelkul. None, ha meg nincs."""
    return _cache.get(symbol)


def ema(values, period):
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period      # SMA az inditashoz
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


async def refresh(symbol, cfg):
    fast_n, slow_n = cfg["emaFast"], cfg["emaSlow"]
    # A klines sulya a limit-tol fugg: 100-ig 1, 101-500 kozott 2. Az EMA-hoz
    # boven eleg 100 gyertya (EMA21-nel ez ~5x a periodus), viszont igy feleannyi
    # sulyt hasznalunk -- elesben 429-et kaptunk a 105-os limittel.
    limit = min(100, slow_n * 5)
    closes = await binance_rest.get_closes(symbol, cfg["emaInterval"], limit)
    if len(closes) < slow_n:
        return None
    fast, slow = ema(closes, fast_n), ema(closes, slow_n)
    trend = "bullish" if fast > slow else "bearish" if fast < slow else "neutral"
    _cache[symbol] = {"fast": fast, "slow": slow, "trend": trend, "ts": time.time()}
    return _cache[symbol]


async def refresh_loop(cfg, symbols_fn):
    """Korbejarja a figyelt parokat, es frissiti az EMA-t.

    Egyenletesen elosztva, nem egyszerre: igy nem lokjuk meg a rate limitet, es
    egy tiltas sem all le semmit -- a detektor EMA nelkul is dolgozik.
    """
    while True:
        c = cfg.detector
        symbols = list(symbols_fn() or [])
        interval = max(10.0, float(c["emaRefreshSec"]))
        if not symbols:
            await asyncio.sleep(5)
            continue
        szunet = interval / len(symbols)
        for symbol in symbols:
            try:
                await refresh(symbol, c)
            except binance_rest.RateLimited as e:
                log.warning("EMA frissites szunetel %.0f mp-ig (rate limit)",
                            e.retry_after)
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                log.debug("[%s] EMA frissites sikertelen: %s", symbol, e)
            await asyncio.sleep(szunet)
