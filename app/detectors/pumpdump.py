"""PumpDumpDetector -- hirtelen, egyiranyu armozgas detektalasa.

A trigger az utolso N TRADE-re illesztett egyenes MEREDEKSEGE (%/masodperc), plusz
egy konzisztencia-feltetel: a lepesek mekkora hanyada mutat egy iranyba.

Miert nem a "most vs 1 masodperccel ezelott" osszehasonlitas? Mert az nem latja,
mi tortent kozben. Meressel:

    esemeny                        1 mp valtozas   meredekseg   konzisztencia
    valodi pump                       +0.25%       +0.249%/mp       100%   <- ez kell
    lassu kuszas (90 mp alatt)        +0.00%       +0.008%/mp       100%
    egyetlen kiugro print, aztan vissza +0.45%     +0.000%/mp         0%   <- hamis jelzes volt
    fureszfog, nagy amplitudo         -0.24%       -0.004%/mp        48%   <- hamis jelzes volt

Az utolso ket sor a regi logikaval jelzest adott. A meredekseg %/masodpercben van,
tehat a sebesseg dimenzio megmarad: a lassu kuszas 30x kisebb erteket ad.

Az 1/3/5 masodperces szamok megmaradnak a tablazatban tajekoztatasul, de nem
triggerelnek.

A statusz tablat is ez a detektor rajzolja (status_lines).
"""
import time
import logging
from collections import deque, defaultdict

from .. import events, binance_rest
from ..fmt import pad, price as fprice, money
from ..links import binance_url
from .base import Detector, make_signal

