"""Halozat nelkuli onteszt a detektor magjara: python tests/test_core.py"""
import sys
import math
import time
import types
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.detectors.pumpdump import PumpDumpDetector
from app.detectors.reversal import ReversalDetector
from app.detectors.manager import DetectorManager
from app.detectors.baseline import Baseline
from app.detectors.base import Trade
from app.eligibility import Eligibility
from app.fmt import pad as _pad
from app import orderbook
from app.ta import ema
from app import events
import app.config as C_CFG

# A TESZTEK FIX PROFILT hasznalnak, nem az eles alapertekeket. Kulonben minden
# erzekenyseg-hangolas atirna a fixture-oket (mekkora mozgas, milyen hosszu tape),
# es a tesztek arrol szolnanak, hogy epp mi az alapertek -- nem arrol, hogy jol
# mukodik-e a logika. Az eles alapertekekre kulon teszt van (lasd lentebb).
TESZT_PROFIL = {
    "detector": {"moveWindowSec": 2.0, "minTradesInWindow": 10, "baselineMinutes": 5,
                 "baselineRatio": 6.0, "minMovePct": 0.50, "maxSingleStepPct": 40,
                 "confirmSec": 10.0, "confirmHoldPct": 70, "symbolCooldownSec": 300},
    "reversal": {"baselineRatio": 6.0, "minMovePct": 0.50, "confirmSec": 10.0,
                 "cooldownSec": 600, "maxExtremeAgeSec": 8, "windowSeconds": 20,
                 "bounceOfMovePct": 12, "pullbackOfBouncePct": 30, "breakOfMovePct": 5,
                 "maxRetracementPct": 25, "wickSliceSec": 0.5},
    "market": {"maxSpreadPct": 0.05},
}

CFG = {**C_CFG.DETECTOR_DEFAULTS, **TESZT_PROFIL["detector"]}
REV = {**C_CFG.REVERSAL_DEFAULTS, **TESZT_PROFIL["reversal"]}
MARKET = {**C_CFG.MARKET_DEFAULTS, **TESZT_PROFIL["market"]}
TG = dict(C_CFG.TELEGRAM_DEFAULTS)
cfg_obj = types.SimpleNamespace(detector=CFG, reversal=REV, market=MARKET, telegram=TG)

rev_cfg = types.SimpleNamespace(detector=CFG, market=MARKET, telegram=TG, reversal=REV)


def eligible_stub():
    """Eligibility, ami mindent atenged -- a detektor tesztekhez."""
    e = Eligibility(cfg_obj)
    e.check = lambda symbol: (True, None, {})
    return e


def kesz_baseline(det, symbol, normal_pct=0.02, t0=900.0, db=70):
    """A par normaljat kozvetlenul feltoltjuk, hogy a teszt ne varjon 1-2 percet.

    Baseline nelkul a detektor szandekosan nem ad candidate-et.
    """
    for i in range(db):
        det.baseline.add(symbol, t0 + i, normal_pct)
    assert det.baseline.value(symbol) is not None
    return det


def tart(prices, mp=12.0, step=0.05):
    """A mozgas utan az ar ott marad: a megerositeshez kellenek tovabbi tickek."""
    return list(prices) + [prices[-1]] * int(mp / step)


def feed(det, symbol, start_ts, prices, step=0.05, usd=3000.0):
    """Tick sorozat betoltese, visszaadja az osszes candidate-et."""
    out = []
    for i, p in enumerate(prices):
        c = det.on_trade(Trade(symbol, p, usd / p, start_ts + i * step, True))
        if c:
            out.append(c)
    return out


def test_burst_of_trades_in_milliseconds_is_not_a_signal():
    """Valos eset: 30 trade 0.03 masodperc alatt, osszesen +0.02% mozgas.

    A darabszam-alapu ablak ebbol +0.4%/mp "meredekseget" szamolt, es jelzett.
    Egy apro arvaltozas apro idotartammal osztva nem mozgas.
    """
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0 * (1 + 0.0002 * i / 30) for i in range(30)]
    assert feed(det, "BURSTUSDT", 1000.0, prices, step=0.001) == []


def test_same_timestamp_trades_do_not_break_measurement():
    """A legnagyobb parokon sok aggTrade azonos idobelyeggel erkezik."""
    det = PumpDumpDetector(cfg_obj)
    for i in range(40):
        det.on_trade(Trade("ETHUSDT", 2455.0 + i * 0.01, 1.0, 1000.0, True))
    assert det.latest["ETHUSDT"] is None, "nem merheto, de nem is szabad hibaznia"


def test_no_trigger_on_slow_drift():
    """Lassu kuszas: nagy teljes elmozdulas, de kicsi tempó."""
    det = PumpDumpDetector(cfg_obj)
    ticks = [(1000.0 + i * 1.5, 100.0 * (1 + 0.005 * i / 60)) for i in range(60)]
    assert [t for t in (det.on_trade(Trade("AAAUSDT", p, 30.0, ts, True))
                        for ts, p in ticks) if t] == []


def test_trigger_on_steady_fast_move():
    """Valodi pump: 2 masodperc alatt egyenletes +0.4%."""
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "BBBUSDT")
    prices = tart([100.0] * 40 + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)])
    triggers = feed(det, "BBBUSDT", 1000.0, prices)
    assert len(triggers) == 1, triggers
    d = triggers[0]["metrics"]
    assert triggers[0]["direction"] == "LONG"
    assert d["spanSec"] >= CFG["moveWindowSec"] / 2
    assert abs(d["movePct"]) >= CFG["minMovePct"]


def test_dump_direction():
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "CCCUSDT")
    prices = tart([100.0] * 40 + [100.0 * (1 - 0.010 * (i + 1) / 40) for i in range(40)])
    triggers = feed(det, "CCCUSDT", 1000.0, prices)
    assert len(triggers) == 1
    assert triggers[0]["direction"] == "SHORT"


def test_cooldown_suppresses_repeat():
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "FFF2USDT")
    prices = tart([100.0] * 40
                  + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]
                  + [101.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)])
    assert len(feed(det, "FFF2USDT", 1000.0, prices)) == 1


def test_no_trigger_without_enough_trades():
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0 * (1 + 0.001 * i) for i in range(8)]
    assert feed(det, "GGG2USDT", 1000.0, prices) == []


