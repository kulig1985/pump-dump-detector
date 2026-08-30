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
from app.plan import build as build_plan
from app import events
import app.config as C_CFG

CFG = {
    "slopeWindowSec": 2.0,
    "minTradesInWindow": 10,
    "minTotalMovePct": 0.15,
    "minSlopePctPerSec": 0.15,
    "minConsistency": 0.70,
    "maxThresholdFactor": 10,
    "symbolCooldownSec": 60,
    "volatilityMultiplier": 0.0,       # a legtobb teszt fix kuszobbel szamol
    "stopBufferPct": 0.05,
    "minRewardRisk": 1.5,
    "maxTickNoisePct": 0.08,
    "shadowMinSamples": 50,
    "shadowMinHitRate": 0.55,
    "minMoveToSpreadRatio": 3.0,
    "minVolumeFactor": 1.0,
    "momentumStopRetracementPct": 50,
    "momentumTargetFactor": 1.0,
    "minSignalScore": 60,
    "wallSensitivity": 3.0,
    "wallMaxDistancePct": 1.5,
}
cfg_obj = types.SimpleNamespace(detector=CFG)


def feed(det, symbol, start_ts, prices, step=0.05):
    """Tick sorozat betoltese, visszaadja az osszes triggert."""
    out = []
    for i, p in enumerate(prices):
        t = det.on_price(symbol, p, start_ts + i * step)
        if t:
            out.append(t)
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
        det.on_price("ETHUSDT", 2455.0 + i * 0.01, 1000.0)      # mind ugyanakkor
    assert det.latest["ETHUSDT"][1] is None, "nem merheto, de nem is szabad hibaznia"


def test_no_trigger_on_slow_drift():
    """Lassu kuszas: nagy teljes elmozdulas, de kicsi tempó."""
    det = PumpDumpDetector(cfg_obj)
    ticks = [(1000.0 + i * 1.5, 100.0 * (1 + 0.005 * i / 60)) for i in range(60)]
    assert [t for t in (det.on_price("AAAUSDT", p, ts) for ts, p in ticks) if t] == []


def test_trigger_on_steady_fast_move():
    """Valodi pump: 2 masodperc alatt egyenletes +0.4%."""
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0] * 40 + [100.0 * (1 + 0.004 * (i + 1) / 40) for i in range(40)]
    triggers = feed(det, "BBBUSDT", 1000.0, prices)
    assert len(triggers) == 1, triggers
    d = triggers[0]["detail"]
    assert triggers[0]["direction"] == "LONG"
    assert d["spanSec"] >= CFG["slopeWindowSec"] / 2
    assert abs(d["totalPct"]) >= CFG["minTotalMovePct"]
    assert d["consistency"] >= CFG["minConsistency"]


def test_dump_direction():
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0] * 40 + [100.0 * (1 - 0.004 * (i + 1) / 40) for i in range(40)]
    triggers = feed(det, "CCCUSDT", 1000.0, prices)
    assert len(triggers) == 1
    assert triggers[0]["direction"] == "SHORT"


def test_single_spike_then_back_is_not_a_signal():
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0] * 40 + [100.45] + [100.0] * 40
    assert feed(det, "DDDUSDT", 1000.0, prices) == []


def test_sawtooth_is_not_a_signal():
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0 * (1 + 0.0025 * math.sin(i * 0.8)) for i in range(120)]
    assert feed(det, "EEEUSDT", 1000.0, prices) == []


def test_tiny_total_move_is_not_a_signal():
    """Meredek tempó, de a nettó elmozdulas jelentektelen."""
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0 * (1 + 0.0005 * (i + 1) / 40) for i in range(40)]   # +0.05%
    assert feed(det, "TINYUSDT", 1000.0, prices) == []


