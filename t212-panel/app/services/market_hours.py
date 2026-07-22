"""
app/services/market_hours.py
==============================
Okna sesji gieldowej (Euronext/Xetra dla EUR, NASDAQ/NYSE dla USD) - WYDZIELONE
z bot_engine.py 2026-07-22, zeby uniknac cyklicznego importu (bot_engine.py
importuje z routes/scalping.py, a wiele routes/*.py chce teraz sprawdzac
market_open() dla kropki gielda otwarta/zamknieta w UI - modul bez zadnych
zaleznosci wewnatrz app/ rozwiazuje to raz na zawsze zamiast per-callsite
importu wewnatrz funkcji).

Swiadome uproszczenie: NIE uwzglednia swiat gieldowych, tylko dni robocze
(pon-pt) i dwa stale okna czasowe.
"""

from __future__ import annotations

import datetime as dt

import pytz

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")
EU_SESSION_WINDOW = (dt.time(9, 5), dt.time(17, 25))
US_SESSION_WINDOW = (dt.time(15, 35), dt.time(21, 55))


def is_market_open(currency: str) -> bool:
    """
    Czy gielda WLASCIWA dla waluty instrumentu (USD -> NASDAQ/NYSE, wszystko
    inne -> Euronext/Xetra) jest teraz otwarta, patrz stale *_SESSION_WINDOW
    wyzej. Poza tym oknem (albo w weekend) bot NIC nie robi dla danej pozycji/
    aktywa - ani nowego wejscia, ani retry/trailing/DCA.
    """
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:  # sobota=5, niedziela=6
        return False
    window = US_SESSION_WINDOW if currency == "USD" else EU_SESSION_WINDOW
    return window[0] <= now_local.time() <= window[1]
