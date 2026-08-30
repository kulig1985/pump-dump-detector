"""OrderBookAnalyzer -- BUY/SELL wall kereses trigger utan.

Triggerkor nyitunk egy rovid eletu WS kapcsolatot a partial book depth streamre
(<symbol>@depth20@100ms), elkapjuk az elso uzenetet, es azonnal bontunk. Igy nem kell
200 symbolra folyamatos depth streamet fenntartani, es maradunk WebSocket-en.

A wall relativ: egy arszint akkor wall, ha a rajta levo notional legalabb
`wallSensitivity`-szerese az adott oldal atlagos szintjenek, es kozel van az arhoz.
"""
import os
import json
import statistics
import asyncio
import logging

import websockets

log = logging.getLogger("orderbook")

# A partial book depth a dokumentacioban a "public" csoportba tartozik, ezert az
# utvonal-szegmense /public (az aggTrade-e /market). Elsonek a dokumentalt utvonalat
# probaljuk, majd a regieket; az elsot, amelyik tenyleg kuld adatot, megjegyezzuk.
WS_HOST = ("wss://stream.binancefuture.com" if os.getenv("FUTURES_TESTNET") == "1"
           else "wss://fstream.binance.com")
WS_BASES = [f"{WS_HOST}/public/ws", f"{WS_HOST}/market/ws", f"{WS_HOST}/ws"]
SNAPSHOT_TIMEOUT = 3.0

_working_base = None      # az elso utvonal, amelyik mukodott


async def analyze(symbol, price, direction, cfg):
    """Visszaad egy dictet a wall elemzessel, vagy None-t ha nem sikerult a snapshot."""
    global _working_base
    levels = cfg["orderBookLevels"]
    stream = f"{symbol.lower()}@depth{levels}@100ms"
    data = None
    for base in ([_working_base] if _working_base else WS_BASES):
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT):
                async with websockets.connect(f"{base}/{stream}") as ws:
                    data = json.loads(await ws.recv())
            if _working_base is None:
                _working_base = base
                log.info("Order book utvonal: %s", base)
            break
        except Exception as e:
            log.debug("[%s] order book %s utvonalon nem jott: %s", symbol, base, e)
    if data is None:
        log.warning("[%s] order book snapshot sikertelen minden utvonalon", symbol)
        return None

    bids = [(float(p), float(q)) for p, q in data.get("b", [])]
    asks = [(float(p), float(q)) for p, q in data.get("a", [])]
    if not bids or not asks:
        log.warning("[%s] ures order book snapshot", symbol)
        return None

    # A trigger ota eltelt par szaz ms alatt az ar elmozdulhat, ezert nem a trigger
    # arahoz merunk, hanem a snapshot sajat kozeparahoz: igy az ask mindig felette,
    # a bid mindig alatta van, es nem szamit akadalynak egy mar mogottunk hagyott szint.
    price = (bids[0][0] + asks[0][0]) / 2

    max_dist = cfg["wallMaxDistancePct"]
    sensitivity = cfg["wallSensitivity"]
    buy_wall = _find_wall(bids, price, sensitivity, max_dist)
    sell_wall = _find_wall(asks, price, sensitivity, max_dist)

    # a mozgas iranyaban levo, illetve mogotti likviditas (notional USDT-ben)
    ahead = asks if direction == "LONG" else bids
    behind = bids if direction == "LONG" else asks
    ahead_liq = _liquidity(ahead, price, max_dist)
    behind_liq = _liquidity(behind, price, max_dist)
    obstacle = sell_wall if direction == "LONG" else buy_wall

    spread_pct = (asks[0][0] - bids[0][0]) / price * 100.0
    result = {
        "refPrice": price,          # a snapshot kozepara, ehhez mertunk
        "spreadPct": round(spread_pct, 4),
        "nearestBuyWall": buy_wall,
        "nearestSellWall": sell_wall,
        "aheadLiquidity": round(ahead_liq, 2),
        "behindLiquidity": round(behind_liq, 2),
        # <1 = keves likviditas a mozgas iranyaban -> konnyen tovabb megy
        "liquidityRatio": round(ahead_liq / behind_liq, 3) if behind_liq else None,
        "obstacleAhead": obstacle,
        "snapshot": {"bids": bids, "asks": asks},
    }
    if obstacle:
        log.info("[%s] %d szint | akadaly %s iranyban: %.2f%% tavolsagra (%.1fx atlag)",
                 symbol, levels, direction, obstacle["distancePct"], obstacle["ratio"])
    else:
        log.info("[%s] %d szint | nincs wall %.1f%%-on belul (ahead/behind likviditas: %s)",
                 symbol, levels, max_dist, result["liquidityRatio"])
    return result


def _find_wall(side, price, sensitivity, max_dist_pct):
    """A legkozelebbi arszint, ami kiugroan nagy a tobbi szinthez kepest.

    A viszonyitasi alap a MEDIAN, nem az atlag: az atlagba a fal maga is beleszamit,
    es 20 szint mellett egy 10x akkora fal ~45%-kal emeli az atlagot -- vagyis a sajat
    aranyat hígitja fel, es alabecsult erteket kapnank.
    """
    # A LEGJOBB szint (a touch) kimarad: ott lepsz be, az nem akadaly. A BTC-n a
    # touch sokszorosa a tobbi szintnek, es igy minden jelzest "fal" miatt dobtunk el.
    side = side[1:]
    if not side:
        return None
    notionals = [p * q for p, q in side]
    alap = statistics.median(notionals)
    if alap <= 0:
        return None
    for (p, q), notional in zip(side, notionals):    # a lista mar ar szerint rendezett
        dist = abs(p - price) / price * 100.0
        if dist > max_dist_pct:
            break
        if notional >= sensitivity * alap:
            return {"price": p, "distancePct": round(dist, 3),
                    "notional": round(notional, 2), "ratio": round(notional / alap, 2)}
    return None


def _liquidity(side, price, max_dist_pct):
    total = 0.0
    for p, q in side:
        if abs(p - price) / price * 100.0 > max_dist_pct:
            break
        total += p * q
    return total
