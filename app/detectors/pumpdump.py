"""PumpDumpDetector -- masodperces skalaju hirtelen armozgas detektalas.

Symbolonkent memoriaban tartjuk az utolso ~6 masodperc arait, es minden uj trade-nel
kiszamoljuk az 1s / 3s / 5s valtozast. Nem varunk gyertyazarasra.

A statusz tablat is ez a detektor rajzolja (status_lines) -- az "mi tortenik az
arakkal" nezet ehhez a detektorhoz tartozik.
"""
import time
import logging
from collections import deque, defaultdict

from .. import events, binance_rest
from ..fmt import pad, pct as fpct, price as fprice, money
from .base import Detector, make_signal

log = logging.getLogger("pumpdump")

WINDOWS = (1, 3, 5)          # masodperc
HISTORY_SEC = max(WINDOWS) + 1
VOL_ALPHA = 0.01             # a volatilitas EWMA tanulasi rata (~40 masodperc emlekezet)


class PumpDumpDetector(Detector):
    name = "pump_dump"
    config_key = "detector"

    def __init__(self, cfg):
        self.cfg = cfg
        self.history = defaultdict(deque)   # symbol -> deque[(ts, price)]
        self.last_trigger = {}              # symbol -> ts
        # az elo statusz tablahoz
        self.ticks = 0
        self.total_triggers = 0
        self.latest = {}                    # symbol -> (ar, valtozasok)
        self.vol = {}                       # symbol -> {ablak: atlagos abszolut valtozas}

    def on_trade(self, trade):
        return self.on_price(trade.symbol, trade.price, trade.ts)

    def on_price(self, symbol, price, ts):
        """Uj ar. Visszaad egy signal dictet, vagy None-t."""
        h = self.history[symbol]
        h.append((ts, price))
        while h and h[0][0] < ts - HISTORY_SEC:
            h.popleft()

        c = self.cfg.detector
        changes = {w: self._change(h, ts, w, price,
                                   c["maxRefAgeFactor"], c["minTicksInWindow"])
                   for w in WINDOWS}

        self.ticks += 1
        self.latest[symbol] = (price, changes)
        self._update_volatility(symbol, changes)
        thresholds = self.thresholds(symbol)
        direction = None
        for w in WINDOWS:
            ch = changes[w]
            if ch is None:
                continue
            if ch >= thresholds[w]:
                direction = "LONG"
                break
            if ch <= -thresholds[w]:
                direction = "SHORT"
                break
        if direction is None:
            return None

        if ts - self.last_trigger.get(symbol, 0) < c["symbolCooldownSec"]:
            return None
        self.last_trigger[symbol] = ts
        self.total_triggers += 1

        log.warning("[%s] TRIGGER %s | 1s %s | 3s %s | 5s %s | sajat kuszob 1mp %.2f%%",
                    symbol, direction, _pct(changes[1]), _pct(changes[3]), _pct(changes[5]),
                    thresholds[1])
        events.add(f"{symbol:<14} PUMP/DUMP {direction:<5} "
                   f"1s {_pct(changes[1])}  3s {_pct(changes[3])}  5s {_pct(changes[5])}")

        ratios = [abs(ch) / thresholds[w] for w, ch in changes.items() if ch is not None]
        c1, c5 = changes[1], changes[5]
        return make_signal(
            self.name, self.config_key, symbol, direction, price, ts,
            strength=max(ratios) if ratios else 0.0,
            # gyorsul, ha az utolso 1 masodperc tempoja meghaladja az 5 mp-es atlagot
            accelerating=(c1 is not None and c5 is not None
                          and abs(c5) > 0 and abs(c1) > abs(c5) / 5),
            context_mode="momentum",
            detail={
                "priceChange": {f"s{w}": v for w, v in changes.items()},
                "thresholds": {f"s{w}": v for w, v in thresholds.items()},
            },
            lines=[f"1s: {_pct(changes[1])}",
                   f"3s: {_pct(changes[3])}",
                   f"5s: {_pct(changes[5])}",
                   f"Trigger threshold (1s): {thresholds[1]:.2f}%"],
            history=list(h),
        )

    @staticmethod
    def _change(history, now, window, price, max_ref_age_factor, min_ticks):
        """Szazalekos valtozas a window masodperccel ezelotti arhoz kepest.

        None-t ad, ha nem merheto megbizhatoan:
          - nincs eleg elozmeny (az elso tickek hamis jelet adnanak),
          - a viszonyitasi pont tul regi (ritkan kereskedett par: egyetlen trade
            a spreaden at ugy nezne ki, mintha 1 masodperc alatt tortent volna),
          - tul keves trade van az ablakban (egyetlen trade nem mozgas, csak zaj).
        """
        start = now - window
        if not history or history[0][0] > start:
            return None

        ref = ref_ts = None
        ticks_inside = 0
        for ts, p in history:          # ponytail: linearis keres, a deque max par szaz elem
            if ts > start:
                ticks_inside += 1
            else:
                ref, ref_ts = p, ts
        if ref is None or ref == 0:
            return None
        if now - ref_ts > window * max_ref_age_factor:
            return None
        if ticks_inside < min_ticks:
            return None
        return (price - ref) / ref * 100.0

    def _update_volatility(self, symbol, changes):
        """Ablakonkenti EWMA az abszolut valtozasbol -- ez a par sajat "zajszintje"."""
        v = self.vol.setdefault(symbol, {})
        for w, ch in changes.items():
            if ch is None:
                continue
            prev = v.get(w)
            v[w] = abs(ch) if prev is None else prev + VOL_ALPHA * (abs(ch) - prev)


    def base_thresholds(self):
        c = self.cfg.detector
        return {1: c["priceChangeThreshold1s"],
                3: c["priceChangeThreshold3s"],
                5: c["priceChangeThreshold5s"]}

    def thresholds(self, symbol=None):
        """A par sajat kuszobei. A configban megadott ertek a padlo: egy nyugtalan
        parnak a sajat zajszintjehez merten tobbet kell mozdulnia a jelzeshez."""
        base = self.base_thresholds()
        mult = self.cfg.detector["volatilityMultiplier"]
        if not symbol or not mult:
            return base
        v = self.vol.get(symbol, {})
        return {w: max(t, mult * v[w]) if v.get(w) else t for w, t in base.items()}

    def snapshot(self, top=10):
        """Az aktualis allapot a statusz tablahoz. A tick szamlalot nullazza."""
        now = time.time()
        cooldown = self.cfg.detector["symbolCooldownSec"]
        rows = []
        for symbol, (price, changes) in self.latest.items():
            th = self.thresholds(symbol)
            measured = [(abs(ch) / th[w], w, ch) for w, ch in changes.items() if ch is not None]
            ratio, window, change = max(measured) if measured else (0.0, None, None)
            rows.append({
                "symbol": symbol,
                "price": price,
                "changes": changes,
                "ratio": ratio,                                    # 1.0 = pont a kuszobon
                "window": window,
                "missing": (th[window] - abs(change)) if window else None,
                "rising": (change or 0) > 0,
                "cooling": now - self.last_trigger.get(symbol, 0) < cooldown,
                "ownThreshold": th[1],
            })
        rows.sort(key=lambda r: r["ratio"], reverse=True)
        ticks, self.ticks = self.ticks, 0
        return {"ticks": ticks, "totalTriggers": self.total_triggers,
                "symbols": len(self.latest), "rows": rows[:top]}


    def status_lines(self):
        """Az "mi tortenik most az arakkal" tabla."""
        snap = self.snapshot()
        th = self.base_thresholds()
        out = [
            f"  ARFOLYAMOK   alap kuszob: 1 mp {th[1]:.2f}%  |  3 mp {th[3]:.2f}%  |  "
            f"5 mp {th[5]:.2f}%   (paronkent a sajat zajszinthez igazitva)",
            f"  {pad('par', 14)}{'24h forg.':>11}{'arfolyam':>13}"
            f"{'1 mp':>8}{'3 mp':>8}{'5 mp':>8}{'sajat kuszob':>14}   mi van vele",
        ]
        for r in snap["rows"]:
            c = r["changes"]
            out.append(f"  {pad(r['symbol'], 14)}"
                       f"{money(binance_rest.SYMBOL_VOLUME.get(r['symbol'])):>11}"
                       f"{fprice(r['price']):>13}"
                       f"{fpct(c[1]):>8}{fpct(c[3]):>8}{fpct(c[5]):>8}"
                       f"{r['ownThreshold']:>13.2f}%   {_verdict(r)}")
        return out


def _verdict(r):
    """Emberi nyelven: mi van ezzel a parral."""
    if r["window"] is None:
        return "keves kereskedes, nem merheto"
    irany = "emelkedik" if r["rising"] else "esik"
    if r["missing"] <= 0:
        return ("jelzes mar elment, varakozas a kovetkezoig" if r["cooling"]
                else f"kuszob atlepve, {irany}")
    hiany = f"{r['missing']:.2f}%"
    if r["ratio"] >= 0.9:
        return f"MINDJART JELZES! {irany}, meg {hiany} hianyzik"
    if r["ratio"] >= 0.6:
        return f"erosen {irany}, meg {hiany} hianyzik a jelzeshez"
    if r["ratio"] >= 0.3:
        return f"{irany}, de meg messze van a jelzestol"
    return "alig mozdul"


def _pct(v):
    return "n/a" if v is None else f"{v:+.2f}%"
