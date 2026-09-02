"""Halozat nelkuli onteszt a detektor magjara: python tests/test_core.py"""
import sys
import time
import types
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.detectors.scalp import ScalpDetector
from app.detectors.manager import DetectorManager
from app.detectors.baseline import Baseline, RollingMedian
from app.detectors.base import Trade
from app.eligibility import Eligibility
from app.fmt import pad as _pad
from app import orderbook
from app.ta import ema
import app.config as C_CFG

CFG = dict(C_CFG.DETECTOR_DEFAULTS)
MARKET = dict(C_CFG.MARKET_DEFAULTS)
TG = dict(C_CFG.TELEGRAM_DEFAULTS)
cfg_obj = types.SimpleNamespace(detector=CFG, market=MARKET, telegram=TG)


def eligible_stub():
    """Eligibility, ami mindent atenged -- a detektor tesztekhez."""
    e = Eligibility(cfg_obj)
    e.check = lambda symbol: (True, None, {})
    return e


def kesz_baseline(det, symbol, normal_pct=0.02, normal_notional=20_000.0,
                  start_ts=2000.0, db=400):
    """A par mindket normaljat kozvetlenul feltoltjuk, hogy a teszt ne varjon.

    A mintak a start_ts (a kesobbi futtat() elso trade-jenek ideje) ELE
    kerulnek, kozvetlenul mellette -- kulonben az elso valodi RollingMedian.add()
    hivas (ami a trade.ts szerint metsz) azonnal kiurítené az ablakot, mert a
    tavolsag a mesterseges seed-ido es a valodi trade-ido kozott nagyobb, mint
    a baseline ablak. Baseline nelkul a detektor szandekosan nem indit impulzust.
    """
    for i in range(db):
        det.baseline.add(symbol, start_ts - db + i, normal_pct)
        det.notional_baseline.add(symbol, start_ts - db + i, normal_notional)
    assert det.baseline.value(symbol) is not None
    assert det.notional_baseline.value(symbol) is not None
    return det


def uj_detektor(cfg=None, book=None, trend=None):
    return ScalpDetector(cfg or cfg_obj, book=book, trend=trend)


def futtat(det, symbol, trades, t0=2000.0):
    """Egy elore epitett tape lejatszasa, visszaadja az osszes jelzest."""
    out = []
    t = t0
    for price, buy, usd, dt in trades:
        t += dt
        sig = det.on_trade(Trade(symbol, price, usd / price, t, buy))
        if sig:
            out.append(sig)
    return out


def continuation_tape(up=True, retrace_frac=0.30, p0=100.0, leg=0.6,
                      breakout_frac=0.10):
    """Impulzus -> retrace_frac-os visszahuzas -> ujratores, egyiranyu flowval."""
    sign = 1 if up else -1
    buy = up
    out = []
    for i in range(40):                                    # 1) impulzus
        out.append((p0 + sign * leg * (i + 1) / 40, buy, 20_000, 0.1))
    peak = p0 + sign * leg
    low = peak - sign * leg * retrace_frac
    for i in range(20):                                     # 2) visszahuzas
        out.append((peak - sign * leg * retrace_frac * (i + 1) / 20, not buy, 3_000, 0.3))
    target = peak + sign * leg * breakout_frac
    for i in range(30):                                     # 3) ujratores
        out.append((low + (target - low) * (i + 1) / 30, buy, 8_000, 0.2))
    return out


def reversal_tape(break_holds=True, hold_ticks=20):
    """Impulzus le -> lassu visszapattanas -> ellen-visszahuzas (szint rogzul)
    -> a szint attorese, ami tart -- vagy nem, ha break_holds=False."""
    out = []
    for i in range(40):
        out.append((100.0 * (1 - 0.006 * (i + 1) / 40), False, 20_000, 0.1))
    for i in range(20):                                     # lassu visszapattanas (~10 mp)
        out.append((99.40 + 0.12 * (i + 1) / 20, True, 4_000, 0.5))
    for i in range(8):                                       # ellen-visszahuzas: a szint rogzul
        out.append((99.52 - 0.045 * (i + 1) / 8, False, 2_500, 0.4))
    for i in range(15):                                       # attores
        out.append((99.475 + 0.08 * (i + 1) / 15, True, 9_000, 0.3))
    if break_holds:
        for _ in range(hold_ticks):
            out.append((99.556, True, 9_000, 0.3))
    else:
        for i in range(10):                                    # azonnal visszaesik
            out.append((99.556 - 0.10 * (i + 1) / 10, False, 4_000, 0.3))
    return out


# ---------------------------------------------------------------- impulzus

def test_impulse_alone_is_not_a_signal():
    """Az impulzus csak egy setup kezdete, nem jelzes."""
    det = uj_detektor()
    kesz_baseline(det, "IUSDT")
    imp = continuation_tape()[:40]                      # csak az impulzus resze
    assert futtat(det, "IUSDT", imp) == []
    assert "IUSDT" in det.setups, "a setup elindult, csak nem jelzett"
    assert det.setups["IUSDT"].state == "IMPULSE_DETECTED"


def test_no_impulse_without_ready_baseline():
    """Amig nincs eleg minta MINDKET normalhoz, nem indul setup."""
    det = uj_detektor()
    assert det.baseline.value("CUSDT") is None
    imp = continuation_tape()[:40]
    assert futtat(det, "CUSDT", imp) == []
    assert "CUSDT" not in det.setups


def test_notional_gate_blocks_thin_impulse():
    """Ugyanaz az arelmozdulas, de vekony konyvbol -- nem eleg penz all mogotte."""
    det = uj_detektor()
    kesz_baseline(det, "TUSDT", normal_notional=20_000.0)
    vekony = [(p, b, usd / 50.0, dt) for p, b, usd, dt in continuation_tape()[:40]]
    assert futtat(det, "TUSDT", vekony) == []
    assert "TUSDT" not in det.setups, "vekony konyvbol nem indulhat impulzus"


