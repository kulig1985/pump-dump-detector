"""Halozat nelkuli onteszt a detektor magjara: python tests/test_core.py"""
import sys
import math
import types
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.detectors.pumpdump import PumpDumpDetector
from app.detectors.reversal import ReversalDetector
from app.detectors.manager import DetectorManager
from app.detectors.base import Trade
from app.fmt import pad as _pad
from app import orderbook, scoring
from app.ta import ema

CFG = {
    "priceChangeThreshold1s": 0.30,
    "priceChangeThreshold3s": 0.60,
    "priceChangeThreshold5s": 0.90,
    "symbolCooldownSec": 60,
    "minTicksInWindow": 3,
    "maxRefAgeFactor": 1.5,
    "volatilityMultiplier": 0.0,       # a legtobb teszt fix kuszobbel szamol
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
    det = PumpDumpDetector(cfg_obj)
    # 6 masodperc alatt +0.1% -- minden kuszob alatt
    prices = [100.0 + i * 0.0033 for i in range(31)]
    assert feed(det, "AAAUSDT", 1000.0, prices) == []


def test_trigger_above_threshold():
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0] * 26 + [100.5] * 5      # ugras +0.5% egy tickben
    triggers = feed(det, "BBBUSDT", 1000.0, prices)
    assert len(triggers) == 1, triggers
    assert triggers[0]["direction"] == "LONG"
    assert triggers[0]["detail"]["priceChange"]["s1"] > 0.30


def test_dump_direction():
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0] * 26 + [99.5] * 5
    triggers = feed(det, "CCCUSDT", 1000.0, prices)
    assert len(triggers) == 1
    assert triggers[0]["direction"] == "SHORT"


def test_cooldown_suppresses_repeat():
    det = PumpDumpDetector(cfg_obj)
    # ket kulon ugras 4 masodperc kulonbseggel, a cooldown 60s -> csak az elso jon at
    prices = [100.0] * 26 + [100.5] * 20 + [101.5] * 5
    assert len(feed(det, "DDDUSDT", 1000.0, prices)) == 1


def test_no_trigger_without_enough_history():
    det = PumpDumpDetector(cfg_obj)
    # rogton egy nagy ugras, elozmeny nelkul -> nincs mihez viszonyitani
    assert feed(det, "EEEUSDT", 1000.0, [100.0, 105.0]) == []


def test_stale_reference_is_rejected():
    """Ritka par: egyetlen trade a spreaden at nem szamit 1 masodperces mozgasnak."""
    det = PumpDumpDetector(cfg_obj)
    # sok tick, majd 2.5 mp szunet, majd egy nagy ugras
    ticks = [(1000.0 + i * 0.1, 100.0) for i in range(30)]      # 0.0 - 2.9
    ticks.append((1005.4, 100.5))                                # 2.5 mp szunet utan +0.5%
    out = [det.on_price("GGGUSDT", p, t) for t, p in ticks]
    assert not any(out), "a tul regi viszonyitasi pont miatt nem lehet trigger"


def test_too_few_ticks_in_window_is_rejected():
    """Ket trade nem mozgas, csak zaj -- minTicksInWindow = 3."""
    det = PumpDumpDetector(cfg_obj)
    base = [(1000.0 + i * 0.1, 100.0) for i in range(30)]
    out = [det.on_price("HHHUSDT", p, t) for t, p in base]
    assert not any(out)
    # a sorozat 1002.9-nel ert veget; az 1 mp-es ablakba (1002.9 - 1003.9] most
    # csak ket tick esik, tehat nem merheto, hiaba +0.5% az ugras
    assert det.on_price("HHHUSDT", 100.2, 1003.6) is None
    assert det.on_price("HHHUSDT", 100.5, 1003.9) is None
    # egy harmadik tick mar eleg a mereshez -- es akkor trigger is lesz
    assert det.on_price("HHHUSDT", 100.5, 1003.95) is not None


