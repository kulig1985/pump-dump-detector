"""Kozos formazok a konzolos statusz tablahoz."""
import time
import unicodedata


def clock(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def pct(v, none="--"):
    return none if v is None else f"{v:+.2f}%"


def pad(text, width):
    """Balra igazitas kijelzesi szelesseg szerint -- a CJK karakter ket oszlop szeles."""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - w)


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
        return f"{v / 1e6:,.0f}M"
    if v >= 1e3:
        return f"{v / 1e3:,.0f}e"
    return f"{v:,.0f}"