def test_imbalance_gate_blocks_two_sided_move():
    """+0.6%-os mozgas, de a taker forgalom fele eladoi -- nem egyiranyu."""
    det = uj_detektor()
    kesz_baseline(det, "MUSDT")
    imp = continuation_tape()[:40]
    vegyes = [(p, (i % 2 == 0), usd, dt) for i, (p, b, usd, dt) in enumerate(imp)]
    assert futtat(det, "MUSDT", vegyes) == []
    assert "MUSDT" not in det.setups


def test_single_step_gate_blocks_book_sweep():
    """Egy nagy kotes atsopri a konyvet, a tobbi mar az uj aron nyomtat."""
    det = uj_detektor()
    kesz_baseline(det, "SUSDT")
    lepcsos = [(100.0, True, 20_000, 0.1)] * 20 + [(100.6, True, 20_000, 0.1)] * 20
    assert futtat(det, "SUSDT", lepcsos) == []
    assert "SUSDT" not in det.setups


def test_absolute_floor_applies_until_baseline_says_otherwise():
    """Abszolut padlo alatti mozgasbol sosem lesz impulzus, akarmennyire nyugodt a par."""
    det = uj_detektor()
    kesz_baseline(det, "FUSDT", normal_pct=0.001)   # nagyon nyugodt par
    kicsi = [(100.0 * (1 + 0.001 * (i + 1) / 40), True, 20_000, 0.1) for i in range(40)]
    assert futtat(det, "FUSDT", kicsi) == []
    assert "FUSDT" not in det.setups


def test_wild_pair_needs_bigger_move_than_calm_pair():
    """Ugyanaz a mozgas az egyik paron impulzus, a masikon a normal resze."""
    det_nyugodt = uj_detektor()
    kesz_baseline(det_nyugodt, "CALMUSDT", normal_pct=0.01)
    det_vad = uj_detektor()
    kesz_baseline(det_vad, "WILDUSDT", normal_pct=0.20)

    imp = continuation_tape()[:40]
    futtat(det_nyugodt, "CALMUSDT", imp)
    futtat(det_vad, "WILDUSDT", imp)
    assert "CALMUSDT" in det_nyugodt.setups, "nyugodt paron a +0.6% rendkivuli"
    assert "WILDUSDT" not in det_vad.setups, "vad paron a +0.6% a normal resze"


# ---------------------------------------------------------------- folytatas

def test_continuation_signal_after_shallow_pullback():
    det = uj_detektor()
    kesz_baseline(det, "CXUSDT")
    jelek = futtat(det, "CXUSDT", continuation_tape(retrace_frac=0.30))
    assert len(jelek) == 1, jelek
    assert jelek[0]["setup"] == "LONG_CONTINUATION"
    assert jelek[0]["direction"] == "LONG"
    assert "CXUSDT" not in det.setups, "a setup lezarult"


def test_short_continuation_is_the_mirror_image():
    det = uj_detektor()
    kesz_baseline(det, "CDUSDT")
    jelek = futtat(det, "CDUSDT", continuation_tape(up=False, retrace_frac=0.30))
    assert len(jelek) == 1, jelek
    assert jelek[0]["setup"] == "SHORT_CONTINUATION"
    assert jelek[0]["direction"] == "SHORT"


def test_deep_pullback_blocks_continuation():
    """Ha a visszahuzas mar tul mely volt, ez mar nem folytatas."""
    det = uj_detektor()
    kesz_baseline(det, "DPUSDT")
    assert futtat(det, "DPUSDT", continuation_tape(retrace_frac=0.80)) == []


def test_continuation_needs_matching_flow():
    """Az ujratores pillanataban a kotesaramlas ne az ellenkezo iranyba mutasson."""
    det = uj_detektor()
    kesz_baseline(det, "FLUSDT")
    tape = continuation_tape(retrace_frac=0.30)
    # az utolso (ujratores) szakaszon megforditjuk a takert -- eladoi nyomas LONG-nal
    forditott = tape[:-30] + [(p, False, usd, dt) for p, b, usd, dt in tape[-30:]]
    assert futtat(det, "FLUSDT", forditott) == []


# ---------------------------------------------------------------- fordulo

def test_reversal_signal_after_exhaustion_and_reclaim():
    det = uj_detektor()
    kesz_baseline(det, "RXUSDT")
    jelek = futtat(det, "RXUSDT", reversal_tape())
    assert len(jelek) == 1, jelek
    assert jelek[0]["setup"] == "LONG_REVERSAL"
    assert jelek[0]["direction"] == "LONG"


def test_reversal_requires_the_break_to_hold():
    """Az attores pillanata meg nem fordulo -- ha azonnal visszaesik, nincs jelzes."""
    det = uj_detektor()
    kesz_baseline(det, "RHUSDT")
    assert futtat(det, "RHUSDT", reversal_tape(break_holds=False)) == []


def test_reversal_needs_a_locked_level():
    """Amig nincs ellen-visszahuzas (a szint sosem fordul vissza), nem rogzul --
    nincs mit attorni, tehat a folyamatosan tovabb kuszo ar sosem lesz fordulo."""
    det = uj_detektor()
    kesz_baseline(det, "NLUSDT")
    tape = reversal_tape()[:60]                    # impulzus + visszapattanas 99.52-ig
    # a visszapattanas MEGALLAS NELKUL folytatodik -- sosem fordul vissza, tehat
    # a szint (counter) sosem rogzul
    folytatodik = [(99.52 + 0.18 * (i + 1) / 15, True, 4_000, 0.4) for i in range(15)]
    assert futtat(det, "NLUSDT", tape + folytatodik) == []


def test_reversal_direction_mirrors_the_impulse():
    """Felfele impulzus utan a fordulo SHORT."""
    det = uj_detektor()
    kesz_baseline(det, "RMUSDT")
    tukor = [(200.0 - p, b, usd, dt) for p, b, usd, dt in reversal_tape()]
    # a taker oldalt is tukrozzuk, hiszen fizikailag forditott mozgasrol van szo
    tukor = [(p, not b, usd, dt) for p, b, usd, dt in tukor]
    jelek = futtat(det, "RMUSDT", tukor)
    assert len(jelek) == 1, jelek
    assert jelek[0]["setup"] == "SHORT_REVERSAL"


