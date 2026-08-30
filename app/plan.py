"""Kereskedelmi terv a jelzeshez: belepo, cel, stop, hozam/kockazat.

A detektor csak a ket horgonyt adja meg (honnan indult a mozgas, hova tarthat),
a szinteket es az aranyt itt szamoljuk -- igy minden detektornal ugyanugy nez ki,
es a Binance-on azonnal ellenorizheto.
"""


def build(sig, cfg):
    """None, ha a detektor nem adott horgonyokat."""
    stop_anchor = sig.get("stopAnchor")
    target_anchor = sig.get("targetAnchor")
    if not stop_anchor or not target_anchor:
        return None

    belepo = sig["price"]
    hosszu = sig["direction"] == "LONG"
    puffer = cfg["stopBufferPct"] / 100.0

    # a stop a horgon TULOLDALARA kerul, hogy egy pillanatnyi kiszuras ne vigye el
    stop = stop_anchor * (1 - puffer) if hosszu else stop_anchor * (1 + puffer)
    cel = target_anchor

    kockazat = (belepo - stop) if hosszu else (stop - belepo)
    hozam = (cel - belepo) if hosszu else (belepo - cel)
    if kockazat <= 0 or hozam <= 0:
        return None

    return {
        "entry": belepo,
        "target": round(cel, 10),
        "stop": round(stop, 10),
        "targetPct": round(hozam / belepo * 100, 4),
        "stopPct": round(kockazat / belepo * 100, 4),
        "rewardRisk": round(hozam / kockazat, 2),
        "weak": hozam / kockazat < cfg["minRewardRisk"],
        "minRewardRisk": cfg["minRewardRisk"],
    }
