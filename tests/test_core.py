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


class FrissKonyv:
    """BookCache helyettesito: friss vagy elavult konyv-adat."""

    def __init__(self, friss=True):
        self.friss = friss

    def fresh(self, symbol):
        return self.friss

    def snapshot(self, symbol):
        return None


def uj_detektor(cfg=None, book="friss"):
    if book == "friss":
        book = FrissKonyv()
    return ScalpDetector(cfg or cfg_obj, book=book)


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


def tape(up=True, retrace_frac=0.30, p0=100.0, leg=0.6, breakout_frac=0.10,
         breakout_lepes=30):
    """IMPULZUS -> visszahuzas -> a kitoresi szint FOKOZATOS keresztezese.

    A kitores lepesenkent kozelit, hogy legyen valodi keresztezes (elozo ar a
    szint alatt, aktualis folotte) -- a detektor csak ezt fogadja el.
    """
    sign = 1 if up else -1
    buy = up
    out = [(p0 + sign * leg * (i + 1) / 40, buy, 20_000, 0.1) for i in range(40)]
    pivot = p0 + sign * leg
    also = pivot - sign * leg * retrace_frac
    out += [(pivot - sign * leg * retrace_frac * (i + 1) / 20, not buy, 3_000, 0.3)
            for i in range(20)]
    cel = pivot + sign * leg * breakout_frac
    out += [(also + (cel - also) * (i + 1) / breakout_lepes, buy, 8_000, 0.2)
            for i in range(breakout_lepes)]
    return out


# ---------------------------------------------------------------- impulzus

def test_impulse_alone_is_not_a_signal():
    """Az impulzus csak egy setup kezdete, nem jelzes."""
    det = uj_detektor()
    kesz_baseline(det, "IUSDT")
    imp = tape()[:40]                      # csak az impulzus resze
    assert futtat(det, "IUSDT", imp) == []
    assert "IUSDT" in det.setups, "a setup elindult, csak nem jelzett"
    assert det.setups["IUSDT"].state in ("IMPULSE", "WAIT_PULLBACK")


def test_no_impulse_without_ready_baseline():
    """Amig nincs eleg minta MINDKET normalhoz, nem indul setup."""
    det = uj_detektor()
    assert det.baseline.value("CUSDT") is None
    imp = tape()[:40]
    assert futtat(det, "CUSDT", imp) == []
    assert "CUSDT" not in det.setups


def test_notional_gate_blocks_thin_impulse():
    """Ugyanaz az arelmozdulas, de vekony konyvbol -- nem eleg penz all mogotte."""
    det = uj_detektor()
    kesz_baseline(det, "TUSDT", normal_notional=20_000.0)
    vekony = [(p, b, usd / 50.0, dt) for p, b, usd, dt in tape()[:40]]
    assert futtat(det, "TUSDT", vekony) == []
    assert "TUSDT" not in det.setups, "vekony konyvbol nem indulhat impulzus"


def test_imbalance_gate_blocks_two_sided_move():
    """+0.6%-os mozgas, de a taker forgalom fele eladoi -- nem egyiranyu."""
    det = uj_detektor()
    kesz_baseline(det, "MUSDT")
    imp = tape()[:40]
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

    imp = tape()[:40]
    futtat(det_nyugodt, "CALMUSDT", imp)
    futtat(det_vad, "WILDUSDT", imp)
    assert "CALMUSDT" in det_nyugodt.setups, "nyugodt paron a +0.6% rendkivuli"
    assert "WILDUSDT" not in det_vad.setups, "vad paron a +0.6% a normal resze"


# ---------------------------------------------------------------- folytatas

def test_signal_after_impulse_pullback_and_fresh_breakout():
    """A teljes ut: IMPULZUS -> PULLBACK -> FRISS KITORES -> JELZES."""
    det = uj_detektor()
    kesz_baseline(det, "CXUSDT")
    jelek = futtat(det, "CXUSDT", tape(retrace_frac=0.30))
    assert len(jelek) == 1, jelek
    assert jelek[0]["direction"] == "LONG"
    m = jelek[0]["metrics"]
    assert m["breakoutAgeSec"] <= CFG["maxBreakoutAgeSec"]
    assert CFG["minPullbackPct"] <= m["pullbackPct"] <= CFG["maxPullbackPct"]
    assert m["flowPct"] > 50, "LONG-nal a veteli oldal dominal"
    assert "CXUSDT" not in det.setups, "a setup lezarult"


