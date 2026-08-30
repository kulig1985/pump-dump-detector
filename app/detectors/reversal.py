"""ReversalDetector -- rovid tavu lokalis arfordulok felismerese.

Nem gyertyakbol dolgozik, hanem symbolonkent egy nehany masodperces rolling
aggTrade ablakbol. Nincs benne EMA / RSI / MACD trigger.

LONG_REVERSAL esemenysor (a SHORT pontos tukorkepe):

    LEMOZGAS  ->  LOKALIS MINIMUM  ->  VISSZAPATTANAS  ->  NINCS UJ MINIMUM
              ->  MICRO-HIGH ROGZUL  ->  VETELI FLOW  ->  MICRO-HIGH ATTORES

Minden kuszob a MongoDB "reversal" config dokumentumabol jon.
"""
import time
import logging
from collections import deque, defaultdict

from .. import events
from ..fmt import pad, price as fprice, money
from .base import Detector, make_signal

log = logging.getLogger("reversal")

MAX_FLOW_RATIO = 99.0    # az egyoldalu flow aranyanak felso korlatja


class Setup:
    """Egy symbol eppen fejlodo fordulo-alakzata."""

    __slots__ = ("side", "extreme", "extreme_ts", "move_pct", "peak", "micro")

    def __init__(self, side, extreme, extreme_ts, move_pct):
        self.side = side               # "LONG" (melypont utan) vagy "SHORT" (csucs utan)
        self.extreme = extreme         # a lokalis minimum / maximum
        self.extreme_ts = extreme_ts
        self.move_pct = move_pct       # az idaig tarto mozgas merteke, szazalekban
        self.peak = extreme            # a szelsoertek ota elert legjobb ar
        self.micro = None              # a rogzitett micro-high / micro-low