# ---------------------------------------------------------------- setup elettartam

def test_setup_invalidates_after_timeout():
    det = uj_detektor()
    kesz_baseline(det, "TOUSDT")
    imp = continuation_tape()[:40]
    futtat(det, "TOUSDT", imp)
    assert "TOUSDT" in det.setups
    hosszu_varakozas = [(100.6, True, 3_000, det.cfg.detector["setupTimeoutSec"] + 1)]
    futtat(det, "TOUSDT", hosszu_varakozas, t0=2000.0 + 40 * 0.1)
    assert "TOUSDT" not in det.setups, "a lejart setupot el kellett dobni"


def test_setup_invalidates_beyond_origin():
    """Ha az ar visszamegy az impulzus kiindulopontja ALA, vege a setupnak."""
    det = uj_detektor()
    kesz_baseline(det, "OBUSDT")
    imp = continuation_tape()[:40]
    futtat(det, "OBUSDT", imp)
    assert "OBUSDT" in det.setups
    zuhanas = [(99.5, False, 3_000, 0.2)]              # jol az origin (100.0) ala
    futtat(det, "OBUSDT", zuhanas, t0=2004.0)
    assert "OBUSDT" not in det.setups


def test_long_structure_is_followed_not_just_the_instant_v():
    """30-90 masodperces szerkezetet is vegig kell tudni kovetni, nem csak a
    masodperces V-fordulot. Csak a VISSZAHUZAS szakaszat nyujtjuk meg -- az
    impulzus es az ujratores sűrűsegenek meg kell maradnia a minTradesInWindow
    es a flowWindowSec ablakokhoz."""
    det = uj_detektor()
    kesz_baseline(det, "LSUSDT")
    tape = continuation_tape(retrace_frac=0.30)
    impulzus, visszahuzas, ujratores = tape[:40], tape[40:60], tape[60:]
    nyujtott_visszahuzas = [(p, b, usd, dt * 6) for p, b, usd, dt in visszahuzas]
    jelek = futtat(det, "LSUSDT", impulzus + nyujtott_visszahuzas + ujratores)
    assert len(jelek) == 1, jelek
    assert jelek[0]["metrics"]["setupAgeSec"] > 30


def test_symbol_cooldown_suppresses_repeat():
    det = uj_detektor()
    kesz_baseline(det, "COUSDT")
    tape = continuation_tape(retrace_frac=0.30) * 1
    egy = futtat(det, "COUSDT", tape)
    assert len(egy) == 1
    # ugyanaz megint, kozvetlen utana -- a cooldown alatt vagyunk
    ketto = futtat(det, "COUSDT", tape, t0=2000.0 + 90 * 0.1 + 20 * 0.3 + 30 * 0.2)
    assert ketto == []


# ---------------------------------------------------------------- konyv es trend

class FakeBook:
    def __init__(self, ctx):
        self._ctx = ctx

    def context(self, symbol):
        return self._ctx

    def snapshot(self, symbol):
        return None


def test_book_wall_blocks_continuation():
    """Egy kozeli fal a mozgas iranyaban -- a folytatas nem mehet ki."""
    fal = {"price": 100.65, "distancePct": 0.05, "notional": 999.0, "ratio": 9.0}
    book = FakeBook({"spreadPct": 0.01, "topImbalance": 0.0,
                     "wallAsk": fal, "wallBid": None, "levels": 20})
    det = uj_detektor(book=book)
    kesz_baseline(det, "WBUSDT")
    assert futtat(det, "WBUSDT", continuation_tape(retrace_frac=0.30)) == []


def test_book_imbalance_blocks_continuation():
    """Eros konyv-tulsuly az ELLENKEZO oldalon -- a folytatas nem mehet ki."""
    book = FakeBook({"spreadPct": 0.01, "topImbalance": -0.9,
                     "wallAsk": None, "wallBid": None, "levels": 20})
    det = uj_detektor(book=book)
    kesz_baseline(det, "BIUSDT")
    assert futtat(det, "BIUSDT", continuation_tape(retrace_frac=0.30)) == []


def test_no_book_data_does_not_block_the_signal():
    """Ha nincs friss konyv-adat, nem nemitunk el mindent -- csak nem szurunk vele."""
    book = FakeBook(None)
    det = uj_detektor(book=book)
    kesz_baseline(det, "NBUSDT")
    jelek = futtat(det, "NBUSDT", continuation_tape(retrace_frac=0.30))
    assert len(jelek) == 1


class FakeTrend:
    def __init__(self, trend):
        self._trend = trend

    def get(self, symbol):
        return {"trend": self._trend} if self._trend else None


def test_continuation_requires_matching_trend_by_default():
    """requireTrendForContinuation alapbol be van kapcsolva."""
    trend = FakeTrend("bearish")             # LONG folytatashoz nem illik
    det = uj_detektor(trend=trend)
    kesz_baseline(det, "TRUSDT")
    assert futtat(det, "TRUSDT", continuation_tape(retrace_frac=0.30)) == []

    trend_jo = FakeTrend("bullish")
    det2 = uj_detektor(trend=trend_jo)
    kesz_baseline(det2, "TR2USDT")
    assert len(futtat(det2, "TR2USDT", continuation_tape(retrace_frac=0.30))) == 1


def test_reversal_does_not_require_trend_by_default():
    """requireTrendForReversal alapbol KI van kapcsolva -- a fordulo szandekosan
    szembe megy a rovid tavu trenddel."""
    trend = FakeTrend("bearish")             # a LONG_REVERSAL "szembe" megy ezzel
    det = uj_detektor(trend=trend)
    kesz_baseline(det, "TR3USDT")
    assert len(futtat(det, "TR3USDT", reversal_tape())) == 1


# ---------------------------------------------------------------- baseline / RollingMedian

def test_rolling_median_ignores_outliers():
    rm = RollingMedian(window_sec=300, min_samples=5)
    assert rm.value("X") is None
    for i in range(20):
        rm.add("X", 1000.0 + i, 10.0)
    assert rm.value("X") == 10.0
    rm.add("X", 1021.0, 10_000.0)             # egyetlen kiugro ertek
    assert rm.value("X") == 10.0, "a median nem viheto el egy kiugro ertekkel"


