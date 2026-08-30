"""Signal score 0-100. Itt lehet hangolni a detektor "izleset".

  mozgas erossege     max 40   mennyivel lepte tul a kuszoboket
  gyorsulas           max 10   az 1s valtozas gyorsabb-e mint az 5s atlaga
  EMA trend           max 25   tamogatja-e az iranyt
  szabad ut           max 25   van-e wall a mozgas iranyaban, milyen kozel
"""


def score_signal(trigger, ob, ta, cfg):
    reasons = []
    parts = {}

    # --- mozgas erossege: a legjobban tullott ablak aranya a kuszobehez ---
    # a parra ervenyes (volatilitashoz igazitott) kuszobot hasznaljuk, nem a globalist
    thresholds = trigger["thresholds"]
    ratios = [abs(ch) / thresholds[w]
              for w, ch in trigger["changes"].items() if ch is not None]
    best = max(ratios) if ratios else 0.0
    parts["movement"] = min(40.0, 20.0 * best)          # 1x kuszob = 20, 2x = 40
    reasons.append(f"mozgas {best:.1f}x kuszob")

    # --- gyorsulas: az utolso 1s gyorsabb-e, mint az 5s atlagos tempoja ---
    c1, c5 = trigger["changes"].get(1), trigger["changes"].get(5)
    if c1 is not None and c5 is not None and abs(c5) > 0 and abs(c1) > abs(c5) / 5:
        parts["acceleration"] = 10.0
        reasons.append("gyorsulo")
    else:
        parts["acceleration"] = 0.0

    # --- EMA trend ---
    want = "bullish" if trigger["direction"] == "LONG" else "bearish"
    if ta is None:
        parts["ema"] = 10.0                              # ismeretlen: semleges
        reasons.append("EMA n/a")
    elif ta["trend"] == want:
        parts["ema"] = 25.0 if ta["aboveFast"] == (want == "bullish") else 18.0
        reasons.append(f"EMA {ta['trend']}")
    else:
        parts["ema"] = 0.0
        reasons.append(f"EMA ellentetes ({ta['trend']})")

    # --- szabad ut a mozgas iranyaban ---
    if ob is None:
        parts["orderbook"] = 10.0
        reasons.append("order book n/a")
    else:
        obstacle = ob["obstacleAhead"]
        if obstacle is None:
            parts["orderbook"] = 25.0
            reasons.append("nincs wall elottunk")
        else:
            # minel kozelebb a wall, annal kevesebb pont
            closeness = obstacle["distancePct"] / cfg["wallMaxDistancePct"]
            parts["orderbook"] = round(25.0 * min(1.0, closeness), 1)
            reasons.append(f"wall {obstacle['distancePct']:.2f}%-ra")
        if ob["liquidityRatio"] is not None and ob["liquidityRatio"] < 0.7:
            reasons.append("vekony likviditas elottunk")

    total = int(round(sum(parts.values())))
    return min(100, total), ", ".join(reasons), parts
