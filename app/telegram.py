"""TelegramNotifier -- Bot API sendMessage, HTML formazassal."""
import html
import logging

from .links import binance_url
from .fmt import price as fprice

import aiohttp

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"

# setup -> (emoji, cim, egysoros magyarazat).
# A "SHORT REVERSAL" onmagaban ketertelmu volt: olvashato ugy is, hogy egy short
# fordul meg. Ezert emberi nyelven irjuk ki, mit jelent.
HEADERS = {
    "LONG_CONTINUATION": (
        "🚀", "LONG belepo", "az emelkedes folytatodik"),
    "SHORT_CONTINUATION": (
        "🔻", "SHORT belepo", "az eses folytatodik"),
    "LONG_REVERSAL": (
        "🟢", "LONG belepo", "az eses kifulladt, az ar visszafordult"),
    "SHORT_REVERSAL": (
        "🔴", "SHORT belepo", "az emelkedes kifulladt, az ar lefordult"),
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
    """A jelzes emberi nyelven: EGYSZER elmondva, szakzsargon nelkul.

    Korabban ket blokk (KONTEXTUS + MIERT) mondta el ugyanazt, "lab", "flow",
    "pivot" szavakkal -- olvashatatlan volt. Most egy tortenet + a piaci allapot.
    """
    setup = sig.get("setup") or sig.get("detector", "")
    direction = sig["direction"]
    emoji, cim, magyarazat = HEADERS.get(setup, ("⚡", f"{direction} belepo", ""))
    url = sig.get("url") or binance_url(sig["symbol"])
    m = sig.get("metrics") or {}
    fel = direction == "LONG"

    fej = (f"{emoji} <b>{cim}</b>  ·  "
           f"<a href=\"{esc(url)}\"><b>{esc(sig['symbol'])}</b></a>\n"
           f"{esc(magyarazat)}\n"
           f"{sig['timestamp'].strftime('%H:%M:%S')} UTC")

    alap = [("belepo ar", fprice(sig["price"]))]
    if sig.get("quoteVolume24h"):
        alap.append(("24h forgalom", f"{sig['quoteVolume24h'] / 1e6:,.0f}M USDT"))

    # ---- MI TORTENT: szamozott tortenet, minden lepes egyszer ----
    #
    # FONTOS: az impulzus iranya NEM azonos a jelzes iranyaval. Egy fordulonal
    # eppen ellentetes: lefele impulzus utan jon a LONG belepo.
    tortenet = []
    if m.get("impulsePct") is not None:
        imp_fel = m["impulsePct"] > 0
        sor = (f"Az ar {m['impulseSec']:.0f} masodperc alatt "
               f"{abs(m['impulsePct']):.2f}%-ot "
               f"{'emelkedett' if imp_fel else 'esett'}"
               if m.get("impulseSec") else
               f"Az ar {abs(m['impulsePct']):.2f}%-ot "
               f"{'emelkedett' if imp_fel else 'esett'}")
        if m.get("impulseFrom") and m.get("impulseTo"):
            sor += f" ({fprice(m['impulseFrom'])} -> {fprice(m['impulseTo'])})"
        tortenet.append(sor)
        if m.get("impulseNotional"):
            tortenet.append(
                f"Ezt {m['impulseNotional']:,.0f} USDT agressziv "
                f"{'vetel' if imp_fel else 'eladas'} hajtotta -- "
                f"{m.get('notionalRatio', 0):.0f}x annyi, mint amennyi ezen a "
                f"paron szokasos")

    if setup.endswith("CONTINUATION"):
        if m.get("maxRetracePct") is not None and m.get("pivot") is not None:
            tortenet.append(
                f"Ezutan visszahuzodott a mozgas {m['maxRetracePct']:.0f}%-aig, "
                f"majd ujra attorte a {fprice(m['pivot'])} "
                f"{'csucsot' if fel else 'melypontot'} -- ez a belepo jel")
    else:
        if m.get("exhaustionSec") is not None:
            tortenet.append(
                f"A mozgas kifulladt: {m['exhaustionSec']:.0f} masodpercig nem "
                f"szuletett uj {'melypont' if fel else 'csucs'}")
        if m.get("counter") is not None:
            tortenet.append(
                f"Az ar {'visszavette' if fel else 'letorte'} a "
                f"{fprice(m['counter'])} szintet, es meg is tartotta -- "
                f"ez a belepo jel")

    if m.get("confirmImbalance") is not None:
        eros = "vetel" if m["confirmImbalance"] > 0 else "eladas"
        tortenet.append(
            f"A belepo pillanataban a {eros} van tulsulyban "
            f"({abs(m['confirmImbalance']) * 100:.0f}%-os tobblet)")

    # ---- PIAC MOST: allapot, magyarazattal ----
    piac = []
    if m.get("spreadPct") is not None:
        piac.append(("spread", f"{m['spreadPct']:.3f}%   "
                               f"({'szuk' if m['spreadPct'] < 0.05 else 'tagabb'})"))
    if m.get("bookImbalance") is not None:
        oldal = "vetel" if m["bookImbalance"] > 0 else "eladas"
        piac.append(("order book", f"{m['bookImbalance']:+.2f}   "
                                   f"(tobb {oldal} all a konyvben)"))
    if m.get("trend"):
        egyezik = m["trend"] == ("bullish" if fel else "bearish")
        piac.append(("1 perces trend", f"{m['trend']}   "
                                       f"({'egyezik' if egyezik else 'szembe megy'})"))
    if m.get("setupAgeSec") is not None:
        piac.append(("setup kora", f"{m['setupAgeSec']:.0f} masodperc"))

    blokkok = [("", alap)]
    if piac:
        blokkok.append(("PIAC MOST", piac))
    torzs = "\n\n".join(_blokk(nev, sorok) for nev, sorok in blokkok if sorok)

    veg = ""
    if tortenet:
        sorok = "\n".join(f"  {i}. {esc(x)}" for i, x in enumerate(tortenet, 1))
        veg = f"\n\n<b>MI TORTENT</b>\n{sorok}"
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