def test_baseline_value_for_scales_with_sqrt_of_time():
    b = Baseline(cfg_obj)
    for i in range(400):
        b.add("WUSDT", 1000.0 + i, 0.02)
    alap = b.value("WUSDT")
    ablak = CFG["impulseWindowSec"]
    assert b.value_for("WUSDT", ablak) == alap
    negyszer = b.value_for("WUSDT", ablak * 4)
    assert abs(negyszer / alap - 2.0) < 0.01, negyszer / alap
    assert b.value_for("ISMERETLEN", ablak) is None


# ---------------------------------------------------------------- order book

def test_wall_detection():
    price = 100.0
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)]
    asks[8] = (100.08, 60.0)
    wall = orderbook.find_wall(asks, price, CFG["wallSensitivity"], CFG["wallMaxDistancePct"])
    assert wall is not None
    assert wall["price"] == 100.08
    assert abs(wall["distancePct"] - 0.08) < 0.001
    assert wall["ratio"] > 3.0


def test_no_wall_when_flat():
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)]
    assert orderbook.find_wall(asks, 100.0, CFG["wallSensitivity"], CFG["wallMaxDistancePct"]) is None


def test_wall_outside_range_ignored():
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)] + [(105.0, 500.0)]
    assert orderbook.find_wall(asks, 100.0, CFG["wallSensitivity"], CFG["wallMaxDistancePct"]) is None


def test_wall_is_measured_against_the_median_not_the_mean():
    import statistics
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)]
    asks[8] = (100.08, 10.0)
    n = [p * q for p, q in asks]
    atlaghoz = n[8] / (sum(n) / len(n))
    w = orderbook.find_wall(asks, 100.0, 3.0, 1.5)
    assert abs(w["ratio"] - n[8] / statistics.median(n)) < 0.01
    assert w["ratio"] > atlaghoz + 2, (atlaghoz, w["ratio"])


def test_touch_level_is_not_a_wall():
    """Elesben a BTC/ETH sajat legjobb ajanlata szamitott 'falnak', es emiatt
    minden jelzesuk elbukott. A touch nem akadaly: ott lepsz be."""
    asks = [(100.01, 1000.0)] + [(100.02 + i * 0.01, 1.0) for i in range(19)]
    assert orderbook.find_wall(asks, 100.0, 3.0, 1.5) is None
    asks[5] = (asks[5][0], 40.0)
    w = orderbook.find_wall(asks, 100.0, 3.0, 1.5)
    assert w is not None and w["distancePct"] > 0.0


def test_bookcache_context_and_snapshot():
    from app.bookcache import BookCache
    bc = BookCache(cfg_obj)
    assert bc.context("XUSDT") is None
    bids = [[100.0 - i * 0.01, 1.0] for i in range(20)]
    asks = [[100.02 + i * 0.01, 1.0] for i in range(20)]
    bids[3] = [99.97, 50.0]
    bc.on_depth({"s": "XUSDT", "b": bids, "a": asks})
    ctx = bc.context("XUSDT")
    assert ctx is not None
    assert -1.0 <= ctx["topImbalance"] <= 1.0
    assert ctx["wallBid"] is not None and ctx["wallBid"]["price"] == 99.97
    snap = bc.snapshot("XUSDT")
    assert len(snap["bids"]) == 20 and len(snap["asks"]) == 20


# ---------------------------------------------------------------- eligibility

def _book(e, symbol, bid, ask, qty=100000.0):
    for _ in range(40):
        e.on_book_ticker({"e": "bookTicker", "s": symbol, "b": str(bid),
                          "B": str(qty / bid), "a": str(ask), "A": str(qty / ask)})


def test_eligibility_accepts_a_liquid_pair():
    e = Eligibility(cfg_obj)
    _book(e, "BTCUSDT", 61000.0, 61000.6)
    mehet, ok, m = e.check("BTCUSDT")
    assert mehet, (ok, m)
    assert m["spreadPct"] < 0.01


def test_eligibility_rejects_wide_spread():
    e = Eligibility(cfg_obj)
    _book(e, "WIDEUSDT", 1.000, 1.002)
    assert e.check("WIDEUSDT")[1] == "spread_too_wide"


def test_eligibility_without_book_data_does_not_pass():
    e = Eligibility(cfg_obj)
    _book(e, "OTHERUSDT", 1.0000, 1.0001)
    assert e.check("UNKNOWNUSDT")[1] == "no_book_data"


def test_total_book_outage_fails_open_and_says_so():
    e = Eligibility(cfg_obj)
    assert e.check("BTCUSDT")[0] is True
    assert "NEM ERKEZIK" in e.book_status()
    e.on_book_ticker({"e": "bookTicker", "s": "BTCUSDT", "b": "1.0000",
                      "B": "100000", "a": "1.0001", "A": "100000"})
    assert "konyv: 1 par" in e.book_status()


def test_blacklist_and_whitelist():
    cfg = types.SimpleNamespace(market={**MARKET, "symbolBlacklist": ["BADUSDT"]})
    e = Eligibility(cfg)
    _book(e, "BADUSDT", 1.0000, 1.0001)
    assert e.check("BADUSDT")[1] == "blacklisted"

    cfg = types.SimpleNamespace(market={**MARKET, "symbolWhitelist": ["ONLYUSDT"]})
    e = Eligibility(cfg)
    for sym in ("ONLYUSDT", "OTHERUSDT"):
        _book(e, sym, 1.0000, 1.0001)
    assert e.check("ONLYUSDT")[0] is True
    assert e.check("OTHERUSDT")[1] == "not_whitelisted"


def test_rejection_reasons_have_hungarian_text_and_machine_key():
    from app.eligibility import OKOK, szoveg
    e = Eligibility(cfg_obj)
    _book(e, "WIDE2USDT", 1.000, 1.002)
    kulcs = e.check("WIDE2USDT")[1]
    assert kulcs == "spread_too_wide"
    assert szoveg(kulcs) == "tul szeles a spread"
    assert all(szoveg(k) != k for k in OKOK)


