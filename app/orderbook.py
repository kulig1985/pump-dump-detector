"""Order book kiertekeles -- tiszta fuggvenyek, halozat nelkul.

Az adatot a BookCache adja (folyamatos <symbol>@depth20 stream), itt csak
ertelmezzuk. A wall RELATIV: egy arszint akkor fal, ha a rajta levo notional
legalabb `sensitivity`-szerese a tobbi szintnek, es kozel van az arhoz.
"""
import statistics


def find_wall(side, price, sensitivity, max_dist_pct):
    """A legkozelebbi arszint, ami kiugroan nagy a tobbi szinthez kepest.

    A viszonyitasi alap a MEDIAN, nem az atlag: az atlagba a fal maga is beleszamit,
    es 20 szint mellett egy 10x akkora fal ~45%-kal emeli az atlagot -- vagyis a
    sajat aranyat higitja fel, es alabecsult erteket kapnank.

    A LEGJOBB szint (a touch) kimarad: ott lepsz be, az nem akadaly. A BTC-n a touch
    sokszorosa a tobbi szintnek, es igy minden jelzest "fal" miatt dobtunk volna el.
    """
    side = side[1:]
    if not side:
        return None
    notionals = [p * q for p, q in side]
    alap = statistics.median(notionals)
    if alap <= 0:
        return None
    for (p, q), notional in zip(side, notionals):    # a lista ar szerint rendezett
        dist = abs(p - price) / price * 100.0
        if dist > max_dist_pct:
            break
        if notional >= sensitivity * alap:
            return {"price": p, "distancePct": round(dist, 3),
                    "notional": round(notional, 2), "ratio": round(notional / alap, 2)}
    return None


def liquidity(side, price, max_dist_pct):
    """Osszes notional a megadott tavolsagon belul."""
    total = 0.0
    for p, q in side:
        if abs(p - price) / price * 100.0 > max_dist_pct:
            break
        total += p * q
    return total
