"""TelegramNotifier -- Bot API sendMessage, HTML formazassal."""
import html
import logging

from .links import binance_url

import aiohttp

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"

# detektor + irany -> (emoji, cim, mi tortent, mit jelent).
# A "SHORT REVERSAL" onmagaban ketertelmu volt: olvashato ugy is, hogy egy short
# fordul meg. Ezert kiirjuk, mi tortent es milyen poziciot jelent.
HEADERS = {
    ("pump_dump", "LONG"): (
        "🚨", "PUMP", "hirtelen, egyiranyu emelkedes", "LONG — veteli pozicio"),
    ("pump_dump", "SHORT"): (
        "🔻", "DUMP", "hirtelen, egyiranyu eses", "SHORT — eladasi pozicio"),
    ("reversal", "LONG"): (
        "🟢", "FORDULO FELFELE", "eses utan aljazott es visszapattant",
        "LONG — veteli pozicio"),
    ("reversal", "SHORT"): (
        "🔴", "FORDULO LEFELE", "emelkedes utan tetozott es lefordult",
        "SHORT — eladasi pozicio"),
}


class TelegramNotifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None

    def _chat_id(self, detector):
        """Detektoronkent kulon chat is megadhato; ures ertek eseten a kozos megy."""
        tg = self.cfg.telegram
        return (tg.get("chatIds", {}) or {}).get(detector) or tg.get("chatId")

    async def send(self, symbol, text, detector="pump_dump"):
        """Visszaad {"sent": bool, "error": str|None}. Sose dob kivetelt."""
        tg = self.cfg.telegram
        chat_id = self._chat_id(detector)
        if not self.cfg.detector["telegramEnabled"]:
            log.info("[%s] Telegram kikapcsolva", symbol)
            return {"sent": False, "error": "disabled"}
        if not tg.get("botToken") or not chat_id:
            log.warning("[%s] hianyzik a Telegram token vagy chatId", symbol)
            return {"sent": False, "error": "missing credentials"}

        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self.session.post(
                API.format(token=tg["botToken"]),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
            ) as r:
                body = await r.json()
                if not body.get("ok"):
                    raise RuntimeError(body.get("description", "ismeretlen hiba"))
            log.info("[%s] %s ertesites elkuldve", symbol, detector)
            return {"sent": True, "error": None}
        except Exception as e:
            log.error("[%s] Telegram kuldes sikertelen: %s", symbol, e)
            return {"sent": False, "error": str(e)}

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


def format_signal(sig):
    """Kozos boritek + a detektor sajat (cimke, ertek) sorai, egymas ala igazitva.

    Uj detektornal itt nincs teendo: a detektor a signal "lines" mezojeben adja a
    sajat bizonyitekait, a HEADERS-be pedig legfeljebb egy sort kell felvenni.
    """
    detector = sig.get("detector", "pump_dump")
    direction = sig["direction"]
    emoji, cim, tortent, jelent = HEADERS.get(
        (detector, direction),
        ("⚡", f"{detector}", "", f"{direction} pozicio"))

    url = sig.get("url") or binance_url(sig["symbol"])
    fej = (f"{emoji} <b>{cim}</b>  ·  "
           f"<a href=\"{esc(url)}\"><b>{esc(sig['symbol'])}</b></a>\n"
           f"{esc(tortent)}\n"
           f"➜ <b>{esc(jelent)}</b>\n"
           f"score <b>{sig['score']}/100</b>  ·  "
           f"{sig['timestamp'].strftime('%H:%M:%S')} UTC  ·  {esc(detector)}")

    alap = [("ar", f"{sig['price']:.8g}")]
    if sig.get("quoteVolume24h"):
        alap.append(("24h forgalom", f"{sig['quoteVolume24h'] / 1e6:,.0f}M USDT"))

    kontextus = []
    ema = sig.get("ema")
    if ema:
        hol = "ar az EMA9 felett" if ema.get("aboveFast") else "ar az EMA9 alatt"
        kontextus.append(("EMA", f"{ema['trend']}   ({hol})"))
    else:
        kontextus.append(("EMA", "n/a"))

    ob = sig.get("orderBook") or {}
    for nev, kulcs in (("sell wall", "nearestSellWall"), ("buy wall", "nearestBuyWall")):
        wall = ob.get(kulcs)
        kontextus.append((nev, f"{wall['distancePct']:.2f}% tavolsagra" if wall else "nincs kozel"))

    r = sig.get("recent")
    if r:
        kontextus.append(("gyakorisag",
                          f"{r['sameDirection']}. {direction} {r['windowMinutes']} percen belul"))
        kontextus.append((f"{detector} / {r['windowMinutes']} perc",
                          f"{r['marketLong']} LONG / {r['marketShort']} SHORT"))

    terv_sorok = []
    t = sig.get("plan")
    if t:
        terv_sorok = [
            ("belepo", f"{t['entry']:.8g}"),
            ("cel", f"{t['target']:.8g}   (+{t['targetPct']:.2f}%)"),
            ("stop", f"{t['stop']:.8g}   (-{t['stopPct']:.2f}%)"),
            ("hozam/kockazat", f"{t['rewardRisk']} : 1"
                               + ("   GYENGE" if t["weak"] else "")),
        ]

    blokkok = [("", alap),
               ("TERV", terv_sorok),
               ("MIERT", [tuple(x) for x in sig.get("lines", [])]),
               ("KONTEXTUS", kontextus)]

    torzs = "\n\n".join(_blokk(nev, sorok) for nev, sorok in blokkok if sorok)
    veg = ""
    if t and t["weak"]:
        veg += (f"\n\n⚠️ <b>Gyenge hozam/kockazat</b> ({t['rewardRisk']}:1, "
                f"ajanlott {t['minRewardRisk']}:1 felett)")
    veg += f"\n\n<i>{esc(sig['reason'])}</i>"
    veg += f"\n{esc(url)}"
    if sig.get("trade", {}).get("executed"):
        veg += f"\n<b>POZICIO NYITVA</b> (order {sig['trade']['orderId']})"
    return f"{fej}\n\n<pre>{torzs}</pre>{veg}"


def _blokk(nev, sorok):
    szeles = max(len(str(c)) for c, _ in sorok)
    torzs = "\n".join(f"  {esc(str(c)):<{szeles}}   {esc(str(v))}" for c, v in sorok)
    return f"{nev}\n{torzs}" if nev else torzs


def esc(t):
    return html.escape(str(t), quote=False)
