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
    "LONG_CONTINUATION": (
        "🚀", "FOLYTATAS FELFELE", "emelkedes, sekely visszahuzas, ujratores",
        "LONG — veteli pozicio"),
    "SHORT_CONTINUATION": (
        "🔻", "FOLYTATAS LEFELE", "eses, sekely visszapattanas, ujratores",
        "SHORT — eladasi pozicio"),
    "LONG_REVERSAL": (
        "🟢", "FORDULO FELFELE", "az eses kifulladt, a szint visszaveve",
        "LONG — veteli pozicio"),
    "SHORT_REVERSAL": (
        "🔴", "FORDULO LEFELE", "az emelkedes kifulladt, a szint letorve",
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

    async def send(self, symbol, text, detector="scalp"):
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
    setup = sig.get("setup") or sig.get("detector", "")
    direction = sig["direction"]
    emoji, cim, tortent, jelent = HEADERS.get(
        setup, ("⚡", setup, "", f"{direction} pozicio"))
    url = sig.get("url") or binance_url(sig["symbol"])

    fej = (f"{emoji} <b>{cim}</b>  ·  "
           f"<a href=\"{esc(url)}\"><b>{esc(sig['symbol'])}</b></a>\n"
           f"{esc(tortent)}\n"
           f"➜ <b>{esc(jelent)}</b>\n"
           f"{sig['timestamp'].strftime('%H:%M:%S')} UTC  ·  {esc(setup)}")

    alap = [("ar", f"{sig['price']:.8g}")]
    if sig.get("quoteVolume24h"):
        alap.append(("24h forgalom", f"{sig['quoteVolume24h'] / 1e6:,.0f}M USDT"))

    m = sig.get("metrics") or {}
    kontextus = []
    if m.get("legPct") is not None:
        kontextus.append(("impulzus", f"{m.get('impulsePct', 0):+.2f}%  "
                                      f"(lab {m['legPct']:.2f}%)"))
    if m.get("maxRetracePct") is not None:
        kontextus.append(("visszahuzas", f"a lab {m['maxRetracePct']:.0f}%-a"))
    if m.get("confirmImbalance") is not None:
        kontextus.append(("kotesaramlas", f"{m['confirmImbalance']:+.2f}"))
    if m.get("bookImbalance") is not None:
        kontextus.append(("konyv-imbalance", f"{m['bookImbalance']:+.2f}"))
    if m.get("spreadPct") is not None:
        kontextus.append(("spread", f"{m['spreadPct']:.3f}%"))
    if m.get("trend"):
        kontextus.append(("EMA trend", m["trend"]))
    if m.get("setupAgeSec") is not None:
        kontextus.append(("setup kora", f"{m['setupAgeSec']:.0f} mp"))

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
        ("ujracsatlakozas", f"{info.get('reconnects5min', 0)} / 5 perc"),
        ("jelzes indulas ota", f"{info['signals']} db"),
    ]
    if info.get("kizarva"):
        allapot.append(("kizarva", info["kizarva"]))

    blokkok = [("ALLAPOT", allapot)]
    if info.get("setups"):
        blokkok.append(("ELO SETUPOK", info["setups"]))
    torzs = "\n\n".join(_blokk(nev, sorok) for nev, sorok in blokkok if sorok)

    veg = ""
    if info.get("kozel"):
        veg += f"\n\n<b>DETEKTOR ALLAPOT</b>\n  • {esc(info['kozel'])}"

    meres = ""
    if info.get("talalat"):
        meres += ("\nEREDMENY  (melyiket erte el elobb: TP vagy SL)\n"
                  + "\n".join(esc(x) for x in info["talalat"]))
    if info.get("utolso"):
        meres += "\n\n" + "\n".join(esc(x) for x in info["utolso"])
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