def test_eligibility_summary_aggregates_by_reason():
    e = Eligibility(cfg_obj)
    for sym in ("A_USDT", "B_USDT"):
        _book(e, sym, 1.000, 1.002)
        e.check(sym)
    e.cfg = types.SimpleNamespace(market={**MARKET, "symbolBlacklist": ["C_USDT"]})
    _book(e, "C_USDT", 1.0000, 1.0001)
    e.check("C_USDT")
    osszegzes = e.summary()[0]
    assert "kizarva 3" in osszegzes, osszegzes
    assert "tul szeles a spread: 2" in osszegzes, osszegzes


def test_distribution_block_is_safe_and_informative():
    e = Eligibility(cfg_obj)
    assert e.distribution([]) == [], "ures adaton se hibazzon"
    for nev, bid, ask in (("A", 100.0, 100.001), ("B", 100.0, 100.01),
                          ("C", 100.0, 100.2)):
        _book(e, nev, bid, ask)
        e.check(nev)
    sorok = e.distribution(["A", "B", "C"])
    assert len(sorok) == 1
    assert "spread" in sorok[0] and "kuszob" in sorok[0] and "par felette" in sorok[0]


def test_ineligible_pair_builds_state_but_emits_nothing():
    """A szuro a JELZESNEL all, nem a detektor elott: az allapot epul, de jelzes
    nem megy ki."""
    e = Eligibility(cfg_obj)
    _book(e, "OTHERUSDT", 1.0000, 1.0001)
    _book(e, "CYSUSDT", 0.7800, 0.7830)            # szeles spread -> nem kereskedheto
    det = uj_detektor()
    kesz_baseline(det, "CYSUSDT")
    mgr = DetectorManager(cfg_obj, [det], e)
    t = 2000.0
    for price, buy, usd, dt in continuation_tape(retrace_frac=0.30):
        t += dt
        mgr.on_trade(Trade("CYSUSDT", price, usd / price, t, buy))
    assert mgr.total_candidates == 0, "jelzes nem mehet ki"
    assert mgr.skipped > 0, "de a detektor eljutott a candidate-ig"
    assert "CYSUSDT" in det.window, "es az allapota epult"


# ---------------------------------------------------------------- manager

class BoomDetector:
    name, config_key = "boom", "detector"

    def on_trade(self, trade):
        raise RuntimeError("szandekos hiba")

    def status_lines(self):
        return []


def test_manager_fans_out_and_survives_a_broken_detector():
    scalp = uj_detektor()
    kesz_baseline(scalp, "CYSUSDT")
    mgr = DetectorManager(cfg_obj, [BoomDetector(), scalp], eligible_stub())
    tape = continuation_tape(retrace_frac=0.30)
    signals = []
    t = 2000.0
    for price, buy, usd, dt in tape:
        t += dt
        signals += mgr.on_trade(Trade("CYSUSDT", price, usd / price, t, buy))
    assert len(signals) == 1, "a hibas detektor nem nyelheti el a masik jelzeset"
    assert mgr.ticks == len(tape)
    assert mgr.total_candidates == 1


def test_manager_skips_disabled_detector():
    cfg = types.SimpleNamespace(detector={**CFG, "enabled": False}, market=MARKET,
                                telegram=TG)
    mgr = DetectorManager(cfg, [ScalpDetector(cfg)], eligible_stub())
    signals = []
    t = 2000.0
    for price, buy, usd, dt in continuation_tape(retrace_frac=0.30):
        t += dt
        signals += mgr.on_trade(Trade("CYSUSDT", price, usd / price, t, buy))
    assert signals == []


# ---------------------------------------------------------------- config

def test_cold_start_creates_every_setting():
    """HIDEGINDITAS: a config collection URES. A rendszernek minden beallitassal
    egyutt fel kell allnia -- semmi nem varhat kezi mongo parancsra.

    A user minden ujrainditasnal torli a configot, tehat ez a normal eset.
    """
    import asyncio

    class FakeCollection:
        def __init__(self): self.docs = {}
        async def find_one(self, q):
            d = self.docs.get(q["_id"])
            return dict(d) if d else None
        async def insert_one(self, doc): self.docs[doc["_id"]] = dict(doc)
        async def update_one(self, q, update, upsert=False):
            doc = self.docs.setdefault(q["_id"], {"_id": q["_id"]})
            doc.update(update.get("$set", {}))
            for k in update.get("$unset", {}):
                doc.pop(k, None)

    coll = FakeCollection()
    store = C_CFG.ConfigStore(types.SimpleNamespace(config=coll))
    asyncio.run(store.load())

    for defaults, attr in C_CFG.ConfigStore.DOCS:
        doc = coll.docs.get(defaults["_id"])
        assert doc, f"a '{defaults['_id']}' dokumentum nem jott letre hidegindulaskor"
        hianyzo = [k for k in defaults if k not in doc]
        assert not hianyzo, f"{defaults['_id']}: hianyzo beallitas {hianyzo}"
        betoltve = getattr(store, attr)
        for k, v in defaults.items():
            assert betoltve[k] == v, f"{defaults['_id']}.{k}: {betoltve[k]!r} != {v!r}"

    from app.main import startup_summary
    assert startup_summary(store)