def test_short_is_the_mirror_image():
    det = uj_detektor()
    kesz_baseline(det, "CDUSDT")
    jelek = futtat(det, "CDUSDT", tape(up=False, retrace_frac=0.30))
    assert len(jelek) == 1, jelek
    assert jelek[0]["direction"] == "SHORT"
    assert jelek[0]["metrics"]["flowPct"] < 50, "SHORT-nal az eladoi oldal dominal"


def test_deep_pullback_blocks_continuation():
    """Ha a visszahuzas mar tul mely volt, ez mar nem folytatas."""
    det = uj_detektor()
    kesz_baseline(det, "DPUSDT")
    assert futtat(det, "DPUSDT", tape(retrace_frac=0.80)) == []


def test_continuation_needs_matching_flow():
    """Az ujratores pillanataban a kotesaramlas ne az ellenkezo iranyba mutasson."""
    det = uj_detektor()
    kesz_baseline(det, "FLUSDT")
    t = tape(retrace_frac=0.30)
    # az utolso (ujratores) szakaszon megforditjuk a takert -- eladoi nyomas LONG-nal
    forditott = t[:-30] + [(p, False, usd, dt) for p, b, usd, dt in t[-30:]]
    assert futtat(det, "FLUSDT", forditott) == []


# ---------------------------------------------------------------- friss kitores

def test_signal_needs_an_actual_crossing_not_just_being_above():
    """A jelzes CSAK a keresztezes pillanataban szulethet.

    Nem eleg, hogy az ar valamikor korabban attorte a szintet es meg mindig
    folotte all -- kulonben minden kesobbi tick ujra jelzest adna.
    """
    det = uj_detektor()
    kesz_baseline(det, "XOUSDT")
    t = tape(retrace_frac=0.30)
    impulzus_es_pullback = t[:60]
    # az ar EGY LEPESBEN a szint fole ugrik, majd ott is marad: van keresztezes,
    # de az ar tul messze kerul a szinttol -> nincs belepo
    ugras = [(101.2, True, 8_000, 0.2) for _ in range(20)]
    assert futtat(det, "XOUSDT", impulzus_es_pullback + ugras) == []


def test_stale_breakout_is_dropped():
    """A kitores utan maxBreakoutAgeSec-ig van esely megerositesre, azutan nincs."""
    det = uj_detektor()
    kesz_baseline(det, "SBUSDT")
    t = tape(retrace_frac=0.30)
    # a kitoresi szakaszt ELADOI oldalra forditjuk: a keresztezes megtortenik,
    # de a flow nem erositi meg -- es kozben lejar a maxBreakoutAgeSec
    lassu = [(p, False, usd, 0.2) for p, b, usd, dt in t[60:]]
    varakozas = [(t[-1][0], False, 3_000, 1.0) for _ in range(5)]
    assert futtat(det, "SBUSDT", t[:60] + lassu + varakozas) == []
    assert "SBUSDT" not in det.setups, "az elavult kitores eldobta a setupot"


def test_entry_extension_blocks_a_late_entry():
    """Ha az ar mar tul messze jart a kitoresi szinttol, nem szallunk be."""
    det = uj_detektor()
    kesz_baseline(det, "EXUSDT")
    # nagy kitores: a szinttol jol tul, egyetlen lepesben
    assert futtat(det, "EXUSDT", tape(breakout_frac=1.0, breakout_lepes=2)) == []


# ---------------------------------------------------------------- stabilizalas

