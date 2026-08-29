"""OrderBookAnalyzer -- BUY/SELL wall kereses trigger utan.

Triggerkor nyitunk egy rovid eletu WS kapcsolatot a partial book depth streamre
(<symbol>@depth20@100ms), elkapjuk az elso uzenetet, es azonnal bontunk. Igy nem kell
200 symbolra folyamatos depth streamet fenntartani, es maradunk WebSocket-en.

A wall relativ: egy arszint akkor wall, ha a rajta levo notional legalabb
`wallSensitivity`-szerese az adott oldal atlagos szintjenek, es kozel van az arhoz.
"""
import json
import asyncio
import logging

import websockets

log = logging.getLogger("orderbook")

WS_BASE = "wss://fstream.binance.com/ws"
SNAPSHOT_TIMEOUT = 3.0


async def analyze(symbol, price, direction, cfg):
    """Visszaad egy dictet a wall elemzessel, vagy None-t ha nem sikerult a snapshot."""
    levels = cfg["orderBookLevels"]
    url = f"{WS_BASE}/{symbol.lower()}@depth{levels}@100ms"
    try:
        async with asyncio.timeout(SNAPSHOT_TIMEOUT):
            async with websockets.connect(url) as ws:
                data = json.loads(await ws.recv())
    except Exception as e:
        log.warning("[%s] order book snapshot sikertelen: %s", symbol, e)
        return None

    bids = [(float(p), float(q)) for p, q in data.get("b", [])]
    asks = [(float(p), float(q)) for p, q in data.get("a", [])]
    if not bids or not asks:
        log.warning("[%s] ures order book snapshot", symbol)
        return None

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

    result = {
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
    """A legkozelebbi arszint, ami kiugroan nagy az oldal atlagahoz kepest."""
    notionals = [p * q for p, q in side]
    avg = sum(notionals) / len(notionals)
    if avg <= 0:
        return None
    for (p, q), notional in zip(side, notionals):    # a lista mar ar szerint rendezett
        dist = abs(p - price) / price * 100.0
        if dist > max_dist_pct:
            break
        if notional >= sensitivity * avg:
            return {"price": p, "distancePct": round(dist, 3),
                    "notional": round(notional, 2), "ratio": round(notional / avg, 2)}
    return None


def _liquidity(side, price, max_dist_pct):
    total = 0.0
    for p, q in side:
        if abs(p - price) / price * 100.0 > max_dist_pct:
            break
        total += p * q
    return total
