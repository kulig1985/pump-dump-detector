"""Mi tortent a jelzes UTAN?

Enelkul nincs mihez merni a hangolast: a "szemre a fele hamis" becsles marad.
Minden mentett jelzes utan par percig figyeljuk az arat, es visszairjuk a
signals dokumentumba, hogy merre ment.

  mfe   max favorable excursion -- a legjobb elmozdulas a jelzes iranyaba
  mae   max adverse excursion   -- a legrosszabb elmozdulas ellenirányba
  good  elerte-e a celt, mielott a stopot utotte volna

Nem szimulal kereskedest, csak arat mer.
"""
import asyncio
import logging

from . import prices

log = logging.getLogger("outcome")


async def track(db, signal_id, signal, cfg):
    """Figyeli az arat, majd frissiti a signals dokumentumot. Sose dob kivetelt."""
    try:
        await _track(db, signal_id, signal, cfg)
    except Exception as e:
        log.warning("[%s] eredmenymeres hiba: %s", signal.get("symbol"), e)


async def _track(db, signal_id, signal, cfg):
    symbol = signal["symbol"]
    entry = signal["price"]
    hosszu = signal["direction"] == "LONG"
    perc = cfg["outcomeMinutes"]
    cel, stop = cfg["outcomeTargetPct"], cfg["outcomeStopPct"]

    mfe = mae = 0.0
    eredmeny = None                 # "cel" vagy "stop", amelyiket elobb eri el
    checkpointok = {}
    mintak = int(perc * 60)

    for i in range(1, mintak + 1):
        await asyncio.sleep(1)
        ar = prices.LAST.get(symbol)
        if ar is None:
            continue
        # elojeles elmozdulas a jelzes iranyaba
        valtozas = (ar - entry) / entry * 100.0 * (1 if hosszu else -1)
        mfe = max(mfe, valtozas)
        mae = min(mae, valtozas)
        if eredmeny is None:
            if valtozas >= cel:
                eredmeny = "cel"
            elif valtozas <= -stop:
                eredmeny = "stop"
        if i in (60, 180, 300):
            checkpointok[f"m{i // 60}"] = round(valtozas, 4)

    vege = prices.LAST.get(symbol, entry)
    zaras = (vege - entry) / entry * 100.0 * (1 if hosszu else -1)
    outcome = {
        "measuredMinutes": perc,
        "mfePct": round(mfe, 4),
        "maePct": round(mae, 4),
        "closePct": round(zaras, 4),
        "checkpoints": checkpointok,
        "result": eredmeny or "semleges",
        "good": eredmeny == "cel",
    }
    await db.signals.update_one({"_id": signal_id}, {"$set": {"outcome": outcome}})

    jel = "JO" if outcome["good"] else ("ROSSZ" if eredmeny == "stop" else "semleges")
    log.info("[%s] %s jelzes eredmenye %d perc utan: %s | zaras %+.2f%% | "
             "legjobb %+.2f%% | legrosszabb %+.2f%%",
             symbol, signal["detector"], perc, jel, zaras, mfe, mae)


async def summary_loop(db, cfg, interval=600):
    """Idonkent osszesitest ir a logba: tenylegesen mennyi jott be."""
    while True:
        await asyncio.sleep(interval)
        try:
            await log_summary(db, cfg)
        except Exception as e:
            log.warning("osszesites sikertelen: %s", e)


async def log_summary(db, cfg):
    """Detektoronkent es score savonkent: hany jelzes, mennyi lett jo."""
    sorok = await db.signals.aggregate([
        {"$match": {"outcome": {"$exists": True}}},
        {"$group": {
            "_id": {"detector": "$detector",
                    "sav": {"$multiply": [{"$floor": {"$divide": ["$score", 10]}}, 10]}},
            "db": {"$sum": 1},
            "jo": {"$sum": {"$cond": ["$outcome.good", 1, 0]}},
            "atlagZaras": {"$avg": "$outcome.closePct"},
            "atlagLegjobb": {"$avg": "$outcome.mfePct"},
            "atlagLegrosszabb": {"$avg": "$outcome.maePct"},
        }},
        {"$sort": {"_id.detector": 1, "_id.sav": 1}},
    ]).to_list(length=100)

    if not sorok:
        log.info("EREDMENYEK: meg nincs lemert jelzes "
                 "(%d perccel a jelzes utan zarul le egy meres)", cfg["outcomeMinutes"])
        return

    out = ["", "  " + "─" * 92,
           "  EREDMENYEK -- mi tortent a jelzesek utan "
           f"{cfg['outcomeMinutes']} percben "
           f"(cel +{cfg['outcomeTargetPct']}%, stop -{cfg['outcomeStopPct']}%)",
           "  " + "─" * 92,
           f"  {'detektor':<12}{'score sav':>11}{'darab':>8}{'talalat':>10}"
           f"{'atlag zaras':>14}{'atlag legjobb':>16}{'atlag legrosszabb':>20}"]
    for r in sorok:
        sav_also = int(r["_id"]["sav"])
        sav = f"{sav_also}-{sav_also + 9}"
        talalat = f"{r['jo'] / r['db']:.0%}"
        out.append(f"  {str(r['_id']['detector']):<12}{sav:>11}{r['db']:>8}{talalat:>10}"
                   f"{r['atlagZaras']:>13.2f}%{r['atlagLegjobb']:>15.2f}%"
                   f"{r['atlagLegrosszabb']:>19.2f}%")
    out.append("  " + "─" * 92)
    log.info("\n".join(out))