def test_measure_returns_the_real_window_high_and_low():
    """A meres az ablak TENYLEGES szelsoertekeit adja vissza, nem a vegpontokat."""
    # felszuras 101.0-ig az ablak KOZEPEN, a vegen 100.9
    w = []
    t = 1000.0
    for ar in ([100.0 + 0.5 * i / 10 for i in range(10)]      # 100.0 -> 100.45
               + [101.0, 100.98]                              # a csucs KOZEPEN
               + [100.9] * 10):                               # a vegen lejjebb
        w.append((t, ar, 5_000.0, 5_000.0, 0.0))
        t += 0.12          # a teljes tape ferjen bele a 3 mp-es ablakba
    m = ScalpDetector._measure(w, t, CFG)
    assert m is not None
    assert m["high"] == 101.0, f"az ablak tenyleges maximuma: {m['high']}"
    assert m["low"] == 100.0, f"az ablak tenyleges minimuma: {m['low']}"
    assert m["high"] > w[-1][1], "a high MAGASABB az utolso kotesnel"


def test_pivot_is_the_window_high_not_the_last_trade():
    """A setup pivotja az ablak high-ja (UP) / low-ja (DOWN).

    Kulonben: az ar mar jart 101.0-n, de az impulzus vegen 100.9-en all -> a
    rendszer 100.9-et venne pivotnak, es a 100.95 mar "kitoresnek" latszana,
    pedig az ar ott mar jart.
    """
    det = uj_detektor()
    m = {"movePct": 0.9, "spanSec": 2.5, "trades": 30, "notional": 500_000.0,
         "delta": 400_000.0, "imbalance": 0.8, "singleStepPct": 10.0,
         "startPrice": 100.0, "high": 101.0, "highTs": 1005.0,
         "low": 100.0, "lowTs": 1000.0,
         "baseline": 0.02, "notionalBaseline": 20_000.0}
    # az UTOLSO kotes ara 100.9 -- a csucs (101.0) mar mogottunk van
    det._detect_impulse(Trade("PVUSDT", 100.9, 10.0, 1008.0, True), m, CFG)

    s = det.setups["PVUSDT"]
    assert s.pivot == 101.0, f"a pivot az ablak high-ja, nem 100.9: {s.pivot}"
    assert s.pivot_ts == 1005.0, "a pivot idobelyege is a high-e"
    assert abs(s.leg - 1.0) < 1e-9, "a lab a pivothoz igazodik (101.0 - 100.0)"

    # DOWN impulzusnal a low a pivot
    det2 = uj_detektor()
    m2 = dict(m, movePct=-0.9, imbalance=-0.8, startPrice=101.0)
    det2._detect_impulse(Trade("PDUSDT", 100.1, 10.0, 1008.0, False), m2, CFG)
    s2 = det2.setups["PDUSDT"]
    assert s2.pivot == 100.0, f"DOWN-nal a low a pivot: {s2.pivot}"


def test_pivot_update_also_updates_the_leg():
    """Uj szelsoertek: a pivot ES a leg is frissul.

    Korabban a pivot elmozdult, de a leg regi erteken maradt, igy a visszahuzas
    szazaleka rossz alaphoz merodott.
    """
    det = uj_detektor()
    kesz_baseline(det, "LGUSDT")
    futtat(det, "LGUSDT", [(100.0 + 1.2 * (i + 1) / 80, True, 20_000, 0.1)
                           for i in range(80)])
    s = det.setups["LGUSDT"]
    assert abs(s.leg - abs(s.pivot - s.p0)) < 1e-9, \
        f"leg={s.leg} de |pivot-p0|={abs(s.pivot - s.p0)}"
    assert s.leg > 1.0, "a lab a MEGNOTT mozgast tukrozi, nem a kezdetit"


def test_reconnect_resets_the_setup_and_prev_price():
    """Adatszakadas utan a regi setupbol nem lehet jelzes.

    A reconnect utani elso kotes kulonben egy regen megtortent kitores "friss
    keresztezesenek" latszana, mert a prev_price a szakadas elottrol maradt.
    """
    det = uj_detektor()
    kesz_baseline(det, "RCUSDT")
    t = tape(retrace_frac=0.30)
    futtat(det, "RCUSDT", t[:60])                  # impulzus + pullback
    assert det.setups["RCUSDT"].state == "WAIT_BREAKOUT"
    assert det.prev_price.get("RCUSDT") is not None

    det.reset(["RCUSDT"])                          # WS szakadas
    assert "RCUSDT" not in det.setups, "a setup torlodott"
    assert "RCUSDT" not in det.prev_price, "a prev_price is torlodott"

    # a reconnect utani kitoresi szakasz onmagaban nem adhat jelzest
    assert futtat(det, "RCUSDT", t[60:], t0=3000.0) == []


