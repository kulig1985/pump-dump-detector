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
        if not tg["enabled"]:
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


def format_signal(sig, app_link_template=""):
    """Kozos boritek + a jelzes indoklas-listaja.

    Nincs score: helyette az, hogy MIERT lett jelzes, es milyen mert szamokkal.
    """
    detector = sig.get("detector", "pump_dump")
    direction = sig["direction"]
    emoji, cim, tortent, jelent = HEADERS.get(
        (detector, direction), ("⚡", detector, "", f"{direction} pozicio"))
    url = sig.get("url") or binance_url(sig["symbol"])

    fej = (f"{emoji} <b>{cim}</b>  ·  "
           f"<a href=\"{esc(url)}\"><b>{esc(sig['symbol'])}</b></a>\n"
           f"{esc(tortent)}\n"
           f"➜ <b>{esc(jelent)}</b>\n"
           f"{sig['timestamp'].strftime('%H:%M:%S')} UTC  ·  {esc(detector)}")

    alap = [("ar", f"{sig['price']:.8g}")]
    if sig.get("quoteVolume24h"):
        alap.append(("24h forgalom", f"{sig['quoteVolume24h'] / 1e6:,.0f}M USDT"))

    kontextus = []
    ema = sig.get("ema")
    kontextus.append(("EMA", f"{ema['trend']} (csak informacio)" if ema else "n/a"))
    ob = sig.get("orderBook") or {}
    for nev, kulcs in (("sell wall", "nearestSellWall"), ("buy wall", "nearestBuyWall")):
        w = ob.get(kulcs)
        kontextus.append((nev, f"{_tav(w['distancePct'])} tavolsagra" if w else "nincs kozel"))
    r = sig.get("recent")
    if r:
        kontextus.append(("gyakorisag",
                          f"{r['sameDirection']}. {direction} {r['windowMinutes']} percen belul"))
        kontextus.append((f"{detector} / {r['windowMinutes']} perc",
                          f"{r['detectorLong']} LONG / {r['detectorShort']} SHORT"))

    blokkok = [("", alap), ("KONTEXTUS", kontextus)]
    torzs = "\n\n".join(_blokk(nev, sorok) for nev, sorok in blokkok if sorok)

    indok = "\n".join(f"  • {esc(x)}" for x in sig.get("reasons", []))
    veg = f"\n\n<b>MIERT</b>\n{indok}" if indok else ""
    if sig.get("trade", {}).get("executed"):
        veg += f"\n\n<b>POZICIO NYITVA</b> (order {sig['trade']['orderId']})"

    linkek = esc(url)
    if app_link_template:
        # sima szovegkent, nem <a href>-ben: a Bot API csak http/https/tg semat fogad el
        linkek += "\n" + esc(app_link_template.format(symbol=sig["symbol"]))
    return f"{fej}\n\n<pre>{torzs}</pre>{veg}\n{linkek}"


def format_status(info):
    """Idoszakos eletjel: fut-e meg, mit nez eppen, es mi lett az eddigi jelzesekbol."""
    fej = (f"🟦 <b>ELETJEL</b>\n"
           f"{esc(info['ido'])} UTC  ·  {esc(info['uptime'])} ota fut")

    allapot = [
        ("figyelt par", f"{info['symbols']} db"),
        ("WS kapcsolat", f"{info['wsConnected']}/{info['wsTotal']}"),
        ("kotes / perc", f"{info['ticksPerMin']:,.0f}"),
        ("jelzes indulas ota", f"{info['signals']} db"),
    ]
    if info.get("kizarva"):
        allapot.append(("kizarva", info["kizarva"]))

    blokkok = [("ALLAPOT", allapot)]
    if info.get("movers"):
        blokkok.append(("MOST A LEGMOZGEKONYABB", info["movers"]))
    torzs = "\n\n".join(_blokk(nev, sorok) for nev, sorok in blokkok if sorok)

    veg = ""
    if info.get("kozel"):
        veg += f"\n\n<b>LEGKOZELEBB A JELZESHEZ</b>\n  • {esc(info['kozel'])}"

    meres = ""
    if info.get("utolso"):
        meres += ("\nUTOLSO JELZESEK  (merre indult el az ar -- tipusonkent az utolso par)\n"
                  + "\n".join(esc(x) for x in info["utolso"]))
    if info.get("talalat"):
        meres += ("\n\nOSSZESITES  (+ = a jelzes iranyaba ment)\n"
                  + "\n".join(esc(x) for x in info["talalat"]))
    if meres:
        veg += f"\n\n<pre>{meres.strip()}</pre>"
    else:
        veg += "\n\n<i>Meg nincs lemert jelzes.</i>"
    return f"{fej}\n\n<pre>{torzs}</pre>{veg}"


def _tav(pct):
    """0.004% ne "0.00%"-kent jelenjen meg -- ugy ugy nez ki, mintha nem lenne adat."""
    return f"{pct:.3f}%" if abs(pct) < 0.01 else f"{pct:.2f}%"


def _blokk(nev, sorok):
    szeles = max(len(str(c)) for c, _ in sorok)
    torzs = "\n".join(f"  {esc(str(c)):<{szeles}}   {esc(str(v))}" for c, v in sorok)
    return f"{nev}\n{torzs}" if nev else torzs


def esc(t):
    return html.escape(str(t), quote=False)