def test_volatility_raises_threshold_for_noisy_symbol():
    """A nyugtalan parnak tobbet kell mozdulnia; a config ertek a padlo."""
    cfg = types.SimpleNamespace(detector={**CFG, "volatilityMultiplier": 4.0})
    det = PumpDumpDetector(cfg)

    # nyugodt par: gyakorlatilag all
    for i in range(300):
        det.on_price("CALMUSDT", 100.0 + i * 1e-6, 1000.0 + i * 0.1)
    # zajos par: masodpercenkent tobb tizedszazalekot hullamzik
    for i in range(300):
        det.on_price("NOISYUSDT", 100.0 * (1 + 0.004 * math.sin(i * 0.37)), 1000.0 + i * 0.1)

    calm = det.thresholds("CALMUSDT")[1]
    noisy = det.thresholds("NOISYUSDT")[1]
    assert calm == CFG["priceChangeThreshold1s"], calm
    assert noisy > calm, (calm, noisy)


# ---------------------------------------------------------------- ReversalDetector

REV = {
    "enabled": True, "minSignalScore": 60, "cooldownSec": 120,
    "windowSeconds": 20, "minTradesInFlowWindow": 5, "maxSetupAgeSec": 30,
    "minMovePct": 0.40, "bouncePct": 0.15, "pullbackPct": 0.08,
    "newExtremeTolerancePct": 0.05, "breakTolerancePct": 0.02,
    "flowWindowSeconds": 3, "minFlowRatio": 1.6,
}
rev_cfg = types.SimpleNamespace(detector=CFG, reversal=REV)


class Tape:
    """Szintetikus trade sorozat epitese a reversal teszteken."""

    def __init__(self, symbol="RRRUSDT", t0=1000.0):
        self.symbol, self.t = symbol, t0
        self.trades = []

    def add(self, price, buy=True, qty=100.0, dt=0.1):
        self.t += dt
        self.trades.append(Trade(self.symbol, price, qty, self.t, buy))
        return self

    def ramp(self, start, end, n, buy=True, qty=100.0):
        for i in range(n):
            self.add(start + (end - start) * (i + 1) / n, buy, qty)
        return self

    def run(self, det):
        return [s for s in (det.on_trade(tr) for tr in self.trades) if s]


def _long_setup(tape):
    """Kozos elotag: 100.0 -> 99.4 eses (-0.6%), majd visszapattanas 99.65-ig,
    aztan visszahuzas 99.55-re (itt rogzul a 99.65-os micro-high)."""
    tape.ramp(100.0, 100.0, 6, buy=False)      # kiindulasi szint
    tape.ramp(99.95, 99.40, 12, buy=False)     # LEMOZGAS: -0.6%
    tape.ramp(99.45, 99.65, 6, buy=True)       # VISSZAPATTANAS: +0.25%
    tape.ramp(99.62, 99.55, 4, buy=True)       # visszahuzas -> micro-high rogzul
    return tape


def test_long_reversal_happy_path():
    det = ReversalDetector(rev_cfg)
    tape = _long_setup(Tape())
    tape.ramp(99.60, 99.80, 8, buy=True, qty=500.0)   # veteli flow + attores
    signals = tape.run(det)
    assert len(signals) == 1, [s["detail"] for s in signals]
    sig = signals[0]
    assert sig["detector"] == "reversal"
    assert sig["direction"] == "LONG"
    assert sig["contextMode"] == "reversal"
    d = sig["detail"]
    assert d["movePct"] >= REV["minMovePct"]
    assert d["flowRatio"] >= REV["minFlowRatio"]
    assert d["microLevel"] is not None and sig["price"] > d["microLevel"]
    assert d["extreme"] <= 99.41


def test_no_reversal_without_buy_flow():
    """Minden alakzat megvan, de az elado oldal marad a domináns."""
    det = ReversalDetector(rev_cfg)
    tape = _long_setup(Tape())
    tape.ramp(99.60, 99.80, 8, buy=False, qty=500.0)   # attores, de eladoi flow
    assert tape.run(det) == []


def test_no_reversal_without_micro_break():
    """Visszapattan, a micro-high rogzul, de nem tori at."""
    det = ReversalDetector(rev_cfg)
    tape = _long_setup(Tape())
    tape.ramp(99.56, 99.62, 8, buy=True, qty=500.0)    # a 99.65 ala marad
    assert tape.run(det) == []


