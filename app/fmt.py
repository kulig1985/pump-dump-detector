"""Kozos formazok a konzolos statusz tablahoz."""
import time
import unicodedata


def clock(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def pct(v, none="--"):
    return none if v is None else f"{v:+.2f}%"


def _width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text, width):
    """Balra igazitas kijelzesi szelesseg szerint.

    A CJK karakter ket oszlop szeles, a tul hosszu nev pedig levagodik -- kulonben
    egy 15 karakteres symbol (BROCCOLIF3BUSDT) szetcsusztatja az egesz tablat.
    """
    if _width(text) > width:
        out = ""
        for c in text:
            if _width(out + c) > width - 1:
                break
            out += c
        text = out + "…"
    return text + " " * max(0, width - _width(text))


def price(p):
    """Olvashato arformatum: a nagy arak ket tizedessel, a torpek teljes hosszban."""
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def money(v):
    """USDT osszeg rovid alakban."""
    if v is None:
        return "?"
    if v >= 1e9:
        return f"{v / 1e9:,.1f}Mrd"
    if v >= 1e6:
        return f"{v / 1e6:,.1f}M"
    return f"{v:,.0f}"