log = logging.getLogger("pumpdump")

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
        self.latest = {}                    # symbol -> (ar, valtozasok, meredekseg)
        self.last_ts = 0.0                  # az utolso feldolgozott trade tozsdei ideje
        self.vol = {}                       # symbol -> atlagos abszolut meredekseg

    def on_trade(self, trade):
        return self.on_price(trade.symbol, trade.price, trade.ts,
                             trade.price * trade.qty)

    def on_price(self, symbol, price, ts, quote_qty=0.0):
        """Uj ar. Visszaad egy signal dictet, vagy None-t."""
        c = self.cfg.detector
        h = self.history[symbol]
        h.append((ts, price, quote_qty))
        while h and h[0][0] < ts - c["slopeWindowSec"] * 2:
            h.popleft()

        self.ticks += 1
        self.last_ts = ts
        trend = self._trend(h, ts, c, symbol)
        self.latest[symbol] = (price, trend)

        if trend is None:
            return None
        self._update_volatility(symbol, trend["pctPerSec"])

        threshold = self.threshold(symbol)
        if abs(trend["pctPerSec"]) < threshold:
            return None
        if trend["consistency"] < c["minConsistency"]:
            return None
        if abs(trend["totalPct"]) < c["minTotalMovePct"]:
            return None

        direction = "LONG" if trend["pctPerSec"] > 0 else "SHORT"
        if ts - self.last_trigger.get(symbol, 0) < c["symbolCooldownSec"]:
            return None
        self.last_trigger[symbol] = ts
        self.total_triggers += 1

        log.warning("[%s] TRIGGER %s | %+.2f%% %.1f mp alatt (%+.3f%%/mp, kuszob %.3f) | "
                    "egyirany %.0f%% | %d trade | %s",
                    symbol, direction, trend["totalPct"], trend["spanSec"],
                    trend["pctPerSec"], threshold, trend["consistency"] * 100,
                    trend["trades"], binance_url(symbol))
        events.add(f"{symbol:<14} PUMP/DUMP {direction:<5} "
                   f"{trend['totalPct']:+.2f}% / {trend['spanSec']:.1f} mp  "
                   f"({trend['pctPerSec']:+.3f}%/mp, egyirany {trend['consistency']:.0%})")

        return make_signal(
            self.name, self.config_key, symbol, direction, price, ts,
            strength=abs(trend["pctPerSec"]) / threshold,
            accelerating=trend["accelerating"],
            context_mode="momentum",
            move_pct=abs(trend["totalPct"]),
            # A stop nem a teljes impulzus aljan van, hanem annak felenel: ha a
            # mozgas felet visszaadja, a tezis mar halott.
            stop_anchor=(trend["origin"] + (price - trend["origin"])
                         * (1 - c["momentumStopRetracementPct"] / 100.0)),
            target_anchor=price + (price - trend["origin"]) * c["momentumTargetFactor"],
            detail={
                "slopePctPerSec": round(trend["pctPerSec"], 5),
                "slopeThreshold": round(threshold, 5),
                "consistency": round(trend["consistency"], 3),
                "spanSec": round(trend["spanSec"], 3),
                "trades": trend["trades"],
                "totalPct": round(trend["totalPct"], 4),
                "volumeUSDT": round(trend["volume"], 2),
                "expectedVolumeUSDT": round(trend["expectedVolume"], 2),
            },
            lines=[
                ("mozgas", f"{trend['totalPct']:+.2f}%   {trend['spanSec']:.1f} mp alatt"),
                ("meredekseg", f"{trend['pctPerSec']:+.3f} %/mp   (kuszob {threshold:.3f})"),
                ("egyiranyusag", f"{trend['consistency']:.0%}   "
                                 f"({trend['trades']} trade)"),
                ("forgalom", f"{money(trend['volume'])} USDT   "
                             f"(atlag {money(trend['expectedVolume'])})"),
            ],
            history=[(t, p) for t, p, _ in h],
        )

    @staticmethod
    def _trend(history, now, c, symbol=None):
        """Az utolso slopeWindowSec masodperc trade-jeire illesztett egyenes.

        None, ha nem merheto megbizhatoan: keves trade, tul rovid tenyleges
        idoszakasz, vagy nincs mogotte valodi forgalom.
        """
        start = now - c["slopeWindowSec"]
        w = [(t, p, q) for t, p, q in history if t >= start]
        if len(w) < c["minTradesInWindow"]:
            return None

        span = w[-1][0] - w[0][0]
        # az ablak legalabb felet le kell fednie, kulonben egy pillanatnyi
        # trade-csokorbol szamolnank %/masodpercet
        if span < c["slopeWindowSec"] / 2:
            return None

        volume = sum(q for _, _, q in w)
        vol24 = binance_rest.SYMBOL_VOLUME.get(symbol) if symbol else None
        elvart = vol24 / 86400.0 * span * c["minVolumeFactor"] if vol24 else 0.0
        if volume < elvart:
            return None

        t0 = w[0][0]
        xs = [t - t0 for t, _, _ in w]
        ys = [p for _, p, _ in w]
        n = len(w)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0 or mean_y == 0 or ys[0] == 0:
            return None
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        slope = sxy / sxx
        pct_per_sec = slope / mean_y * 100.0

        steps = [ys[i + 1] - ys[i] for i in range(n - 1)]
        moved = [st for st in steps if st != 0]
        consistency = (sum(1 for st in moved if st * slope > 0) / len(moved)
                       if moved else 0.0)

        half = n // 2
        first = (ys[half] - ys[0]) / max(xs[half] - xs[0], 1e-9)
        second = (ys[-1] - ys[half]) / max(xs[-1] - xs[half], 1e-9)

        return {"pctPerSec": pct_per_sec, "spanSec": span, "consistency": consistency,
                "trades": n, "origin": ys[0], "volume": volume, "expectedVolume": elvart,
                "totalPct": (ys[-1] - ys[0]) / ys[0] * 100.0,
                "accelerating": abs(second) > abs(first) and second * first > 0}

    def _update_volatility(self, symbol, pct_per_sec):
        """EWMA az abszolut meredeksegbol -- ez a par sajat "zajszintje"."""
        prev = self.vol.get(symbol)
        self.vol[symbol] = (abs(pct_per_sec) if prev is None
                            else prev + VOL_ALPHA * (abs(pct_per_sec) - prev))

    def base_threshold(self):
        return self.cfg.detector["minSlopePctPerSec"]

    def threshold(self, symbol=None):
        """A par sajat meredekseg-kuszobe. A configban megadott ertek a padlo: egy
        nyugtalan parnak a sajat zajszintjehez merten meredekebben kell mozdulnia.
        Felfele is van korlat, hogy egy elszallt merteg ne nemitson el egy part."""
        base = self.base_threshold()
        mult = self.cfg.detector["volatilityMultiplier"]
        if not symbol or not mult:
            return base
        v = self.vol.get(symbol)
        if not v:
            return base
        return min(max(base, mult * v), base * self.cfg.detector["maxThresholdFactor"])

    def snapshot(self, top=10):
        """Az aktualis allapot a statusz tablahoz. A tick szamlalot nullazza."""
        now = self.last_ts or time.time()
        cooldown = self.cfg.detector["symbolCooldownSec"]
        min_cons = self.cfg.detector["minConsistency"]
        rows = []
        for symbol, (price, trend) in self.latest.items():
            th = self.threshold(symbol)
            slope = trend["pctPerSec"] if trend else None
            rows.append({
                "symbol": symbol,
                "price": price,
                "movePct": trend["totalPct"] if trend else None,
                "spanSec": trend["spanSec"] if trend else None,
                "slope": slope,
                "consistency": trend["consistency"] if trend else None,
                "threshold": th,
                "minConsistency": min_cons,
                "ratio": abs(slope) / th if slope is not None and th else 0.0,
                "rising": (slope or 0) > 0,
                "cooling": now - self.last_trigger.get(symbol, 0) < cooldown,
            })
        rows.sort(key=lambda r: r["ratio"], reverse=True)
        ticks, self.ticks = self.ticks, 0
        return {"ticks": ticks, "totalTriggers": self.total_triggers,
                "symbols": len(self.latest), "rows": rows[:top]}

    blocked = frozenset()      # a manager tolti, csak a kijelzeshez
    noise = {}                 # symbol -> tick zaj %, szinten a managertol

    def status_lines(self):
        """Az "mi tortenik most az arakkal" tabla."""
        snap = self.snapshot()
        c = self.cfg.detector
        out = [
            f"  ARFOLYAMOK   jelzeshez kell: legalabb {c['minTotalMovePct']:.2f}% mozgas "
            f"{c['slopeWindowSec']:.0f} masodpercen belul, {c['minSlopePctPerSec']:.2f}%/mp "
            f"tempoval es {c['minConsistency']:.0%} egyiranyusaggal",
            f"  {pad('par', 14)}{'24h forg.':>11}{'arfolyam':>13}"
            f"{'mozgas':>9}{'%/mp':>9}{'kuszob':>8}{'egyirany':>10}{'tick zaj':>10}"
            f"   mi van vele",
        ]
        for r in snap["rows"]:
            mozgas = f"{r['movePct']:+.2f}%" if r["movePct"] is not None else "--"
            slope = f"{r['slope']:+.3f}" if r["slope"] is not None else "--"
            cons = f"{r['consistency']:.0%}" if r["consistency"] is not None else "--"
            allapot = ("KIZARVA -- egy kotes tul sokat mozdit"
                       if r["symbol"] in self.blocked else _verdict(r))
            zaj = self.noise.get(r["symbol"])
            zaj_txt = f"{zaj:.3f}%" if zaj is not None else "--"
            out.append(f"  {pad(r['symbol'], 14)}"
                       f"{money(binance_rest.SYMBOL_VOLUME.get(r['symbol'])):>11}"
                       f"{fprice(r['price']):>13}"
                       f"{mozgas:>9}{slope:>9}{r['threshold']:>8.3f}{cons:>10}{zaj_txt:>10}"
                       f"   {allapot}")
        return out