def test_cooldown_suppresses_repeat():
    det = PumpDumpDetector(cfg_obj)
    prices = ([100.0] * 40
              + [100.0 * (1 + 0.004 * (i + 1) / 40) for i in range(40)]
              + [100.4 * (1 + 0.004 * (i + 1) / 40) for i in range(40)])
    assert len(feed(det, "FFF2USDT", 1000.0, prices)) == 1


def test_no_trigger_without_enough_trades():
    det = PumpDumpDetector(cfg_obj)
    prices = [100.0 * (1 + 0.001 * i) for i in range(8)]
    assert feed(det, "GGG2USDT", 1000.0, prices) == []


def test_volatility_raises_threshold_for_noisy_symbol():
    """A nyugtalan parnak meredekebben kell mozdulnia; a config ertek a padlo."""
    cfg = types.SimpleNamespace(detector={**CFG, "volatilityMultiplier": 4.0})
    det = PumpDumpDetector(cfg)

    # a "zajos" par itt nem szapora oszcillaciot jelent (annak a MEREDEKSEGE nulla,
    # azt a konzisztencia-szuro fogja), hanem hogy folyamatosan nagy tempóval
    # lendul ide-oda -- ilyen parnak tenyleg tobbet kell mozdulnia a jelzeshez
    for i in range(600):
        det.on_price("CALMUSDT", 100.0 + i * 1e-6, 1000.0 + i * 0.05)
        det.on_price("NOISYUSDT", 100.0 * (1 + 0.004 * math.sin(i * 0.04)),
                     1000.0 + i * 0.05)

    calm = det.threshold("CALMUSDT")
    noisy = det.threshold("NOISYUSDT")
    assert calm == CFG["minSlopePctPerSec"], calm
    assert noisy > calm, (calm, noisy)
    # felfele is van korlat, hogy egy elszallt mertek ne nemitson el egy part
    assert noisy <= CFG["minSlopePctPerSec"] * CFG["maxThresholdFactor"]


# ---------------------------------------------------------------- ReversalDetector

REV = dict(C_CFG.REVERSAL_DEFAULTS)
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


# ---- a valos CYSUSDT eset, ami a rendszer atdolgozasat kivaltotta -----------
#
# A jelzes, ami elesben kiment: melypont 12.9 masodperce, a mozgas 48%-a mar
# visszajott, es egy 0.03%-os "attoresre" hivatkozott. A felhasznalo a Binance-on
# rendszeresen az ellenkezojet latta. Ezek a tesztek ezt orzik.

CSUCS, MELY = 0.79246, 0.78240          # a valodi jelzesbol visszaszamolva


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
    for _ in range(8):
        add(belepo, utana, 4000, 0.15)
    return out


def rev_run(det, tape):
    return [s for s in (det.on_trade(t) for t in tape) if s]


def test_late_entry_is_rejected():
    """A valodi jelzes: a mozgas 48%-a mar visszajott. Nem szabad jelezni."""
    det = ReversalDetector(rev_cfg)
    assert rev_run(det, rev_tape(0.78600, 0.78520, 0.78720)) == []


def test_early_entry_is_accepted_with_a_usable_plan():
    """Ugyanaz a mozgas, a visszapattanas 21%-anal elkapva -> van meg hely."""
    det = ReversalDetector(rev_cfg)
    sigs = rev_run(det, rev_tape(0.78380, 0.78330, 0.78450))
    assert len(sigs) == 1, sigs
    d = sigs[0]["detail"]
    assert d["retracementPct"] <= REV["maxRetracementPct"]
    assert d["extremeAgeSec"] <= REV["maxExtremeAgeSec"]
    assert sigs[0]["stopAnchor"] == MELY
    assert MELY < sigs[0]["targetAnchor"] < CSUCS

    terv = build_plan(sigs[0], CFG)
    assert terv["stop"] < MELY < terv["entry"] < terv["target"]
    assert terv["rewardRisk"] > 1.0


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
    for t in rev_tape(0.78380, 0.78330, 0.78450):
        det.on_trade(t)
        st = det.setups.get("CYSUSDT")
        if st and st.micro:
            assert st.micro == 0.78380
            return
    raise AssertionError("a micro szint sosem rogzult")


