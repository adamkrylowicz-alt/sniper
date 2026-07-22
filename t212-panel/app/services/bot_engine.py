"""
app/services/bot_engine.py
============================
Silnik Micro-Grid Bota:

1. tick() - wołane cyklicznie przez APScheduler (patrz app/__init__.py),
   dla każdego aktywnego bota woła w kolejności: _detect_exit_fills()
   (wykryj wykonanie trailing STOP - dawniej też LIMIT SELL, patrz punkt 5,
   nadal sprawdzane dla pozycji sprzed przeprojektowania), _confirm_dca_fills()
   (potwierdź zawieszone nogi DCA, resetuje trailing exit), _retry_pending_buys()
   (dogoń cenę LIMIT BUY poziomu 0, jeśli rynek odjechał), _retry_pending_sells()
   (potwierdź wypełnienie zakupu - buy_confirmed=True, BEZ wystawiania zlecenia
   wyjścia), _manage_trailing_exit() (trailing STOP, patrz punkt 5),
   _trigger_dca_buys() (dokup kolejny poziom siatki, jeśli cena spadła dość
   nisko), potem _process_entries() (całkiem nowe wejścia). Większość dzieli
   JEDNO wspólne get_pending_orders() per user per tick (rate limit demo
   jest ciasny).
2. reconcile(user_id) - Reconciliation Loop (PRD sekcja 4): porównuje
   lokalny stan ActiveTrade z rzeczywistymi zleceniami na koncie T212, i woła
   TE SAME funkcje co tick() (punkt 1) - żeby zachowanie było spójne
   niezależnie od tego, co je wywołało (aktywacja czy zwykły cykl). Wołane
   PRZY AKTYWACJI bota (routes/bot.py::activate), NIE przy starcie appki -
   po restarcie nie ma jeszcze niczyich poświadczeń w bot_credentials.py,
   więc "przy starcie" nie miałoby czego uzgadniać.
3. _process_entries()/_enter_position() - strategia wejścia POZIOMU 0
   (dca_level=0, PRD sekcja 3.1): kupuje mikro-kwotę LIMIT BUY po aktualnej
   cenie (marketable, NIE Market Order - patrz historia tego pliku, punkt
   4). Zlecenie wyjścia NIE jest wystawiane od razu - o to dba
   _manage_trailing_exit() (punkt 5) dopiero po potwierdzeniu kupna.
4. Pętla DCA (dodane 2026-07-20 na życzenie Adama - "cena i tak odjechała") -
   _trigger_dca_buys() wyzwala kolejne poziomy dokupowania gdy cena spadnie o
   RiskSettings.dca_trigger_pct na poziom (liczone od STAŁEJ
   ActiveTrade.grid_anchor_price, nie ruchomej średniej), z kwotą
   entry_amount * mnożnik z RiskSettings.dca_scenario (np. "1,1,1,1,1" - te
   same kwoty na każdym poziomie); _confirm_dca_fills() po potwierdzeniu
   wykonania dolicza do pozycji, przelicza średnią cenę i resetuje trailing
   exit (Cancel-Replace obu nóg, patrz punkt 5). Fail-Safe (limit dziennej
   straty, RiskSettings.max_daily_loss) NADAL jest wyłącznie polem
   formularza - żaden kod go nie czyta, poza zakresem tej części.
   Świadomie pominięte (patrz PLAN.md z sesji): Spread Guard (Finnhub free
   tier nie ma bid/ask) i proaktywny Fractional Guard (rate limit T212
   uniemożliwił bezpieczną weryfikację pól /equity/metadata/instruments) -
   zamiast tego odrzucenie przez T212 (np. brak wsparcia ułamków) jest po
   prostu logowane jako ERROR, bot spróbuje ponownie przy kolejnym tick-u.
   Wyjątek dodany 2026-07-21 (znalezione na żywo - MAIN_US_EQ, potem
   IPOE_US_EQ/SOFI): zła precyzja ilości (quantity-precision-mismatch) NIE
   czeka na kolejny tick - _place_buy_with_precision_fallback() od razu
   przelicza ilość do precyzji podanej w komunikacie T212 i ponawia RAZ w
   tym samym wywołaniu (zaokrąglenie W GÓRĘ, żeby zainwestowana wartość
   nigdy nie spadła poniżej zamierzonej kwoty).
5. Trailing exit (zastąpił sztywny take_profit_usd, 2026-07-21 na życzenie
   Adama - patrz uzasadnienie w RiskSettings.take_profit_step_pct). Pierwsza
   wersja (tego samego dnia) próbowała trzymać RÓWNOCZEŚNIE LIMIT SELL
   (take-profit, przesuwany w górę) + STOP (stop-loss, uzbrajany raz) jako
   ręczne OCO - T212 tego nie pozwala (400 selling-equity-not-owned, broker
   traktuje akcje jako już "zaklepane" przez pierwsze zlecenie, potwierdzone
   na żywo, patrz docs/IDEAS_v2.md pkt 4). PRZEPROJEKTOWANE tego samego dnia
   na TYLKO JEDNO zlecenie - pojedynczy STOP, który nie jest wystawiany
   dopóki cena nie minie 2 progów (take_profit_step_pct), przy pierwszym
   uzbrojeniu siada na average_price*(1-stop_loss_pct) (jak dawny STOP), a
   każdy kolejny próg przesuwa TEN SAM STOP w górę (Cancel-Replace) zamiast
   dokładać drugie zlecenie - patrz pełny docstring _manage_trailing_exit().
   Zero ręcznego OCO do pilnowania, bo nigdy nie ma dwóch zleceń na raz.
6. Okna sesji giełdowej (dodane 2026-07-21, patrz stałe EU_SESSION_WINDOW/
   US_SESSION_WINDOW i funkcja _market_open()) - zanim to dodano,
   ASMLa_EQ retry'owało bez sensu CAŁĄ NOC, mimo że Euronext Amsterdam był
   dawno zamknięty. Teraz każda funkcja, która ponawia/wystawia/przesuwa
   zlecenie (_process_entries, _retry_pending_buys, _retry_pending_sells,
   _manage_trailing_exit, _trigger_dca_buys) pomija pozycję/aktywo, jeśli
   WŁAŚCIWA dla jego waluty giełda jest teraz zamknięta (poza oknem
   9:05-17:25 dla EUR / 15:35-21:55 dla USD, czas Amsterdamu, tylko dni
   robocze). _detect_exit_fills/_confirm_dca_fills CELOWO bez tego gate'u -
   to tylko odczyt stanu już złożonych zleceń, nie warto opóźniać wykrycia
   wykonania.

Bot działa WYŁĄCZNIE na demo (patrz routes/bot.py - blokada environment="live"
na poziomie aktywacji, bo T212 nie wspiera zleceń LIMIT na live) - stąd
"demo" na sztywno tutaj, nie parametr.

Historia buga (2026-07-20, potwierdzone realnym testem na koncie demo, DWIE
niezależne przyczyny):

1. Pierwsza wersja tej części próbowała wystawić LIMIT SELL OD RAZU po
   Market Buy, z krótkim (max ~14s) blokującym time.sleep() retry tylko dla
   błędu "selling-equity-not-owned". W realnym teście zlecenie kupna miało
   status=NEW/filledQuantity=0 jeszcze 2+ minuty po złożeniu (MARKET order
   złożony przed otwarciem giełdy US czeka w kolejce) - żaden 14-sekundowy
   retry by tego nie załatwił. Fix: _enter_position() nigdy nie blokuje ani
   nie zgaduje, _retry_pending_sells() ponawia CYKLICZNIE (co tick, z
   rosnącym backoffem) aż się uda albo T212 zwróci błąd, który się sam nie
   naprawi (sell_blocked=True).

2. Pierwsza wersja fixu z punktu 1 sprawdzała "ile tickera X mam ŁĄCZNIE w
   portfolio" (T212Client.get_portfolio()) zamiast "ile pochodzi z TEGO
   konkretnego zlecenia kupna". W teście to konto demo miało z wcześniejszych
   sesji (17.07) niepowiązaną, starą pozycję 0.006 AAPL - kod zobaczył
   "owned=0.006" w portfolio i SPRZEDAŁ TĘ STARĄ pozycję, podczas gdy
   dzisiejsze zlecenie kupna (0.009) wciąż czekało niewypełnione w kolejce.
   Fix (druga wersja): _retry_pending_sells() sprawdzało filledQuantity
   KONKRETNEGO trade.buy_order_id - get_pending_orders() dla zleceń wciąż w
   kolejce, get_order_history() (stronicowana) dla tych co już z niej
   zniknęły.

3. Druga wersja fixu (get_order_history) okazała się niewystarczająca na
   koncie z BARDZO dużą ręczną aktywnością (ten sam produkcyjny użytkownik
   handluje ręcznie tą samą appką) - zlecenie bota potrafiło "wypaść" poza
   nawet 150 najnowszych zleceń (3 strony po 50) zanim retry zdążył je
   znaleźć, więc pozycja utykała w nieskończonym "nie znaleziono, spróbuję
   później". Ostateczny fix: ActiveTrade.baseline_owned_quantity - zapis ile
   tickera user posiadał w portfolio TUŻ PRZED złożeniem zlecenia kupna
   (_enter_position). Gdy zlecenie zniknie z pending, liczymy
   filled = aktualne_owned_w_portfolio - baseline_owned_quantity, NIE surowe
   aktualne_owned (to był dokładnie błąd z punktu 2) - odejmowanie baseline
   izoluje wkład TEGO zlecenia nawet gdy user ma inne pozycje tego samego
   tickera, a portfolio (w odróżnieniu od historii zleceń) nie ma problemu ze
   skalą niezależnie od tego ile zleceń konto wygenerowało.

4. Na koncie produkcyjnym zaobserwowano też: Market Order na tym demo
   potrafi wisieć status=NEW/filledQuantity=0 znacznie dłużej niż
   odpowiadający mu LIMIT BUY po tej samej cenie (marketable - limit >=
   cena rynkowa), który wypełnia się NATYCHMIAST - potwierdzone wielokrotnie
   ręcznie tego samego dnia (cancel Market Order, LIMIT BUY po aktualnej
   cenie -> fill w kilka sekund). Fix: _enter_position() składa LIMIT BUY
   zamiast Market Order. Dodatkowo: LIMIT BUY z definicji nigdy nie wypełni
   się drożej niż jego limit - jeśli cena rynkowa odjedzie POWYŻEJ limitu
   zanim się wykona, zlecenie czekałoby w nieskończoność (matematycznie nie
   może się już wypełnić). Fix: _retry_pending_buys() (ten sam wzorzec
   backoffu co _retry_pending_sells, kolumny buy_retry_count/
   next_buy_retry_at) anuluje takie zlecenie i wystawia nowe po aktualnej
   cenie - "gonienie" ceny, dokładnie to co wcześniej robiono ręcznie.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path

import pytz
from flask import current_app

from ..extensions import db
from ..models import ActiveTrade, BotAsset, BotAuditLog, RiskSettings, User
from ..routes.api_keys import get_decrypted_credentials
from ..routes.scalping import _log_order
from . import bot_credentials, mailer, price_feed
from .t212_client import T212APIError, T212Client

BOT_ENVIRONMENT = "demo"

# Empirycznie zaobserwowana granica (NIE oficjalnie udokumentowana przez T212):
# próba zakupu SPCX za 1.00 USD dała błąd "min-quantity-exceeded" - "must
# trade at least 0.00874595" - przy ówczesnej cenie SPCX to odpowiada
# minimalnej WARTOŚCI zlecenia rzędu ~1.00 USD. Trzymamy tu próg z zapasem
# (1.20), żeby ostrzegać ZANIM appka wyśle zlecenie do T212, nie dopiero po
# fakcie. To SZACUNEK z jednego zaobserwowanego przypadku, nie gwarancja -
# T212 może mieć różne minima per instrument (stąd i tak realne zlecenie
# może się nie udać nawet powyżej tego progu, albo odwrotnie).
MIN_ORDER_VALUE_ESTIMATE = Decimal("1.20")

SELLING_EQUITY_NOT_OWNED_ERROR_TYPE = "/api-errors/selling-equity-not-owned"

# Okna sesji (ustalone z Adamem 2026-07-21, po tym jak ASMLa_EQ retry'owało
# bez sensu CAŁĄ NOC podczas gdy Euronext Amsterdam był dawno zamknięty -
# każda z funkcji poniżej, ktora goni cene/zarzadza zleceniem, dzieli ten sam
# problem: gielda zamknieta marnuje ciasny rate limit demo na nic).
# _AMSTERDAM_TZ/EU_SESSION_WINDOW/US_SESSION_WINDOW/_market_open WYDZIELONE
# do services/market_hours.py 2026-07-22 (uzywane teraz tez przez routes/*.py
# do kropki "gielda otwarta/zamknieta" w UI - osobny modul bez zaleznosci
# unika cyklicznego importu z routes/scalping.py, ktory ponizej importuje
# _log_order stamtad).
from .market_hours import (  # noqa: E402
    _AMSTERDAM_TZ, EU_SESSION_WINDOW, US_SESSION_WINDOW, is_market_open as _market_open,
)

# Konto Adama jest w EUR - kupno/sprzedaż instrumentu w USD wymaga DWÓCH
# konwersji walutowych (EUR->USD przy kupnie, USD->EUR przy sprzedaży),
# każda z opłatą FX T212 0.15% (patrz docs/IDEAS_v2.md "Koszty i opłaty").
# Surowa cena instrumentu w USD tego nie widzi - żeby EUR->USD->EUR wyszło
# na zero, cena musi wzrosnąć o ok. 2*0.15% = 0.3% (przybliżenie dla małych
# opłat: 1/(1-fx)^2 - 1 ≈ 2*fx). Bez tego bot "zamykałby zysk" trailing
# stopem na progu, który po przewalutowaniu jest już stratą albo zerem
# (zgłoszone przez Adama 2026-07-22, przed pierwszym dzisiejszym wejściem
# w pozycję USD).
FX_FEE_PCT = Decimal("0.0015")
FX_ROUND_TRIP_PCT = FX_FEE_PCT * 2

# Filtr trendu przy PIERWSZYM wejściu (dca_level=0) - dodane 2026-07-22 na
# życzenie Adama, żeby bot nie kupował ślepo bez sprawdzenia kierunku
# ("kup i módl się"). Dotyczy WYŁĄCZNIE _enter_position (nowa pozycja) -
# _trigger_dca_buys (dokupywanie w dołki na już otwartej pozycji) to
# świadomie CAŁA strategia Micro-Grid/DCA, filtr by ją unieważnił, więc
# tam się nie stosuje. Świece dzienne (price_feed.get_mini_chart_ohlc,
# Finnhub->Yahoo) - jeśli cena spadła o więcej niż
# ENTRY_TREND_MAX_DROP_PCT w ostatnich ENTRY_TREND_LOOKBACK_DAYS dniach,
# traktujemy to jako "łapanie spadającego noża" i pomijamy wejście.
ENTRY_TREND_LOOKBACK_DAYS = 6
ENTRY_TREND_MAX_DROP_PCT = Decimal("0.03")

# Twardy limit RÓWNOCZEŚNIE otwartych pozycji (real, nie paper) - dodane
# 2026-07-22 na życzenie Adama. Powód: rozbudowanie listy BotAsset do 38
# kandydatów (żeby filtr trendu miał z czego wybierać) doprowadziło do 17
# jednocześnie otwartych pozycji tego samego dnia - każda z nich to
# osobne zapytania do T212 co tick (pending orders, trailing exit, DCA),
# więc strona "Aktywa" zaczęła stale pokazywać "Z cache" (jej własny
# request o portfolio przegrywał o ten sam ciasny budżet demo z tickiem
# bota). Limit dotyczy TYLKO liczby otwartych pozycji, NIE liczby
# kandydatów na liście BotAsset - można mieć dowolnie dużo tickerów do
# wyboru, bot i tak nie otworzy więcej niż to jednocześnie.
MAX_CONCURRENT_POSITIONS = 10


# Znalezione na żywo 2026-07-21 (MAIN_US_EQ, potem IPOE_US_EQ/SOFI) - T212
# wymaga RÓŻNEJ liczby miejsc po przecinku w ilości w zależności od instrumentu
# (my zawsze liczymy quantize(Decimal("0.0001")), czyli 4 - część spółek
# akceptuje tylko 3). Zamiast twardo obniżać precyzję dla WSZYSTKICH
# instrumentów (zbędna utrata dokładności tam, gdzie 4 miejsca działają), bot
# reaguje NA błąd konkretnego zlecenia - patrz _place_buy_with_precision_fallback.
QUANTITY_PRECISION_MISMATCH_ERROR_TYPE = "/api-errors/quantity-precision-mismatch"


def _required_precision(exc: T212APIError) -> int | None:
    """
    Wyciąga wymaganą liczbę miejsc po przecinku z komunikatu T212 przy
    quantity-precision-mismatch (np. "invalid quantity precision 3" -> 3).
    None gdy to inny typ błędu albo T212 kiedyś zmieni format komunikatu -
    wywołujący ma wtedy zrezygnować z automatycznego retry, nie zgadywać.
    """
    payload = exc.payload
    if not isinstance(payload, dict) or payload.get("type") != QUANTITY_PRECISION_MISMATCH_ERROR_TYPE:
        return None
    match = re.search(r"(\d+)", str(payload.get("detail", "")))
    return int(match.group(1)) if match else None


def _place_buy_with_precision_fallback(
    client: T212Client, ticker: str, quantity: Decimal, price: Decimal,
):
    """
    Składa LIMIT BUY; jeśli T212 odrzuci z powodu złej precyzji ilości,
    przelicza ilość do wymaganej liczby miejsc po przecinku i próbuje RAZ
    jeszcze tą samą ceną. Zaokrągla W GÓRĘ (ROUND_UP), nie w dół - żeby
    zainwestowana wartość nigdy nie wypadła PONIŻEJ zamierzonej kwoty
    (ustalone z Adamem 2026-07-21: "podciągaj wartość jak będzie potrzebna").
    Rzuca dalej oryginalny/nowy T212APIError, jeśli mimo to się nie uda albo
    błąd jest innego typu - wywołujący loguje ERROR jak dotychczas.

    Zwraca (OrderResult, faktycznie użyta ilość) - wywołujący MUSI użyć
    zwróconej ilości przy zapisie ActiveTrade/allocated_value, nie
    oryginalnej `quantity` przekazanej tutaj.
    """
    try:
        return client.place_limit_order(ticker, quantity, price), quantity
    except T212APIError as exc:
        precision = _required_precision(exc)
        if precision is None:
            raise
        adjusted = quantity.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_UP)
        if adjusted <= 0 or adjusted == quantity:
            raise
        return client.place_limit_order(ticker, adjusted, price), adjusted

# Backoff (minuty) między kolejnymi próbami LIMIT SELL dla pozycji, która
# jeszcze nie ma potwierdzonej ilości w portfolio T212 (patrz
# _attempt_sell_placement/_bump_retry). Indeks = min(sell_retry_count,
# len-1) - po wyczerpaniu listy odstęp zostaje na stałe 30 min. Bez sztywnego
# limitu liczby prób - pozycja może legalnie czekać godzinami na otwarcie
# giełdy, to nie jest awaria. Strop 30 min chroni ciasny rate limit demo
# (patrz t212_client.py) przed pozycją, która failuje w nieskończoność.
SELL_RETRY_BACKOFF_MINUTES = (1, 2, 5, 15, 30)


def _next_retry_delay(retry_count: int) -> dt.timedelta:
    idx = min(retry_count, len(SELL_RETRY_BACKOFF_MINUTES) - 1)
    return dt.timedelta(minutes=SELL_RETRY_BACKOFF_MINUTES[idx])


# Backoff (minuty) dla tick()::get_pending_orders po DOWOLNYM błędzie T212API
# (nie tylko 429 - historia 2026-07-21 pokazała, że każdy uporczywy błąd tego
# wywołania, nie tylko rate limit, prowadzi do tego samego: tick co 60s
# dobija się bez końca o ten sam problem zamiast dać mu czas się wyjaśnić).
# Konto usera 2 utknęło w 429 na KAŻDYM ticku przez 13.5h bez ani jednego
# udanego zapytania - stąd ten sam rosnący backoff co SELL_RETRY_BACKOFF_MINUTES,
# osobna stała bo to inny licznik (per-user/per-tick, nie per-trade).
TICK_ERROR_BACKOFF_MINUTES = (1, 2, 5, 15, 30)

# user_id -> (kolejnych błędów z rzędu, kiedy wolno spróbować znowu).
# W pamięci procesu (restart czyści, jak bot_credentials/price_feed cache) -
# celowo nietrwałe, nie ma potrzeby przeżywać restartu appki.
_tick_error_backoff: dict[int, tuple[int, dt.datetime]] = {}


def _next_tick_error_delay(consecutive_errors: int) -> dt.timedelta:
    idx = min(consecutive_errors - 1, len(TICK_ERROR_BACKOFF_MINUTES) - 1)
    return dt.timedelta(minutes=TICK_ERROR_BACKOFF_MINUTES[idx])


# (user_id, ticker) -> (kolejnych nieudanych prób wejścia z rzędu, kiedy
# wolno spróbować znowu). W pamięci procesu, jak _tick_error_backoff wyżej.
# Dodane 2026-07-22 - znaleziony realny problem: MAIN_US_EQ, potem DIS_US_EQ
# nieprzerwanie łapały 429 przy próbie wejścia, a każda taka próba (nawet
# nieudana) "zużywa slot" jednego wejścia na tick (patrz _process_entries) -
# ticker wcześniej na liście BotAsset blokował WSZYSTKIE kolejne w kolejce,
# W NIESKOŃCZONOŚĆ, aż ktoś ręcznie go usunął z listy. Teraz po serii
# nieudanych prób ticker dostaje rosnący backoff i _process_entries go
# POMIJA (nie próbuje w ogóle, nie zużywa slotu) dopóki backoff nie minie -
# reszta listy przestaje być zakładnikiem jednego zepsutego/rate-limitowanego
# assetu, bez potrzeby ręcznego usuwania.
_entry_fail_backoff: dict[tuple[int, str], tuple[int, dt.datetime]] = {}
ENTRY_FAIL_BACKOFF_MINUTES = (2, 5, 15, 30, 60)


def _next_entry_fail_delay(consecutive_fails: int) -> dt.timedelta:
    idx = min(consecutive_fails - 1, len(ENTRY_FAIL_BACKOFF_MINUTES) - 1)
    return dt.timedelta(minutes=ENTRY_FAIL_BACKOFF_MINUTES[idx])


# Plik na bledy bota (ERROR), OSOBNO od BotAuditLog/UI - ustalone z Adamem
# 2026-07-21, po tym jak powtarzajace sie bledy (429 rate limit, precyzja
# ilosci) zalewaly Dziennik bota w appce szumem, przez ktory nie bylo widac
# "pozadanych" wpisow (BUY/INFO/WARN o realnym postepie). instance/ - obok
# sniper.db, jest juz gitignored i traktowane jako stan per-instalacja.
BOT_ERROR_LOG_FILENAME = "bot_errors.log"


def _log_error_to_file(user_id: int, message: str) -> None:
    path = Path(current_app.instance_path) / BOT_ERROR_LOG_FILENAME
    timestamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} UTC | user={user_id} | {message}\n")


def _log(user_id: int, action_type: str, message: str, position_group_id: str | None = None) -> None:
    """
    ERROR leci do osobnego pliku tekstowego (_log_error_to_file), NIE do
    BotAuditLog/Dziennika w appce - patrz komentarz przy BOT_ERROR_LOG_FILENAME.
    Wszystko inne (BUY/INFO/WARN) zapisywane jak dotychczas, z commitem
    natychmiast (każdy wpis niezależny, ten sam styl co OrderLog).
    """
    if action_type == "ERROR":
        _log_error_to_file(user_id, message)
        return
    db.session.add(BotAuditLog(
        user_id=user_id, action_type=action_type, message=message,
        position_group_id=position_group_id,
    ))
    db.session.commit()


def _get_client_for_user(user_id: int, settings: RiskSettings) -> T212Client | None:
    """
    Wspólna budowa T212Client dla kroku retry w tick() - None gdy paper
    trading (nigdy nie potrzebujemy prawdziwego klienta) albo brak
    poświadczeń/klucza demo. Celowo bez logowania błędu tutaj - brakujący
    klucz i tak zostanie zgłoszony przez _process_entries() w tym samym
    ticku (dla dowolnego BotAsset czekającego na wejście), nie ma sensu
    dublować tego samego ostrzeżenia.
    """
    if settings.is_paper_trading:
        return None
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return None
    creds = get_decrypted_credentials(user_id, master_key, BOT_ENVIRONMENT)
    if creds is None:
        return None
    return T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=BOT_ENVIRONMENT)


def _portfolio_quantities(client: T212Client) -> dict[str, Decimal]:
    """
    Jedno zapytanie /equity/portfolio, zamienione na {ticker: owned_quantity}.
    Współdzielone przez cały retry-pass w _retry_pending_sells() - przy N
    zawieszonych pozycjach to i tak jedno zapytanie, nie N (rate limit demo
    jest ciasny). UWAGA: to jest CAŁKOWITE, bieżące posiadanie tickera - do
    ustalenia ile pochodzi z KONKRETNEGO zlecenia trzeba odjąć
    trade.baseline_owned_quantity (patrz _attempt_sell_placement i "Historia
    buga" w docstringu modułu, punkty 2-3).
    """
    portfolio = client.get_portfolio()
    return {p["ticker"]: Decimal(str(p.get("quantity", 0))) for p in portfolio}


def _bump_retry(user_id: int, trade: ActiveTrade, reason: str) -> None:
    trade.sell_retry_count += 1
    trade.next_sell_retry_at = dt.datetime.utcnow() + _next_retry_delay(trade.sell_retry_count)
    db.session.commit()
    _log(
        user_id, "INFO",
        f"{trade.ticker}: retry LIMIT SELL #{trade.sell_retry_count} odłożony - {reason}",
        trade.position_group_id,
    )


def _bump_buy_retry(user_id: int, trade: ActiveTrade, reason: str) -> None:
    trade.buy_retry_count += 1
    trade.next_buy_retry_at = dt.datetime.utcnow() + _next_retry_delay(trade.buy_retry_count)
    db.session.commit()
    _log(
        user_id, "INFO",
        f"{trade.ticker}: sprawdzenie LIMIT BUY #{trade.buy_retry_count} - {reason}",
        trade.position_group_id,
    )


def _confirm_buy_fill(
    user_id: int, client: T212Client, trade: ActiveTrade,
    pending_by_id: dict[str, dict], owned_map: dict[str, Decimal] | None,
) -> None:
    """
    Sprawdza wypełnienie KONKRETNEGO trade.buy_order_id, nie zbiorczego stanu
    portfela wprost (patrz "Historia buga" punkt 2 w docstringu modułu).
    Zlecenie wciąż w pending_by_id -> filledQuantity stamtąd wprost. Zlecenie
    które z pending_by_id zniknęło -> stan końcowy (wykonane w całości albo
    anulowane/odrzucone) - liczymy filled = owned_map[ticker] -
    trade.baseline_owned_quantity (ile PRZYBYŁO tego tickera odkąd złożyliśmy
    TO zlecenie, patrz punkt 3 - odjęcie baseline izoluje wkład tego
    konkretnego zlecenia nawet gdy user ma inne pozycje tego samego tickera).

    Przy częściowym wykonaniu koryguje trade.quantity/allocated_value do
    faktycznie wypełnionej ilości. NIE wystawia tu żadnego zlecenia wyjścia -
    tylko ustawia buy_confirmed=True. Trailing take-profit/stop-loss
    (RiskSettings.take_profit_step_pct/stop_loss_pct) zajmuje się tym
    _manage_trailing_exit() na kolejnych tickach - bot celowo czeka aż cena
    minie pierwsze 2 progi, zamiast wystawiać sztywne zlecenie od razu
    (ustalone z Adamem 2026-07-21).
    """
    pending_order = pending_by_id.get(trade.buy_order_id)

    if pending_order is not None:
        filled_qty = Decimal(str(pending_order.get("filledQuantity", 0)))
        if filled_qty <= 0:
            _bump_retry(
                user_id, trade,
                f"zakup jeszcze niewypełniony (status {pending_order.get('status')}), wciąż w kolejce T212.",
            )
            return
    else:
        if owned_map is None:
            _bump_retry(
                user_id, trade,
                "zlecenie kupna zniknęło z pending, ale nie udało się pobrać portfolio żeby "
                "sprawdzić faktyczną ilość - spróbuję ponownie.",
            )
            return

        current_owned = owned_map.get(trade.ticker, Decimal("0"))
        filled_qty = max(Decimal("0"), current_owned - trade.baseline_owned_quantity)
        if filled_qty <= 0:
            # Zlecenie zniknęło z pending, ale portfolio nie pokazuje żadnego
            # PRZYROSTU od baseline - albo anulowane/odrzucone (zero kupione,
            # na zawsze), albo księgowanie portfolio jeszcze nie nadążyło.
            # Nie da się tego pewnie rozróżnić bez statusu zlecenia (a tego
            # unikamy - patrz punkt 3), więc bezpieczny default to dalszy
            # retry z rosnącym backoffem, nie trwałe zablokowanie.
            _bump_retry(
                user_id, trade,
                f"zlecenie kupna zniknęło z pending, portfolio nie pokazuje przyrostu "
                f"tickera od baseline ({trade.baseline_owned_quantity}) - być może jeszcze się księguje.",
            )
            return

    # Znaleziony realny bug 2026-07-22: dawniej `sell_qty = min(filled_qty,
    # trade.quantity)` obcinało WYŁĄCZNIE w dół (częściowe wykonanie), a
    # nadwyżkę (filled_qty > requested) ciche wyrzucało - trade.quantity
    # zostawało przy zamówionej ilości, jakby nic się nie stało. Nadwyżka
    # realnie może się zdarzyć: _retry_pending_buys() robi cancel-then-replace
    # gdy cena odjedzie, a jeśli stare zlecenie wypełni się DOKŁADNIE w
    # momencie próby anulowania (T212 potwierdza cancel mimo że fill już
    # poszedł), zarówno stare, jak i nowe zlecenie mogą się wykonać - bot
    # kupuje 2x. Bez tej poprawki taki podwójny zakup byłby całkowicie
    # niewidoczny w bazie/dzienniku, mimo że na koncie T212 leżałaby
    # faktycznie podwójna pozycja.
    requested_qty = trade.quantity
    trade.quantity = filled_qty
    trade.allocated_value = (filled_qty * trade.buy_price).quantize(Decimal("0.01"))
    trade.average_price = trade.buy_price

    trade.buy_confirmed = True
    trade.sell_retry_count = 0
    trade.next_sell_retry_at = None
    db.session.commit()

    if filled_qty < requested_qty:
        _log(
            user_id, "WARN",
            f"{trade.ticker}: kupno potwierdzone CZĘŚCIOWO ({filled_qty} z {requested_qty}) - "
            "bot zacznie zarządzać wyjściem (trailing take-profit) na najbliższym ticku.",
            trade.position_group_id,
        )
    elif filled_qty > requested_qty:
        _log(
            user_id, "WARN",
            f"{trade.ticker}: kupno potwierdzone Z NADWYŻKĄ ({filled_qty} zamiast {requested_qty}) - "
            "prawdopodobne PODWÓJNE wykonanie zlecenia (cancel/replace race w _retry_pending_buys), "
            "sprawdź ręcznie w T212. Bot i tak zarządzi wyjściem dla CAŁEJ faktycznej ilości.",
            trade.position_group_id,
        )
    else:
        _log(
            user_id, "INFO",
            f"{trade.ticker}: kupno potwierdzone ({filled_qty}) - bot zacznie zarządzać "
            "wyjściem (trailing take-profit) na najbliższym ticku.",
            trade.position_group_id,
        )


def _retry_pending_sells(
    user_id: int, client: T212Client, settings: RiskSettings, pending: list[dict] | None = None,
) -> None:
    """
    Znajduje pozycje OPEN, których kupno jeszcze NIE jest potwierdzone
    (buy_confirmed=False, patrz ActiveTrade.buy_confirmed - zastępuje stare
    "sell_order_id IS NULL" jako sygnał "jeszcze nie rozliczone"), których
    backoff (next_sell_retry_at) już minął, i próbuje potwierdzić - patrz
    _confirm_buy_fill. Wołane z KAŻDEGO tick() (co 60s) ORAZ z reconcile()
    (przy aktywacji bota) - bez tego pozycja zostałaby trwale zawieszona aż
    do ręcznej dezaktywacji/reaktywacji bota.

    `pending`: opcjonalna, już pobrana lista z get_pending_orders() - reconcile()
    ją i tak potrzebuje dla własnej logiki CLOSED-detection, więc przekazuje
    tutaj zamiast dublować to samo zapytanie (rate limit demo jest ciasny).
    """
    now = dt.datetime.utcnow()
    candidates = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False)
        .filter(db.or_(ActiveTrade.next_sell_retry_at.is_(None), ActiveTrade.next_sell_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if _market_open(t.currency)]
    if not candidates:
        return

    if pending is None:
        try:
            pending = client.get_pending_orders()
        except T212APIError as exc:
            _log(user_id, "ERROR", f"Potwierdzenie kupna: błąd pobierania pending orders - {exc}")
            return
    pending_by_id = {str(o.get("id")): o for o in pending}

    # Portfolio odpytujemy TYLKO gdy faktycznie potrzebne (co najmniej jedno
    # zlecenie zniknęło już z pending) - oszczędza zapytanie w ciasnym rate
    # limicie demo, gdy wszystkie kandydaty wciąż grzecznie czekają w kolejce.
    # owned_map=None (nie pusty dict) gdy zapytanie się nie udało - odróżnia
    # "sprawdzone, zero przyrostu" od "nie udało się sprawdzić" w
    # _confirm_buy_fill, żeby nie zgadywać na podstawie brakujących danych.
    owned_map: dict[str, Decimal] | None = None
    if any(trade.buy_order_id not in pending_by_id for trade in candidates):
        try:
            owned_map = _portfolio_quantities(client)
        except T212APIError as exc:
            _log(user_id, "ERROR", f"Potwierdzenie kupna: błąd pobierania portfolio - {exc}")

    for trade in candidates:
        _confirm_buy_fill(user_id, client, trade, pending_by_id, owned_map)


def _retry_pending_buys(
    user_id: int, client: T212Client, settings: RiskSettings, pending: list[dict] | None = None,
) -> None:
    """
    "Goni" cenę LIMIT BUY, który jeszcze się nie wypełnił i przy obecnej
    cenie rynkowej JUŻ SIĘ NIE MOŻE wypełnić (rynek odjechał POWYŻEJ limitu -
    LIMIT BUY z definicji nigdy nie wykona się drożej niż jego limit) -
    anuluje stare zlecenie i wystawia nowe po aktualnej cenie, żeby pozycja
    nie czekała w nieskończoność. `_enter_position()` zamiast Market Order
    składa marketable LIMIT BUY (po cenie z momentu wejścia) - zaobserwowane
    2026-07-20, że Market Order na tym demo potrafi wisieć NEW/niewypełniony
    znacznie dłużej niż odpowiadający mu LIMIT BUY po tej samej cenie, który
    wypełnia się natychmiast.

    `pending`: opcjonalna, już pobrana lista z get_pending_orders() - dzielona
    z _retry_pending_sells w tym samym cyklu (rate limit demo jest ciasny).
    """
    now = dt.datetime.utcnow()
    candidates = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False)
        .filter(db.or_(ActiveTrade.next_buy_retry_at.is_(None), ActiveTrade.next_buy_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if _market_open(t.currency)]
    if not candidates:
        return

    if pending is None:
        try:
            pending = client.get_pending_orders()
        except T212APIError as exc:
            _log(user_id, "ERROR", f"Retry LIMIT BUY: błąd pobierania pending orders - {exc}")
            return
    pending_by_id = {str(o.get("id")): o for o in pending}

    for trade in candidates:
        pending_order = pending_by_id.get(trade.buy_order_id)
        if pending_order is None:
            # Zlecenie już nie w kolejce - wypełnione albo anulowane skądinąd,
            # tym zajmuje się _retry_pending_sells (baseline-delta w portfolio).
            continue

        if Decimal(str(pending_order.get("filledQuantity", 0))) > 0:
            continue  # częściowo już wypełnione - nie anulujemy w połowie, niech dokończy

        current_price = price_feed.get_live_price(
            current_app.config.get("FINNHUB_API_KEY"), trade.ticker,
            current_app.config.get("ALPACA_API_KEY"), current_app.config.get("ALPACA_API_SECRET"),
        )
        if current_price is None or current_price <= 0:
            _bump_buy_retry(user_id, trade, "brak aktualnej ceny do porównania z limitem, spróbuję ponownie.")
            continue

        if current_price <= trade.buy_price:
            # Limit wciąż marketable (cena nie odjechała w górę) - zwyczajnie
            # jeszcze się nie wykonało, nic do gonienia.
            _bump_buy_retry(
                user_id, trade,
                f"cena ({current_price}) wciąż <= limitu ({trade.buy_price}), czekam na wypełnienie.",
            )
            continue

        # Przeliczamy ILOŚĆ względem NOWEJ ceny, zachowując docelową kwotę
        # alokacji (BotAsset.entry_amount) - realny bug znaleziony na żywo
        # 2026-07-22: FB_US_EQ (Meta) miało błędną cenę wejścia 44.61 (martwy
        # symbol "FB" w Alpaca, patrz TICKER_MAP fix), gdy się poprawiła na
        # prawdziwe ~632, ten kod PRZED fixem trzymał starą trade.quantity
        # (policzoną po błędnej, 14x niższej cenie) i tylko podmieniał cenę -
        # pozycja urosła z zamierzonych 25 USD do 354 USD. Przy normalnych,
        # małych ruchach ceny błąd jest niezauważalny (parę % dryfu), ale
        # mechanizm był zepsuty dla KAŻDEGO price-chase, nie tylko tego
        # ekstremalnego przypadku - stąd fix ogólny, nie tylko dla Meta.
        asset = BotAsset.query.get(trade.bot_asset_id)
        target_amount = asset.entry_amount if asset is not None else (trade.quantity * trade.buy_price)
        new_quantity = (target_amount / current_price).quantize(Decimal("0.0001"))
        if new_quantity <= 0:
            _bump_buy_retry(
                user_id, trade,
                f"cena odjechała ({current_price} > limit {trade.buy_price}), ale przeliczona ilość <= 0 "
                f"(kwota {target_amount} / cena {current_price}), pomijam ten tick.",
            )
            continue

        try:
            client.cancel_order(trade.buy_order_id)
        except T212APIError as exc:
            _bump_buy_retry(
                user_id, trade,
                f"cena odjechała ({current_price} > limit {trade.buy_price}), ale anulowanie starego "
                f"zlecenia nie powiodło się - {exc}",
            )
            continue

        try:
            new_result = client.place_limit_order(trade.ticker, new_quantity, current_price)
        except T212APIError as exc:
            _bump_buy_retry(
                user_id, trade,
                f"stare LIMIT BUY anulowane, ale nowe po {current_price} nie powiodło się - {exc}",
            )
            continue

        old_price = trade.buy_price
        old_quantity = trade.quantity
        trade.buy_order_id = new_result.order_id
        trade.buy_price = current_price
        trade.average_price = current_price
        trade.quantity = new_quantity
        trade.allocated_value = (new_quantity * current_price).quantize(Decimal("0.01"))
        trade.buy_retry_count = 0
        trade.next_buy_retry_at = None
        db.session.commit()
        _log(
            user_id, "INFO",
            f"{trade.ticker}: cena odjechała ({old_price} -> {current_price}) - LIMIT BUY ponowiony po nowej "
            f"cenie, ilość przeliczona ({old_quantity} -> {new_quantity}) żeby zachować alokację ~{target_amount}.",
            trade.position_group_id,
        )


def _manage_trailing_exit(user_id: int, client: T212Client, settings: RiskSettings) -> None:
    """
    Trailing STOP - JEDNO zlecenie na raz (przeprojektowane 2026-07-21;
    pierwsza wersja tego samego dnia próbowała trzymać RÓWNOCZEŚNIE LIMIT
    SELL (take-profit) + STOP (stop-loss) na te same akcje jako ręczne OCO -
    T212 tego nie pozwala, próba uzbrojenia STOP-a po tym jak LIMIT SELL już
    rezerwuje akcje kończyła się `400 selling-equity-not-owned` (broker
    traktuje akcje jako już "zaklepane" przez pierwsze zlecenie), potwierdzone
    na żywo na koncie demo - patrz docs/IDEAS_v2.md, punkt 4).

    Mechanika (procent od average_price, NIE stała kwota - żeby krok/stop
    skalowały się z ceną instrumentu, patrz uzasadnienie w
    RiskSettings.take_profit_step_pct):

    1. Bot NIC nie wystawia dopóki cena nie minie DWÓCH progów
       (2 * take_profit_step_pct powyżej average_price) - jak dawniej, żeby
       zwykły szum tuż po zakupie nie wyciął pozycji.
    2. Przy pierwszym uzbrojeniu (dokładnie na progu 2) STOP siada na
       average_price * (1 - stop_loss_pct) - klasyczna ochrona kapitału,
       dokładnie tam gdzie dawniej siadał osobny STOP.
    3. Każdy kolejny próg PRZESUWA TEN SAM STOP w górę (Cancel-Replace) na
       average_price * (1 + take_profit_step_pct * (milestone_steps - 2)) -
       od progu 3 w górę zaczyna już blokować realny zysk, nie tylko chronić
       kapitał. Formuła rośnie monotonicznie z milestone_steps, więc STOP
       nigdy nie cofa się w dół.

    Migracja ze starego dwunożnego OCO: jeśli pozycja ma jeszcze
    trade.sell_order_id (LIMIT SELL założony PRZED tym przeprojektowaniem),
    ten tick go najpierw anuluje i zeruje - dopiero potem liczy/wystawia
    pojedynczy STOP jak wyżej (może się to zdarzyć na tym samym ticku, gdy
    stary LIMIT SELL faktycznie jeszcze wisiał na koncie).
    """
    step = settings.take_profit_step_pct
    if step <= 0:
        return  # błędna konfiguracja (0 albo ujemny krok) - nie ma jak liczyć progów, nie zgaduj

    now = dt.datetime.utcnow()
    candidates = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True, sell_blocked=False)
        .filter(db.or_(ActiveTrade.next_sell_retry_at.is_(None), ActiveTrade.next_sell_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if _market_open(t.currency)]
    if not candidates:
        return

    for trade in candidates:
        if trade.sell_order_id:
            old_sell_order_id = trade.sell_order_id
            try:
                client.cancel_order(old_sell_order_id)
            except T212APIError as exc:
                _bump_retry(
                    user_id, trade,
                    f"migracja ze starego dwunożnego OCO - anulowanie starego LIMIT SELL "
                    f"({old_sell_order_id}) nie powiodło się (mógł się już wykonać) - {exc}",
                )
                continue
            trade.sell_order_id = None
            db.session.commit()
            _log(
                user_id, "INFO",
                f"{trade.ticker}: stary LIMIT SELL ({old_sell_order_id}) z dawnego dwunożnego OCO "
                "anulowany - przechodzę na pojedynczy, przesuwany STOP.",
                trade.position_group_id,
            )

        current_price = price_feed.get_live_price(
            current_app.config.get("FINNHUB_API_KEY"), trade.ticker,
            current_app.config.get("ALPACA_API_KEY"), current_app.config.get("ALPACA_API_SECRET"),
        )
        if current_price is None or current_price <= 0:
            continue  # brak ceny - spróbujemy przy kolejnym ticku, nic pilnego do zrobienia

        # Dla USD: liczymy progi/STOP względem ref_price (average_price
        # podbite o round-trip FX), nie surowej average_price - patrz
        # FX_ROUND_TRIP_PCT wyżej. Dla EUR (konto Adama jest w EUR, zero
        # konwersji) ref_price == average_price, zero zmiany zachowania.
        ref_price = (
            trade.average_price * (1 + FX_ROUND_TRIP_PCT) if trade.currency == "USD"
            else trade.average_price
        )

        profit_pct = (current_price - ref_price) / ref_price
        milestone_steps = int(profit_pct / step) if profit_pct > 0 else 0
        if milestone_steps < 2:
            continue  # jeszcze przed progiem uzbrojenia (potrzeba 2 progów, po odjęciu FX dla USD)

        if milestone_steps <= trade.trail_milestone_steps and trade.stop_order_id is not None:
            continue  # STOP już uzbrojony na tym (albo wyższym) progu, nic do zrobienia

        if milestone_steps == 2:
            new_stop_price = (ref_price * (1 - settings.stop_loss_pct)).quantize(Decimal("0.0001"))
        else:
            new_stop_price = (ref_price * (1 + step * (milestone_steps - 2))).quantize(Decimal("0.0001"))

        if trade.stop_order_id:
            try:
                client.cancel_order(trade.stop_order_id)
            except T212APIError as exc:
                _bump_retry(
                    user_id, trade,
                    f"przesunięcie trailing STOP na próg {milestone_steps} - anulowanie starego "
                    f"({trade.stop_order_id}) nie powiodło się (mógł się już wykonać) - {exc}",
                )
                continue

        try:
            stop_result = client.place_stop_order(trade.ticker, -trade.quantity, new_stop_price)
        except T212APIError as exc:
            trade.stop_order_id = None
            _bump_retry(
                user_id, trade,
                f"uzbrojenie trailing STOP na próg {milestone_steps} (target {new_stop_price}) "
                f"nie powiodło się - {exc}",
            )
            continue

        was_armed = trade.trail_milestone_steps > 0
        trade.stop_order_id = stop_result.order_id
        trade.stop_target_price = new_stop_price
        trade.trail_milestone_steps = milestone_steps
        trade.sell_retry_count = 0
        trade.next_sell_retry_at = None
        db.session.commit()
        _log(
            user_id, "INFO",
            f"{trade.ticker}: trailing STOP {'uzbrojony' if not was_armed else 'przesunięty'} "
            f"na próg {milestone_steps} (target {new_stop_price}).",
            trade.position_group_id,
        )


def _parse_dca_scenario(dca_scenario: str) -> list[Decimal]:
    """
    "1,1,1,1,1" -> [Decimal("1")]*5. Mnożnik entry_amount per poziom DCA
    (poziom 0 = pierwsze wejście, NIE liczone tutaj - _enter_position ma
    własną, niezależną ścieżkę). Puste/niepoprawne wpisy pomijane; pusty
    wynik (np. usera wpisał śmieci) -> [Decimal("1")], żeby nigdy nie
    zablokować DCA przez błąd formatu.
    """
    multipliers = []
    for part in dca_scenario.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            multipliers.append(Decimal(part))
        except InvalidOperation:
            continue
    return multipliers or [Decimal("1")]


def _dca_multiplier(multipliers: list[Decimal], level: int) -> Decimal:
    """Poziom poza zdefiniowanym scenariuszem (dłuższy max_dca_levels niż lista) - powtarza ostatni mnożnik."""
    if level < len(multipliers):
        return multipliers[level]
    return multipliers[-1]


def _trigger_dca_buys(user_id: int, client: T212Client, settings: RiskSettings) -> None:
    """
    Micro-Grid: dla każdej pozycji z potwierdzonym kupnem (buy_confirmed=True
    - poziom 0 w pełni rozliczony, NIEZALEŻNE od tego czy trailing exit zdążył
    już uzbroić LIMIT SELL, patrz _manage_trailing_exit) sprawdza czy cena
    spadła poniżej kolejnego poziomu siatki (grid_anchor_price * (1 -
    dca_trigger_pct * (dca_level+1)), STAŁY punkt odniesienia - patrz
    ActiveTrade.grid_anchor_price) i jeśli tak, otwiera kolejną nogę DCA
    (dca_pending_*). NIE dotyka jeszcze głównej pozycji (quantity/
    average_price) - to robi dopiero _confirm_dca_fills() po potwierdzeniu
    wykonania (Cancel-Replace ewentualnego starego LIMIT SELL/STOP).
    """
    candidates = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True)
        .filter(ActiveTrade.dca_pending_buy_order_id.is_(None))
        .filter(ActiveTrade.dca_level < settings.max_dca_levels - 1)
        .all()
    )
    candidates = [t for t in candidates if _market_open(t.currency)]
    if not candidates:
        return

    multipliers = _parse_dca_scenario(settings.dca_scenario)

    for trade in candidates:
        next_level = trade.dca_level + 1
        trigger_price = trade.grid_anchor_price * (Decimal("1") - settings.dca_trigger_pct * next_level)
        if trigger_price <= 0:
            continue  # grid_anchor_price=0 (np. stara pozycja sprzed migracji) - DCA celowo wyłączone

        current_price = price_feed.get_live_price(
            current_app.config.get("FINNHUB_API_KEY"), trade.ticker,
            current_app.config.get("ALPACA_API_KEY"), current_app.config.get("ALPACA_API_SECRET"),
        )
        if current_price is None or current_price <= 0 or current_price > trigger_price:
            continue  # cena jeszcze nie spadła dość nisko (albo brak danych) - nic do zrobienia

        asset = BotAsset.query.get(trade.bot_asset_id)
        if asset is None:
            continue  # aktywo usunięte z listy bota od czasu wejścia - nie dokupuj

        multiplier = _dca_multiplier(multipliers, next_level)
        dca_amount = asset.entry_amount * multiplier
        dca_quantity = (dca_amount / current_price).quantize(Decimal("0.0001"))
        if dca_quantity <= 0:
            continue

        try:
            buy_result, dca_quantity = _place_buy_with_precision_fallback(
                client, trade.ticker, dca_quantity, current_price,
            )
        except T212APIError as exc:
            _log(
                user_id, "ERROR",
                f"{trade.ticker}: DCA poziom {next_level} nieudany (cena {current_price} <= trigger "
                f"{trigger_price:.4f}) - {exc}",
                trade.position_group_id,
            )
            continue

        trade.dca_pending_buy_order_id = buy_result.order_id
        trade.dca_pending_quantity = dca_quantity
        trade.dca_pending_price = current_price
        trade.dca_pending_baseline_quantity = trade.quantity
        db.session.commit()
        _log(
            user_id, "BUY",
            f"{trade.ticker}: DCA poziom {next_level} wyzwolony (cena {current_price} <= trigger "
            f"{trigger_price:.4f}) - dokupuję {dca_quantity} @ ~{current_price}.",
            trade.position_group_id,
        )


def _confirm_dca_fills(
    user_id: int, client: T212Client, settings: RiskSettings, pending: list[dict] | None = None,
) -> None:
    """
    Sprawdza wypełnienie zawieszonych nóg DCA (dca_pending_*) - ten sam wzorzec
    co _confirm_buy_fill (pending_by_id -> filledQuantity wprost; zniknęło z
    pending -> portfolio delta względem dca_pending_baseline_quantity, czyli
    ile było PRZED TĄ KONKRETNĄ nogą). Po potwierdzeniu: dolicza do głównej
    pozycji (quantity/allocated_value/average_price) i CANCELUJE obie nogi
    trailing exitu (LIMIT SELL i STOP, jeśli były uzbrojone - liczone były
    dla starej, mniejszej pozycji po starej średniej cenie) oraz zeruje
    trail_milestone_steps - _manage_trailing_exit() uzbroi je od nowa na
    najbliższym możliwym cyklu, licząc progi od nowej average_price
    (Cancel-Replace).
    """
    candidates = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False)
        .filter(ActiveTrade.dca_pending_buy_order_id.isnot(None))
        .all()
    )
    if not candidates:
        return

    if pending is None:
        try:
            pending = client.get_pending_orders()
        except T212APIError as exc:
            _log(user_id, "ERROR", f"DCA: błąd pobierania pending orders - {exc}")
            return
    pending_by_id = {str(o.get("id")): o for o in pending}

    owned_map: dict[str, Decimal] | None = None
    if any(trade.dca_pending_buy_order_id not in pending_by_id for trade in candidates):
        try:
            owned_map = _portfolio_quantities(client)
        except T212APIError as exc:
            _log(user_id, "ERROR", f"DCA: błąd pobierania portfolio - {exc}")

    for trade in candidates:
        pending_order = pending_by_id.get(trade.dca_pending_buy_order_id)

        if pending_order is not None:
            filled_qty = Decimal(str(pending_order.get("filledQuantity", 0)))
            if filled_qty <= 0:
                continue  # jeszcze w kolejce, sprawdzimy przy kolejnym ticku
        else:
            if owned_map is None:
                continue
            current_owned = owned_map.get(trade.ticker, Decimal("0"))
            filled_qty = max(Decimal("0"), current_owned - trade.dca_pending_baseline_quantity)
            if filled_qty <= 0:
                continue  # zniknęło z pending, brak przyrostu - jeszcze się księguje, sprawdzimy później

        # Ten sam bug co w _confirm_buy_fill (naprawiony 2026-07-22) -
        # min() ciął WYŁĄCZNIE w dół, nadwyżkę (podwójne wykonanie nogi DCA
        # przy cancel/replace race) ciche gubił. Doliczamy CAŁĄ faktyczną
        # ilość, nie tylko zamówioną.
        leg_qty = filled_qty
        overfilled = leg_qty > trade.dca_pending_quantity
        leg_price = trade.dca_pending_price
        leg_cost = leg_qty * leg_price

        trade.quantity = trade.quantity + leg_qty
        trade.allocated_value = (trade.allocated_value + leg_cost).quantize(Decimal("0.01"))
        trade.average_price = (trade.allocated_value / trade.quantity).quantize(Decimal("0.0001"))
        trade.dca_level += 1

        for leg_name, old_order_id in (("LIMIT SELL", trade.sell_order_id), ("STOP", trade.stop_order_id)):
            if not old_order_id:
                continue
            try:
                client.cancel_order(old_order_id)
            except T212APIError as exc:
                # Stara noga mogła się już sama wykonać między sprawdzeniem
                # pending a teraz (rzadkie, ale nieszkodliwe) - i tak zerujemy
                # jej ID, _manage_trailing_exit/reconcile() przestaną jej
                # szukać, a portfolio-delta w kolejnym cyklu i tak odzwierciedli
                # rzeczywisty stan.
                _log(
                    user_id, "INFO",
                    f"{trade.ticker}: anulowanie starego {leg_name} ({old_order_id}) po DCA "
                    f"nie powiodło się (prawdopodobnie już wykonany) - {exc}",
                    trade.position_group_id,
                )
        trade.sell_order_id = None
        trade.stop_order_id = None
        trade.trail_milestone_steps = 0
        trade.sell_retry_count = 0
        trade.next_sell_retry_at = None

        trade.dca_pending_buy_order_id = None
        trade.dca_pending_quantity = None
        trade.dca_pending_price = None
        trade.dca_pending_baseline_quantity = None
        db.session.commit()

        if overfilled:
            _log(
                user_id, "WARN",
                f"{trade.ticker}: DCA poziom {trade.dca_level} wypełniony Z NADWYŻKĄ ({leg_qty} @ ~{leg_price}, "
                "prawdopodobne PODWÓJNE wykonanie tej nogi - cancel/replace race, sprawdź ręcznie w T212) - "
                f"nowa średnia {trade.average_price}, łącznie {trade.quantity}. Trailing exit zresetowany, "
                "uzbroi się od nowa od nowej średniej (Cancel-Replace).",
                trade.position_group_id,
            )
        else:
            _log(
                user_id, "BUY",
                f"{trade.ticker}: DCA poziom {trade.dca_level} wypełniony ({leg_qty} @ ~{leg_price}) - "
                f"nowa średnia {trade.average_price}, łącznie {trade.quantity}. Trailing exit zresetowany, "
                "uzbroi się od nowa od nowej średniej (Cancel-Replace).",
                trade.position_group_id,
            )


def _finalize_closed_trade(user_id: int, client: T212Client, trade: ActiveTrade, filled_via: str) -> None:
    """
    Oznacza trade jako CLOSED i anuluje "osieroconą" drugą nogę (ręczne OCO -
    T212 nie ma natywnego one-cancels-other, patrz _manage_trailing_exit).
    filled_via: "take-profit" albo "stop-loss", tylko do logu/wyboru której
    nogi szukać jako osieroconej.

    close_price = trade.stop_target_price (patrz models.py::ActiveTrade,
    dodane 2026-07-21, żeby zysk/strata zrealizowana dało się policzyć bez
    dziennika/T212) - PRZYBLIŻENIE dla "stop-loss" (Market Order po
    przebiciu stopu może wykonać się z poślizgiem), a dla "take-profit" (stara
    ścieżka sell_order_id sprzed przeprojektowania, patrz komentarz przy
    ActiveTrade.sell_order_id) w ogóle nieznane - stop_target_price dotyczy
    tylko nogi STOP, więc zostaje NULL.
    """
    sibling_id = trade.stop_order_id if filled_via == "take-profit" else trade.sell_order_id
    if sibling_id:
        try:
            client.cancel_order(sibling_id)
        except T212APIError as exc:
            _log(
                user_id, "INFO",
                f"{trade.ticker}: anulowanie drugiej nogi ({sibling_id}) po zamknięciu przez "
                f"{filled_via} nie powiodło się (mogła się wykonać w tym samym momencie) - {exc}",
                trade.position_group_id,
            )
    if filled_via == "stop-loss":
        trade.close_price = trade.stop_target_price
    trade.status = "CLOSED"
    trade.closed_at = dt.datetime.utcnow()
    db.session.commit()


def _detect_exit_fills(user_id: int, client: T212Client, pending_ids: set[str]) -> int:
    """
    Sprawdza czy sell_order_id (take-profit) albo stop_order_id (stop-loss)
    jakiejś OPEN pozycji zniknęło z pending - jeśli tak, pozycja wykonana,
    oznacza CLOSED i anuluje osieroconą drugą nogę (patrz
    _finalize_closed_trade). Wołane z KAŻDEGO tick() (nie tylko reconcile()
    przy aktywacji) - inaczej pozycja wykonana W TRAKCIE gdy bot jest aktywny
    nigdy nie zostałaby lokalnie zamknięta, a druga noga wisiałaby na T212
    bez końca. Zwraca liczbę zamkniętych pozycji.
    """
    open_trades = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False)
        .filter(db.or_(ActiveTrade.sell_order_id.isnot(None), ActiveTrade.stop_order_id.isnot(None)))
        .all()
    )
    closed_count = 0
    for trade in open_trades:
        if trade.sell_order_id and trade.sell_order_id not in pending_ids:
            _log(
                user_id, "INFO",
                f"{trade.ticker} (grupa {trade.position_group_id}): LIMIT SELL (take-profit) wykonany "
                "- pozycja zamknięta.",
                position_group_id=trade.position_group_id,
            )
            _finalize_closed_trade(user_id, client, trade, "take-profit")
            closed_count += 1
        elif trade.stop_order_id and trade.stop_order_id not in pending_ids:
            # Ten sam mechanizm STOP obsługuje DWA różne etapy trailing exitu
            # (patrz _manage_trailing_exit): próg 2 to ochrona kapitału
            # (stop POD ceną wejścia, realna strata), próg 3+ to już
            # blokowanie ZYSKU (stop NAD ceną wejścia) - komunikat na sztywno
            # "ze stratą" mylił Adama dwukrotnie (2026-07-22, Siemens x2),
            # bo próg 3+ to w rzeczywistości zysk. Porównujemy
            # stop_target_price (gdzie się wykonało) z average_price (skąd
            # weszliśmy) żeby podpisać właściwie.
            result_word = (
                "z ZYSKIEM" if trade.stop_target_price is not None and trade.stop_target_price > trade.average_price
                else "ze STRATĄ"
            )
            _log(
                user_id, "WARN",
                f"{trade.ticker} (grupa {trade.position_group_id}): STOP wykonany na {trade.stop_target_price} "
                f"(wejście {trade.average_price}) - pozycja zamknięta {result_word}.",
                position_group_id=trade.position_group_id,
            )
            _finalize_closed_trade(user_id, client, trade, "stop-loss")
            closed_count += 1
    return closed_count


def reconcile(user_id: int) -> None:
    """
    1. Sprawdza czy jakaś lokalnie "OPEN" pozycja (sell_order_id/stop_order_id)
       wykonała się na T212 podczas gdy bot był nieaktywny - patrz
       _detect_exit_fills().
    2. Woła _confirm_dca_fills()/_retry_pending_buys()/_retry_pending_sells()/
       _manage_trailing_exit()/_trigger_dca_buys() - ten sam mechanizm co
       cykliczny tick(), więc zachowanie jest identyczne niezależnie od tego,
       co je wywołało.
    """
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        # Nie powinno się zdarzyć - routes/bot.py::activate() woła to TUŻ PO
        # bot_credentials.activate(). Jeśli mimo to tu trafiamy, to realny
        # problem (nie cichy no-op) - ma być widoczny w dzienniku, nie milczeć.
        _log(user_id, "ERROR", "Reconciliation: brak poświadczeń w bot_credentials mimo aktywacji - zgłoś to.")
        return

    creds = get_decrypted_credentials(user_id, master_key, BOT_ENVIRONMENT)
    if creds is None:
        _log(user_id, "ERROR", "Reconciliation: brak zapisanego klucza API demo, pomijam.")
        return

    settings = RiskSettings.query.filter_by(user_id=user_id).first()

    # is_paper=False - pozycje papierowe nigdy nie trafily do T212, wiec nie
    # ma czego z nim uzgadniac (patrz models.py::ActiveTrade.is_paper).
    open_trades = ActiveTrade.query.filter_by(user_id=user_id, status="OPEN", is_paper=False).all()
    if not open_trades:
        _log(user_id, "INFO", "Reconciliation: brak otwartych pozycji (realnych) do sprawdzenia.")
        return

    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=BOT_ENVIRONMENT)
    try:
        pending = client.get_pending_orders()
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Reconciliation: błąd T212 - {exc}")
        return

    pending_ids = {str(o.get("id")) for o in pending}
    closed_count = _detect_exit_fills(user_id, client, pending_ids)
    if closed_count == 0:
        _log(user_id, "INFO", f"Reconciliation: {len(open_trades)} pozycji sprawdzonych, wszystkie nadal aktualne.")

    # Weryfikacja nadwyżek (dodane 2026-07-22 razem z fixem w _confirm_buy_fill/
    # _confirm_dca_fills) - dla KAŻDEJ otwartej pozycji, nie tylko tych jeszcze
    # niepotwierdzonych, porównuje rzeczywiste posiadanie na T212 z tym co baza
    # oczekuje (baseline_owned_quantity sprzed zlecenia + trade.quantity).
    # Łapie też nadwyżki potwierdzone JUŻ WCZEŚNIEJ pod starym (przed fixem)
    # kodem, które wtedy zostały po cichu obcięte i nigdy nie trafiły do logu.
    try:
        owned_now = _portfolio_quantities(client)
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Reconciliation: błąd pobierania portfolio do weryfikacji nadwyżek - {exc}")
    else:
        for trade in open_trades:
            expected = trade.baseline_owned_quantity + trade.quantity
            actual = owned_now.get(trade.ticker, Decimal("0"))
            surplus = actual - expected
            if surplus > Decimal("0.0001"):
                _log(
                    user_id, "WARN",
                    f"{trade.ticker}: NADWYŻKA wykryta przy weryfikacji - T212 pokazuje {actual}, "
                    f"baza oczekuje {expected} (baseline {trade.baseline_owned_quantity} + zlecenie "
                    f"{trade.quantity}), różnica {surplus}. Sprawdź ręcznie w T212 - możliwe podwójne "
                    "wykonanie zlecenia (cancel/replace race).",
                    trade.position_group_id,
                )

    if settings is not None:
        _confirm_dca_fills(user_id, client, settings, pending=pending)
        _retry_pending_buys(user_id, client, settings, pending=pending)
        _retry_pending_sells(user_id, client, settings, pending=pending)
        _manage_trailing_exit(user_id, client, settings)
        _trigger_dca_buys(user_id, client, settings)


def tick(app) -> None:
    """
    Wołane cyklicznie przez APScheduler (patrz app/__init__.py). `app` musi
    być prawdziwym obiektem Flask, nie proxy current_app - ten sam wzorzec
    co services/logo_cache.py::start_bulk_fetch (wątek/job w tle potrzebuje
    własnego app_context()).
    """
    with app.app_context():
        for user_id in bot_credentials.active_user_ids():
            settings = RiskSettings.query.filter_by(user_id=user_id).first()
            if not settings or not settings.is_bot_active:
                continue

            client = _get_client_for_user(user_id, settings)
            if client is not None:
                now = dt.datetime.utcnow()
                backoff = _tick_error_backoff.get(user_id)
                if backoff is not None and now < backoff[1]:
                    # Wciąż w backoffie po poprzednich błędach - pomijamy CAŁY
                    # T212-zależny odcinek tego ticku bez logowania (inaczej
                    # dokładnie ten sam spam co próbowaliśmy tu zlikwidować),
                    # żeby nie dokładać kolejnego zapytania do ciasnego limitu.
                    pass
                else:
                    # Jedno wspólne pobranie pending orders dla obu retry - unika
                    # dublowania zapytania w ciasnym rate limicie demo. Kolejność:
                    # najpierw goń kupno (bez tego sprzedaż i tak nie ma czego
                    # dotyczyć), potem sprzedaż.
                    try:
                        pending = client.get_pending_orders()
                    except T212APIError as exc:
                        # Backoff dla KAŻDEGO błędu tego zapytania, nie tylko 429 -
                        # 401/500/timeout uporczywie powtarzane co 60s to ten sam
                        # spam i to samo obciążenie ciasnego limitu demo co rate
                        # limit (patrz historia 2026-07-21 w komentarzu nad stałą).
                        consecutive = (backoff[0] if backoff else 0) + 1
                        delay = _next_tick_error_delay(consecutive)
                        _tick_error_backoff[user_id] = (consecutive, now + delay)
                        _log(
                            user_id, "ERROR",
                            f"Tick: błąd pobierania pending orders #{consecutive} z rzędu ({exc}) - "
                            f"kolejna próba za {int(delay.total_seconds() // 60)} min zamiast za 60s.",
                        )
                    else:
                        _tick_error_backoff.pop(user_id, None)
                        pending_ids = {str(o.get("id")) for o in pending}
                        _detect_exit_fills(user_id, client, pending_ids)
                        _confirm_dca_fills(user_id, client, settings, pending=pending)
                        _retry_pending_buys(user_id, client, settings, pending=pending)
                        _retry_pending_sells(user_id, client, settings, pending=pending)
                        _manage_trailing_exit(user_id, client, settings)
                        _trigger_dca_buys(user_id, client, settings)

            _process_entries(user_id, settings)


def _process_entries(user_id: int, settings: RiskSettings) -> None:
    """
    Strategia wejścia (dca_level=0) - PRD sekcja 3.1. Dla każdego BotAsset
    usera (WŁASNA lista bota, patrz models.py::BotAsset - niezależna od
    Smart Virtual Pie) z is_penny_stock=False - jeśli nie ma już otwartej
    pozycji na tym aktywie, otwiera nową.

    "Już otwarta" sprawdzane PO TICKERZE (user_id + ticker), NIE po
    bot_asset_id - znaleziony realny bug 2026-07-21: usunięcie tickera z
    listy bota i dodanie go z powrotem tworzy NOWY wiersz BotAsset (nowe
    id), więc stara otwarta pozycja (przypisana do już nieistniejącego
    bot_asset_id) stawała się "niewidoczna" dla tego sprawdzenia - bot
    otwierał DRUGĄ, niezależną pozycję na tym samym tickerze (ASMLa_EQ,
    złapane na żywo: dwie otwarte pozycje jednocześnie).

    NAJWYŻEJ JEDNO nowe wejście na tick, ale TYLKO jeśli faktycznie dotarło
    do T212 (return dopiero gdy _enter_position zwróci True) - znaleziony
    realny bug 2026-07-22: przy kilku BotAssetach na tej samej giełdzie
    (np. 5 tickerów EUR po otwarciu Euronext) wszystkie próbowały wejść w
    TYM SAMYM ticku, jedno zaraz po drugim - demo T212 ma tak ciasny rate
    limit na /equity/orders (patrz "0/1 pozostało" w logu), że tylko
    pierwsze zlecenie się udawało, a reszta dostawała 429 co tick (co 60s)
    w nieskończoność, bez szans na wejście. Jeden entry na tick naturalnie
    rozkłada zlecenia w czasie (kolejny asset dostanie szansę w
    następnym ticku, gdy limit się odnowi) zamiast próbować wszystkich
    naraz. Odrzucenia PRZED kontaktem z T212 (brak ceny, filtr trendu -
    patrz _entry_trend_ok) NIE zużywają tego slotu (continue, nie return) -
    inaczej jeden asset zablokowany trendem/brakiem ceny wiecznie
    zasłaniałby kolejne w liście.
    """
    open_count = ActiveTrade.query.filter_by(user_id=user_id, status="OPEN", is_paper=False).count()
    if open_count >= MAX_CONCURRENT_POSITIONS:
        return  # limit otwartych pozycji osiągnięty (patrz MAX_CONCURRENT_POSITIONS) - nic nowego dziś

    now = dt.datetime.utcnow()
    assets = BotAsset.query.filter_by(user_id=user_id, is_penny_stock=False).all()
    for asset in assets:
        if not _market_open(asset.currency):
            continue  # giełda właściwa dla tej waluty zamknięta - patrz _market_open
        already_open = ActiveTrade.query.filter_by(user_id=user_id, ticker=asset.ticker, status="OPEN").first()
        if already_open:
            continue
        backoff = _entry_fail_backoff.get((user_id, asset.ticker))
        if backoff is not None and now < backoff[1]:
            continue  # asset "w pauzie" po serii nieudanych prób - pomijamy, próbujemy dalej listy
        if _enter_position(user_id, asset, settings):
            return


def _entry_trend_ok(ticker: str) -> bool:
    """
    Filtr trendu przy wejściu - patrz ENTRY_TREND_* wyżej. Zwraca True
    (wejście dozwolone) gdy trend jest OK ALBO gdy nie da się go ocenić
    (brak świec z Finnhub/Yahoo) - filtr to DODATKOWA ochrona, nie twardy
    wymóg, przejściowa awaria źródła danych nie powinna całkiem zatrzymać
    wejść bota.
    """
    candles = price_feed.get_mini_chart_ohlc(
        current_app.config.get("FINNHUB_API_KEY"), ticker, days=ENTRY_TREND_LOOKBACK_DAYS,
    )
    if not candles or len(candles) < 2:
        return True

    oldest_close = Decimal(str(candles[0]["c"]))
    newest_close = Decimal(str(candles[-1]["c"]))
    if oldest_close <= 0:
        return True

    drop_pct = (oldest_close - newest_close) / oldest_close
    return drop_pct <= ENTRY_TREND_MAX_DROP_PCT


def _enter_position(user_id: int, asset: BotAsset, settings: RiskSettings) -> bool:
    """
    Zwraca True gdy dotarliśmy do faktycznego kontaktu z T212 (zlecenie
    wysłane, niezależnie od sukcesu) albo do zapisu pozycji papierowej -
    to "zużywa" slot jednego wejścia na tick (patrz _process_entries).
    Zwraca False przy wcześniejszych odrzuceniach (brak poświadczeń/ceny,
    filtr trendu, zła ilość) - te NIE dotykają T212 wcale (tylko Finnhub/
    Yahoo/Alpaca i lokalne liczenie), więc _process_entries może
    bezpiecznie spróbować kolejnego assetu w TYM SAMYM ticku zamiast
    czekać do następnego.
    """
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return False  # nie powinno się zdarzyć - user_id pochodzi z bot_credentials.active_user_ids()

    creds = get_decrypted_credentials(user_id, master_key, BOT_ENVIRONMENT)
    if creds is None:
        _log(user_id, "ERROR", f"{asset.ticker}: brak zapisanego klucza API demo.")
        return False

    price = price_feed.get_live_price(
        current_app.config.get("FINNHUB_API_KEY"), asset.ticker,
        current_app.config.get("ALPACA_API_KEY"), current_app.config.get("ALPACA_API_SECRET"),
    )
    if price is None or price <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: brak ceny (Finnhub i Yahoo zawiodły), pomijam ten tick.")
        return False

    if not _entry_trend_ok(asset.ticker):
        _log(
            user_id, "INFO",
            f"{asset.ticker}: pomijam wejście - cena spadła o więcej niż {ENTRY_TREND_MAX_DROP_PCT * 100}% "
            f"w ostatnich {ENTRY_TREND_LOOKBACK_DAYS} dniach (nie łapiemy spadającego noża).",
        )
        return False

    quantity = (asset.entry_amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: wyliczona ilość <= 0 (kwota {asset.entry_amount} / cena {price}).")
        return False

    buy_price = price
    allocated_value = quantity * buy_price
    position_group_id = str(uuid.uuid4())

    if settings.is_paper_trading:
        # Symulacja - ZERO requestow do T212, tylko zapis do ActiveTrade.
        # buy_confirmed=True od razu (nie ma czego czekać, "kupno" już się
        # "wykonało") - i tak bez znaczenia, bo _manage_trailing_exit i cała
        # reszta zarządzania wyjściem filtruje is_paper=False, więc pozycja
        # papierowa nigdy nie dostanie symulowanego trailing exitu (ten sam,
        # już wcześniej istniejący brak symulacji co przy dawnym target).
        trade = ActiveTrade(
            user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
            ticker=asset.ticker, currency=asset.currency,
            buy_order_id=f"PAPER-{uuid.uuid4()}", sell_order_id=None,
            buy_price=buy_price, quantity=quantity, allocated_value=allocated_value,
            average_price=buy_price, dca_level=0, status="OPEN", is_paper=True,
            buy_confirmed=True,
        )
        db.session.add(trade)
        db.session.commit()
        _log(
            user_id, "BUY",
            f"[PAPER] {asset.ticker}: symulowane wejście {quantity} @ ~{buy_price} - "
            "ŻADNE zlecenie nie poszło do T212 (i żaden trailing exit nie jest symulowany).",
            position_group_id,
        )
        return True

    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=BOT_ENVIRONMENT)

    # Zapis TUŻ PRZED złożeniem zlecenia - ile tickera user już posiada
    # (np. z ręcznego tradingu tą samą appką). _retry_pending_sells() później
    # odejmuje tę wartość od aktualnego portfolio, żeby wyizolować wkład
    # TEGO konkretnego zlecenia (patrz "Historia buga" punkt 3 w docstringu
    # modułu). Błąd tego zapytania NIE blokuje zakupu - to tylko dokładność
    # późniejszego dopasowania, nie warunek wejścia w pozycję; brak baseline
    # (0) w najgorszym razie odtwarza dawne, prostsze zachowanie.
    try:
        existing_position = client.get_position(asset.ticker)
    except T212APIError:
        existing_position = None
    baseline_owned_quantity = Decimal(str(existing_position["quantity"])) if existing_position else Decimal("0")

    # LIMIT BUY (marketable, po aktualnej cenie), NIE Market Order - zaobserwowane
    # 2026-07-20, że Market Order na tym demo potrafi wisieć NEW/niewypełniony
    # znacznie dłużej niż odpowiadający mu LIMIT BUY po tej samej cenie (który
    # wypełnia się natychmiast, bo jest "marketable" - limit >= cena rynkowa).
    # Jeśli cena i tak odjedzie zanim się wypełni, _retry_pending_buys()
    # (wołane z tick()) anuluje i ponowi po nowej cenie - patrz ta funkcja.
    try:
        buy_result, quantity = _place_buy_with_precision_fallback(client, asset.ticker, quantity, price)
    except T212APIError as exc:
        key = (user_id, asset.ticker)
        consecutive = _entry_fail_backoff.get(key, (0, dt.datetime.utcnow()))[0] + 1
        delay = _next_entry_fail_delay(consecutive)
        _entry_fail_backoff[key] = (consecutive, dt.datetime.utcnow() + delay)
        _log(
            user_id, "ERROR",
            f"{asset.ticker}: zakup nieudany ({consecutive}. próba z rzędu) - {exc} - "
            f"kolejna próba za {int(delay.total_seconds() // 60)} min, w międzyczasie pomijany.",
        )
        return True  # dotarliśmy do T212 (zlecenie odrzucone, ale slot na ten tick zużyty)
    _entry_fail_backoff.pop((user_id, asset.ticker), None)
    allocated_value = quantity * buy_price

    # LIMIT SELL NIE jest wystawiany tutaj (patrz historia buga w docstringu
    # modułu) - zakup może wciąż być w kolejce znacznie dłużej niż sensowny
    # blokujący retry, a nawet po wykonaniu faktycznie kupiona ilość może
    # różnić się od zażądanej (częściowe wykonanie). _retry_pending_sells()
    # (wołane z tick()) sprawdzi FAKTYCZNIE posiadaną ilość w portfolio T212 i
    # ustawi buy_confirmed=True - dopiero wtedy _manage_trailing_exit()
    # zacznie pilnować ceny i wystawi LIMIT SELL/STOP, gdy przyjdzie na to
    # pora (2 progi take_profit_step_pct, patrz ta funkcja).
    trade = ActiveTrade(
        user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
        ticker=asset.ticker, currency=asset.currency,
        buy_order_id=buy_result.order_id, sell_order_id=None,
        buy_price=buy_price, quantity=quantity, allocated_value=allocated_value,
        average_price=buy_price, dca_level=0, status="OPEN", is_paper=False,
        baseline_owned_quantity=baseline_owned_quantity,
        grid_anchor_price=buy_price, buy_confirmed=False,
    )
    db.session.add(trade)
    db.session.commit()

    # pie_id=None - BotAsset jest niezależne od Pie, więc te zlecenia nie
    # są przypisane do żadnego koszyka (patrz models.py::BotAsset).
    _log_order(
        user_id=user_id, ticker=asset.ticker, side="buy", quantity=quantity,
        price_snapshot=buy_price, status="sent", t212_order_id=buy_result.order_id,
    )
    _log(
        user_id, "BUY",
        f"{asset.ticker}: wejście {quantity} @ ~{buy_price} - bot zacznie zarządzać wyjściem "
        "(trailing take-profit + stop-loss) po potwierdzeniu kupna.",
        position_group_id,
    )
    return True


def daily_report(app) -> None:
    """
    Wołane raz dziennie o 22:01 czasu Amsterdamu (patrz
    app/__init__.py::_register_scheduler, tuż po zamknięciu NASDAQ/NYSE) -
    liczy zysk/stratę za ostatnie 24h i wysyła mailem na REPORT_TO_EMAIL (ten
    sam adres co "Zgłoś problem", patrz routes/report.py/services/mailer.py).
    Jeden wspólny mail dla wszystkich userów z choć jednym RiskSettings
    (czyli którzy kiedykolwiek dotknęli bota) - appka jest jednoosobowa w
    praktyce, ale to nie zakłada tego na sztywno.

    Zrealizowany zysk/strata: pozycje CLOSED w ostatnich 24h, liczone z
    ActiveTrade.close_price (dodane 2026-07-21 - patrz models.py, NULL dla
    starej ścieżki take-profit sprzed przeprojektowania OCO, wtedy pozycja
    jest wypisana bez kwoty zamiast zgadywać). Niezrealizowany: WSZYSTKIE
    aktualnie otwarte pozycje (nie tylko z ostatnich 24h - to stan "teraz",
    nie zdarzenie z okna czasowego), wyceniane żywą ceną z price_feed (ta
    sama funkcja co bot używa do decyzji).
    """
    with app.app_context():
        cutoff = dt.datetime.utcnow() - dt.timedelta(hours=24)
        sections: list[str] = []

        for settings in RiskSettings.query.all():
            user = User.query.get(settings.user_id)
            if user is None:
                continue

            closed = (
                ActiveTrade.query
                .filter_by(user_id=settings.user_id, is_paper=False, status="CLOSED")
                .filter(ActiveTrade.closed_at >= cutoff)
                .all()
            )
            realized_total = Decimal("0")
            realized_unknown = 0
            closed_lines = []
            for t in closed:
                if t.close_price is not None:
                    pnl = (t.close_price - t.buy_price) * t.quantity
                    realized_total += pnl
                    closed_lines.append(f"  ZAMKNIĘTA {t.ticker}: {pnl:+.2f} {t.currency} (wejście {t.buy_price}, wyjście {t.close_price})")
                else:
                    realized_unknown += 1
                    closed_lines.append(f"  ZAMKNIĘTA {t.ticker}: cena wyjścia nieznana (sprzed 2026-07-21)")

            open_trades = ActiveTrade.query.filter_by(user_id=settings.user_id, is_paper=False, status="OPEN").all()
            unrealized_total = Decimal("0")
            unrealized_known = 0
            open_lines = []
            for t in open_trades:
                price = price_feed.get_live_price(
                    current_app.config.get("FINNHUB_API_KEY"), t.ticker,
                    current_app.config.get("ALPACA_API_KEY"), current_app.config.get("ALPACA_API_SECRET"),
                )
                if price is None:
                    open_lines.append(f"  OTWARTA {t.ticker}: brak żywej ceny")
                    continue
                pnl = (price - t.average_price) * t.quantity
                unrealized_total += pnl
                unrealized_known += 1
                open_lines.append(f"  OTWARTA {t.ticker}: {pnl:+.2f} niezrealizowane (średnia {t.average_price}, teraz {price})")

            if not closed and not open_trades:
                continue  # user ma RiskSettings, ale nigdy nie miał żadnej pozycji - nic do raportowania

            lines = [
                f"=== {user.username} ===",
                f"Zrealizowany zysk/strata (24h): {realized_total:+.2f} "
                f"({len(closed)} zamkniętych" + (f", {realized_unknown} bez znanej ceny wyjścia" if realized_unknown else "") + ")",
                f"Niezrealizowany zysk/strata (teraz): {unrealized_total:+.2f} "
                f"({unrealized_known}/{len(open_trades)} otwartych wycenionych)",
            ]
            lines += closed_lines + open_lines
            sections.append("\n".join(lines))

        if not sections:
            return  # nikt nigdy nie uzywal bota - nic do raportowania

        body_text = "\n\n".join(sections)
        try:
            mailer.send_report(
                smtp_host=current_app.config["SMTP_HOST"], smtp_port=current_app.config["SMTP_PORT"],
                smtp_user=current_app.config["SMTP_USER"], smtp_password=current_app.config["SMTP_PASSWORD"],
                mail_from=current_app.config["SMTP_FROM"], mail_to=current_app.config["REPORT_TO_EMAIL"],
                subject=f"[SNAJPER] Dzienny raport bota - {dt.datetime.utcnow().strftime('%Y-%m-%d')} (UTC)",
                body_text=body_text,
            )
        except (mailer.MailerNotConfiguredError, mailer.MailSendError) as exc:
            # Brak dobrego miejsca na zalogowanie tego usera (to nie akcja
            # z UI konkretnego usera) - tylko do stdout/stderr procesu
            # (widoczne w /tmp/sniper_start.log).
            print(f"[daily_report] wysyłka nie powiodła się: {exc}")