def test_manager_reset_reaches_the_detector():
    det = uj_detektor()
    kesz_baseline(det, "MRUSDT")
    mgr = DetectorManager(cfg_obj, [det], eligible_stub())
    futtat(det, "MRUSDT", tape(retrace_frac=0.30)[:60])
    assert "MRUSDT" in det.setups
    mgr.reset(["MRUSDT"])
    assert "MRUSDT" not in det.setups


def test_confirmation_flow_ignores_the_impulse_flow():
    """A kitores megerositese CSAK a pivot rogzitese ota erkezett kotesekbol szamol.

    Az impulzus alatti eros egyiranyu aramlas kulonben magatol "megerositene" a
    kesobbi kitorest.
    """
    det = uj_detektor()
    kesz_baseline(det, "FWUSDT")
    # GYORS szekvencia: a kitoreskor az 5 mp-es flow ablak MEG VISSZANYULNA az
    # impulzusba. Az impulzus vegig eros veteli, a kitores viszont semleges.
    imp = [(100.0 + 0.6 * (i + 1) / 40, True, 20_000, 0.1) for i in range(40)]
    pull = [(100.6 - 0.6 * 0.25 * (i + 1) / 8, False, 3_000, 0.1) for i in range(8)]
    also = 100.6 - 0.6 * 0.25
    kitores = [(also + (100.64 - also) * (i + 1) / 14, i % 2 == 0, 8_000, 0.1)
               for i in range(14)]
    assert futtat(det, "FWUSDT", imp + pull + kitores) == [], \
        "a regi impulzus-flow nem szamithat bele a megerositesbe"
    s = det.setups.get("FWUSDT")
    assert s is not None and s.wait_breakout_ts is not None


def test_rejected_eligibility_does_not_start_a_cooldown():
    """breakout -> eligibility reject eseten a setup ELETBEN MARAD, es NINCS cooldown."""
    e = Eligibility(cfg_obj)
    _book(e, "REJUSDT", 1.000, 1.002)          # szeles spread -> elutasitva
    det = ScalpDetector(cfg_obj, book=FrissKonyv(), eligibility=e)
    kesz_baseline(det, "REJUSDT")

    assert futtat(det, "REJUSDT", tape(retrace_frac=0.30)) == []
    assert "REJUSDT" not in det.cooldown, "elutasitas NEM indit cooldownt"
    assert "REJUSDT" in det.setups, "a setup el tovabb, ujra probalkozhat"


def test_only_an_accepted_signal_starts_the_cooldown():
    e = Eligibility(cfg_obj)
    _book(e, "OKUSDT", 1.0000, 1.0001)         # szuk spread -> atmegy
    det = ScalpDetector(cfg_obj, book=FrissKonyv(), eligibility=e)
    kesz_baseline(det, "OKUSDT")

    jelek = futtat(det, "OKUSDT", tape(retrace_frac=0.30))
    assert len(jelek) == 1, jelek
    assert "OKUSDT" in det.cooldown, "elfogadott jelzes UTAN indul a cooldown"
    assert "OKUSDT" not in det.setups, "es a setup lezarul"


def test_websocket_request_id_is_an_unsigned_integer():
    """A SUBSCRIBE/UNSUBSCRIBE id mezoje novekvo unsigned int, nem UUID string."""
    from app.market_data import next_request_id
    a, b = next_request_id(), next_request_id()
    assert isinstance(a, int) and isinstance(b, int)
    assert a > 0 and b == a + 1, "novekvo, pozitiv egesz"

    forras = (pathlib.Path(__file__).parent.parent / "app" / "market_data.py").read_text()
    assert "uuid" not in forras, "nincs tobb UUID a request ID-ban"
    assert forras.count('"id": next_request_id()') >= 2