def test_relative_sizing_works_on_a_small_move():
    """Minden meret a mozgas aranyaban van, igy egy 0.6%-os mozgason is mukodik."""
    det = ReversalDetector(rev_cfg)
    sigs = rev_run(det, rev_tape(99.48, 99.455, 99.52, csucs=100.0, mely=99.40))
    assert len(sigs) == 1, sigs
    assert sigs[0]["detail"]["retracementPct"] <= REV["maxRetracementPct"]


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
        assert sig["detail"]["extreme"] <= 0.78101, sig["detail"]


def test_short_reversal_mirror():
    """Tukorkep: emelkedes utan tetozott, majd lefordult."""
    det = ReversalDetector(rev_cfg)
    # csucs 100.60, melypont 100.00 -> a mozgas felfele ment, a fordulo lefele
    tape = rev_tape(100.52, 100.545, 100.48, csucs=100.00, mely=100.60)
    sigs = rev_run(det, tape)
    assert len(sigs) == 1, sigs
    assert sigs[0]["direction"] == "SHORT"
    terv = build_plan(sigs[0], CFG)
    assert terv["target"] < terv["entry"] < terv["stop"]


def test_reversal_cooldown():
    det = ReversalDetector(rev_cfg)
    assert len(rev_run(det, rev_tape(0.78380, 0.78330, 0.78450))) == 1
    assert rev_run(det, rev_tape(0.78380, 0.78330, 0.78450)) == []


# ---------------------------------------------------------------- mozgasminoseg

def test_every_config_key_read_by_the_code_exists():
    """Ha atnevezunk egy beallitast, ne maradjon regi hivatkozas a kodban.

    Egy ilyen elmaradt hivatkozas (c["bouncePct"]) a statusz blokkot dontotte el
    futas kozben, es csak egy egysoros "statusz hiba" latszott belole.
    """
    import re
    ismert = set().union(*(set(d) for d in (
        C_CFG.DETECTOR_DEFAULTS, C_CFG.REVERSAL_DEFAULTS,
        C_CFG.TRADING_DEFAULTS, C_CFG.TELEGRAM_DEFAULTS)))
    # barmilyen config-szeru hozzaferes: c[...], cfg[...], own[...], shared[...],
    # es a dokumentumok nev szerint is (cfg.detector[...], self.cfg.reversal[...])
    minta = re.compile(
        r'(?:\b(?:c|cfg|own|shared|conf)\b|\.(?:detector|reversal|trading|telegram))'
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
        detector=dict(C_CFG.DETECTOR_DEFAULTS), reversal=dict(C_CFG.REVERSAL_DEFAULTS),
        trading=dict(C_CFG.TRADING_DEFAULTS), telegram=dict(C_CFG.TELEGRAM_DEFAULTS))
    sorok = startup_summary(cfg)
    assert len(sorok) == 5 and all(isinstance(x, str) and x for x in sorok)


def test_detector_status_lines_render():
    """A statusz blokkoknak hiba nelkul ki kell rajzolodniuk (ures allapotban is)."""
    for det in (PumpDumpDetector(cfg_obj), ReversalDetector(rev_cfg)):
        assert isinstance(det.status_lines(), list)
    det = ReversalDetector(rev_cfg)
    rev_run(det, rev_tape(0.78380, 0.78330, 0.78400))     # alakzat, attores nelkul
    sorok = det.status_lines()
    assert any("REVERSAL FIGYELO" in x for x in sorok), sorok
    # a statusz a TOZSDEI idot hasznalja, nem a helyi orat -- kulonben a szintetikus
    # (vagy elcsuszott oraju) idobelyegeknel minden alakzat "elavultnak" latszana
    assert any("CYSUSDT" in x for x in sorok), sorok
    assert det.last_ts > 0