def test_new_lower_low_resets_the_setup():
    """Uj, melyebb minimum -> az alakzat ujraindul, nincs jelzes a regi micro-szintre."""
    det = ReversalDetector(rev_cfg)
    tape = _long_setup(Tape())
    tape.ramp(99.50, 99.10, 6, buy=False)              # UJ MELYPONT
    tape.ramp(99.15, 99.66, 10, buy=True, qty=500.0)   # a regi micro-high fole megy
    signals = tape.run(det)
    # ha lenne jelzes, az mar az uj melypontra epulne, nem a regire
    for s in signals:
        assert s["detail"]["extreme"] <= 99.11, s["detail"]


def test_short_reversal_mirror():
    det = ReversalDetector(rev_cfg)
    tape = Tape("SSSUSDT")
    tape.ramp(100.0, 100.0, 6, buy=True)
    tape.ramp(100.05, 100.60, 12, buy=True)            # FELMOZGAS: +0.6%
    tape.ramp(100.55, 100.35, 6, buy=False)            # visszafordulas
    tape.ramp(100.38, 100.45, 4, buy=False)            # micro-low rogzul
    tape.ramp(100.40, 100.20, 8, buy=False, qty=500.0) # eladoi flow + attores
    signals = tape.run(det)
    assert len(signals) == 1, [s["detail"] for s in signals]
    assert signals[0]["direction"] == "SHORT"
    assert signals[0]["detail"]["extreme"] >= 100.59


def test_reversal_cooldown():
    det = ReversalDetector(rev_cfg)
    tape = _long_setup(Tape())
    tape.ramp(99.60, 99.80, 8, buy=True, qty=500.0)
    assert len(tape.run(det)) == 1
    # ugyanaz az alakzat ujra, a cooldown ablakon belul
    tape2 = _long_setup(Tape(t0=tape.t))
    tape2.ramp(99.60, 99.80, 8, buy=True, qty=500.0)
    assert tape2.run(det) == []


def test_flow_ratio_is_finite_when_one_side_is_empty():
    """Ha az egyik oldalon nincs volumen, az arany nem lehet vegtelen (Mongo + score)."""
    det = ReversalDetector(rev_cfg)
    tape = Tape("JJJUSDT")
    for i in range(10):
        tape.add(100.0 + i * 0.01, buy=True)          # kizarolag veteli oldal
    flow = det._flow([t for t in tape.trades], tape.t, REV)
    assert flow["sell"] == 0
    assert flow["ratio"] == 99.0
    assert math.isfinite(flow["ratio"])


def test_reversal_scoring_survives_opposite_ema():
    """A fo buktato: LONG reversal utan az EMA meg bearish. Momentum modban ez
    0 pont es a jelzes sose menne ki; reversal modban az szamit, hogy az ar
    visszavette-e az EMA9-et."""
    bearish_but_reclaimed = {"trend": "bearish", "aboveFast": True,
                             "fast": 1.0, "slow": 2.0}
    # tipikus kep egy aljon: tamasz alattunk, ellenallas felettunk
    resistance = {"distancePct": 0.3, "ratio": 5.0}
    support = {"distancePct": 0.2, "ratio": 5.0}
    ob = {"obstacleAhead": resistance, "liquidityRatio": 1.0,
          "nearestBuyWall": support, "nearestSellWall": resistance}

    momentum, _, _ = scoring.score_signal(
        _sig(1.5, accelerating=True, mode="momentum"), ob, bearish_but_reclaimed, CFG)
    reversal, _, _ = scoring.score_signal(
        _sig(1.5, accelerating=True, mode="reversal"), ob, bearish_but_reclaimed, CFG)

    assert momentum < 60, momentum          # a regi logikaval sosem menne ki
    assert reversal >= 60, reversal         # a reversal ertelmezessel atmegy


# ---------------------------------------------------------------- DetectorManager

class BoomDetector:
    name, config_key = "boom", "detector"

    def on_trade(self, trade):
        raise RuntimeError("szandekos hiba")

    def status_lines(self):
        return []