def test_breakout_invalidated_when_price_falls_back_long():
    """LONG: ha az ar a megerositesre varva visszaesik a szint ALA, a kitores
    ervenyet veszti. Csak egy UJ, valodi cross indithat uj ablakot."""
    det = uj_detektor()
    kesz_baseline(det, "BLUSDT")
    t = tape(retrace_frac=0.30)
    futtat(det, "BLUSDT", t[:60])                     # impulzus + pullback
    s = det.setups["BLUSDT"]
    assert s.state == "WAIT_BREAKOUT"
    szint = s.breakout_level

    ora = 2010.5                # kozvetlenul a tape utan: a setup meg nem jart le
    def kot(ar, buy=True, dt=0.2):
        nonlocal ora
        ora += dt
        return det.on_trade(Trade("BLUSDT", ar, 8_000 / ar, ora, buy))

    kot(szint - 0.02)                                  # a szint alatt
    assert kot(szint + 0.01) is None, "a cross pillanataban meg nincs jelzes"
    assert s.breakout_ts is not None, "a kitores elindult"

    kot(szint - 0.01, buy=False)                       # VISSZAESIK a szint ala
    assert s.breakout_ts is None, "a kitores ervenyet vesztette"

    # innentol a szint alatt maradva SOSEM lehet jelzes, akarmennyi veteli kotes jon
    assert [j for _ in range(30) if (j := kot(szint - 0.005))] == [], \
        "a szint alatt nincs LONG jelzes"
    assert s.breakout_ts is None
    assert "BLUSDT" in det.setups, "az ervenytelenites nem oli meg a setupot"


def test_breakout_invalidated_when_price_comes_back_short():
    """SHORT: ha az ar visszamegy a szint FOLE, a kitores ervenyet veszti."""
    det = uj_detektor()
    kesz_baseline(det, "BSUSDT")
    t = tape(up=False, retrace_frac=0.30)
    futtat(det, "BSUSDT", t[:60])
    s = det.setups["BSUSDT"]
    assert s.state == "WAIT_BREAKOUT" and s.up is False
    szint = s.breakout_level

    ora = 2010.5
    def kot(ar, buy=False, dt=0.2):
        nonlocal ora
        ora += dt
        return det.on_trade(Trade("BSUSDT", ar, 8_000 / ar, ora, buy))

    kot(szint + 0.02)                                  # a szint folott
    assert kot(szint - 0.01) is None
    assert s.breakout_ts is not None, "a kitores elindult"

    kot(szint + 0.01, buy=True)                        # VISSZAMEGY a szint fole
    assert s.breakout_ts is None, "a kitores ervenyet vesztette"

    assert [j for _ in range(30) if (j := kot(szint + 0.005))] == [], \
        "a szint folott nincs SHORT jelzes"


def test_a_new_cross_can_still_signal_after_invalidation():
    """Az ervenytelenites nem oli meg a setupot: egy UJ valodi cross jelezhet."""
    det = uj_detektor()
    kesz_baseline(det, "NCUSDT")
    t = tape(retrace_frac=0.30)
    futtat(det, "NCUSDT", t[:60])
    s = det.setups["NCUSDT"]
    szint = s.breakout_level

    ora = 2010.5
    def kot(ar, buy=True, dt=0.15):
        nonlocal ora
        ora += dt
        return det.on_trade(Trade("NCUSDT", ar, 8_000 / ar, ora, buy))

    kot(szint - 0.02)
    kot(szint + 0.01)                                  # 1. cross
    kot(szint - 0.01, buy=False)                       # ervenytelenites
    assert s.breakout_ts is None
    assert "NCUSDT" in det.setups, "a setup EL tovabb"

    jelek = [j for _ in range(20) if (j := kot(szint + 0.004))]   # 2. cross
    assert len(jelek) == 1, f"az uj cross utan jon a jelzes: {jelek}"


def test_short_reconnect_keeps_a_still_fresh_baseline():
    """Rovid gap utan ne kelljen feleslegesen ujra 5 percet varni."""
    b = Baseline(cfg_obj)
    perc = CFG["baselineMinutes"]
    for i in range(perc * 60):
        b.add("XUSDT", 1000.0 + i, 0.02)
    kesz = 1000.0 + perc * 60 - 1
    assert b.value("XUSDT", kesz) == 0.02

    # 20 masodperces szakadas: a mintak tulnyomo resze meg friss
    assert b.value("XUSDT", kesz + 20) == 0.02, "rovid gap utan meg hasznalhato"


