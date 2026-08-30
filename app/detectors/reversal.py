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

from .. import events, binance_rest
from ..fmt import pad, price as fprice, money
from .base import Detector, make_signal

log = logging.getLogger("reversal")

MAX_FLOW_RATIO = 99.0    # az egyoldalu flow aranyanak felso korlatja


class Setup:
    """Egy symbol eppen fejlodo fordulo-alakzata."""

    __slots__ = ("side", "extreme", "extreme_ts", "move_pct", "origin", "peak", "micro")

    def __init__(self, side, extreme, extreme_ts, move_pct, origin):
        self.side = side               # "LONG" (melypont utan) vagy "SHORT" (csucs utan)
        self.extreme = extreme         # a lokalis minimum / maximum
        self.extreme_ts = extreme_ts
        self.move_pct = move_pct       # az idaig tarto mozgas merteke, szazalekban
        self.origin = origin           # ahonnan a mozgas indult (a masik vegpont)
        self.peak = extreme            # a szelsoertek ota elert legjobb ar
        self.micro = None              # a rogzitett micro-high / micro-low

    def retracement(self, price):
        """A jelzes pillanataban az elozo mozgas hany szazaleka jott mar vissza."""
        teljes = abs(self.origin - self.extreme)
        return abs(price - self.extreme) / teljes * 100.0 if teljes else 100.0


