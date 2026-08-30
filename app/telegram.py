"""TelegramNotifier -- Bot API sendMessage."""
import logging

import aiohttp

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None

    async def send(self, symbol, text):
        """Visszaad {"sent": bool, "error": str|None}. Sose dob kivetelt."""
        tg = self.cfg.telegram
        if not self.cfg.detector["telegramEnabled"]:
            log.info("[%s] Telegram kikapcsolva", symbol)
            return {"sent": False, "error": "disabled"}
        if not tg.get("botToken") or not tg.get("chatId"):
            log.warning("[%s] hianyzik a Telegram token vagy chatId", symbol)
            return {"sent": False, "error": "missing credentials"}

        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self.session.post(
                API.format(token=tg["botToken"]),
                json={"chat_id": tg["chatId"], "text": text},
            ) as r:
                body = await r.json()
                if not body.get("ok"):
                    raise RuntimeError(body.get("description", "ismeretlen hiba"))
            log.info("[%s] ertesites elkuldve", symbol)
            return {"sent": True, "error": None}
        except Exception as e:
            log.error("[%s] Telegram kuldes sikertelen: %s", symbol, e)
            return {"sent": False, "error": str(e)}

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


HEADERS = {
    ("pump_dump", "LONG"): "🚨 FUTURES PUMP DETECTED",
    ("pump_dump", "SHORT"): "🔻 FUTURES DUMP DETECTED",
    ("reversal", "LONG"): "🟢 FUTURES LONG REVERSAL",
    ("reversal", "SHORT"): "🔴 FUTURES SHORT REVERSAL",
}


def format_signal(sig):
    """Kozos boritek + a detektor sajat reszletezo sorai.

    Uj detektor eseten itt nem kell modositani semmit: a detektor a signal "lines"
    mezojeben adja a sajat bizonyitekait. Csak a fejlec szovege johet a HEADERS-bol
    (ha nincs benne, egy altalanos fejlecet hasznalunk).
    """
    detector, direction = sig.get("detector", "pump_dump"), sig["direction"]
    lines = [
        HEADERS.get((detector, direction), f"⚡ {detector.upper()} {direction}"),
        sig["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"),
        "",
        sig["symbol"],
        f"Direction: {direction}",
        f"Price: {sig['price']:.8g}",
    ]
    if sig.get("quoteVolume24h"):
        lines.append(f"24h volume: {sig['quoteVolume24h'] / 1e6:,.0f}M USDT")

    # a detektor sajat blokkja
    lines.append("")
    lines += sig.get("lines", [])

    lines.append("")
    ema = sig.get("ema")
    lines.append(f"EMA: {ema['trend'] if ema else 'n/a'}")

    ob = sig.get("orderBook") or {}
    pump = direction == "LONG"
    wall = ob.get("nearestSellWall") if pump else ob.get("nearestBuyWall")
    label = "Nearest sell wall" if pump else "Nearest buy wall"
    lines.append(f"{label}: {wall['distancePct']:.2f}% away" if wall else f"{label}: none nearby")

    lines.append(f"Signal score: {sig['score']}/100")

    r = sig.get("recent")
    if r:
        lines.append("")
        lines.append(f"Ez a(z) {r['sameDirection']}. {direction} jelzes "
                     f"{sig['symbol']}-re {r['windowMinutes']} percen belul "
                     f"({detector})")
        lines.append(f"Ettol a detektortol {r['windowMinutes']} perc alatt: "
                     f"{r['marketLong']} LONG / {r['marketShort']} SHORT")

    lines.append("")
    lines.append(f"Reason: {sig['reason']}")
    if sig.get("trade", {}).get("executed"):
        lines.append(f"Trade: OPENED (order {sig['trade']['orderId']})")
    return "\n".join(lines)