CSUCS, MELY = 0.79246, 0.78240          # a valodi CYSUSDT jelzesbol visszaszamolva


def rev_tape(micro, visszahuzas, belepo, tetlen_mp=0.0, csucs=CSUCS, mely=MELY):
    """Fordulo szekvencia. A lemozgas lassabb (0.3 mp/trade), hogy a 3 masodperces
    flow ablakba mar csak a visszapattanas essen bele -- ahogy elesben is.

    tetlen_mp: ennyi ideig all az ar a visszahuzas szintjen az attores elott
               (ezzel oregitheto a szelsoertek)
    """
    from app import binance_rest
    binance_rest.SYMBOL_VOLUME["CYSUSDT"] = 52e6
    t, out = [1000.0], []
    # a szelsoertek utani oldal a fordulo iranyaval egyezik: lefele mozgas utan
    # veteli flow (LONG fordulo), felfele mozgas utan eladoi (SHORT fordulo)
    utana = mely < csucs

    def add(ar, buy, usd, dt):
        t[0] += dt
        out.append(Trade("CYSUSDT", ar, usd / ar, t[0], buy))

    for _ in range(8):
        add(csucs, not utana, 3000, 0.3)
    for i in range(12):
        add(csucs + (mely - csucs) * (i + 1) / 12, not utana, 3000, 0.3)
    for i in range(6):
        add(mely + (micro - mely) * (i + 1) / 6, utana, 3000, 0.1)
    for _ in range(4):
        add(visszahuzas, utana, 3000, 0.1)
    if tetlen_mp:                       # varakozas: az alakzat oregszik
        for _ in range(6):
            add(visszahuzas, utana, 3000, tetlen_mp / 6)
    # az attores utan meg legalabb confirmSec masodpercig tart az ar -- a jelzes
    # nem az attores pillanataban megy ki, hanem a megerosites utan
    for _ in range(90):
        add(belepo, utana, 4000, 0.15)
    return out


def rev_run(det, tape):
    """Feltolti a par normaljat (baseline nelkul nincs alakzat), majd lejatssza."""
    kesz_baseline(det, "CYSUSDT", normal_pct=0.02)
    return [s for s in (det.on_trade(t) for t in tape) if s]


def test_late_entry_is_rejected():
    """A valodi jelzes: a mozgas 48%-a mar visszajott. Nem szabad jelezni."""
    det = ReversalDetector(rev_cfg)
    assert rev_run(det, rev_tape(0.78600, 0.78520, 0.78720)) == []


def test_stale_extreme_is_rejected():
    """Helyes alakzat, de a melypont mar 12 masodperces -- a mozgas lefutott."""
    det = ReversalDetector(rev_cfg)
    assert rev_run(det, rev_tape(0.78380, 0.78330, 0.78450, tetlen_mp=12.0)) == []


def test_tiny_break_is_rejected():
    """Az attores a mozgas 5%-a alatt marad -- az nem informacio, csak zaj."""
    det = ReversalDetector(rev_cfg)
    # a mozgas 1.006 arpont, 5%-a 0.000503; itt csak 0.0002-t torunk at
    assert rev_run(det, rev_tape(0.78380, 0.78330, 0.78400)) == []


def test_pullback_below_bounce_still_locks_the_micro_level():
    """Regresszio: a visszapattanast a MAR ELERT csucshoz merjuk, nem a pillanatnyi
    arhoz. Kulonben a visszahuzas kilokne az alakzatot, es a micro szint csak NAGY
    visszapattanasoknal rogzulne -- pontosan ez okozta a kesoi jelzeseket."""
    det = ReversalDetector(rev_cfg)
    kesz_baseline(det, "CYSUSDT", normal_pct=0.02)
    for t in rev_tape(0.78380, 0.78330, 0.78450):
        det.on_trade(t)
        st = det.setups.get("CYSUSDT")
        if st and st.micro:
            assert st.micro == 0.78380
            return
    raise AssertionError("a micro szint sosem rogzult")


def test_breakout_that_falls_back_is_not_a_reversal():
    """Az attores pillanata meg nem fordulo. Ha az ar confirmSec masodpercen belul
    visszaesik a micro szint moge, nem volt fordulo -- csak egy kilenges."""
    det = ReversalDetector(rev_cfg)
    tape = rev_tape(0.78380, 0.78330, 0.78450)
    # az attores utani reszt lecsereljuk: visszaesik a micro szint ala
    attores_elott = [t for t in tape if t.price != 0.78450]
    t0 = attores_elott[-1].ts
    vissza = ([Trade("CYSUSDT", 0.78450, 4000 / 0.78450, t0 + 0.15 * (i + 1), True)
               for i in range(4)]
              + [Trade("CYSUSDT", 0.78350, 4000 / 0.78350, t0 + 0.6 + 0.15 * (i + 1), True)
                 for i in range(90)])
    assert rev_run(det, attores_elott + vissza) == []
    assert "CYSUSDT" not in det.pending, "a fuggo jelzes eldolt, nem ragadt bent"


def test_relative_sizing_works_on_a_small_move():
    """Minden meret a mozgas aranyaban van, igy egy 1%-os mozgason is mukodik."""
    det = ReversalDetector(rev_cfg)
    sigs = rev_run(det, rev_tape(99.13, 99.09, 99.20, csucs=100.0, mely=99.00))
    assert len(sigs) == 1, sigs
    assert sigs[0]["metrics"]["retracementPct"] <= REV["maxRetracementPct"]


def rev_tape_sell_flow(micro, visszahuzas, belepo):
    """Ugyanaz az alakzat, de az attorest ELADOI oldal viszi -> LONG-hoz rossz."""
    tape = rev_tape(micro, visszahuzas, belepo)
    return [t._replace(buy_taker=False) if t.price == belepo else t for t in tape]


def test_no_reversal_without_buy_flow():
    det = ReversalDetector(rev_cfg)
    assert rev_run(det, rev_tape_sell_flow(0.78380, 0.78330, 0.78450)) == []


def test_no_reversal_without_micro_break():
    """Visszapattan, a micro szint rogzul, de nem tori at."""
    det = ReversalDetector(rev_cfg)
    assert rev_run(det, rev_tape(0.78380, 0.78330, 0.78370)) == []


