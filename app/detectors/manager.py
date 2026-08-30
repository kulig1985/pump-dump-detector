"""DetectorManager -- minden trade-et vegigad az engedelyezett detektorokon.

Egyetlen dolga, hogy a market data es a detektorok kozott alljon: nem ertelmezi
a jelzeseket, nem ment, nem ertesit.
"""
import logging

from ..quality import SymbolQuality

log = logging.getLogger("detectors")


class DetectorManager:
    def __init__(self, cfg, detectors):
        self.cfg = cfg
        self.detectors = detectors
        self.quality = SymbolQuality(cfg)
        self.skipped = 0
        self.ticks = 0
        self.total_signals = 0
        self._broken = set()      # amelyik detektor mar dobott hibat (ne spammeljunk)

    def enabled(self, detector):
        return getattr(self.cfg, detector.config_key, {}).get("enabled", True)

    def on_trade(self, trade):
        """Visszaadja az osszes detektor jelzeset erre a trade-re (altalaban ures lista)."""
        self.ticks += 1
        self.quality.on_trade(trade)

        # A szaggatott, ossze-vissza ugralo parokra egyik detektor sem jelez:
        # ott nincs mit megfogni, csak zajra jonne a jelzes.
        if self.cfg.detector["minEfficiency"]:
            mehet, _ = self.quality.tradeable(trade.symbol)
            if not mehet:
                self.skipped += 1
                return []

        signals = []
        for d in self.detectors:
            if not self.enabled(d):
                continue
            try:
                sig = d.on_trade(trade)
            except Exception as e:
                # egy hibas detektor nem allithatja meg a streamet, es a tobbi
                # detektor jelzeset sem nyelheti el
                if d.name not in self._broken:
                    self._broken.add(d.name)
                    log.exception("[%s] a(z) %s detektor hibat dobott: %s",
                                  trade.symbol, d.name, e)
                continue
            if sig:
                self.total_signals += 1
                signals.append(sig)
        return signals

    def status_lines(self):
        """A detektorok sajat blokkjai a statusz tablahoz, egymas ala fuzve."""
        out = list(self.quality.blocked_summary())
        if out:
            out.append("")
        for d in self.detectors:
            if not self.enabled(d):
                out.append(f"  {d.name.upper()}  kikapcsolva "
                           f"(config: {d.config_key}.enabled)")
                continue
            try:
                lines = d.status_lines()
            except Exception as e:
                lines = [f"  {d.name.upper()}  statusz hiba: {e}"]
            if lines:
                if out:
                    out.append("")
                out.extend(lines)
        return out

    def take_ticks(self):
        n, self.ticks = self.ticks, 0
        return n
