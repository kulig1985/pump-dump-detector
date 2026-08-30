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
from ..links import binance_url
from .base import Detector, make_signal
from .baseline import Baseline

log = logging.getLogger("reversal")

MAX_FLOW_RATIO = 99.0    # az egyoldalu flow aranyanak felso korlatja


class Setup:
    """Egy symbol eppen fejlodo fordulo-alakzata."""

    __slots__ = ("side", "extreme", "extreme_ts", "move_pct", "origin", "origin_ts",
                 "peak", "micro")

    def __init__(self, side, extreme, extreme_ts, move_pct, origin, origin_ts):
        self.side = side               # "LONG" (melypont utan) vagy "SHORT" (csucs utan)
        self.extreme = extreme         # a lokalis minimum / maximum
        self.extreme_ts = extreme_ts
        self.move_pct = move_pct       # az idaig tarto mozgas merteke, szazalekban
        self.origin = origin           # ahonnan a mozgas indult (a masik vegpont)
        self.origin_ts = origin_ts     # es mikor -- ehhez skalazzuk a normal mozgast
        self.peak = extreme            # a szelsoertek ota elert legjobb ar
        self.micro = None              # a rogzitett micro-high / micro-low

    def duration(self):
        """Mennyi ido alatt zajlott le a fordulo elotti mozgas."""
        return max(0.0, self.extreme_ts - self.origin_ts)

    def retracement(self, price):
        """A jelzes pillanataban az elozo mozgas hany szazaleka jott mar vissza."""
        teljes = abs(self.origin - self.extreme)
        return abs(price - self.extreme) / teljes * 100.0 if teljes else 100.0