def test_liquid_pair_is_not_excluded_by_bid_ask_bounce():
    """Regresszio: a BTCUSDT-t kizarta a szuro, mert a likvid parokon az ar a
    spreaden pattog. Az nem "szaggatott mozgas", hanem normalis mikrostruktura --
    es a jelzes epp ilyen parokon a legertekesebb."""
    from app.quality import SymbolQuality
    q = SymbolQuality(cfg_obj)
    for i in range(200):                       # 0.1 tick pattogas 61000-en
        q.on_trade(Trade("BTCUSDT", 61000.0 + (i % 2) * 0.1, 1.0, 1000.0 + i * 0.01, True))
    zaj = q.tick_noise("BTCUSDT")
    assert zaj < 0.001, zaj                    # ~0.00016%
    assert q.tradeable("BTCUSDT")[0] is True


def test_jumpy_symbol_is_excluded():
    """Egy kotes tized szazalekokat mozdit -> ott nem lehet 0.2%-ot megfogni."""
    from app.quality import SymbolQuality
    q = SymbolQuality(cfg_obj)
    for i in range(200):                       # +-0.25% ugrasok kotesenkent
        q.on_trade(Trade("JUMPUSDT", 0.01 * (1 + 0.005 * (i % 2)), 1.0,
                         1000.0 + i * 0.1, True))
    zaj = q.tick_noise("JUMPUSDT")
    assert zaj > CFG["maxTickNoisePct"], zaj
    assert q.tradeable("JUMPUSDT")[0] is False
    assert "JUMPUSDT" in q.blocked_summary()[1]


def test_quality_needs_samples_before_judging():
    from app.quality import SymbolQuality
    q = SymbolQuality(cfg_obj)
    for i in range(5):
        q.on_trade(Trade("NEWUSDT", 1.0 * (1 + 0.01 * i), 1.0, 1000.0 + i, True))
    assert q.tradeable("NEWUSDT")[0] is True, "keves mintabol nem itelunk"


def test_manager_reports_dropped_signals():
    """A detektor mar kiirta a triggert -- ha a szuro eldobja, azt is lassuk."""
    mgr = DetectorManager(rev_cfg, [ReversalDetector(rev_cfg)])
    for i in range(200):                       # eloszor ugralonak tanitjuk
        mgr.on_trade(Trade("CYSUSDT", 0.78 * (1 + 0.005 * (i % 2)), 1.0,
                           900.0 + i * 0.1, True))
    assert mgr.quality.tradeable("CYSUSDT")[0] is False
    events.drain()
    assert [s for t in rev_tape(0.78380, 0.78330, 0.78450) for s in mgr.on_trade(t)] == []
    assert mgr.skipped > 0
    assert any("ELDOBVA" in txt for _, txt in events.drain()), "az eldobas legyen lathato"


# ---------------------------------------------------------------- kereskedelmi terv

def test_plan_levels_and_reward_risk():
    long_sig = {"price": 100.0, "direction": "LONG",
                "stopAnchor": 99.0, "targetAnchor": 102.0}
    t = build_plan(long_sig, CFG)
    assert t["stop"] < 99.0 < t["entry"] < t["target"]
    assert t["rewardRisk"] == round(2.0 / (100.0 - t["stop"]), 2)
    assert t["weak"] is False

    short_sig = {"price": 100.0, "direction": "SHORT",
                 "stopAnchor": 101.0, "targetAnchor": 98.0}
    t = build_plan(short_sig, CFG)
    assert t["target"] < t["entry"] < 101.0 < t["stop"]

    # rossz irany -> nincs ertelmes terv
    assert build_plan({"price": 100.0, "direction": "LONG",
                       "stopAnchor": 101.0, "targetAnchor": 102.0}, CFG) is None
    assert build_plan({"price": 100.0, "direction": "LONG"}, CFG) is None