def _verdict(r):
    """Emberi nyelven: mi van ezzel a parral."""
    if r["slope"] is None:
        return "nincs eleg friss kereskedes a mereshez"
    irany = "emelkedik" if r["rising"] else "esik"
    eleg_egyiranyu = r["consistency"] >= r["minConsistency"]

    if r["ratio"] >= 1.0:
        if r["cooling"]:
            return "jelzes mar elment, varakozas a kovetkezoig"
        if not eleg_egyiranyu:
            return (f"gyorsan {irany}, de osszevissza "
                    f"(egyirany {r['consistency']:.0%}, kell {r['minConsistency']:.0%})")
        return f"kuszob atlepve, {irany}"

    hiany = f"{(r['threshold'] - abs(r['slope'])):.3f}%/mp"
    if not eleg_egyiranyu and r["ratio"] >= 0.5:
        return f"{irany}, de osszevissza (egyirany {r['consistency']:.0%})"
    if r["ratio"] >= 0.9:
        return f"MINDJART JELZES! {irany}, meg {hiany} hianyzik"
    if r["ratio"] >= 0.6:
        return f"erosen {irany}, meg {hiany} hianyzik a jelzeshez"
    if r["ratio"] >= 0.3:
        return f"{irany}, de meg messze van a jelzestol"
    return "alig mozdul"



