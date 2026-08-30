"""Rovid esemenynaplo a statusz tablahoz.

A trigger / score / Telegram esemenyek kozvetlenul is a logba kerulnek, de ott
elkeverednek a tablak kozott. Ide is bekerulnek, es minden statusz tabla kiirja,
mi tortent az elozo tabla ota -- igy a ciklusok kovethetok.
"""
import time
from collections import deque

_buffer = deque(maxlen=200)


def add(text):
    _buffer.append((time.time(), text))


def drain():
    """Visszaadja es kiuriti az eddig gyult esemenyeket."""
    items = list(_buffer)
    _buffer.clear()
    return items