def test_manager_fans_out_and_survives_a_broken_detector():
    rev = ReversalDetector(rev_cfg)
    mgr = DetectorManager(rev_cfg, [BoomDetector(), rev])
    tape = _long_setup(Tape())
    tape.ramp(99.60, 99.80, 8, buy=True, qty=500.0)
    signals = [s for tr in tape.trades for s in mgr.on_trade(tr)]
    assert len(signals) == 1, "a hibas detektor nem nyelheti el a masik jelzeset"
    assert mgr.ticks == len(tape.trades)
    assert mgr.total_signals == 1


def test_manager_skips_disabled_detector():
    cfg = types.SimpleNamespace(detector=CFG, reversal={**REV, "enabled": False})
    mgr = DetectorManager(cfg, [ReversalDetector(cfg)])
    tape = _long_setup(Tape())
    tape.ramp(99.60, 99.80, 8, buy=True, qty=500.0)
    assert [s for tr in tape.trades for s in mgr.on_trade(tr)] == []


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


def test_cjk_symbol_column_alignment():
    """A CJK karakter ket oszlop szeles -- kulonben szetcsuszik a tabla."""
    assert _pad("NEARUSDT", 15) == "NEARUSDT" + " " * 7      # 8 karakter, 8 oszlop
    assert _pad("龙虾USDT", 15) == "龙虾USDT" + " " * 7        # 6 karakter, de 8 oszlop
    assert _pad("MAGMAUSDT", 15) == "MAGMAUSDT" + " " * 6
    # a kirajzolt szelesseg legyen azonos
    def width(t):
        import unicodedata
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in t)
    assert width(_pad("龙虾USDT", 15)) == width(_pad("NEARUSDT", 15)) == 15


def test_signal_detail_is_mongo_safe():
    """A Mongo csak string kulcsot fogad -- a detail nem tartalmazhat int kulcsot."""
    det = PumpDumpDetector(cfg_obj)
    sig = feed(det, "FFFUSDT", 1000.0, [100.0] * 26 + [100.5] * 5)[0]

    def check(o, path="doc"):
        if isinstance(o, dict):
            for k, v in o.items():
                assert isinstance(k, str), f"{path}: nem string kulcs -> {k!r}"
                check(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                check(v, f"{path}[{i}]")

    check(sig["detail"])
    assert set(sig["detail"]["priceChange"]) == {"s1", "s3", "s5"}
    assert set(sig["detail"]["thresholds"]) == {"s1", "s3", "s5"}


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


def _sig(strength, accelerating=False, mode="momentum", direction="LONG"):
    return {"symbol": "X", "direction": direction, "price": 100.0,
            "strength": strength, "accelerating": accelerating, "contextMode": mode}


def test_signal_carries_its_own_thresholds():
    """A jelzes viszi magaval az ervenyes kuszoboket es a sajat bizonyitekat."""
    det = PumpDumpDetector(cfg_obj)
    sig = feed(det, "IIIUSDT", 1000.0, [100.0] * 26 + [100.5] * 5)[0]
    assert sig["detector"] == "pump_dump"
    assert sig["configKey"] == "detector"
    assert sig["contextMode"] == "momentum"
    assert sig["detail"]["thresholds"] == {"s1": 0.30, "s3": 0.60, "s5": 0.90}
    assert sig["strength"] > 1.0


def test_score_increases_with_stronger_move():
    weak, _, _ = scoring.score_signal(_sig(1.0), None, None, CFG)
    strong, _, _ = scoring.score_signal(_sig(2.0), None, None, CFG)
    assert strong > weak, (weak, strong)


def test_score_penalises_opposite_ema_and_near_wall():
    trig = _sig(1.5)
    good_ta = {"trend": "bullish", "aboveFast": True, "fast": 1, "slow": 0}
    bad_ta = {"trend": "bearish", "aboveFast": False, "fast": 0, "slow": 1}
    clear_ob = {"obstacleAhead": None, "liquidityRatio": 1.0,
                "nearestBuyWall": None, "nearestSellWall": None}
    blocked_ob = {"obstacleAhead": {"distancePct": 0.05, "ratio": 8.0},
                  "liquidityRatio": 1.0, "nearestBuyWall": None,
                  "nearestSellWall": {"distancePct": 0.05, "ratio": 8.0}}

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