def test_weak_reward_risk_is_flagged_not_dropped():
    sig = {"price": 100.0, "direction": "LONG",
           "stopAnchor": 99.0, "targetAnchor": 100.5}
    t = build_plan(sig, CFG)
    assert t is not None, "a gyenge aranyu jelzest jelolni kell, nem eldobni"
    assert t["weak"] is True
    assert t["rewardRisk"] < CFG["minRewardRisk"]


# ---------------------------------------------------------------- arnyek mod

def test_shadow_mode_gate():
    from app.signals import SignalService
    svc = SignalService.__new__(SignalService)
    svc.cfg = types.SimpleNamespace(detector=CFG)
    own = {"telegramMode": "auto"}

    svc.hit_rates = {}
    assert svc._telegram_gate("reversal", 72, own)[0] is False       # nincs meres
    svc.hit_rates = {("reversal", 70): (18, 0.90)}
    assert svc._telegram_gate("reversal", 72, own)[0] is False       # keves minta
    svc.hit_rates = {("reversal", 70): (64, 0.33)}
    assert svc._telegram_gate("reversal", 72, own)[0] is False       # gyenge arany
    svc.hit_rates = {("reversal", 70): (64, 0.61)}
    assert svc._telegram_gate("reversal", 72, own)[0] is True        # bizonyitott

    assert svc._telegram_gate("reversal", 72, {"telegramMode": "always"})[0] is True
    assert svc._telegram_gate("reversal", 72, {"telegramMode": "never"})[0] is False


