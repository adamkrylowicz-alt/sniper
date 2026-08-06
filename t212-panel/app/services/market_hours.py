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
EU_SESSION_WINDOW = (dt.time(9, 0), dt.time(17, 30))

# ZMIANA 2026-08-05 (Adam: "boty na usa dzialaja 24/5 eu dziala 5dni od 9 do
# 17.30") - USD nie ma już OKNA GODZINOWEGO dla nowych wejść, tylko dzień
# roboczy (patrz is_market_open() niżej - warunek godziny zdjęty WYŁĄCZNIE
# dla USD, EUR zostaje w regularnej sesji). ŚWIADOME ryzyko zaakceptowane
# przez Adama po tym jak wyjaśniłem że NIE jest to identyczne cofnięcie
# fixu z 27.07 (patrz "ZMIANA 2026-07-27" wyżej) - tamten problem (rate
# limit T212 zalany przez wszystkie 3 silniki naraz) od tego czasu dostał
# realne łagodzenie: wspólny 50s cache portfolio/pending-orders (08.2026)
# + wspólny eskalujący backoff po 429 + Telegram (dziś) daje natychmiastową
# widoczność gdyby się powtórzyło - czego 27.07 nie było. Ryzyko które
# ZOSTAJE i którego cache NIE rozwiązuje: niższa płynność/szerszy spread w
# obrocie pre/post-market - to ryzyko rynkowe, nie techniczne, akceptowane
# świadomie. `US_SESSION_WINDOW` zostaje jako stała (opisowa/używana gdzie
# indziej jeśli trzeba), ale is_market_open() już jej nie sprawdza dla USD.
US_SESSION_WINDOW = (dt.time(15, 35), dt.time(21, 55))


def is_market_open(currency: str) -> bool:
    """
    Czy gielda WLASCIWA dla waluty instrumentu jest teraz otwarta dla NOWYCH
    wejsc (_process_entries w bot_engine.py/signal_engine.py/eod_engine.py) -
    dla zarzadzania JUZ otwarta pozycja patrz is_position_management_hours()
    nizej (24/5 dla obu walut, bez zmian).

    USD: 24/5 (tylko dzien roboczy, bez okna godzinowego) - patrz komentarz
    przy US_SESSION_WINDOW wyzej. EUR: regularna sesja Euronext/Xetra,
    EU_SESSION_WINDOW.
    """
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:  # sobota=5, niedziela=6
        return False
    if currency == "USD":
        return True
    return EU_SESSION_WINDOW[0] <= now_local.time() <= EU_SESSION_WINDOW[1]


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


# CAŁA lista EOD jest ZAREZERWOWANA wyłącznie dla EOD (Adam, 2026-08-05:
# "eod ma miec swoj slot az 1" -> potem "eod ma priorytet bo to jest tzw
# strzal jak on cos zacznie reszta ma czekac i nie przeszkadzac"). Wersja 1
# (tego samego dnia, wcześniej) rezerwowała tylko 4 sztywno wybrane tickery -
# ROZSZERZONE na CAŁĄ listę EODAsset, dynamicznie (nie hardkodowana lista -
# automatycznie w synchronie z tym co Adam faktycznie ma na liście EOD,
# łącznie z planowanym rozszerzeniem do ~100 tickerów). Micro-Grid i Sygnał
# mają pomijać KAŻDY ticker z listy EOD w _process_entries TAK SAMO jakby
# był już otwarty przez EOD - patrz held_by_other_engine() niżej, jedyne
# miejsce które trzeba było zmienić (obie pętle wejść już i tak wołają tę
# funkcję przed KAŻDYM nowym wejściem). Import modeli LENIWY - patrz
# uzasadnienie w held_by_other_engine() niżej (ten sam powód, ten sam plik).
def _eod_reserved_tickers(user_id: int) -> frozenset[str]:
    from ..models import EODAsset
    from ..utils import current_environment

    return frozenset(
        row.ticker for row in EODAsset.query.filter_by(
            user_id=user_id, environment=current_environment(user_id),
        ).with_entities(EODAsset.ticker).all()
    )


def held_by_other_engine(user_id: int, ticker: str, this_engine: str) -> str | None:
    """
    Zwraca nazwe INNEGO silnika (Micro-Grid/Sygnal/EOD), ktory ma juz OTWARTA
    pozycje na tym tickerze dla tego usera, albo None gdy ticker jest wolny.
    `this_engine` ("bot"/"signal"/"eod") - silnik wolajacy sam siebie oczywiscie
    pomija.

    Dodane 2026-07-30 (znalezione na zywo - SUp_EQ/Schneider Electric, Micro-Grid
    i Sygnal niezaleznie otworzyly pozycje na TYM SAMYM tickerze 29.07, kazdy
    nieswiadomy drugiego, bo kazdy sprawdzal WYLACZNIE wlasna tabele. T212
    pozwala tylko na JEDEN wylaczny resting stop-sell na dostepna ilosc akcji,
    wiec ktory silnik zlozyl swoj stop pierwszy, ten "wygrywal", a drugi w
    nieskonczonosc dostawal "selling more than owned, owned: 0.0" przy kazdej
    probie przesuniecia wlasnego stopu - Micro-Grid utknal na retry #17 i dalej
    rosnacym, bez zadnego naturalnego konca). Adam: "najlepiej niech kazdy bot
    trzyma i zarzadza swoja pozycja" - wolane PRZED otwarciem KAZDEJ nowej
    pozycji (_process_entries w kazdym z 3 silnikow, bot_engine.py::
    _auto_adopt_foreign_positions, routes/bot.py::adopt_position) - zapobiega
    kolizji zamiast leczyc ja po fakcie.

    Import modeli LENIWY (nie na poziomie modulu) - market_hours.py jest
    swiadomie bez zadnych zaleznosci wewnatrz app/ (patrz docstring modulu),
    zeby uniknac cyklicznego importu; ten sam wzorzec co finnhub_client.py::
    t212_to_finnhub() (lazy `from . import yahoo_resolver`).
    """
    from ..models import ActiveTrade, EODTrade, SignalTrade
    from ..utils import current_environment

    env = current_environment(user_id)

    # Rezerwacja EOD (patrz _eod_reserved_tickers() wyżej) - blokuje Micro-Grid/
    # Sygnał NIEZALEŻNIE od tego czy EOD faktycznie ma tam już otwartą
    # pozycję (samo zarezerwowanie tickera - bycie na LIŚCIE EOD - ma
    # znaczenie, nie aktualny stan pozycji EOD).
    if this_engine != "eod" and ticker in _eod_reserved_tickers(user_id):
        return "EOD"

    if this_engine != "bot" and ActiveTrade.query.filter_by(
        user_id=user_id, ticker=ticker, status="OPEN", environment=env,
    ).first():
        return "Micro-Grid"
    if this_engine != "signal" and SignalTrade.query.filter_by(
        user_id=user_id, ticker=ticker, status="OPEN", environment=env,
    ).first():
        return "Sygnał"
    if this_engine != "eod" and EODTrade.query.filter_by(
        user_id=user_id, ticker=ticker, status="OPEN", environment=env,
    ).first():
        return "EOD"
    return None