def test_config_migration_moves_your_existing_values():
    """A kozos beallitasok kikerultek a 'detector'-bol egy uj 'market' dokumentumba.

    A koltoztetesnek a MAR BEALLITOTT ertekeket kell atvinnie -- kulonben a
    szetvalasztas csendben visszaallitana mindent alapertelmezettre.
    """
    import asyncio

    class FakeCollection:
        def __init__(self, docs):
            self.docs = {d["_id"]: dict(d) for d in docs}

        async def find_one(self, q):
            d = self.docs.get(q["_id"])
            return dict(d) if d else None

        async def insert_one(self, doc):
            self.docs[doc["_id"]] = dict(doc)

        async def update_one(self, q, update, upsert=False):
            doc = self.docs.setdefault(q["_id"], {"_id": q["_id"]}) if upsert \
                else self.docs[q["_id"]]
            doc.update(update.get("$set", {}))
            for k in update.get("$unset", {}):
                doc.pop(k, None)

    coll = FakeCollection([{"_id": "detector", "minQuoteVolume24h": 250_000_000,
                            "symbolWhitelist": ["BTCUSDT"], "telegramEnabled": False,
                            "impulseBaselineRatio": 9.0}])
    store = C_CFG.ConfigStore(types.SimpleNamespace(config=coll))
    asyncio.run(store.load())

    assert store.market["minQuoteVolume24h"] == 250_000_000, store.market
    assert store.market["symbolWhitelist"] == ["BTCUSDT"]
    assert store.telegram["enabled"] is False, "a telegramEnabled=false nem veszhet el"
    assert store.detector["impulseBaselineRatio"] == 9.0, "a sajat detector ertek marad"
    assert "minQuoteVolume24h" not in coll.docs["detector"], "a regi kulcs kikerult"


def test_every_config_key_is_documented():
    """A doksi ne csusszon el a kodtol: minden beallitas szerepeljen a
    docs/PARAMETEREK.md-ben."""
    doksi = (pathlib.Path(__file__).parent.parent / "docs" / "PARAMETEREK.md").read_text()
    for defaults in (C_CFG.MARKET_DEFAULTS, C_CFG.DETECTOR_DEFAULTS,
                     C_CFG.TELEGRAM_DEFAULTS):
        for k in defaults:
            if k == "_id":
                continue
            assert f"`{k}`" in doksi, f"{defaults['_id']}.{k} nincs dokumentalva"


def test_every_config_key_read_by_the_code_exists():
    """Ha atnevezunk egy beallitast, ne maradjon regi hivatkozas a kodban."""
    import re
    ismert = set().union(*(set(d) for d in (
        C_CFG.MARKET_DEFAULTS, C_CFG.DETECTOR_DEFAULTS,
        C_CFG.TRADING_DEFAULTS, C_CFG.TELEGRAM_DEFAULTS)))
    minta = re.compile(
        r'(?:\b(?:c|cfg|own|shared|conf|tg)\b|\.(?:market|detector|trading|telegram))'
        r'\s*\[\s*["\'](\w+)["\']\s*\]')
    hibas = []
    for f in sorted((pathlib.Path(__file__).parent.parent / "app").rglob("*.py")):
        src = f.read_text()
        for m in minta.finditer(src):
            if m.group(1) not in ismert:
                sor = src[:m.start()].count("\n") + 1
                hibas.append(f"{f.name}:{sor} -> {m.group(1)}")
    assert not hibas, "nem letezo config kulcsra hivatkozunk: " + ", ".join(hibas)


def test_startup_summary_renders():
    from app.main import startup_summary
    cfg = types.SimpleNamespace(
        market=dict(C_CFG.MARKET_DEFAULTS), detector=dict(C_CFG.DETECTOR_DEFAULTS),
        trading=dict(C_CFG.TRADING_DEFAULTS), telegram=dict(C_CFG.TELEGRAM_DEFAULTS))
    sorok = startup_summary(cfg)
    assert len(sorok) == 6 and all(isinstance(x, str) and x for x in sorok)


def test_status_line_only_uses_existing_attributes():
    """Regresszio: a STATUS sor egy mar nem letezo mezore hivatkozott, es a
    status task elesben elszallt AttributeError-ral."""
    import re
    from app.signals import SignalService
    forras = (pathlib.Path(__file__).parent.parent / "app" / "market_data.py").read_text()
    for attr in set(re.findall(r"\bsvc\.([a-zA-Z_]+)", forras)):
        assert hasattr(SignalService, attr) or attr in SignalService.__init__.__code__.co_names, \
            f"SignalService.{attr} nem letezik"
    for attr in set(re.findall(r"self\.detectors\.([a-zA-Z_]+)", forras)):
        assert hasattr(DetectorManager, attr) or attr in DetectorManager.__init__.__code__.co_names, \
            f"DetectorManager.{attr} nem letezik"


def test_no_blocking_calls_on_the_signal_path():
    """A jelzes utjan NINCS halozati varakozas: sem order book lekeres, sem klines.
    Korabban epp a jelzes pillanataban vartunk ezekre."""
    forras = (pathlib.Path(__file__).parent.parent / "app" / "signals.py").read_text()
    assert "orderbook.analyze" not in forras
    assert "ta.analyze" not in forras
    assert "await asyncio.gather" not in forras


def test_reconnect_storm_is_rate_limited():
    """A Binance az IP-t tiltja ki, ha tul suru a kapcsolodasi kiserlet."""
    import asyncio
    from app.market_data import ConnectLimiter

    async def probal():
        lim = ConnectLimiter(min_gap=0.01, max_per_5min=5)
        for _ in range(5):
            await lim.wait()
        assert lim.utolso_5_perc() == 5
        lim.kiserletek[0] = time.time() - 299.95
        t0 = time.time()
        await lim.wait()
        assert time.time() - t0 >= 0.04, "varnia kellett a keret felszabadulasara"

    asyncio.run(probal())

    forras = (pathlib.Path(__file__).parent.parent / "app" / "market_data.py").read_text()
    stream = forras[forras.index("async def _stream"):forras.index("async def _depth_stream")]
    assert "await self.limiter.wait()" in stream
    assert stream.count("await asyncio.sleep(backoff)") == 1
    assert "EGESZSEGES_SEC" in stream