class ReversalDetector(Detector):
    name = "reversal"
    config_key = "reversal"

    def __init__(self, cfg):
        self.cfg = cfg
        self.trades = defaultdict(deque)   # symbol -> deque[Trade]
        self.setups = {}                   # symbol -> Setup
        self.last_signal = {}              # symbol -> ts
        self.total_signals = 0
        self.last_ts = 0.0                 # az utolso feldolgozott trade tozsdei ideje

    # ------------------------------------------------------------------ fo utvonal

    def on_trade(self, trade):
        c = self.cfg.reversal
        self.last_ts = trade.ts
        w = self.trades[trade.symbol]
        w.append(trade)
        while w and w[0].ts < trade.ts - c["windowSeconds"]:
            w.popleft()
        if len(w) < c["minTradesInFlowWindow"]:
            return None

        setup = self._track_setup(trade, w, c)
        if setup is None or setup.micro is None:
            return None

        # A szelsoertek legyen friss: egy 15 masodperces melypontra mar nincs ertelme
        # beszallni, a mozgas nagy resze lefutott.
        kor = trade.ts - setup.extreme_ts
        if kor > c["maxExtremeAgeSec"]:
            return None

        # EZ A LENYEG: ha az ar mar visszatette az elozo mozgas nagy reszet, akkor a
        # kereskedheto resz elfogyott. Enelkul a rendszer a dead-cat bounce tetejen
        # szallt be, es utana rendszeresen visszaesett az ar.
        retrace = setup.retracement(trade.price)
        if retrace > c["maxRetracementPct"]:
            return None

        # 6. attores a rogzitett micro szinten, a mozgas aranyaban mert merettel.
        # Egy 0.02%-os "attores" egy 1.3%-os mozgas utan nem informacio, csak zaj.
        tav = abs(setup.origin - setup.extreme)
        kell_break = tav * c["breakOfMovePct"] / 100.0
        if setup.side == "LONG":
            if trade.price <= setup.micro + kell_break:
                return None
        elif trade.price >= setup.micro - kell_break:
            return None
        break_pct = abs(trade.price - setup.micro) / setup.micro * 100.0

        # 5. a trade flow a megfelelo oldal fele fordult
        flow = self._flow(w, trade.ts, c, trade.symbol)
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

        return self._signal(trade, setup, flow, break_pct, retrace, kor, c, w)

    # ------------------------------------------------------------------ allapotgep

    def _track_setup(self, trade, window, c):
        """1-4. lepes: lemozgas, szelsoertek, visszapattanas, micro szint rogzitese."""
        symbol = trade.symbol
        setup = self.setups.get(symbol)

        # elavult setup eldobasa
        if setup and trade.ts - setup.extreme_ts > c["maxSetupAgeSec"]:
            setup = None
            self.setups.pop(symbol, None)

        # Uj, erdemben melyebb minimum (vagy magasabb maximum) -> az alakzat ujraindul.
        # A turest a mozgas aranyaban merjuk: egy abszolut szazalek egy kis mozgasnal
        # tul nagy lenne, es a valodi szelsoertek sosem frissulne.
        if setup:
            tures = abs(setup.origin - setup.extreme) * c["newExtremeOfMovePct"] / 100.0
            if setup.side == "LONG" and trade.price < setup.extreme - tures:
                setup = None
            elif setup.side == "SHORT" and trade.price > setup.extreme + tures:
                setup = None

        if setup is None:
            setup = self._find_setup(window, c)
            if setup is None:
                self.setups.pop(symbol, None)
                return None
            self.setups[symbol] = setup

        # 3-4. visszapattanas, majd a csucs rogzitese micro szintkent.
        #
        # Minden meret a MOZGAS ARANYABAN ertendo, nem abszolut szazalekban -- igy
        # egy 0.5%-os es egy 3%-os mozgasnal ugyanaz a logika mukodik.
        #
        # A visszapattanast a MAR ELERT csucshoz merjuk, nem a pillanatnyi arhoz:
        # kulonben a visszahuzas (ami eppen a micro szint rogzitesehez kell) kilokne
        # az alakzatot, es a micro szint csak NAGY visszapattanasoknal rogzulne --
        # ez okozta, hogy a rendszer szisztematikusan keson jelzett.
        tav = abs(setup.origin - setup.extreme)          # a mozgas teljes hossza arban
        kell_bounce = tav * c["bounceOfMovePct"] / 100.0
        hosszu = setup.side == "LONG"

        setup.peak = max(setup.peak, trade.price) if hosszu else min(setup.peak, trade.price)
        visszapattant = abs(setup.peak - setup.extreme)
        if visszapattant < kell_bounce:
            return setup                                 # meg nem pattant vissza eleget

        if setup.micro is None:
            kell_pullback = visszapattant * c["pullbackOfBouncePct"] / 100.0
            huzott = abs(setup.peak - trade.price)
            if huzott >= kell_pullback:
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
                return Setup("LONG", lo.price, lo.ts, drop, hi.price)
        # SHORT jelolt: a maximum a minimum UTAN keletkezett -> felfele mozgas
        if hi.ts > lo.ts and lo.price > 0:
            rise = (hi.price - lo.price) / lo.price * 100.0
            if rise >= min_move:
                return Setup("SHORT", hi.price, hi.ts, rise, lo.price)
        return None

    @staticmethod
    def _flow(window, now, c, symbol=None):
        """5. lepes: veteli / eladoi oldal aranya az utolso par masodpercben.

        Quote (USDT) mennyiseggel szamolunk, nem darabszammal. Az arany onmagaban
        nem eleg: par szaz USDT-bol is kijon egy 1.9x, ezert megkoveteljuk, hogy az
        ablakban legalabb annyi forgalom legyen, amennyi a par atlaga ennyi ido alatt.
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

        total = buy + sell
        vol24 = binance_rest.SYMBOL_VOLUME.get(symbol) if symbol else None
        elvart = (vol24 / 86400.0 * c["flowWindowSeconds"] * c["minFlowVolumeFactor"]
                  if vol24 else 0.0)
        if total < elvart:
            return None
        buy_dominant = buy >= sell
        strong, weak = (buy, sell) if buy_dominant else (sell, buy)
        # a masik oldal lehet pontosan nulla -- a vegtelen aranyt korlatozzuk,
        # kulonben inf kerulne a Mongo-ba es a score szamitasba
        ratio = min(strong / weak, MAX_FLOW_RATIO) if weak > 0 else MAX_FLOW_RATIO
        return {"buy": buy, "sell": sell, "total": total, "expected": elvart,
                "ratio": ratio, "buyDominant": buy_dominant, "trades": count}

    # ------------------------------------------------------------------ signal

    def _signal(self, trade, setup, flow, break_pct, retrace, kor, c, window):
        direction = setup.side
        bounce_pct = abs(trade.price - setup.extreme) / setup.extreme * 100.0
        szint = "melypont" if direction == "LONG" else "csucs"
        micro_nev = "micro-high" if direction == "LONG" else "micro-low"
        oldal = "veteli" if direction == "LONG" else "eladoi"

        fordulo = "FORDULO FELFELE -> LONG" if direction == "LONG" else \
                  "FORDULO LEFELE -> SHORT"
        log.warning("[%s] %s | %s %.8g (%.1f mp-e) | mozgas %.2f%% | "
                    "visszapattanas %.2f%% | %s attores %.2f%% | flow %.1fx",
                    trade.symbol, fordulo, szint, setup.extreme,
                    trade.ts - setup.extreme_ts, setup.move_pct, bounce_pct,
                    micro_nev, break_pct, flow["ratio"])
        events.add(f"{trade.symbol:<14} {fordulo:<24} "
                   f"{szint} {fprice(setup.extreme)}  mozgas {setup.move_pct:.2f}%  "
                   f"flow {flow['ratio']:.1f}x")

        return make_signal(
            self.name, self.config_key, trade.symbol, direction, trade.price, trade.ts,
            strength=min(setup.move_pct / c["minMovePct"],
                         flow["ratio"] / c["minFlowRatio"]),
            # eros attores: a mozgas kettoszeres attores-kuszobet is meghaladja
            accelerating=(abs(trade.price - setup.micro)
                          >= 2 * abs(setup.origin - setup.extreme)
                          * c["breakOfMovePct"] / 100.0),
            context_mode="reversal",
            move_pct=bounce_pct + break_pct,
            # a stop a szelsoertek tuloldalara kerul, a cel az elozo mozgas
            # targetRetracementPct szazalekanal van
            stop_anchor=setup.extreme,
            target_anchor=(setup.extreme + (setup.origin - setup.extreme)
                           * c["targetRetracementPct"] / 100.0),
            detail={
                "extreme": setup.extreme,
                "extremeAt": setup.extreme_ts,
                "movePct": round(setup.move_pct, 4),
                "bounceFromExtremePct": round(bounce_pct, 4),
                "retracementPct": round(retrace, 2),
                "extremeAgeSec": round(kor, 2),
                "origin": setup.origin,
                "movePct2": round(bounce_pct + break_pct, 4),
                "microLevel": setup.micro,
                "breakPct": round(break_pct, 4),
                "buyVolume": round(flow["buy"], 2),
                "sellVolume": round(flow["sell"], 2),
                "flowVolume": round(flow["total"], 2),
                "expectedFlowVolume": round(flow["expected"], 2),
                "flowRatio": round(flow["ratio"], 3),
                "tradesInFlow": flow["trades"],
            },
            lines=[
                ("elotte" if direction == "LONG" else "elotte",
                 f"{'eses' if direction == 'LONG' else 'emelkedes'} {setup.move_pct:.2f}%"),
                (szint, f"{fprice(setup.extreme)}   ({kor:.1f} mp-e)"),
                ("visszafordulas", f"{bounce_pct:.2f}%   "
                                   f"(a mozgas {retrace:.0f}%-a, max "
                                   f"{c['maxRetracementPct']:.0f}%)"),
                (f"{micro_nev} attores",
                 f"{fprice(setup.micro)}   "
                 f"({'+' if direction == 'LONG' else '-'}{break_pct:.2f}%)"),
                ("trade flow", f"{flow['ratio']:.1f}x {oldal}   "
                               f"(buy {money(flow['buy'])} / sell {money(flow['sell'])} "
                               f"USDT / {c['flowWindowSeconds']} mp)"),
            ],
            history=[(t.ts, t.price) for t in window],
        )

    # ------------------------------------------------------------------ statusz

    def status_lines(self, top=6):
        """Mindig megmondja, mit csinal eppen: hany paron van adat, hany alakzat
        all, es azok melyik fazisban vannak."""
        c = self.cfg.reversal
        # tozsdei ido, nem helyi ora: a detektor is azzal szamol
        now = self.last_ts or time.time()
        eleg_adat = sum(1 for w in self.trades.values()
                        if len(w) >= c["minTradesInFlowWindow"])

        fazisok = {"visszapattanasra var": 0, "micro szintre var": 0, "attoresre var": 0}
        rows = []
        for symbol, st in self.setups.items():
            if now - st.extreme_ts > c["maxSetupAgeSec"]:
                continue
            if st.micro is not None:
                fazis = "attoresre var"
            elif st.peak != st.extreme:
                fazis = "micro szintre var"
            else:
                fazis = "visszapattanasra var"
            fazisok[fazis] += 1
            rows.append((st.move_pct, symbol, st, fazis))

        fej = (f"  REVERSAL FIGYELO   {eleg_adat} paron van eleg adat   "
               f"{len(rows)} alakzat all   jelzes indulas ota: {self.total_signals}")
        felt = (f"    kell hozza: {c['minMovePct']:.2f}% elozetes mozgas, majd a mozgas "
                f"{c['bounceOfMovePct']:.0f}%-ig visszapattanas, "
                f"{c['pullbackOfBouncePct']:.0f}% visszahuzas, "
                f"{c['minFlowRatio']:.1f}x flow es attores -- de a belepes a mozgas "
                f"{c['maxRetracementPct']:.0f}%-an belul")
        if not rows:
            return [fej, felt, "    -- eppen egyetlen par sem all fordulo-alakzatban --"]

        allapot = "    ".join(f"{n}: {db}" for n, db in fazisok.items() if db)
        out = [fej, felt, f"    fazisok:  {allapot}"]
        rows.sort(reverse=True, key=lambda r: r[0])
        for _, symbol, st, fazis in rows[:top]:
            szint = "LOW " if st.side == "LONG" else "HIGH"
            flow = self._flow(self.trades[symbol], now, c, symbol)
            kell_vetel = st.side == "LONG"
            if flow is None:
                flow_txt, flow_jo = "n/a", False
            else:
                oldal = "vetel" if flow["buyDominant"] else "elado"
                flow_txt = f"{flow['ratio']:.1f}x {oldal}"
                flow_jo = (flow["buyDominant"] == kell_vetel
                           and flow["ratio"] >= c["minFlowRatio"])
            if st.micro is None:
                kell = f"kell: {fazis}"
            elif not flow_jo:
                kell = (f"kell: {c['minFlowRatio']:.1f}x "
                        f"{'veteli' if kell_vetel else 'eladoi'} flow, majd attores")
            else:
                irany = ">" if st.side == "LONG" else "<"
                kell = f"kell: attores, ar {irany} {fprice(st.micro)}"
            out.append(f"    {pad(symbol, 14)}{szint} {fprice(st.extreme):>13}  "
                       f"mozgas {st.move_pct:5.2f}%  "
                       f"micro {(fprice(st.micro) if st.micro else '-'):>12}  "
                       f"flow {flow_txt:<13} {kell}")
        return out
