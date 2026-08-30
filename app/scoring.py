"""Signal score 0-100. Itt lehet hangolni a detektorok "izleset".

  mozgas / bizonyitek   max 40   mennyivel lepte tul a sajat kuszobet (strength)
  gyorsulas             max 10   gyorsul-e a mozgas
  EMA                   max 25   kontextus -- a jelentese detektoronkent mas
  order book            max 25   kontextus -- a jelentese detektoronkent mas

A ket utolso resz `contextMode` szerint agazik:

  "momentum" (pump/dump):  a trend TAMOGASSA az iranyt, es ne legyen wall elottunk.
  "reversal":              a trend meg a regi iranyba mutat -- ez normalis, nem hiba.
                           Az szamit, hogy az ar visszavette-e az EMA9-et, es hogy
                           van-e TAMASZ a szelsoertek mogott.
"""


def score_signal(sig, ob, ta, cfg):
    reasons = []
    parts = {}
    long_ = sig["direction"] == "LONG"
    reversal = sig.get("contextMode") == "reversal"

    # --- 1. a detektor sajat bizonyiteka ---
    strength = sig["strength"]
    parts["movement"] = min(40.0, 20.0 * strength)      # 1x kuszob = 20, 2x = 40
    reasons.append(f"{'bizonyitek' if reversal else 'mozgas'} {strength:.1f}x kuszob")

    # --- 2. gyorsulas ---
    parts["acceleration"] = 10.0 if sig["accelerating"] else 0.0
    if sig["accelerating"]:
        reasons.append("eros attores" if reversal else "gyorsulo")

    # --- 3. EMA kontextus ---
    if ta is None:
        parts["ema"] = 10.0
        reasons.append("EMA n/a")
    elif reversal:
        # fordulonal a trend meg szinte biztosan a regi irany -- az szamit,
        # hogy az ar mar visszavette-e az EMA9-et
        reclaimed = ta["aboveFast"] == long_
        turned = ta["trend"] == ("bullish" if long_ else "bearish")
        parts["ema"] = 25.0 if (reclaimed and turned) else 18.0 if reclaimed else 5.0
        if reclaimed:
            reasons.append("ar visszavette az EMA9-et" if long_
                           else "ar az EMA9 ala esett")
        else:
            reasons.append("ar meg az EMA9 rossz oldalan")
    else:
        want = "bullish" if long_ else "bearish"
        if ta["trend"] == want:
            parts["ema"] = 25.0 if ta["aboveFast"] == long_ else 18.0
            reasons.append(f"EMA {ta['trend']}")
        else:
            parts["ema"] = 0.0
            reasons.append(f"EMA ellentetes ({ta['trend']})")

    # --- 4. order book kontextus ---
    if ob is None:
        parts["orderbook"] = 10.0
        reasons.append("order book n/a")
    elif reversal:
        # a szelsoertek mogotti wall tamasz: LONG fordulonal a buy wall alattunk
        support = ob["nearestBuyWall"] if long_ else ob["nearestSellWall"]
        obstacle = ob["nearestSellWall"] if long_ else ob["nearestBuyWall"]
        pontok = 12.0
        if support:
            pontok += 13.0
            reasons.append(f"tamasz {support['distancePct']:.2f}%-ra")
        if obstacle:
            closeness = obstacle["distancePct"] / cfg["wallMaxDistancePct"]
            pontok *= min(1.0, closeness)
            reasons.append(f"wall {obstacle['distancePct']:.2f}%-ra")
        parts["orderbook"] = round(min(25.0, pontok), 1)
    else:
        obstacle = ob["obstacleAhead"]
        if obstacle is None:
            parts["orderbook"] = 25.0
            reasons.append("nincs wall elottunk")
        else:
            closeness = obstacle["distancePct"] / cfg["wallMaxDistancePct"]
            parts["orderbook"] = round(25.0 * min(1.0, closeness), 1)
            reasons.append(f"wall {obstacle['distancePct']:.2f}%-ra")
        if ob["liquidityRatio"] is not None and ob["liquidityRatio"] < 0.7:
            reasons.append("vekony likviditas elottunk")

    total = int(round(sum(parts.values())))
    return min(100, total), ", ".join(reasons), parts
