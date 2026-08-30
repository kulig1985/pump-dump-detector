"""Halozat nelkuli onteszt a detektor magjara: python tests/test_core.py"""
import sys
import types
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.detector import MovementDetector
from app.market_data import _mongo_row
from app import orderbook, scoring
from app.ta import ema

CFG = {
    "priceChangeThreshold1s": 0.30,
    "priceChangeThreshold3s": 0.60,
    "priceChangeThreshold5s": 0.90,
    "symbolCooldownSec": 60,
    "wallSensitivity": 3.0,
    "wallMaxDistancePct": 1.5,
}
cfg_obj = types.SimpleNamespace(detector=CFG)


def feed(det, symbol, start_ts, prices, step=0.2):
    """Tick sorozat betoltese, visszaadja az osszes triggert."""
    out = []
    for i, p in enumerate(prices):
        t = det.on_price(symbol, p, start_ts + i * step)
        if t:
            out.append(t)
    return out


def test_no_trigger_below_threshold():
    det = MovementDetector(cfg_obj)
    # 6 masodperc alatt +0.1% -- minden kuszob alatt
    prices = [100.0 + i * 0.0033 for i in range(31)]
    assert feed(det, "AAAUSDT", 1000.0, prices) == []


def test_trigger_above_threshold():
    det = MovementDetector(cfg_obj)
    prices = [100.0] * 26 + [100.5] * 5      # ugras +0.5% egy tickben
    triggers = feed(det, "BBBUSDT", 1000.0, prices)
    assert len(triggers) == 1, triggers
    assert triggers[0]["direction"] == "LONG"
    assert triggers[0]["changes"][1] > 0.30


def test_dump_direction():
    det = MovementDetector(cfg_obj)
    prices = [100.0] * 26 + [99.5] * 5
    triggers = feed(det, "CCCUSDT", 1000.0, prices)
    assert len(triggers) == 1
    assert triggers[0]["direction"] == "SHORT"


def test_cooldown_suppresses_repeat():
    det = MovementDetector(cfg_obj)
    # ket kulon ugras 4 masodperc kulonbseggel, a cooldown 60s -> csak az elso jon at
    prices = [100.0] * 26 + [100.5] * 20 + [101.5] * 5
    assert len(feed(det, "DDDUSDT", 1000.0, prices)) == 1


def test_no_trigger_without_enough_history():
    det = MovementDetector(cfg_obj)
    # rogton egy nagy ugras, elozmeny nelkul -> nincs mihez viszonyitani
    assert feed(det, "EEEUSDT", 1000.0, [100.0, 105.0]) == []


def test_wall_detection():
    price = 100.0
    # 20 ask szint, a 8. (100.08) kiugroan nagy
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)]
    asks[8] = (100.08, 60.0)
    wall = orderbook._find_wall(asks, price, CFG["wallSensitivity"], CFG["wallMaxDistancePct"])
    assert wall is not None
    assert wall["price"] == 100.08
    assert abs(wall["distancePct"] - 0.08) < 0.001
    assert wall["ratio"] > 3.0


def test_no_wall_when_flat():
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)]
    assert orderbook._find_wall(asks, 100.0, CFG["wallSensitivity"], CFG["wallMaxDistancePct"]) is None


def test_wall_outside_range_ignored():
    # a wall 5%-ra van, a max tavolsag 1.5% -> nem erdekel
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)] + [(105.0, 500.0)]
    assert orderbook._find_wall(asks, 100.0, CFG["wallSensitivity"], CFG["wallMaxDistancePct"]) is None


def test_status_row_is_mongo_safe():
    """A Mongo csak string kulcsot fogad -- a changes dict kulcsai egesz szamok."""
    det = MovementDetector(cfg_obj)
    feed(det, "FFFUSDT", 1000.0, [100.0 + i * 0.01 for i in range(40)])
    rows = [_mongo_row(r) for r in det.snapshot()["rows"]]
    assert rows

    def check(o, path="doc"):
        if isinstance(o, dict):
            for k, v in o.items():
                assert isinstance(k, str), f"{path}: nem string kulcs -> {k!r}"
                check(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                check(v, f"{path}[{i}]")

    check({"topMovers": rows})
    assert set(rows[0]["changes"]) == {"s1", "s3", "s5"}


def test_wall_behind_price_is_not_an_obstacle():
    """Ha az ar a snapshotig visszaesett, a mogottunk levo szint nem akadaly."""
    # kozepar 100.005; a nagy bid 99.95 (alatta), a nagy ask 100.06 (felette)
    bids = [(100.00 - i * 0.01, 1.0) for i in range(20)]
    asks = [(100.01 + i * 0.01, 1.0) for i in range(20)]
    bids[5] = (99.95, 80.0)
    mid = (bids[0][0] + asks[0][0]) / 2

    buy = orderbook._find_wall(bids, mid, CFG["wallSensitivity"], CFG["wallMaxDistancePct"])
    sell = orderbook._find_wall(asks, mid, CFG["wallSensitivity"], CFG["wallMaxDistancePct"])
    assert buy is not None and buy["price"] == 99.95
    assert sell is None, "az ask oldalon nincs wall, nem szabad talalni"
    # a bid wall tavolsaga a kozepartol mert, pozitiv szazalek
    assert 0.0 < buy["distancePct"] < 0.1


def _trigger(c1, c3, c5):
    return {"symbol": "X", "direction": "LONG", "price": 100.0,
            "changes": {1: c1, 3: c3, 5: c5}}


def test_score_increases_with_stronger_move():
    weak, _, _ = scoring.score_signal(_trigger(0.31, 0.4, 0.5), None, None, CFG)
    strong, _, _ = scoring.score_signal(_trigger(0.90, 1.2, 1.4), None, None, CFG)
    assert strong > weak, (weak, strong)


def test_score_penalises_opposite_ema_and_near_wall():
    trig = _trigger(0.6, 0.8, 1.0)
    good_ta = {"trend": "bullish", "aboveFast": True, "fast": 1, "slow": 0}
    bad_ta = {"trend": "bearish", "aboveFast": False, "fast": 0, "slow": 1}
    clear_ob = {"obstacleAhead": None, "liquidityRatio": 1.0}
    blocked_ob = {"obstacleAhead": {"distancePct": 0.05, "ratio": 8.0}, "liquidityRatio": 1.0}

    best, _, _ = scoring.score_signal(trig, clear_ob, good_ta, CFG)
    worst, _, _ = scoring.score_signal(trig, blocked_ob, bad_ta, CFG)
    assert best > worst
    assert best <= 100 and worst >= 0


def test_ema_matches_known_values():
    # sima novekvo sor: az EMA a vegen az utolso ertekek kozeleben van, es fast > slow
    closes = [float(i) for i in range(1, 61)]
    assert ema(closes, 9) > ema(closes, 21)
    # konstans sor eseten az EMA maga a konstans
    assert abs(ema([5.0] * 40, 9) - 5.0) < 1e-9


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} teszt rendben")
