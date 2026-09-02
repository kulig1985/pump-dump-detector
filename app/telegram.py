"""TelegramNotifier -- Bot API sendMessage, HTML formazassal."""
import html
import logging

from .links import binance_url
from .fmt import price as fprice

import aiohttp

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"

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
    """Rovid, egyertelmu belepo jelzes. Semmi magyarazat, csak a szamok."""
    direction = sig["direction"]
    url = sig.get("url") or binance_url(sig["symbol"])
    m = sig.get("metrics") or {}
    emoji = "🟢" if direction == "LONG" else "🔴"

    sorok = [f"{emoji} <b>{direction} {esc(sig['symbol'])}</b>",
             f"{sig['timestamp'].strftime('%Y-%m-%d %H:%M:%S')} UTC",
             f"Entry: {fprice(sig['price'])}"]
    if m.get("impulsePct") is not None:
        sorok.append(f"Impulse: {m['impulsePct']:+.2f}%")
    if m.get("pullbackPct") is not None:
        sorok.append(f"Pullback: {m['pullbackPct']:.0f}%")
    if m.get("flowPct") is not None:
        oldal = "Buy" if direction == "LONG" else "Sell"
        ertek = m["flowPct"] if direction == "LONG" else 100 - m["flowPct"]
        sorok.append(f"{oldal} flow: {ertek:.0f}%")
    if m.get("breakoutAgeSec") is not None:
        sorok.append(f"Breakout age: {m['breakoutAgeSec']:.1f}s")
    if sig.get("trade", {}).get("executed"):
        sorok.append(f"Pozicio nyitva (order {sig['trade']['orderId']})")

    linkek = esc(url)
    if app_link_template:
        # sima szovegkent, nem <a href>-ben: a Bot API csak http/https/tg semat fogad el
        linkek += "\n" + esc(app_link_template.format(symbol=sig["symbol"]))
    return "\n".join(sorok) + "\n" + linkek


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