class ReversalDetector(Detector):
    name = "reversal"
    config_key = "reversal"

    def __init__(self, cfg, baseline=None):
        self.cfg = cfg
        self.baseline = baseline or Baseline(cfg)
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

        # elavult setup eldobasa (ugyanaz a hatarido, mint a jelzesnel)
        if setup and trade.ts - setup.extreme_ts > c["maxExtremeAgeSec"]:
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

    def _find_setup(self, window, c):
        """1-2. lepes: volt-e erdemi lemozgas (vagy felmozgas) egy szelsoertekig.

        "Erdemi" = a par sajat rovid tavu normaljanak baselineRatio-szorosa, es
        legalabb minMovePct. Fix kuszob itt is felrevinne: egy meme coinon a
        0.4%-os mozgas semmi, a BTC-n sok.
        """
        lo = min(window, key=lambda t: t.price)
        hi = max(window, key=lambda t: t.price)
        symbol = window[-1].symbol

        def eleg_nagy(mozgas_pct, hossz_sec):
            """A normalt a mozgas TENYLEGES hosszara skalazva hasonlitjuk.

            A baseline egy 2 masodperces ablakbol keszul; egy 20 masodperces mozgas
            termeszetesen nagyobb (bolyongasnal az ido gyokevel no). Skalazas nelkul
            egy 20 mp-es normal kuszas is "rendkivulinek" latszana.
            """
            # Baseline nelkul nem tudjuk, mi szamit rendkivulinek ezen a paron
            alap = self.baseline.value_for(symbol, hossz_sec)
            if not alap:
                return False
            return mozgas_pct >= max(c["minMovePct"], alap * c["baselineRatio"])

        # LONG jelolt: a minimum a maximum UTAN keletkezett -> lefele mozgas
        if lo.ts > hi.ts and lo.price > 0:
            drop = (hi.price - lo.price) / hi.price * 100.0
            if eleg_nagy(drop, lo.ts - hi.ts):
                return Setup("LONG", lo.price, lo.ts, drop, hi.price, hi.ts)
        # SHORT jelolt: a maximum a minimum UTAN keletkezett -> felfele mozgas
        if hi.ts > lo.ts and lo.price > 0:
            rise = (hi.price - lo.price) / lo.price * 100.0
            if eleg_nagy(rise, hi.ts - lo.ts):
                return Setup("SHORT", hi.price, hi.ts, rise, lo.price, lo.ts)
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
        buy_db = sell_db = 0
        count = 0
        for t in window:
            if t.ts < start:
                continue
            count += 1
            if t.buy_taker:
                buy += t.price * t.qty
                buy_db += 1
            else:
                sell += t.price * t.qty
                sell_db += 1
        if count < c["minTradesInFlowWindow"] or (buy == 0 and sell == 0):
            return None

        total = buy + sell
        buy_dominant = buy >= sell
        # Egyetlen nagy kotes onmagaban ne hozzon letre "fordulast": a domináns
        # oldalnak KOTESSZAMBAN is vezetnie kell, ne csak notionalban.
        if (buy_db > sell_db) != buy_dominant and buy_db != sell_db:
            return None
        strong, weak = (buy, sell) if buy_dominant else (sell, buy)
        # a masik oldal lehet pontosan nulla -- a vegtelen aranyt korlatozzuk,
        # kulonben inf kerulne a Mongo-ba es a score szamitasba
        ratio = min(strong / weak, MAX_FLOW_RATIO) if weak > 0 else MAX_FLOW_RATIO
        return {"buy": buy, "sell": sell, "total": total, "ratio": ratio,
                "buyDominant": buy_dominant, "trades": count,
                "buyTrades": buy_db, "sellTrades": sell_db}

    # ------------------------------------------------------------------ signal

    def _signal(self, trade, setup, flow, break_pct, retrace, kor, c, window):
        direction = setup.side
        bounce_pct = abs(trade.price - setup.extreme) / setup.extreme * 100.0
        szint = "melypont" if direction == "LONG" else "csucs"
        micro_nev = "micro-high" if direction == "LONG" else "micro-low"
        oldal = "veteli" if direction == "LONG" else "eladoi"

        fordulo = "FORDULO FELFELE -> LONG" if direction == "LONG" else \
                  "FORDULO LEFELE -> SHORT"
        log.info("CANDIDATE  %-14s %-5s %s  move %.2f%%  visszafordulas %.0f%%  flow %.1fx",
                 trade.symbol, direction, fordulo, setup.move_pct, retrace, flow["ratio"])
        events.add(f"{trade.symbol:<14} CANDIDATE {direction:<5} {fordulo}  "
                   f"mozgas {setup.move_pct:.2f}%  flow {flow['ratio']:.1f}x")

        return make_signal(
            self.name, self.config_key, trade.symbol, direction, trade.price, trade.ts,
            move_pct=bounce_pct + break_pct,
            reasons=[
                f"{'eses' if direction == 'LONG' else 'emelkedes'} "
                f"{setup.move_pct:.2f}% elotte",
                f"{szint} {fprice(setup.extreme)} ({kor:.1f}s), "
                f"visszafordulas {bounce_pct:.2f}% (a mozgas {retrace:.0f}%-a)",
                f"{micro_nev} attores {fprice(setup.micro)} ({break_pct:+.2f}%)",
                f"kotesaramlas {flow['ratio']:.1f}x {oldal} "
                f"({c['flowWindowSeconds']}s)",
            ],
            metrics={
                "extreme": setup.extreme,
                "extremeAgeSec": round(kor, 2),
                "movePct": round(setup.move_pct, 4),
                "bounceFromExtremePct": round(bounce_pct, 4),
                "retracementPct": round(retrace, 2),
                "microLevel": setup.micro,
                "breakPct": round(break_pct, 4),
                "buyVolume": round(flow["buy"], 2),
                "sellVolume": round(flow["sell"], 2),
                "flowVolume": round(flow["total"], 2),
                "flowRatio": round(flow["ratio"], 3),
                "tradesInFlow": flow["trades"],
                "origin": setup.origin,
            },
            stop_anchor=setup.extreme,
            target_anchor=(setup.extreme + (setup.origin - setup.extreme)
                           * c["targetRetracementPct"] / 100.0),
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

        fazisok = {"meg nem pattant vissza": 0, "micro szint hianyzik": 0,
                   "attoresre var": 0}
        rows = []
        for symbol, st in self.setups.items():
            if now - st.extreme_ts > c["maxExtremeAgeSec"]:
                continue
            if st.micro is not None:
                fazis = "attoresre var"
            elif st.peak != st.extreme:
                fazis = "micro szint hianyzik"
            else:
                fazis = "meg nem pattant vissza"
            fazisok[fazis] += 1
            rows.append((st.move_pct, symbol, st, fazis))

        fej = (f"  FORDULOK   {eleg_adat} paron van eleg adat   "
               f"{len(rows)} alakzat epul   jelzes indulas ota: {self.total_signals}")
        felt = (f"    a jelzeshez kell: {c['minMovePct']:.2f}% elozetes mozgas, "
                f"visszapattanas a mozgas {c['bounceOfMovePct']:.0f}%-aig, majd "
                f"{c['pullbackOfBouncePct']:.0f}% visszahuzas, vegul "
                f"{c['minFlowRatio']:.1f}x kotesaramlas es attores. "
                f"Belepni csak a mozgas {c['maxRetracementPct']:.0f}%-an belul lehet.")
        if not rows:
            return [fej, felt, "    -- eppen egy paron sem epul fordulo --"]

        allapot = "    ".join(f"{n}: {db}" for n, db in fazisok.items() if db)
        out = [fej, felt, f"    hol tartanak:  {allapot}"]
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
                kell = fazis
            elif not flow_jo:
                kell = (f"varunk {c['minFlowRatio']:.1f}x "
                        f"{'veteli' if kell_vetel else 'eladoi'} kotesaramlast, majd attorest")
            else:
                irany = ">" if st.side == "LONG" else "<"
                kell = f"MINDJART -- attores kell, ar {irany} {fprice(st.micro)}"
            out.append(f"    {pad(symbol, 14)}{szint} {fprice(st.extreme):>13}  "
                       f"mozgas {st.move_pct:5.2f}%  "
                       f"micro {(fprice(st.micro) if st.micro else '-'):>12}  "
                       f"aramlas {flow_txt:<13} {kell}")
        return out
