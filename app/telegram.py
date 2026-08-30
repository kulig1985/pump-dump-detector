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


def format_signal(sig):
    """A kert uzenetformatum."""
    pump = sig["direction"] == "LONG"
    ch = sig["priceChange"]
    lines = [
        "🚨 FUTURES PUMP DETECTED" if pump else "🔻 FUTURES DUMP DETECTED",
        sig["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"),
        "",
        sig["symbol"],
        f"Direction: {sig['direction']}",
        f"Price: {sig['price']:.8g}",
        "",
        f"1s: {_pct(ch['s1'])}",
        f"3s: {_pct(ch['s3'])}",
        f"5s: {_pct(ch['s5'])}",
        "",
    ]
    ema = sig.get("ema")
    lines.append(f"EMA: {ema['trend'] if ema else 'n/a'}")

    ob = sig.get("orderBook") or {}
    wall = ob.get("nearestSellWall") if pump else ob.get("nearestBuyWall")
    label = "Nearest sell wall" if pump else "Nearest buy wall"
    lines.append(f"{label}: {wall['distancePct']:.2f}% away" if wall else f"{label}: none nearby")

    lines.append(f"Signal score: {sig['score']}/100")

    r = sig.get("recent")
    if r:
        lines.append("")
        lines.append(f"Ez a(z) {r['sameSymbolSameDirection']}. {sig['direction']} jelzes "
                     f"{sig['symbol']}-re {r['windowMinutes']} percen belul")
        lines.append(f"A teljes piacon {r['windowMinutes']} perc alatt: "
                     f"{r['marketLong']} LONG / {r['marketShort']} SHORT")

    lines.append("")
    lines.append(f"Reason: {sig['reason']}")
    if sig.get("trade", {}).get("executed"):
        lines.append(f"Trade: OPENED (order {sig['trade']['orderId']})")
    return "\n".join(lines)


def _pct(v):
    return "n/a" if v is None else f"{v:+.2f}%"