class ReversalDetector(Detector):
    name = "reversal"
    config_key = "reversal"

    def __init__(self, cfg):
        self.cfg = cfg
        self.trades = defaultdict(deque)   # symbol -> deque[Trade]
        self.setups = {}                   # symbol -> Setup
        self.last_signal = {}              # symbol -> ts
        self.total_signals = 0

    # ------------------------------------------------------------------ fo utvonal

    def on_trade(self, trade):
        c = self.cfg.reversal
        w = self.trades[trade.symbol]
        w.append(trade)
        while w and w[0].ts < trade.ts - c["windowSeconds"]:
            w.popleft()
        if len(w) < c["minTradesInFlowWindow"]:
            return None

        setup = self._track_setup(trade, w, c)
        if setup is None or setup.micro is None:
            return None

        # 6. attores a rogzitett micro szinten
        tol = c["breakTolerancePct"] / 100.0
        if setup.side == "LONG":
            level = setup.micro * (1 + tol)
            if trade.price <= level:
                return None
            break_pct = (trade.price - setup.micro) / setup.micro * 100.0
        else:
            level = setup.micro * (1 - tol)
            if trade.price >= level:
                return None
            break_pct = (setup.micro - trade.price) / setup.micro * 100.0

        # 5. a trade flow a megfelelo oldal fele fordult
        flow = self._flow(w, trade.ts, c)
        if flow is None or flow["ratio"] < c["minFlowRatio"]:
            return None

        want_buy = setup.side == "LONG"
        if flow["buyDominant"] != want_buy:
            return None

        if trade.ts - self.last_signal.get(trade.symbol, 0) < c["cooldownSec"]:
            return None
        self.last_signal[trade.symbol] = trade.ts
        self.total_signals += 1
        self.setups.pop(trade.symbol, None)

        return self._signal(trade, setup, flow, break_pct, c, w)

    # ------------------------------------------------------------------ allapotgep

    def _track_setup(self, trade, window, c):
        """1-4. lepes: lemozgas, szelsoertek, visszapattanas, micro szint rogzitese."""
        symbol = trade.symbol
        setup = self.setups.get(symbol)

        # elavult setup eldobasa
        if setup and trade.ts - setup.extreme_ts > c["maxSetupAgeSec"]:
            setup = None
            self.setups.pop(symbol, None)

        new_extreme_tol = c["newExtremeTolerancePct"] / 100.0

        # uj, erdemben melyebb minimum (vagy magasabb maximum) -> az alakzat ujraindul
        if setup:
            if setup.side == "LONG" and trade.price < setup.extreme * (1 - new_extreme_tol):
                setup = None
            elif setup.side == "SHORT" and trade.price > setup.extreme * (1 + new_extreme_tol):
                setup = None

        if setup is None:
            setup = self._find_setup(window, c)
            if setup is None:
                self.setups.pop(symbol, None)
                return None
            self.setups[symbol] = setup

        # 3. visszapattanas: enelkul nem kezdunk micro szintet kovetni
        bounce = c["bouncePct"] / 100.0
        pullback = c["pullbackPct"] / 100.0
        if setup.side == "LONG":
            if trade.price < setup.extreme * (1 + bounce):
                return setup
            setup.peak = max(setup.peak, trade.price)
            # 4. a csucs akkor rogzul micro-high-kent, ha onnan visszahuzott
            if setup.micro is None and trade.price <= setup.peak * (1 - pullback):
                setup.micro = setup.peak
        else:
            if trade.price > setup.extreme * (1 - bounce):
                return setup
            setup.peak = min(setup.peak, trade.price)
            if setup.micro is None and trade.price >= setup.peak * (1 + pullback):
                setup.micro = setup.peak
        return setup

    @staticmethod
    def _find_setup(window, c):
        """1-2. lepes: volt-e erdemi lemozgas (vagy felmozgas) egy szelsoertekig."""
        lo = min(window, key=lambda t: t.price)
        hi = max(window, key=lambda t: t.price)
        min_move = c["minMovePct"]

        # LONG jelolt: a minimum a maximum UTAN keletkezett -> lefele mozgas
        if lo.ts > hi.ts and lo.price > 0:
            drop = (hi.price - lo.price) / hi.price * 100.0
            if drop >= min_move:
                return Setup("LONG", lo.price, lo.ts, drop)
        # SHORT jelolt: a maximum a minimum UTAN keletkezett -> felfele mozgas
        if hi.ts > lo.ts and lo.price > 0:
            rise = (hi.price - lo.price) / lo.price * 100.0
            if rise >= min_move:
                return Setup("SHORT", hi.price, hi.ts, rise)
        return None

    @staticmethod
    def _flow(window, now, c):
        """5. lepes: veteli / eladoi oldal aranya az utolso par masodpercben.

        Quote (USDT) mennyiseggel szamolunk, nem darabszammal.
        """
        start = now - c["flowWindowSeconds"]
        buy = sell = 0.0
        count = 0
        for t in window:
            if t.ts < start:
                continue
            count += 1
            if t.buy_taker:
                buy += t.price * t.qty
            else:
                sell += t.price * t.qty
        if count < c["minTradesInFlowWindow"] or (buy == 0 and sell == 0):
            return None
        buy_dominant = buy >= sell
        strong, weak = (buy, sell) if buy_dominant else (sell, buy)
        # a masik oldal lehet pontosan nulla -- a vegtelen aranyt korlatozzuk,
        # kulonben inf kerulne a Mongo-ba es a score szamitasba
        ratio = min(strong / weak, MAX_FLOW_RATIO) if weak > 0 else MAX_FLOW_RATIO
        return {"buy": buy, "sell": sell, "ratio": ratio,
                "buyDominant": buy_dominant, "trades": count}

    # ------------------------------------------------------------------ signal

    def _signal(self, trade, setup, flow, break_pct, c, window):
        direction = setup.side
        bounce_pct = abs(trade.price - setup.extreme) / setup.extreme * 100.0
        szint = "melypont" if direction == "LONG" else "csucs"
        micro_nev = "micro-high" if direction == "LONG" else "micro-low"
        oldal = "veteli" if direction == "LONG" else "eladoi"

        log.warning("[%s] %s_REVERSAL | %s %.8g (%.1f mp-e) | mozgas %.2f%% | "
                    "visszapattanas %.2f%% | %s attores %.2f%% | flow %.1fx",
                    trade.symbol, direction, szint, setup.extreme,
                    trade.ts - setup.extreme_ts, setup.move_pct, bounce_pct,
                    micro_nev, break_pct, flow["ratio"])
        events.add(f"{trade.symbol:<14} REVERSAL {direction:<5} "
                   f"{szint} {fprice(setup.extreme)}  mozgas {setup.move_pct:.2f}%  "
                   f"flow {flow['ratio']:.1f}x")

        return make_signal(
            self.name, self.config_key, trade.symbol, direction, trade.price, trade.ts,
            strength=min(setup.move_pct / c["minMovePct"],
                         flow["ratio"] / c["minFlowRatio"]),
            accelerating=break_pct >= 2 * c["breakTolerancePct"],
            context_mode="reversal",
            detail={
                "extreme": setup.extreme,
                "extremeAt": setup.extreme_ts,
                "extremeAgeSec": round(trade.ts - setup.extreme_ts, 2),
                "movePct": round(setup.move_pct, 4),
                "bouncePct": round(bounce_pct, 4),
                "microLevel": setup.micro,
                "breakPct": round(break_pct, 4),
                "buyVolume": round(flow["buy"], 2),
                "sellVolume": round(flow["sell"], 2),
                "flowRatio": round(flow["ratio"], 3),
                "tradesInFlow": flow["trades"],
            },
            lines=[
                f"{'Esés' if direction == 'LONG' else 'Emelkedés'} előtte: "
                f"{setup.move_pct:.2f}%",
                f"{szint.capitalize()}: {fprice(setup.extreme)} "
                f"({trade.ts - setup.extreme_ts:.1f} mp-e)",
                f"Visszafordulás: {bounce_pct:.2f}%",
                f"{micro_nev} áttörés: {fprice(setup.micro)} ({break_pct:+.2f}%)",
                f"Trade flow: {flow['ratio']:.1f}x {oldal} "
                f"(buy {money(flow['buy'])} / sell {money(flow['sell'])} USDT, "
                f"{c['flowWindowSeconds']} mp)",
            ],
            history=[(t.ts, t.price) for t in window],
        )

    # ------------------------------------------------------------------ statusz

    def status_lines(self, top=6):
        """Csak azok a parok, ahol mar all egy alakzat -- es hogy min mulik meg."""
        c = self.cfg.reversal
        now = time.time()
        rows = []
        for symbol, s in self.setups.items():
            if now - s.extreme_ts > c["maxSetupAgeSec"]:
                continue
            rows.append((s.move_pct, symbol, s))
        if not rows:
            return [f"  REVERSAL FIGYELO  nincs eppen fejlodo fordulo-alakzat "
                    f"(kell hozza {c['minMovePct']:.2f}% mozgas)"]

        rows.sort(reverse=True, key=lambda r: r[0])
        out = [f"  REVERSAL FIGYELO  ({len(rows)} par alakzatban, jelzes indulas ota: "
               f"{self.total_signals})"]
        for _, symbol, s in rows[:top]:
            szint = "LOW " if s.side == "LONG" else "HIGH"
            flow = self._flow(self.trades[symbol], now, c)
            flow_txt = f"flow {flow['ratio']:.1f}x" if flow else "flow n/a"
            if s.micro is None:
                kell = f"kell: micro-{'high' if s.side == 'LONG' else 'low'} rogzulese"
            else:
                irany = ">" if s.side == "LONG" else "<"
                kell = (f"kell: flow {c['minFlowRatio']:.1f}x + attores "
                        f"{irany} {fprice(s.micro)}")
            out.append(f"    {pad(symbol, 14)}{szint} {fprice(s.extreme):>13}  "
                       f"mozgas {s.move_pct:5.2f}%  "
                       f"micro {fprice(s.micro) if s.micro else '-':>12}  "
                       f"{flow_txt:<11} {kell}")
        return out