def test_long_gap_makes_the_baseline_stale():
    """Hosszabb adatkimaradas utan a regi baseline NEM hasznalhato.

    Korabban az add() csak uj minta erkezesekor vagott, tehat a value() egy
    orakkal korabbi ablak medianjat adta vissza.
    """
    b = Baseline(cfg_obj)
    perc = CFG["baselineMinutes"]
    for i in range(perc * 60):
        b.add("XUSDT", 1000.0 + i, 0.02)
    kesz = 1000.0 + perc * 60 - 1
    assert b.value("XUSDT", kesz) == 0.02

    # fel ablaknyi kimaradas: a maradek mar nem fedi le a szukseges idoszakot
    assert b.value("XUSDT", kesz + perc * 60 * 0.5) is None
    # teljes ablaknyi kimaradas: minden minta kiesett
    assert b.value("XUSDT", kesz + perc * 60 + 1) is None


def test_baseline_rebuilds_after_a_long_gap():
    """Uj adatokkal ismet felepul -- a hosszu gap nem "meregezi meg" veglegesen."""
    b = Baseline(cfg_obj)
    perc = CFG["baselineMinutes"]
    for i in range(perc * 60):
        b.add("XUSDT", 1000.0 + i, 0.02)
    gap_utan = 1000.0 + perc * 60 + 3600         # egy oras kimaradas
    assert b.value("XUSDT", gap_utan) is None

    for i in range(perc * 60):
        b.add("XUSDT", gap_utan + i, 0.05)
    most = gap_utan + perc * 60 - 1
    assert b.value("XUSDT", most) == 0.05, "az UJ adatokbol epult fel"


def test_status_readiness_uses_the_exchange_clock():
    """A STATUS "normal kesz" szamlalojanak is az aktualis idot kell nezni.

    Itt nincs add() hivas, ami trimmelne -- ha a value() nem vag az aktualis
    idohoz, a sor orakkal kesobb is "keszet" mutatna.
    """
    friss = uj_detektor()
    kesz_baseline(friss, "RDUSDT")
    friss.last_ts = 2000.0
    assert "normal kesz: 1/" in friss.readiness(), "frissen kesz"

    # kulon detektor: a value() trimmelese mellekhatas, egy korabbi hivas
    # elfedne a kulonbseget
    elavult = uj_detektor()
    kesz_baseline(elavult, "RDUSDT")
    elavult.last_ts = 2000.0 + 3600              # egy oras kimaradas
    assert "normal kesz: 0/" in elavult.readiness(), "elavult normal nem kesz"


# ---------------------------------------------------------------- adat-frissesseg

def test_no_signal_without_fresh_order_book():
    """FAIL-CLOSED: elavult konyv-adattal NINCS jelzes."""
    det = uj_detektor(book=FrissKonyv(friss=False))
    kesz_baseline(det, "STUSDT")
    assert futtat(det, "STUSDT", tape(retrace_frac=0.30)) == []


def test_no_signal_without_any_book_at_all():
    det = uj_detektor(book=None)
    kesz_baseline(det, "NOUSDT")
    assert futtat(det, "NOUSDT", tape(retrace_frac=0.30)) == []


def test_eligibility_is_fail_closed_on_missing_book_data():
    """Korabban atengedtunk, ha SEMMILYEN konyv-adat nem jott. Ez fail-open volt."""
    e = Eligibility(cfg_obj)
    assert e.check("BTCUSDT")[1] == "no_book_data", "adat nelkul NINCS jelzes"


def test_eligibility_rejects_stale_book_data():
    e = Eligibility(cfg_obj)
    _book(e, "OLDUSDT", 1.0000, 1.0001)
    assert e.check("OLDUSDT")[0] is True
    # az adat oregszik: a bookTicker idobelyeget visszadatáljuk
    ts, *tobbi = e.book["OLDUSDT"]
    e.book["OLDUSDT"] = (ts - CFG["maxDataAgeSec"] - 1, *tobbi)
    assert e.check("OLDUSDT")[1] == "stale_book_data"