def test_flow_needs_real_volume():
    """Par szaz USDT-bol is kijon egy 1.9x arany -- az nem trade flow, hanem zaj."""
    from app import binance_rest
    binance_rest.SYMBOL_VOLUME["TUTUSDT"] = 110e6          # 3 mp atlaga ~3820 USDT
    det = ReversalDetector(rev_cfg)

    def ablak(usdt_osszesen):
        db = 12
        qty = usdt_osszesen / 0.03534 / db
        return ([Trade("TUTUSDT", 0.03534, qty, 1000.0 + i * 0.2, True)
                 for i in range(db // 2)] +
                [Trade("TUTUSDT", 0.03534, qty * 2, 1000.0 + 1.2 + i * 0.1, False)
                 for i in range(db // 2)])

    assert det._flow(ablak(1500), 1002.0, REV, "TUTUSDT") is None, "1500 USDT keves"
    eleg = det._flow(ablak(20000), 1002.0, REV, "TUTUSDT")
    assert eleg is not None and eleg["ratio"] >= 1.6
    # ismeretlen forgalmu parnal nincs mihez merni -- ilyenkor atengedjuk
    assert det._flow(ablak(1500), 1002.0, REV, "ISMERETLENUSDT") is not None
    binance_rest.SYMBOL_VOLUME.pop("TUTUSDT")


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
        _sig(1.5, accelerating=True, mode="momentum"), ob, bearish_but_reclaimed,
        CFG, JO_TERV)
    reversal, _, _ = scoring.score_signal(
        _sig(1.5, accelerating=True, mode="reversal"), ob, bearish_but_reclaimed,
        CFG, JO_TERV)

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
    mgr = DetectorManager(rev_cfg, [BoomDetector(), ReversalDetector(rev_cfg)])
    tape = rev_tape(0.78380, 0.78330, 0.78450)
    signals = [s for tr in tape for s in mgr.on_trade(tr)]
    assert len(signals) == 1, "a hibas detektor nem nyelheti el a masik jelzeset"
    assert mgr.ticks == len(tape)
    assert mgr.total_signals == 1


def test_manager_skips_disabled_detector():
    cfg = types.SimpleNamespace(detector=CFG, reversal={**REV, "enabled": False})
    mgr = DetectorManager(cfg, [ReversalDetector(cfg)])
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


def test_signal_detail_is_mongo_safe():
    """A Mongo csak string kulcsot fogad -- a detail nem tartalmazhat int kulcsot."""
    det = PumpDumpDetector(cfg_obj)
    sig = feed(det, "FFFUSDT", 1000.0,
               [100.0] * 40 + [100.0 * (1 + 0.005 * (i + 1) / 40) for i in range(40)])[0]

    def check(o, path="doc"):
        if isinstance(o, dict):
            for k, v in o.items():
                assert isinstance(k, str), f"{path}: nem string kulcs -> {k!r}"
                check(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                check(v, f"{path}[{i}]")

    check(sig["detail"])
    assert "slopePctPerSec" in sig["detail"]


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
    sig = feed(det, "IIIUSDT", 1000.0,
               [100.0] * 40 + [100.0 * (1 + 0.005 * (i + 1) / 40) for i in range(40)])[0]
    assert sig["detector"] == "pump_dump"
    assert sig["configKey"] == "detector"
    assert sig["contextMode"] == "momentum"
    assert sig["detail"]["slopeThreshold"] == CFG["minSlopePctPerSec"]
    assert sig["strength"] > 1.0


JO_TERV = {"rewardRisk": 3.0}


def test_score_accounts_for_reward_risk():
    """Valos eset: 67 pont allt egy 0.8:1 aranyu terv mellett -- ellentmondas.

    Az "erosen mozog" onmagaban nem jelzes; csak akkor az, ha van hova mennie.
    """
    sig = {"direction": "SHORT", "strength": 3.3, "accelerating": False,
           "contextMode": "momentum"}
    ta = {"trend": "bearish", "aboveFast": False}
    ob = {"obstacleAhead": {"distancePct": 0.13}, "liquidityRatio": 1.0,
          "nearestBuyWall": {"distancePct": 0.13}, "nearestSellWall": None}

    rossz, _, p1 = scoring.score_signal(sig, ob, ta, CFG, {"rewardRisk": 0.8})
    jo, _, p2 = scoring.score_signal(sig, ob, ta, CFG, {"rewardRisk": 3.0})
    assert p1["rewardRisk"] == 0.0
    assert rossz < CFG["minSignalScore"], rossz     # nem mehet ki
    assert jo > rossz and jo - rossz == p2["rewardRisk"]


def test_score_parts_sum_to_100_at_best():
    sig = {"direction": "LONG", "strength": 10.0, "accelerating": True,
           "contextMode": "momentum"}
    ta = {"trend": "bullish", "aboveFast": True}
    ob = {"obstacleAhead": None, "liquidityRatio": 1.0,
          "nearestBuyWall": None, "nearestSellWall": None}
    total, _, parts = scoring.score_signal(sig, ob, ta, CFG, {"rewardRisk": 5.0})
    assert total == 100, parts
    assert sum(parts.values()) == 100


def test_momentum_plan_risks_half_the_impulse():
    """A stop az impulzus felenel van, nem az aljan -- kulonben az arany 1:1 lenne."""
    origin, ar = 100.00, 100.22
    stop_a = origin + (ar - origin) * (1 - CFG["momentumStopRetracementPct"] / 100)
    cel_a = ar + (ar - origin) * CFG["momentumTargetFactor"]
    t = build_plan({"price": ar, "direction": "LONG",
                    "stopAnchor": stop_a, "targetAnchor": cel_a}, CFG)
    assert t["rewardRisk"] > 1.3, t
    assert t["stop"] < stop_a < t["entry"] < t["target"]


def test_score_increases_with_stronger_move():
    weak, _, _ = scoring.score_signal(_sig(1.0), None, None, CFG, JO_TERV)
    strong, _, _ = scoring.score_signal(_sig(2.0), None, None, CFG, JO_TERV)
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

    best, _, _ = scoring.score_signal(trig, clear_ob, good_ta, CFG, JO_TERV)
    worst, _, _ = scoring.score_signal(trig, blocked_ob, bad_ta, CFG, JO_TERV)
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
