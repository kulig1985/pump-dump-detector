"""DetectorManager -- minden trade-et vegigad az engedelyezett detektorokon.

Elotte azonban lefut a kereskedhetosegi szuro: ha egy paron a spread tul szeles,
a konyv vekony, vagy alig van kotes, a detektorok oda sem jutnak el.
"""
import logging

log = logging.getLogger("detectors")


class DetectorManager:
    def __init__(self, cfg, detectors, eligibility):
        self.cfg = cfg
        self.detectors = detectors
        self.eligibility = eligibility
        self.ticks = 0
        self.skipped = 0
        self.total_candidates = 0
        self._broken = set()

    def enabled(self, detector):
        return getattr(self.cfg, detector.config_key, {}).get("enabled", True)

    def on_trade(self, trade):
        """Az osszes detektor CANDIDATE-je erre a trade-re (altalaban ures lista)."""
        self.ticks += 1
        self.eligibility.on_trade(trade)

        # A szuro NEM a detektor elott all meg, hanem a jelzes kiadasanal. Igy a
        # baseline minden figyelt paron epul, es amint egy par kereskedhetove valik,
        # azonnal kesz allapotbol indul -- nem nullarol. (40 par mellett a detektorok
        # futtatasa elhanyagolhato koltseg.)
        mehet, kizaras_oka, _ = self.eligibility.check(trade.symbol)

        candidates = []
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
            if not sig:
                continue
            if not mehet:
                # a detektor allapota epult, de jelzest nem adunk ki
                self.skipped += 1
                log.info("REJECTED   %-14s %-5s %s", trade.symbol,
                         sig.get("direction", ""), kizaras_oka)
                continue
            self.total_candidates += 1
            candidates.append(sig)
        return candidates

    def debug_lines(self):
        """Reszletes detektor-allapot -- csak DEBUG szinten."""
        out = []
        for d in self.detectors:
            if not self.enabled(d):
                continue
            try:
                sorok = d.status_lines()
            except Exception as e:
                sorok = [f"  statusz hiba: {e}"]
            if sorok:
                out.append(f"[{d.name}]")
                out.extend(sorok)
        return out

    def take_ticks(self):
        n, self.ticks = self.ticks, 0
        return n
