"""Signal score 0-100.

    mozgas / bizonyitek   max 30   mennyivel lepte tul a sajat kuszobet (strength)
    gyorsulas             max 10   gyorsul-e a mozgas
    EMA                   max 20   kontextus -- a jelentese detektoronkent mas
    order book            max 20   kontextus -- a jelentese detektoronkent mas
    hozam / kockazat      max 20   megeri-e egyaltalan felvenni a poziciot
                                   ------
                                   100

A hozam/kockazat resz nelkul a score ellentmondott a tervnek: egy 67 pontos jelzes
mellett allhatott 0.8:1 arany, vagyis a cel kozelebb volt, mint a stop. Az "erosen
mozog" onmagaban nem jelzes -- csak akkor az, ha van hova mennie.

A ket kontextus resz `contextMode` szerint agazik:

  "momentum" (pump/dump):  a trend TAMOGASSA az iranyt, es ne legyen wall elottunk.
  "reversal":              a trend meg a regi iranyba mutat -- ez normalis, nem hiba.
                           Az szamit, hogy az ar visszavette-e az EMA9-et, es hogy
                           van-e TAMASZ a szelsoertek mogott.
"""

MOZGAS_MAX = 30.0
GYORSULAS_MAX = 10.0
EMA_MAX = 20.0
ORDERBOOK_MAX = 20.0
HOZAM_MAX = 20.0

RR_ALSO = 1.0      # ez alatt 0 pont: a cel kozelebb van, mint a stop
RR_FELSO = 3.0     # ettol felfele teljes pont


def score_signal(sig, ob, ta, cfg, terv=None):
    reasons = []
    parts = {}
    long_ = sig["direction"] == "LONG"
    reversal = sig.get("contextMode") == "reversal"

    # --- 1. a detektor sajat bizonyiteka ---
    strength = sig["strength"]
    parts["movement"] = min(MOZGAS_MAX, MOZGAS_MAX / 2 * strength)   # 1x kuszob = fel pont
    reasons.append(f"{'bizonyitek' if reversal else 'mozgas'} {strength:.1f}x kuszob")

    # --- 2. gyorsulas ---
    parts["acceleration"] = GYORSULAS_MAX if sig["accelerating"] else 0.0
    if sig["accelerating"]:
        reasons.append("eros attores" if reversal else "gyorsulo")

    # --- 3. EMA kontextus ---
    if ta is None:
        parts["ema"] = EMA_MAX * 0.4
        reasons.append("EMA n/a")
    elif reversal:
        # fordulonal a trend meg szinte biztosan a regi irany -- az szamit,
        # hogy az ar mar visszavette-e az EMA9-et
        reclaimed = ta["aboveFast"] == long_
        turned = ta["trend"] == ("bullish" if long_ else "bearish")
        parts["ema"] = (EMA_MAX if (reclaimed and turned)
                        else EMA_MAX * 0.7 if reclaimed else EMA_MAX * 0.2)
        if reclaimed:
            reasons.append("ar visszavette az EMA9-et" if long_
                           else "ar az EMA9 ala esett")
        else:
            reasons.append("ar meg az EMA9 rossz oldalan")
    else:
        want = "bullish" if long_ else "bearish"
        if ta["trend"] == want:
            parts["ema"] = EMA_MAX if ta["aboveFast"] == long_ else EMA_MAX * 0.7
            reasons.append(f"EMA {ta['trend']}")
        else:
            parts["ema"] = 0.0
            reasons.append(f"EMA ellentetes ({ta['trend']})")

    # --- 4. order book kontextus ---
    if ob is None:
        parts["orderbook"] = ORDERBOOK_MAX * 0.4
        reasons.append("order book n/a")
    elif reversal:
        # a szelsoertek mogotti wall tamasz: LONG fordulonal a buy wall alattunk
        support = ob["nearestBuyWall"] if long_ else ob["nearestSellWall"]
        obstacle = ob["nearestSellWall"] if long_ else ob["nearestBuyWall"]
        pontok = ORDERBOOK_MAX * 0.5
        if support:
            pontok += ORDERBOOK_MAX * 0.5
            reasons.append(f"tamasz {support['distancePct']:.2f}%-ra")
        if obstacle:
            pontok *= min(1.0, obstacle["distancePct"] / cfg["wallMaxDistancePct"])
            reasons.append(f"wall {obstacle['distancePct']:.2f}%-ra")
        parts["orderbook"] = round(min(ORDERBOOK_MAX, pontok), 1)
    else:
        obstacle = ob["obstacleAhead"]
        if obstacle is None:
            parts["orderbook"] = ORDERBOOK_MAX
            reasons.append("nincs wall elottunk")
        else:
            closeness = obstacle["distancePct"] / cfg["wallMaxDistancePct"]
            parts["orderbook"] = round(ORDERBOOK_MAX * min(1.0, closeness), 1)
            reasons.append(f"wall {obstacle['distancePct']:.2f}%-ra")
        if ob["liquidityRatio"] is not None and ob["liquidityRatio"] < 0.7:
            reasons.append("vekony likviditas elottunk")

    # --- 5. megeri-e felvenni: hozam / kockazat ---
    if terv is None:
        parts["rewardRisk"] = HOZAM_MAX * 0.4
        reasons.append("terv n/a")
    else:
        rr = terv["rewardRisk"]
        arany = (rr - RR_ALSO) / (RR_FELSO - RR_ALSO)
        parts["rewardRisk"] = round(HOZAM_MAX * max(0.0, min(1.0, arany)), 1)
        reasons.append(f"hozam/kockazat {rr:.1f}:1")

    total = int(round(sum(parts.values())))
    return min(100, total), ", ".join(reasons), parts