def test_new_lower_low_resets_the_setup():
    """Uj, melyebb minimum -> az alakzat ujraindul a regi micro szint nelkul."""
    det = ReversalDetector(rev_cfg)
    tape = rev_tape(0.78380, 0.78330, 0.78450)
    # az attores ele beszurunk egy uj melypontot
    uj_mely = [t._replace(price=0.78100) for t in tape[22:26]]
    for sig in rev_run(det, tape[:22] + uj_mely + tape[26:]):
        assert sig["metrics"]["extreme"] <= 0.78101, sig["metrics"]


def test_reversal_cooldown():
    det = ReversalDetector(rev_cfg)
    assert len(rev_run(det, rev_tape(0.78380, 0.78330, 0.78450))) == 1
    assert rev_run(det, rev_tape(0.78380, 0.78330, 0.78450)) == []


# ---------------------------------------------------------------- baseline

def _tanit(det, symbol, amplitudo_pct, perc=6, t0=1000.0):
    """A par sajat normaljanak felepitese: folyamatos hullamzas."""
    t = t0
    while t < t0 + perc * 60:
        for i in range(12):
            ar = 100.0 * (1 + amplitudo_pct / 100 * math.sin(t * 3.0))
            det.on_trade(Trade(symbol, ar, 30.0, t + i * 0.08, True))
        t += 1.0
    return t


def test_same_move_signals_on_a_calm_pair_but_not_on_a_wild_one():
    """A refaktoralas lenyege: ugyanaz a +0.8%-os mozgas az egyik paron rendkivuli,
    a masikon a normalis mukodes resze."""
    def probal(amplitudo):
        det = PumpDumpDetector(cfg_obj)
        t = _tanit(det, "XUSDT", amplitudo)
        alap = det.baseline.value("XUSDT")
        jelzesek = []
        arak = tart([100.0 * (1 + 0.008 * (i + 1) / 30) for i in range(30)], step=0.07)
        for i, ar in enumerate(arak):
            c = det.on_trade(Trade("XUSDT", ar, 30.0, t + i * 0.07, True))
            if c:
                jelzesek.append(c)
        return alap, jelzesek

    nyugodt_alap, nyugodt = probal(0.01)
    vad_alap, vad = probal(0.60)
    assert vad_alap > nyugodt_alap * 5, (nyugodt_alap, vad_alap)
    assert nyugodt, "a nyugodt paron a +0.8% rendkivuli -> jelzes"
    assert not vad, "a vad paron a +0.8% a normalis mukodes resze -> nincs jelzes"


def test_absolute_floor_applies_until_baseline_is_ready():
    """Amig nincs eleg minta, az abszolut padlo dont -- nem jelzunk vaktaban."""
    det = PumpDumpDetector(cfg_obj)
    assert det.baseline.value("YUSDT") is None
    assert feed(det, "YUSDT", 1000.0,
                [100.0 * (1 + 0.0005 * i / 40) for i in range(40)]) == []


def test_baseline_is_compared_before_it_is_updated():
    """Eloszor a KORABBI normalhoz hasonlitunk, csak utana frissitunk -- kulonben
    az eppen vizsgalt mozgas resze lenne annak, amihez merjuk."""
    det = PumpDumpDetector(cfg_obj)
    t = _tanit(det, "ZUSDT", 0.02)

    sorrend = []
    ratio_eredeti, add_eredeti = det.baseline.ratio, det.baseline.add
    det.baseline.ratio = lambda *a, **k: (sorrend.append("hasonlit"),
                                          ratio_eredeti(*a, **k))[1]
    det.baseline.add = lambda *a, **k: (sorrend.append("frissit"),
                                        add_eredeti(*a, **k))[1]

    for i in range(10):
        det.on_trade(Trade("ZUSDT", 100.0 * (1 + 0.001 * i), 30.0, t + i * 0.07, True))

    assert sorrend, "egyik hivas sem tortent meg"
    # minden korben eloszor hasonlitunk, aztan frissitunk
    assert sorrend[0] == "hasonlit", sorrend[:4]
    for i in range(0, len(sorrend) - 1, 2):
        assert sorrend[i:i + 2] == ["hasonlit", "frissit"], sorrend[i:i + 4]


def test_baseline_scales_with_the_measured_window():
    """Bolyongasnal az elmozdulas az ido gyokevel no: egy 4x hosszabb ablakban a
    NORMAL mozgas ~2x akkora. Skalazas nelkul egy hosszabb kuszas rendkivulinek tunne."""
    det = PumpDumpDetector(cfg_obj)
    _tanit(det, "WUSDT", 0.02)
    alap = det.baseline.value("WUSDT")
    ablak = CFG["moveWindowSec"]
    assert det.baseline.value_for("WUSDT", ablak) == alap
    negyszer = det.baseline.value_for("WUSDT", ablak * 4)
    assert abs(negyszer / alap - 2.0) < 0.01, negyszer / alap
    assert det.baseline.value_for("ISMERETLEN", ablak) is None


def test_slow_drift_over_a_long_window_is_not_a_reversal_setup():
    """Idoskala-javitas: ugyanaz a 0.70%-os mozgas 3 masodperc alatt rendkivuli,
    20 masodperc alatt viszont a normal bolyongas resze."""
    def probal(hossz_sec):
        base = Baseline(cfg_obj)
        det = ReversalDetector(rev_cfg, base)
        # normal: 2 mp-es ablakban 0.05%
        for i in range(400):
            base.add("QUSDT", 1000.0 + i, 0.05)
        ablak = [Trade("QUSDT", 100.0 * (1 - 0.0070 * (i + 1) / 40), 30.0,
                       2000.0 + i * hossz_sec / 40, False) for i in range(40)]
        return det._find_setup(ablak, {**REV, "minMovePct": 0.0})

    assert probal(3.0) is not None, "3 mp alatt 0.70% rendkivuli"
    assert probal(20.0) is None, "20 mp alatt ugyanez a normal bolyongas"