# ---------------------------------------------------------------- baseline / RollingMedian

def test_rolling_median_ignores_outliers():
    rm = RollingMedian(window_sec=30, min_samples=5)
    assert rm.value("X") is None
    for i in range(30):
        rm.add("X", 1000.0 + i, 10.0)
    assert rm.value("X") == 10.0
    rm.add("X", 1030.0, 10_000.0)             # egyetlen kiugro ertek
    assert rm.value("X") == 10.0, "a median nem viheto el egy kiugro ertekkel"


def test_baseline_is_not_ready_too_early():
    """5 perces baseline-hoz kb. 5 percnyi elozmeny kell -- nem 1 percnyi.

    Korabban 60 minta mar keszne nyilvanitotta, igy restart utan a rendszer
    percekkel korabban jelezhetett, hianyos elozmenybol.
    """
    b = Baseline(cfg_obj)
    perc = CFG["baselineMinutes"]
    for i in range(70):                       # ~70 masodpercnyi adat
        b.add("XUSDT", 1000.0 + i, 0.02)
    assert b.value("XUSDT") is None, "70 mp adat NEM eleg egy 5 perces normalhoz"

    # 200 masodperc: a MINTASZAM mar eleg lenne, de az IDOTARTAM meg nem
    for i in range(70, 200):
        b.add("XUSDT", 1000.0 + i, 0.02)
    assert len(b.median.samples["XUSDT"]) >= b.median._min_samples, \
        "a mintaszam-feltetel mar teljesul"
    assert b.value("XUSDT") is None, \
        "de 200 mp meg mindig nem 5 percnyi elozmeny"

    for i in range(200, perc * 60):           # a teljes ablak
        b.add("XUSDT", 1000.0 + i, 0.02)
    assert b.value("XUSDT") == 0.02, "a teljes ablak utan kesz"


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

def test_bookcache_freshness_is_fail_closed():
    """Friss adat nelkul a snapshot es a fresh() is nemet mond."""
    from app.bookcache import BookCache
    bc = BookCache(cfg_obj)
    assert bc.fresh("XUSDT") is False and bc.snapshot("XUSDT") is None

    bids = [[100.0 - i * 0.01, 1.0] for i in range(20)]
    asks = [[100.02 + i * 0.01, 1.0] for i in range(20)]
    bc.on_depth({"s": "XUSDT", "b": bids, "a": asks})
    assert bc.fresh("XUSDT") is True
    snap = bc.snapshot("XUSDT")
    assert len(snap["bids"]) == 20 and len(snap["asks"]) == 20

    # oregedes: a bejegyzes idobelyeget visszadatáljuk
    ts, b, a = bc.books["XUSDT"]
    bc.books["XUSDT"] = (ts - CFG["maxDataAgeSec"] - 1, b, a)
    assert bc.fresh("XUSDT") is False and bc.snapshot("XUSDT") is None


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


def test_total_book_outage_blocks_signals_and_says_so():
    """Korabban ilyenkor MINDENT atengedtunk (fail-open). Most nincs jelzes."""
    e = Eligibility(cfg_obj)
    assert e.check("BTCUSDT")[1] == "no_book_data"
    assert "NEM ERKEZIK" in e.book_status()
    e.on_book_ticker({"e": "bookTicker", "s": "BTCUSDT", "b": "1.0000",
                      "B": "100000", "a": "1.0001", "A": "100000"})
    assert "konyv: 1 par" in e.book_status()
    assert e.check("BTCUSDT")[0] is True