def test_rate_limit_is_recognised_and_never_crashes_the_app():
    import asyncio
    from app import binance_rest as BR

    assert BR.ban_seconds(418, {"Retry-After": "120"}) == 120.0
    assert BR.ban_seconds(429, {}) == 60.0
    assert BR.ban_seconds(429, {"Retry-After": "x"}) == 60.0
    assert BR.ban_seconds(200, {}) is None and BR.ban_seconds(404, {}) is None

    from app.market_data import MarketDataService

    class FakeStatus:
        def __init__(self, symbols): self.doc = {"_id": "symbols", "symbols": symbols}
        async def find_one(self, q): return dict(self.doc)
        async def update_one(self, q, u, upsert=False): self.doc.update(u["$set"])

    db = types.SimpleNamespace(status=FakeStatus(["BTCUSDT", "ETHUSDT"]))
    m = MarketDataService(cfg_obj, db, None, None, None)
    eredeti = BR.load_symbols

    async def tiltva(*a, **k):
        raise BR.RateLimited(418, 120)

    BR.load_symbols = tiltva
    try:
        symbols = asyncio.run(m._load_symbols())
    finally:
        BR.load_symbols = eredeti
    assert symbols == ["BTCUSDT", "ETHUSDT"], symbols
    assert m.stale_symbols is True

    forras = (pathlib.Path(__file__).parent.parent / "app" / "main.py").read_text()
    assert "await asyncio.sleep(60)" in forras and "raise" in forras


def test_book_and_trade_streams_use_different_url_segments():
    from app import market_data as MD
    assert MD.WS_BASES[0].endswith("/market/stream")
    assert MD.BOOK_BASES[0].endswith("/public/stream")
    assert MD.WS_BASES[0] != MD.BOOK_BASES[0]
    assert MD.BOOK_BASES[1:] == MD.WS_BASES[1:]


def test_cjk_symbol_column_alignment():
    assert _pad("NEARUSDT", 15) == "NEARUSDT" + " " * 7
    assert _pad("龙虾USDT", 15) == "龙虾USDT" + " " * 7
    assert _pad("MAGMAUSDT", 15) == "MAGMAUSDT" + " " * 6

    def width(t):
        import unicodedata
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in t)
    assert width(_pad("龙虾USDT", 15)) == width(_pad("NEARUSDT", 15)) == 15


def test_ema_matches_known_values():
    closes = [float(i) for i in range(1, 61)]
    assert ema(closes, 9) > ema(closes, 21)
    assert abs(ema([5.0] * 40, 9) - 5.0) < 1e-9


# ---------------------------------------------------------------- outcome

def _outcome_cfg(tp=None, sl=None, track_sec=600):
    m = {**MARKET, "outcomeTrackSec": track_sec,
         "tpLevels": tp or [0.3, 0.5, 0.8], "slLevels": sl or [0.2, 0.3, 0.5],
         "reportTp": 0.5, "reportSl": 0.3}
    return types.SimpleNamespace(market=m)


def test_outcome_mfe_mae_and_direction_correction():
    """LONG-nal a felfele mozgas MFE, SHORT-nal a lefele -- iranyhelyesen."""
    from app.outcome import Tracker

    long_t = Tracker("id1", "AUSDT", "LONG_CONTINUATION", "LONG", 100.0, 0.0, 600.0, [], [])
    for price, ts in ((101.0, 1.0), (99.0, 2.0), (100.5, 3.0)):
        long_t.on_price(price, ts)
    assert abs(long_t.mfe - 1.0) < 1e-9 and long_t.t_mfe == 1.0
    assert abs(long_t.mae - (-1.0)) < 1e-9 and long_t.t_mae == 2.0
    assert abs(long_t.final - 0.5) < 1e-9

    short_t = Tracker("id2", "BUSDT", "SHORT_REVERSAL", "SHORT", 100.0, 0.0, 600.0, [], [])
    for price, ts in ((99.0, 1.0), (101.0, 2.0)):
        short_t.on_price(price, ts)
    assert abs(short_t.mfe - 1.0) < 1e-9, "SHORT-nal a leeso ar a nyereseg"
    assert abs(short_t.mae - (-1.0)) < 1e-9, "SHORT-nal a felmeno ar a veszteseg"


def test_outcome_tp_sl_first_touch_decides_the_winner():
    """Mivel minden kotest latunk, barmely TP/SL parra utolag eldontheto, melyiket
    erte el elobb -- kulon meres nelkul."""
    from app.outcome import Tracker

    # eloszor eleri a TP 0.5-ot, csak utana esik SL 0.3 ala
    t = Tracker("id", "XUSDT", "LONG_CONTINUATION", "LONG", 100.0, 0.0, 600.0,
                [0.3, 0.5], [0.2, 0.3])
    for price, ts in ((100.4, 1.0), (100.6, 2.0), (99.6, 3.0)):
        t.on_price(price, ts)
    assert t.tp["0.5"] == 2.0 and t.tp["0.3"] == 1.0
    assert t.sl["0.3"] == 3.0 and t.sl["0.2"] == 3.0, "a -0.4% mindket SL szintet eleri"
    assert t.eredmeny(0.5, 0.3) == "nyero", "a TP 0.5 hamarabb jott, mint az SL 0.3"
    assert t.eredmeny(0.3, 0.2) == "nyero", "a TP 0.3 (t=1) hamarabb jott, mint az SL 0.2 (t=3)"

    # forditva: eloszor SL, csak utana TP
    t2 = Tracker("id2", "YUSDT", "LONG_CONTINUATION", "LONG", 100.0, 0.0, 600.0,
                 [0.5], [0.3])
    for price, ts in ((99.6, 1.0), (100.6, 2.0)):
        t2.on_price(price, ts)
    assert t2.eredmeny(0.5, 0.3) == "buko", "az SL hamarabb jott"


def test_outcome_tracks_after_signal_without_touching_the_detector():
    """Az eredmenymeres nem kapuz semmit: a jelzes UTAN jegyzi fel az arat."""
    import asyncio
    from app.outcome import OutcomeTracker

    class FakeSignals:
        def __init__(self): self.updates = []
        async def update_one(self, q, u): self.updates.append((q, u))

    coll = FakeSignals()
    o = OutcomeTracker(_outcome_cfg(track_sec=1), types.SimpleNamespace(signals=coll))
    o.track("id-long", "AUSDT", "LONG_CONTINUATION", "LONG", 100.0)
    o.on_trade(Trade("AUSDT", 100.5, 1.0, 0.0, True))
    asyncio.run(o._flush())
    assert coll.updates, "az elo merest is menteni kell (dirty flush)"
    doc = coll.updates[-1][1]["$set"]["outcome"]
    assert doc["mfePct"] > 0 and doc["done"] is False

    # amirol nem erkezik kotes, arrol nem talalunk ki adatot -- a belepo marad az entry
    o2 = OutcomeTracker(_outcome_cfg(track_sec=0.001),
                        types.SimpleNamespace(signals=FakeSignals()))
    o2.track("id-nema", "CUSDT", "LONG_CONTINUATION", "LONG", 100.0)
    time.sleep(0.01)
    asyncio.run(o2._flush())
    assert o2.keszek[0].final == 0.0