def test_instant_wick_is_not_the_start_of_a_move():
    """VALODI eset (SKRUSDT, 2026-08-30 13:50:41 UTC): egyetlen pillanatban negy
    print 0.015642-ig, aztan azonnal vissza. A detektor ezt vette a mozgas
    kezdopontjanak, es "0.61% emelkedest" jelentett -- a valosagban az ar
    0.01570 es 0.015738 kozott mozgott, azaz 0.24%-ot.
    """
    base = Baseline(cfg_obj)
    det = ReversalDetector(rev_cfg, base)
    for i in range(400):
        base.add("SKRUSDT", 1000.0 + i, 0.049)

    ablak = []
    t = 2000.0
    for i in range(120):                       # ~0.01570 korul rezeg
        ablak.append(Trade("SKRUSDT", 0.015700 + (i % 5) * 0.000002, 30.0, t, False))
        t += 0.05
    kanoc_ts = t
    for ar in (0.015689, 0.015650, 0.015642, 0.015670):   # egyetlen pillanat
        ablak.append(Trade("SKRUSDT", ar, 30.0, kanoc_ts, False))
    t += 0.05
    for i in range(120):                       # majd fel 0.015738-ig
        ablak.append(Trade("SKRUSDT", 0.015700 + 0.000038 * (i + 1) / 120, 30.0, t, True))
        t += 0.05

    setup = det._find_setup(ablak, REV)
    assert setup is None or setup.move_pct < 0.4, (
        setup and (setup.move_pct, setup.origin))

    # ugyanez a lemozgas SOK kotessel, 2 masodperc alatt -> ez valodi, meg kell latni
    valodi = []
    t = 2000.0
    for i in range(40):
        valodi.append(Trade("SKRUSDT", 0.015840 * (1 - 0.0250 * (i + 1) / 40), 30.0, t, False))
        t += 0.05
    for i in range(10):
        valodi.append(Trade("SKRUSDT", 0.015460, 30.0, t, True))
        t += 0.05
    s2 = det._find_setup(valodi, REV)
    assert s2 is not None and s2.side == "LONG", s2


def test_single_whale_print_cannot_create_flow():
    """Egyetlen nagy kotes ne csinaljon 'fordulast': a domináns oldalnak
    kotesszamban is vezetnie kell."""
    det = ReversalDetector(rev_cfg)
    balna = ([Trade("X", 100.0, 500.0, 1000.0, True)]
             + [Trade("X", 100.0, 10.0, 1000.1 + i * 0.1, False) for i in range(8)])
    assert det._flow(balna, 1001.0, REV, None) is None

    valodi = ([Trade("X", 100.0, 50.0, 1000.0 + i * 0.1, True) for i in range(6)]
              + [Trade("X", 100.0, 20.0, 1000.7 + i * 0.1, False) for i in range(3)])
    f = det._flow(valodi, 1001.0, REV, None)
    assert f and f["buyDominant"] and f["buyTrades"] > f["sellTrades"]


def test_wall_is_measured_against_the_median_not_the_mean():
    """Az atlagba a fal maga is beleszamit, es felhigitja a sajat aranyat."""
    import statistics
    asks = [(100.0 + i * 0.01, 1.0) for i in range(20)]
    asks[8] = (100.08, 10.0)
    n = [p * q for p, q in asks]
    atlaghoz = n[8] / (sum(n) / len(n))
    w = orderbook._find_wall(asks, 100.0, 3.0, 1.5)
    assert abs(w["ratio"] - n[8] / statistics.median(n)) < 0.01
    assert w["ratio"] > atlaghoz + 2, (atlaghoz, w["ratio"])


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


def test_detectors_build_state_even_for_ineligible_pairs():
    """A szuro a JELZESNEL all, nem a detektor elott: igy a baseline minden figyelt
    paron epul, es egy par nem nullarol indul, amint kereskedhetove valik."""
    e = Eligibility(cfg_obj)
    _book(e, "OTHERUSDT", 1.0000, 1.0001)
    _book(e, "CYSUSDT", 0.7800, 0.7830)            # szeles spread -> nem kereskedheto
    det = ReversalDetector(rev_cfg)
    kesz_baseline(det, "CYSUSDT", normal_pct=0.02)
    mgr = DetectorManager(rev_cfg, [det], e)
    for t in rev_tape(0.78380, 0.78330, 0.78450):
        mgr.on_trade(t)
    assert mgr.total_candidates == 0, "jelzes nem mehet ki"
    assert mgr.skipped > 0
    assert det.trades["CYSUSDT"], "de a detektor allapota epult"


def test_readiness_shows_raw_numbers_not_just_a_ratio():
    """Hidegindulaskor a median lehet ~0.001%, amitol barmi '266x'-nek latszana."""
    det = PumpDumpDetector(cfg_obj)
    _tanit(det, "RUSDT", 0.02)
    sor = det.readiness()
    assert "normal kesz:" in sor
    assert "%" in sor and "kell" in sor, sor


def test_touch_level_is_not_a_wall():
    """Elesben a BTC/ETH sajat legjobb ajanlata szamitott 'falnak' (0.00% tavolsag,
    1124x atlag), es emiatt minden jelzesuk elbukott. A touch nem akadaly: ott
    lepsz be."""
    # a touch hatalmas, a tobbi szint lapos
    asks = [(100.01, 1000.0)] + [(100.02 + i * 0.01, 1.0) for i in range(19)]
    assert orderbook._find_wall(asks, 100.0, 3.0, 1.5) is None

    # de egy valodi fal a konyv belsejeben tovabbra is fal
    asks[5] = (asks[5][0], 40.0)
    w = orderbook._find_wall(asks, 100.0, 3.0, 1.5)
    assert w is not None and w["distancePct"] > 0.0


def test_no_candidate_without_a_baseline():
    """Baseline nelkul a rendszer csak egy fix kuszob lenne -- epp az, amitol el
    akartunk jutni. Elesben minden jelzes pontosan a 0.15%-os padlon szuletett."""
    det = PumpDumpDetector(cfg_obj)
    assert det.baseline.value("COLDUSDT") is None
    # bőven a padlo folotti, tiszta mozgas -- megsem jelez, mert nincs meg normal
    assert feed(det, "COLDUSDT", 1000.0,
                [100.0 * (1 + 0.006 * (i + 1) / 40) for i in range(40)]) == []