def test_blacklist_and_whitelist():
    cfg = types.SimpleNamespace(detector=CFG,
                                market={**MARKET, "symbolBlacklist": ["BADUSDT"]})
    e = Eligibility(cfg)
    _book(e, "BADUSDT", 1.0000, 1.0001)
    assert e.check("BADUSDT")[1] == "blacklisted"

    cfg = types.SimpleNamespace(detector=CFG,
                                market={**MARKET, "symbolWhitelist": ["ONLYUSDT"]})
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
    e.cfg = types.SimpleNamespace(detector=CFG,
                                  market={**MARKET, "symbolBlacklist": ["C_USDT"]})
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
    for price, buy, usd, dt in tape(retrace_frac=0.30):
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
    t = tape(retrace_frac=0.30)
    signals = []
    ora = 2000.0
    for price, buy, usd, dt in t:
        ora += dt
        signals += mgr.on_trade(Trade("CYSUSDT", price, usd / price, ora, buy))
    assert len(signals) == 1, "a hibas detektor nem nyelheti el a masik jelzeset"
    assert mgr.ticks == len(t)
    assert mgr.total_candidates == 1


def test_manager_skips_disabled_detector():
    cfg = types.SimpleNamespace(detector={**CFG, "enabled": False}, market=MARKET,
                                telegram=TG)
    mgr = DetectorManager(cfg, [ScalpDetector(cfg)], eligible_stub())
    signals = []
    t = 2000.0
    for price, buy, usd, dt in tape(retrace_frac=0.30):
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
    assert len(sorok) == 7 and all(isinstance(x, str) and x for x in sorok)


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


def test_outcome_records_interval_prices():
    """1 / 3 / 5 / 10 perces ar rogzitese a jelzes utan."""
    from app.outcome import Tracker
    t = Tracker("id", "XUSDT", "LONG", "LONG", 100.0, 0.0, 600.0,
                [], [], mark_sec=[60, 180])
    t.on_price(100.5, 30.0)                 # meg egyik merespont sem jart le
    assert t.marks["60"] is None and t.marks["180"] is None
    t.on_price(101.0, 65.0)                 # az 1 perces pont lejart
    assert t.marks["60"]["price"] == 101.0
    assert abs(t.marks["60"]["pct"] - 1.0) < 1e-9
    assert t.marks["180"] is None
    t.on_price(99.0, 200.0)                 # a 3 perces is
    assert t.marks["180"]["price"] == 99.0
    assert t.marks["60"]["price"] == 101.0, "a mar rogzitett pontot nem irjuk felul"


def test_outcome_starts_before_the_telegram_call():
    """Az eredmenymeres a jelzes letrejottekor indul -- a Telegram HTTP hivas
    masodpercekig is tarthat, addig mar mernunk kell az arat."""
    forras = (pathlib.Path(__file__).parent.parent / "app" / "signals.py").read_text()
    mentes = forras.index("await self._save(")
    telegram_hivas = forras.index("await self.notifier.send(")
    assert mentes < telegram_hivas, "eloszor mentes + outcome.track, csak utana halozat"


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


def test_telegram_signal_is_short_and_plain():
    """Rovid, egyertelmu uzenet -- semmi magyarazat, csak a szamok."""
    from app.telegram import format_signal
    import datetime as _dt

    sig = {
        "timestamp": _dt.datetime(2026, 9, 1, 12, 0, tzinfo=_dt.timezone.utc),
        "detector": "scalp", "setup": "LONG", "symbol": "BTCUSDT",
        "direction": "LONG", "price": 123.45, "url": "https://example.test/BTCUSDT",
        "metrics": {"impulsePct": 0.72, "pullbackPct": 28.0, "flowPct": 67.0,
                    "breakoutAgeSec": 0.8},
        "trade": {"executed": False},
    }
    sorok = format_signal(sig).split("\n")
    assert "LONG" in sorok[0] and "BTCUSDT" in sorok[0]
    assert sorok[1] == "2026-09-01 12:00:00 UTC", "a jelzes IDOPONTJA"
    assert sorok[2] == "Entry: 123.45"
    assert sorok[3] == "Impulse: +0.72%"
    assert sorok[4] == "Pullback: 28%"
    assert sorok[5] == "Buy flow: 67%"
    assert sorok[6] == "Breakout age: 0.8s"
    assert len(sorok) == 8, "het sor + a link, semmi tobb"

    # SHORT-nal az ELADOI oldalt mutatjuk
    sig["direction"] = "SHORT"
    sig["metrics"]["flowPct"] = 22.0
    szoveg = format_signal(sig)
    assert "Sell flow: 78%" in szoveg, szoveg


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} teszt rendben")