def test_outcome_summary_groups_by_setup_type():
    o_cfg = _outcome_cfg()
    from app.outcome import OutcomeTracker, Tracker
    o = OutcomeTracker(o_cfg, None)

    def kesz(sid, setup, direction, tp_hit, sl_hit):
        t = Tracker(sid, "XUSDT", setup, direction, 100.0, 0.0, 600.0, [0.5], [0.3])
        t.tp["0.5"] = tp_hit
        t.sl["0.3"] = sl_hit
        t.mfe, t.mae = 0.5, -0.1
        t.done = True
        return t

    o.keszek = [
        kesz("a", "LONG_CONTINUATION", "LONG", 1.0, None),   # nyero
        kesz("b", "LONG_CONTINUATION", "LONG", None, 1.0),   # buko
        kesz("c", "SHORT_REVERSAL", "SHORT", 1.0, None),     # nyero
    ]
    sorok = o.summary_lines()
    szoveg = "\n".join(sorok)
    assert "LONG_CONTINUATION" in szoveg and "SHORT_REVERSAL" in szoveg
    assert "SL -0.3%" in szoveg or "SL -0.3" in szoveg

    utolso = "\n".join(o.recent_lines())
    assert "UTOLSO" in utolso and "nyero" in utolso


def test_outcome_history_is_loaded_at_startup():
    """Az osszesites ne nullazodjon minden ujrainditasnal."""
    import asyncio
    from app.outcome import OutcomeTracker

    class FakeCursor:
        def __init__(self, docs): self.docs = docs
        def sort(self, *a): return self
        def limit(self, n): return self
        async def to_list(self, length=None): return self.docs

    class FakeSignals:
        def __init__(self, docs): self.docs = docs
        def find(self, q): return FakeCursor(self.docs)

    import datetime as _dt
    most = _dt.datetime(2026, 9, 1, 6, 56, tzinfo=_dt.timezone.utc)
    docs = [{
        "_id": "a", "timestamp": most, "symbol": "BTRUSDT",
        "detector": "scalp", "setup": "LONG_CONTINUATION", "direction": "LONG",
        "price": 100.0,
        "outcome": {"entry": 100.0, "setup": "LONG_CONTINUATION",
                   "mfePct": 1.2, "maePct": -0.3, "timeToMfeSec": 30,
                   "timeToMaeSec": 5, "maxPrice": 101.2, "minPrice": 99.7,
                   "tp": {"0.5": 25.0}, "sl": {"0.3": None},
                   "finalPct": 0.8, "done": True},
    }]
    o = OutcomeTracker(_outcome_cfg(), types.SimpleNamespace(signals=FakeSignals(docs)))
    asyncio.run(o.load_history())
    assert len(o.keszek) == 1
    assert o.keszek[0].mfe == 1.2 and o.keszek[0].done is True


# ---------------------------------------------------------------- telegram

def test_telegram_heartbeat_renders():
    """Idoszakos eletjel: ures allapotban se hibazzon."""
    from app.telegram import format_status

    ures = {"ido": "14:20:03", "uptime": "0h 1p", "symbols": 0, "wsConnected": 0,
            "wsTotal": 1, "ticksPerMin": 0, "signals": 0, "kizarva": "",
            "setups": [], "kozel": "", "talalat": [], "utolso": [],
            "reconnects5min": 0}
    szoveg = format_status(ures)
    assert "ELETJEL" in szoveg and "Meg nincs lemert jelzes" in szoveg

    teli = {**ures, "symbols": 58, "wsConnected": 1, "ticksPerMin": 1932,
            "signals": 7, "kizarva": "kizarva 2: tul szeles a spread: 2",
            "setups": [("IMPULSE_DETECTED", "2"), ("WAITING_CONFIRMATION", "1")],
            "kozel": "normal kesz: 30/58 par | legkozelebb: SOLUSDT 0.31%",
            "talalat": ["TP +0.5% / SL -0.3%",
                        "LONG_CONTINUATION       7      4     2        1   67%    +0.60%    -0.20%"],
            "utolso": ["UTOLSO 3 LONG_CONTINUATION",
                       "SOLUSDT     09-01 04:02  belepo 100.00        MFE  +0.60%  MAE  -0.20%  -> nyero"]}
    szoveg = format_status(teli)
    for kell in ("58", "1,932", "SOLUSDT", "DETEKTOR ALLAPOT", "IMPULSE_DETECTED",
                 "UTOLSO 3 LONG_CONTINUATION", "nyero"):
        assert kell in szoveg, (kell, szoveg)
    assert "Meg nincs lemert jelzes" not in szoveg


def test_telegram_format_signal_uses_setup_headers():
    from app.telegram import format_signal
    import datetime as _dt

    sig = {
        "timestamp": _dt.datetime(2026, 9, 1, 12, 0, tzinfo=_dt.timezone.utc),
        "detector": "scalp", "setup": "LONG_REVERSAL", "symbol": "XUSDT",
        "direction": "LONG", "price": 100.0, "url": "https://example.test/XUSDT",
        "quoteVolume24h": 500_000_000,
        "reasons": ["impulzus -0.40% / 3.0s", "a szint reclaimelve"],
        "metrics": {"legPct": 0.40, "maxRetracePct": 30.0, "confirmImbalance": 0.5,
                   "bookImbalance": 0.1, "spreadPct": 0.01, "trend": "bearish",
                   "setupAgeSec": 22.0},
        "trade": {"executed": False},
    }
    szoveg = format_signal(sig)
    assert "FORDULO FELFELE" in szoveg
    assert "LONG" in szoveg
    assert "impulzus -0.40%" in szoveg
    assert "setup kora" in szoveg


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} teszt rendben")
