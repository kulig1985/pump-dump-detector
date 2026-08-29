"""MovementDetector -- masodperces skalaju hirtelen armozgas detektalas.

Symbolonkent memoriaban tartjuk az utolso ~6 masodperc arait, es minden uj tick-nel
kiszamoljuk az 1s / 3s / 5s valtozast. Nem varunk gyertyazarasra.
"""
import logging
from collections import deque, defaultdict

log = logging.getLogger("detector")

WINDOWS = (1, 3, 5)          # masodperc
HISTORY_SEC = max(WINDOWS) + 1


class MovementDetector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.history = defaultdict(deque)   # symbol -> deque[(ts, price)]
        self.last_trigger = {}              # symbol -> ts

    def on_price(self, symbol, price, ts):
        """Uj ar. Visszaad egy trigger dictet, vagy None-t."""
        h = self.history[symbol]
        h.append((ts, price))
        while h and h[0][0] < ts - HISTORY_SEC:
            h.popleft()

        changes = {w: self._change(h, ts, w, price) for w in WINDOWS}

        c = self.cfg.detector
        thresholds = {
            1: c["priceChangeThreshold1s"],
            3: c["priceChangeThreshold3s"],
            5: c["priceChangeThreshold5s"],
        }
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

        log.warning("[%s] TRIGGER %s | 1s %s | 3s %s | 5s %s", symbol, direction,
                    _pct(changes[1]), _pct(changes[3]), _pct(changes[5]))
        return {
            "symbol": symbol,
            "direction": direction,
            "price": price,
            "timestamp": ts,
            "changes": changes,
            "history": list(h),
        }

    @staticmethod
    def _change(history, now, window, price):
        """Szazalekos valtozas a window masodperccel ezelotti arhoz kepest.

        None, ha meg nincs eleg elozmeny (kulonben az elso tickek hamis jelet adnanak).
        """
        start = now - window
        if not history or history[0][0] > start:
            return None
        ref = None
        for ts, p in history:          # ponytail: linearis keres, a deque max par szaz elem
            if ts > start:
                break
            ref = p
        if not ref:
            return None
        return (price - ref) / ref * 100.0


def _pct(v):
    return "n/a" if v is None else f"{v:+.2f}%"
