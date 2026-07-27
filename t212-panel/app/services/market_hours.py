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

ZMIANA 2026-07-27, DWIE CZESCI (rozdzielenie "nowe wejscia" vs "zarzadzanie
juz otwarta pozycja"):

1. Adam zauwazyl zywy problem - trailing STOP na CRM_US_EQ zostal uzbrojony
   za szeroko (patrz fix w bot_engine.py::_manage_trailing_exit tego samego
   dnia) i utknal tak, bo is_market_open() blokowal jakakolwiek korekte poza
   oknem 15:35-21:55 (sesja regularna), mimo ze cena w obrocie pogodzinowym
   (after-hours, nizsza plynnosc, ale realny ruch ceny) dalej sie zmieniala.
   Pierwsza proba: is_market_open() na sztywno 24/5 dla WSZYSTKICH wywolan.
2. Skutek uboczny: 24/5 dla WSZYSTKICH wywolan objal tez _process_entries()
   (nowe wejscia) we wszystkich trzech silnikach - nagle WSZYSTKIE tickery
   EUR (wczesniej wylaczone poza godzinami Euronext) zaczely byc sprawdzane
   przez caly dzien, w kazdym z trzech botow rownoczesnie, co zalalo i tak
   ciasny rate limit demo T212 (get_pending_orders 429 na kazdym ticku) -
   PARADOKSALNIE spowolnilo to naprawe samego CRM (jego retry przegrywal o
   miejsce w kolejce z lawina nowych zapytan o EUR-owe kandydatow do wejscia,
   ktorych i tak jeszcze nie mamy). Adam poprosil o ograniczenie 24/5 tylko
   do JUZ OTWARTYCH pozycji.

Stad DWIE osobne funkcje ponizej: is_market_open() (nazwa zostaje, uzywana
WYLACZNIE w _process_entries() kazdego silnika - nowe wejscia NADAL czekaja
na regularna sesje, mniejsze ryzyko wejscia po zlej cenie przy cienkiej
plynnosci) i is_position_management_hours() (24/5, uzywana we WSZYSTKICH
pozostalych miejscach - retry buy/sell, trailing exit, DCA, exit management
- czyli wszystko co dotyczy pozycji ktora JUZ jest otwarta, gdzie chodzi o
OCHRONE juz zaangazowanego kapitalu, nie o decyzje czy wchodzic).
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
    inne -> Euronext/Xetra) jest teraz w regularnej sesji, patrz stale
    *_SESSION_WINDOW wyzej. UZYWANE WYLACZNIE dla NOWYCH wejsc (_process_entries
    w bot_engine.py/signal_engine.py/eod_engine.py) - dla zarzadzania JUZ
    otwarta pozycja patrz is_position_management_hours() nizej (24/5).
    """
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:  # sobota=5, niedziela=6
        return False
    window = US_SESSION_WINDOW if currency == "USD" else EU_SESSION_WINDOW
    return window[0] <= now_local.time() <= window[1]


def is_position_management_hours(currency: str) -> bool:
    """
    24/5: dzien roboczy (pon-pt), bez okna godzinowego - patrz "ZMIANA
    2026-07-27" w docstringu modulu. UZYWANE dla zarzadzania JUZ otwarta
    pozycja (retry buy/sell, trailing exit, DCA, exit management) - T212
    pozwala handlowac w obrocie pogodzinowym, wiec ochrona kapitalu (stop,
    take-profit) nie powinna czekac do otwarcia regularnej sesji. Dla nowych
    wejsc patrz is_market_open() wyzej (zostaje na starych, weszych oknach).
    `currency` zostaje w sygnaturze dla symetrii z is_market_open(), mimo ze
    nieuzywane w ciele - wywolujacy kod przekazuje je jednolicie wszedzie.
    """
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    return now_local.weekday() < 5  # sobota=5, niedziela=6