def _book(e, symbol, bid, ask, qty=100000.0):
    """Konyv-adat feltoltese: annyi megfigyeles, hogy a median beallja."""
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
    """Van konyv-adat a rendszerben, de EZT a part meg nem lattuk -> varunk."""
    e = Eligibility(cfg_obj)
    _book(e, "OTHERUSDT", 1.0000, 1.0001)
    assert e.check("UNKNOWNUSDT")[1] == "no_book_data"


def test_book_and_trade_streams_use_different_url_segments():
    """Regresszio: a !bookTicker a /market/stream vegponton NEM erkezik meg (a
    feliratkozast nyugtazza, de nem kuld adatot) -- ezert kell kulon kapcsolat a
    "public" szegmensre. Elesben ez 39 parbol 39-et zart ki no_book_data-val."""
    from app import market_data as MD
    assert MD.WS_BASES[0].endswith("/market/stream")
    assert MD.BOOK_BASES[0].endswith("/public/stream")
    assert MD.WS_BASES[0] != MD.BOOK_BASES[0]
    # mindketto vegigprobalja a regi utvonalakat is, ha az elso nem kuld adatot
    assert MD.BOOK_BASES[1:] == MD.WS_BASES[1:]


def test_total_book_outage_fails_open_and_says_so():
    """Ha SEMMILYEN konyv-adat nem erkezik, az rendszerszintu baj (rossz WS utvonal).

    Ilyenkor nem nemitjuk el az egesz rendszert -- atengedunk, es a STATUS sor
    hangosan szol rola. Kulonben orakig nem lenne jelzes, ok nelkul.
    """
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
    """A gepi kulcs megy a Mongo-ba (aggregalhatosag), a szoveg a logba."""
    from app.eligibility import OKOK, szoveg
    e = Eligibility(cfg_obj)
    _book(e, "WIDE2USDT", 1.000, 1.002)
    kulcs = e.check("WIDE2USDT")[1]
    assert kulcs == "spread_too_wide", "a Mongo-ba gepi kulcs kerul"
    assert szoveg(kulcs) == "tul szeles a spread", "a logba magyar szoveg"
    assert all(szoveg(k) != k for k in OKOK), "minden oknak van magyar szovege"


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


def test_ineligible_pair_builds_state_but_emits_nothing():
    """A szuro a JELZESNEL all, nem a detektor elott: az allapot epul, de jelzes
    nem megy ki -- igy egy par nem nullarol indul, amint kereskedhetove valik."""
    e = Eligibility(cfg_obj)
    _book(e, "OTHERUSDT", 1.0000, 1.0001)          # van konyv-adat a rendszerben
    _book(e, "CYSUSDT", 0.7800, 0.7830)            # de EZ a par szeles spreadu
    det = ReversalDetector(rev_cfg)
    kesz_baseline(det, "CYSUSDT", normal_pct=0.02)
    mgr = DetectorManager(rev_cfg, [det], e)
    assert [s for t in rev_tape(0.78380, 0.78330, 0.78450) for s in mgr.on_trade(t)] == []
    assert mgr.total_candidates == 0, "jelzes nem mehet ki"
    assert mgr.skipped > 0, "de a detektor eljutott a candidate-ig"
    assert det.trades["CYSUSDT"], "es az allapota epult"


# ---------------------------------------------------------------- egyeb

def test_status_line_only_uses_existing_attributes():
    """Regresszio: a STATUS sor egy mar nem letezo mezore hivatkozott
    (svc.rejected_today), es a status task elesben elszallt AttributeError-ral.
    A statusz csak percenkent fut, ezert a teszt sose latta."""
    import re
    from app.signals import SignalService
    forras = (pathlib.Path(__file__).parent.parent / "app" / "market_data.py").read_text()
    for attr in set(re.findall(r"\bsvc\.([a-zA-Z_]+)", forras)):
        assert hasattr(SignalService, attr) or attr in SignalService.__init__.__code__.co_names, \
            f"SignalService.{attr} nem letezik"
    for attr in set(re.findall(r"self\.detectors\.([a-zA-Z_]+)", forras)):
        assert hasattr(DetectorManager, attr) or attr in DetectorManager.__init__.__code__.co_names, \
            f"DetectorManager.{attr} nem letezik"


