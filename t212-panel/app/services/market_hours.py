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
(pon-pt).

ZMIANA 2026-07-27 (24/5 zamiast stalych okien godzinowych): Adam zauwazyl
zywy problem - trailing STOP na CRM_US_EQ zostal uzbrojony za szeroko (patrz
fix w bot_engine.py::_manage_trailing_exit tego samego dnia) i utknal tak,
bo _market_open() blokowal jakakolwiek korekte poza oknem 15:35-21:55 (sesja
regularna), mimo ze cena w obrocie pogodzinowym (after-hours, nizsza
plynnosc, ale realny ruch ceny) dalej sie zmieniala. Stale okna z 2026-07-21
mialy chronic przed sensownym problemem (retry w NOC, gdy gielda faktycznie
stoi) - ale przy okazji blokowaly tez after-hours/pre-market, gdzie T212
dalej pozwala handlowac. Zmienione na 24/5: caly dzien roboczy (pon-pt),
zero okna godzinowego - te same stale EU_SESSION_WINDOW/US_SESSION_WINDOW
ZOSTAJA (uzywane gdzie indziej do samego wyswietlania "sesja regularna"
w UI), ale is_market_open() ich juz nie sprawdza.
"""

from __future__ import annotations

import datetime as dt

import pytz

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")
EU_SESSION_WINDOW = (dt.time(9, 5), dt.time(17, 25))
US_SESSION_WINDOW = (dt.time(15, 35), dt.time(21, 55))


def is_market_open(currency: str) -> bool:
    """
    24/5: dzien roboczy (pon-pt), bez okna godzinowego - patrz "ZMIANA
    2026-07-27" w docstringu modulu. `currency` zostaje w sygnaturze mimo ze
    juz nieuzywane w ciele funkcji - wywolania w bot_engine.py/signal_engine.py/
    eod_engine.py przekazuja je wszedzie, zmiana sygnatury zrobilaby niepotrzebny
    diff bez realnej korzysci.
    """
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    return now_local.weekday() < 5  # sobota=5, niedziela=6