def test_cold_start_creates_every_setting():
    """HIDEGINDITAS: a config collection URES. A rendszernek minden beallitassal
    egyutt fel kell allnia -- semmi nem varhat kezi mongo parancsra.

    A user minden ujrainditasnal torli a configot, tehat ez a normal eset,
    nem a kivetel.
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

    # es a startup osszefoglalo se hasaljon el a friss configon
    from app.main import startup_summary
    assert startup_summary(store)


def test_config_split_moves_your_existing_values():
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

    # a regi vilag: minden a 'detector' dokumentumban, sajat hangolt ertekekkel
    coll = FakeCollection([{"_id": "detector", "minQuoteVolume24h": 250_000_000,
                            "symbolWhitelist": ["BTCUSDT"], "telegramEnabled": False,
                            "baselineRatio": 9.0}])
    store = C_CFG.ConfigStore(types.SimpleNamespace(config=coll))
    asyncio.run(store.load())

    assert store.market["minQuoteVolume24h"] == 250_000_000, store.market
    assert store.market["symbolWhitelist"] == ["BTCUSDT"]
    assert store.telegram["enabled"] is False, "a telegramEnabled=false nem veszhet el"
    assert store.detector["baselineRatio"] == 9.0, "a sajat detector ertek marad"
    assert "minQuoteVolume24h" not in coll.docs["detector"], "a regi kulcs kikerult"


def test_saved_history_contains_the_move_not_the_confirmation():
    """A jelzes csak confirmSec mulva megy ki -- addigra a detektor ablaka mar
    tovagordul a mozgason. VALODI eset (ZECUSDT): a market_snapshots egy lapos
    4 masodpercet orzott 860.1-860.6 kozott, a +0.80%-os mozgas helyett.
    """
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "ZECUSDT")
    fel = [100.0] * 40 + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]
    sig = feed(det, "ZECUSDT", 1000.0, tart(fel))[0]
    h = sig["history"]
    arak = [p for _, p in h]
    assert min(arak) <= 100.001, "a mozgas ELOTTI szintnek benne kell lennie"
    assert max(arak) >= 100.9, "es a mozgas tetejenek is"
    assert h[-1][0] - h[0][0] >= CFG["confirmSec"], "a megerositesig tarto ut is"


def test_wall_distance_is_not_rounded_into_meaninglessness():
    """A "fal a mozgas iranyaban 0.00%-ra" ugy nez ki, mintha hianyozna az adat."""
    from app.telegram import _tav
    assert _tav(0.004) == "0.004%"
    assert _tav(0.05) == "0.05%"
    assert _tav(1.5) == "1.50%"


def test_telegram_heartbeat_renders():
    """Idoszakos eletjel: ures allapotban (meg semmi nem tortent) se hibazzon,
    es teljes allapotban tartalmazza a lenyeget."""
    from app.telegram import format_status

    ures = {"ido": "14:20:03", "uptime": "0h 1p", "symbols": 0, "wsConnected": 0,
            "wsTotal": 1, "ticksPerMin": 0, "signals": 0, "kizarva": "",
            "movers": [], "kozel": "", "talalat": [], "utolso": []}
    szoveg = format_status(ures)
    assert "ELETJEL" in szoveg and "Meg nincs lemert jelzes" in szoveg

    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "SOLUSDT", normal_pct=0.041)
    feed(det, "SOLUSDT", 1000.0, [100.0 * (1 + 0.0002 * i) for i in range(40)])
    movers = det.top_movers()
    assert movers and "kell" in movers[0][1], movers

    teli = {**ures, "symbols": 58, "wsConnected": 1, "ticksPerMin": 1932,
            "signals": 7, "kizarva": "kizarva 2: tul szeles a spread: 2",
            "movers": movers, "kozel": det.readiness(),
            "talalat": ["tipus   ido   db    atlag  talalat",
                        "pump    +1p    7   -0.31%      57%"],
            "utolso": ["ido   par          tipus irany      +1p",
                       "04:02 SKRUSDT      pump  LONG    -0.31%"]}
    szoveg = format_status(teli)
    for kell in ("58", "1,932", "SOLUSDT", "LEGKOZELEBB", "57%",
                 "OSSZESITES", "UTOLSO JELZESEK", "-0.31%", "atlag"):
        assert kell in szoveg, (kell, szoveg)
    assert "Meg nincs lemert jelzes" not in szoveg


def test_recent_table_shows_three_of_each_type():
    """Tipusonkent az utolso n jelzes -- kulonben egy sokat jelzo detektor
    kiszoritana a masikat a listarol."""
    import types as _t
    from app.outcome import OutcomeTracker
    cfg = _t.SimpleNamespace(market={**MARKET, "outcomeMinutes": [1]})
    o = OutcomeTracker(cfg, None)
    # 6 pump es 2 reversal, novekvo idoben
    minta = [("P1", "pump_dump"), ("P2", "pump_dump"), ("R1", "reversal"),
             ("P3", "pump_dump"), ("P4", "pump_dump"), ("P5", "pump_dump"),
             ("R2", "reversal"), ("P6", "pump_dump")]
    for i, (sym, det) in enumerate(minta):
        o.jelzesek[sym] = {"ts": 1000.0 + i, "symbol": sym, "detector": det,
                           "direction": "LONG", "price": 100.0, "m": {1: 0.5}}
    sorok = o.recent_lines(3)
    torzs = sorok[1:]
    assert len(torzs) == 5, "3 pump + a ket letezo reversal"
    assert sum(1 for x in torzs if " pump " in x) == 3, torzs
    assert sum(1 for x in torzs if " rev " in x) == 2, torzs
    assert "P6" in torzs[0] and "P4" in "".join(torzs), "a legfrissebb pumpok"
    assert not any("P1" in x or "P2" in x for x in torzs), "a regiek kimaradnak"
    idok = [x[:5] for x in torzs]
    assert idok == sorted(idok, reverse=True), "idorendben, legfrissebb elol"


def test_outcome_records_what_happened_after_the_signal():
    """Az eredmenymeres nem kapuz semmit: a jelzes UTAN jegyzi fel az arat.

    Elojelhelyesen: pozitiv = a jelzes iranyaba ment az ar, SHORT-nal is.
    """
    import asyncio
    from app.outcome import OutcomeTracker

    class FakeSignals:
        def __init__(self): self.updates = []
        async def update_one(self, q, u): self.updates.append((q, u))

    coll = FakeSignals()
    cfg = types.SimpleNamespace(market={**MARKET, "outcomeMinutes": [1]})
    o = OutcomeTracker(cfg, types.SimpleNamespace(signals=coll))

    o.track("id-long", "AUSDT", "pump_dump", "LONG", 100.0)
    o.track("id-short", "BUSDT", "reversal", "SHORT", 100.0)
    for x in o.varolista:
        x["esedekes"] = 0                      # jarjon le azonnal
    o.on_trade(Trade("AUSDT", 101.0, 1.0, 0.0, True))   # LONG: +1% -> jo irany
    o.on_trade(Trade("BUSDT", 101.0, 1.0, 0.0, True))   # SHORT: +1% -> ROSSZ irany
    asyncio.run(o._kiertekel())

    assert len(coll.updates) == 2, coll.updates
    ertekek = {q["_id"]: u["$set"]["outcome.m1"]["pct"] for q, u in coll.updates}
    assert abs(ertekek["id-long"] - 1.0) < 0.01, ertekek
    assert abs(ertekek["id-short"] + 1.0) < 0.01, "SHORT-nal a foleme a jo irany"
    assert o.varolista == [], "a lejart merespont kikerult a sorbol"

    talalat = o.summary_lines()
    assert talalat[0].split() == ["tipus", "ido", "db", "atlag", "talalat"], talalat[0]
    sorok = {x.split()[0] + x.split()[1]: x.split() for x in talalat[1:]}
    assert sorok["pump+1p"][2:] == ["1", "+1.00%", "100%"], sorok["pump+1p"]
    assert sorok["rev+1p"][2:] == ["1", "-1.00%", "0%"], sorok["rev+1p"]

    utolso = o.recent_lines()
    assert len(utolso) == 3, "fejlec + 2 jelzes"
    assert utolso[0].startswith("ido") and "+1p" in utolso[0], utolso[0]
    assert any("AUSDT" in x and "pump" in x and "LONG" in x and "+1.00%" in x
               for x in utolso[1:]), utolso
    assert any("BUSDT" in x and "rev" in x and "SHORT" in x and "-1.00%" in x
               for x in utolso[1:]), utolso

    # amirol nem erkezik kotes, arrol nem talalunk ki adatot
    o2 = OutcomeTracker(cfg, types.SimpleNamespace(signals=FakeSignals()))
    o2.track("id-nema", "CUSDT", "reversal", "LONG", 100.0)
    for x in o2.varolista:
        x["esedekes"] = 0
    asyncio.run(o2._kiertekel())
    assert o2.status_lines() == [], "nema paron nincs mert eredmeny"
    assert o2.recent_lines() == [] and o2.summary_lines() == []


def test_live_defaults_are_at_least_as_strict_as_the_test_profile():
    """A tesztek fix profilon futnak, hogy a hangolas ne torje oket. Cserebe ITT
    orizzuk, hogy az ELES alapertekek ne legyenek lazabbak a profilnal."""
    szigorubb_ha_nagyobb = {
        "detector": ("baselineRatio", "minMovePct", "confirmSec", "confirmHoldPct",
                     "symbolCooldownSec"),
        "reversal": ("baselineRatio", "minMovePct", "confirmSec", "cooldownSec"),
    }
    elesek = {"detector": C_CFG.DETECTOR_DEFAULTS, "reversal": C_CFG.REVERSAL_DEFAULTS}
    for doksi, kulcsok in szigorubb_ha_nagyobb.items():
        for k in kulcsok:
            assert elesek[doksi][k] >= TESZT_PROFIL[doksi][k], (
                f"{doksi}.{k}: az eles alapertek ({elesek[doksi][k]}) lazabb, "
                f"mint a teszt profil ({TESZT_PROFIL[doksi][k]})")
    assert C_CFG.DETECTOR_DEFAULTS["maxSingleStepPct"] <= \
        TESZT_PROFIL["detector"]["maxSingleStepPct"], "itt a KISEBB a szigorubb"


def test_every_config_key_is_documented():
    """A doksi ne csusszon el a kodtol: minden detector/reversal beallitas
    szerepeljen a docs/PARAMETEREK.md-ben."""
    doksi = (pathlib.Path(__file__).parent.parent / "docs" / "PARAMETEREK.md").read_text()
    for defaults in (C_CFG.MARKET_DEFAULTS, C_CFG.DETECTOR_DEFAULTS,
                     C_CFG.REVERSAL_DEFAULTS, C_CFG.TELEGRAM_DEFAULTS):
        for k in defaults:
            if k == "_id":
                continue
            assert f"`{k}`" in doksi, f"{defaults['_id']}.{k} nincs dokumentalva"


def test_every_config_key_read_by_the_code_exists():
    """Ha atnevezunk egy beallitast, ne maradjon regi hivatkozas a kodban.

    Egy ilyen elmaradt hivatkozas (c["bouncePct"]) a statusz blokkot dontotte el
    futas kozben, es csak egy egysoros "statusz hiba" latszott belole.
    """
    import re
    ismert = set().union(*(set(d) for d in (
        C_CFG.MARKET_DEFAULTS, C_CFG.DETECTOR_DEFAULTS, C_CFG.REVERSAL_DEFAULTS,
        C_CFG.TRADING_DEFAULTS, C_CFG.TELEGRAM_DEFAULTS)))
    # barmilyen config-szeru hozzaferes: c[...], cfg[...], own[...], shared[...],
    # es a dokumentumok nev szerint is (cfg.detector[...], self.cfg.reversal[...])
    minta = re.compile(
        r'(?:\b(?:c|cfg|own|shared|conf|tg)\b|\.(?:market|detector|reversal|trading|telegram))'
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
    """Indulaskor kiirt osszefoglalo: itt egy atnevezett kulcs miatt korabban
    elhasalt az egesz alkalmazas, mielott barmit csinalt volna."""
    from app.main import startup_summary
    cfg = types.SimpleNamespace(
        market=dict(C_CFG.MARKET_DEFAULTS),
        detector=dict(C_CFG.DETECTOR_DEFAULTS), reversal=dict(C_CFG.REVERSAL_DEFAULTS),
        trading=dict(C_CFG.TRADING_DEFAULTS), telegram=dict(C_CFG.TELEGRAM_DEFAULTS))
    sorok = startup_summary(cfg)
    assert len(sorok) == 5 and all(isinstance(x, str) and x for x in sorok)


def test_detector_status_lines_render():
    """A statusz blokkoknak hiba nelkul ki kell rajzolodniuk (ures allapotban is)."""
    for det in (PumpDumpDetector(cfg_obj), ReversalDetector(rev_cfg)):
        assert isinstance(det.status_lines(), list)
    det = ReversalDetector(rev_cfg)
    # alakzat, attores nelkul -- a tape vegen levo hosszu varakozas nelkul, kulonben
    # a szelsoertek elavul (maxExtremeAgeSec), es az alakzatot eldobjuk
    rev_run(det, rev_tape(0.78380, 0.78330, 0.78400)[:-85])
    sorok = det.status_lines()
    assert any("FORDULOK" in x for x in sorok), sorok
    # a statusz a TOZSDEI idot hasznalja, nem a helyi orat -- kulonben a szintetikus
    # (vagy elcsuszott oraju) idobelyegeknel minden alakzat "elavultnak" latszana
    assert any("CYSUSDT" in x for x in sorok), sorok
    assert det.last_ts > 0


def test_flow_ratio_is_finite_when_one_side_is_empty():
    """Ha az egyik oldalon nincs volumen, az arany nem lehet vegtelen (Mongo + score)."""
    det = ReversalDetector(rev_cfg)
    trades = [Trade("JJJUSDT", 100.0 + i * 0.01, 30.0, 1000.0 + i * 0.1, True)
              for i in range(10)]                      # kizarolag veteli oldal
    flow = det._flow(trades, 1001.0, REV, "JJJUSDT")
    assert flow["sell"] == 0
    assert flow["ratio"] == 99.0
    assert math.isfinite(flow["ratio"])


class BoomDetector:
    name, config_key = "boom", "detector"

    def on_trade(self, trade):
        raise RuntimeError("szandekos hiba")

    def status_lines(self):
        return []


def test_manager_fans_out_and_survives_a_broken_detector():
    rev = ReversalDetector(rev_cfg)
    kesz_baseline(rev, "CYSUSDT", normal_pct=0.02)
    mgr = DetectorManager(rev_cfg, [BoomDetector(), rev], eligible_stub())
    tape = rev_tape(0.78380, 0.78330, 0.78450)
    signals = [s for tr in tape for s in mgr.on_trade(tr)]
    assert len(signals) == 1, "a hibas detektor nem nyelheti el a masik jelzeset"
    assert mgr.ticks == len(tape)
    assert mgr.total_candidates == 1


def test_manager_skips_disabled_detector():
    cfg = types.SimpleNamespace(detector=CFG, reversal={**REV, "enabled": False})
    mgr = DetectorManager(cfg, [ReversalDetector(cfg)], eligible_stub())
    assert [s for tr in rev_tape(0.78380, 0.78330, 0.78450) for s in mgr.on_trade(tr)] == []


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


def test_slowly_decaying_wick_is_dropped_mid_window():
    """VALODI eset (ZECUSDT): a kanoc 15 masodperccel kesobb MEG fent volt, es csak
    utana csorgott vissza. Egy vegponti pillantas ezt atengedi -- ezert nezzuk a
    megerositest FOLYAMATOSAN, es dobjuk el, amint visszaesik.
    """
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "WICKUSDT")
    fel = [100.0] * 40 + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]
    # 5 masodpercig tartja (a vegponti ellenorzes ezt latna), majd lassan visszacsorog
    tartja = [101.0] * 100
    csorog = [101.0 - 1.0 * (i + 1) / 100 for i in range(100)]
    assert feed(det, "WICKUSDT", 1000.0, fel + tartja + csorog) == []
    assert "WICKUSDT" not in det.pending

    # a naplo mutassa, hogy a visszaeses a hatarido ELOTT dolt el
    det2 = PumpDumpDetector(cfg_obj)
    kesz_baseline(det2, "WICK2USDT")
    for i, ar in enumerate(fel + tartja + csorog):
        det2.on_trade(Trade("WICK2USDT", ar, 30.0, 1000.0 + i * 0.05, True))
        if "WICK2USDT" not in det2.pending and i > len(fel):
            break
    eltelt = i * 0.05 - len(fel) * 0.05
    assert eltelt < CFG["confirmSec"], f"a hatarido elott eldolt ({eltelt:.1f}s)"


def test_momentary_spike_that_falls_back_is_not_a_signal():
    """Pillanatnyi korrekcio: az ar felugrik, majd visszaesik oda, ahonnan indult.

    Ez a leggyakoribb hamis jelzes. A jelzes nem a mozgas pillanataban megy ki,
    hanem confirmSec masodperccel kesobb -- addigra ez visszaesett.
    """
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "SPIKEUSDT")
    fel = [100.0] * 40 + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]
    vissza = [101.0 * (1 - 0.010 * (i + 1) / 40) for i in range(40)] + [100.0] * 240
    assert feed(det, "SPIKEUSDT", 1000.0, fel + vissza) == []
    assert "SPIKEUSDT" not in det.pending, "a fuggo jelzes eldolt, nem ragadt bent"


def test_real_move_that_holds_is_a_signal():
    """Ugyanaz a mozgas, de az ar OTT MARAD: ez valodi elindulas."""
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "REALUSDT")
    fel = [100.0] * 40 + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]
    jelzesek = feed(det, "REALUSDT", 1000.0, tart(fel))
    assert len(jelzesek) == 1, jelzesek
    m = jelzesek[0]["metrics"]
    assert m["heldOfMovePct"] >= CFG["confirmHoldPct"], m
    assert any("VEGIG megvolt" in r for r in jelzesek[0]["reasons"]), jelzesek[0]["reasons"]


def test_half_retracement_is_not_enough():
    """A latott mozgas felet visszaadja -> a confirmHoldPct (60%) alatt, nincs jelzes."""
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "HALFUSDT")
    fel = [100.0] * 40 + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]
    assert feed(det, "HALFUSDT", 1000.0, fel) == [], "meg csak megerositesre var"
    p = det.pending["HALFUSDT"]
    fele = p["startPrice"] + (p["triggerPrice"] - p["startPrice"]) * 0.5
    kesobb = [Trade("HALFUSDT", fele, 30.0, 1000.0 + (len(fel) + i) * 0.05, True)
              for i in range(80)]
    assert [c for c in (det.on_trade(t) for t in kesobb) if c] == []


def test_one_big_trade_that_sweeps_the_book_is_not_a_signal():
    """Egy nagy kotes atsopri a konyvet, a tobbi kotes mar az uj aron nyomtat.

    Az ablak igy szep egyenletes mozgasnak latszik, pedig egyetlen lepcso az egesz.
    """
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "SWEEPUSDT")
    # 40 tick 100.0-n, egyetlen ugras 100.4-re, majd ott marad
    arak = [100.0] * 40 + [101.0] * 40
    assert feed(det, "SWEEPUSDT", 1000.0, tart(arak)) == []

    # ugyanaz az elmozdulas sok kis lepesben mar atmegy
    det2 = PumpDumpDetector(cfg_obj)
    kesz_baseline(det2, "STEPSUSDT")
    lepcsos = [100.0] * 40 + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]
    assert feed(det2, "STEPSUSDT", 1000.0, tart(lepcsos))


def test_signal_detail_is_mongo_safe():
    """A Mongo csak string kulcsot fogad -- a detail nem tartalmazhat int kulcsot."""
    det = PumpDumpDetector(cfg_obj)
    kesz_baseline(det, "FFFUSDT")
    sig = feed(det, "FFFUSDT", 1000.0,
               tart([100.0] * 40
                    + [100.0 * (1 + 0.010 * (i + 1) / 40) for i in range(40)]))[0]

    def check(o, path="doc"):
        if isinstance(o, dict):
            for k, v in o.items():
                assert isinstance(k, str), f"{path}: nem string kulcs -> {k!r}"
                check(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                check(v, f"{path}[{i}]")

    check(sig["metrics"])
    assert set(sig["metrics"]) == {"movePct", "spanSec", "trades", "baseline",
                                   "baselineRatio", "singleStepPct", "startPrice",
                                   "heldPct", "heldOfMovePct", "confirmSec"}, sig["metrics"]


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
