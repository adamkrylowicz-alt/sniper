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

Bot do 2026-08-06 działał WYŁĄCZNIE na demo (stara, HISTORYCZNA teza -
patrz UPDATE niżej - że T212 nie wspiera zleceń LIMIT na koncie live,
patrz `routes/bot.py::activate()`, blokada aktywacji bez klucza demo) - od
tego dnia środowisko jest per-user (patrz `utils.current_environment`/
`UserSettings.active_environment`), Adam świadomie testuje na koncie live.

UPDATE 2026-08-06/07: stara teza OBALONA empirycznie, ręcznie przez Adama
(Warp/instrument, NIE przez żaden z 3 silników bota - te wciąż mają
`stop_loss_only_mode` jako dodatkowy bezpiecznik, patrz niżej): LIMIT BUY
zadziałał na live 22:07:56 UTC (SPCX_US_EQ), STOP-LIMIT BUY zadziałał na
live 23:31:01 UTC (SPCX_US_EQ, order_id 55310447353, po 3 wcześniejszych
400 przy nierealistycznych cenach). Czysty STOP (bez limitu) NIE był
jeszcze osobno testowany na live (tylko na demo, przed migracją) - biorąc
pod uwagę że STOP-LIMIT (bardziej złożony typ) działa, prawdopodobnie
zadziała też, ale to WNIOSEK, nie potwierdzony fakt.

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

5. Znalezione na żywo 2026-07-30 (DTEd_EQ): "gonienie" ceny z punktu 4
   wystawiało nowe LIMIT BUY przez surowe client.place_limit_order(...),
   pomijając _place_buy_with_precision_fallback() z punktu 4 sekcji
   "Pętla DCA" - w przeciwieństwie do _enter_position()/_trigger_dca_buys(),
   które już go używały. Efekt na żywo: stare zlecenie anulowane, nowe
   odrzucone przez quantity-precision-mismatch, ŻADNE zlecenie kupna nie
   zostawało złożone - pozycja utykała trwale w buy_confirmed=False (retry
   co 30 min w nieskończoność, bo _confirm_buy_fill nie odróżnia "czeka na
   fill" od "nigdy nie złożono zlecenia"). Fix: _retry_pending_buys() woła
   teraz _place_buy_with_precision_fallback() jak pozostałe trzy miejsca
   składania zleceń.
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
from ..models import ActiveTrade, BotAsset, BotAuditLog, Instrument, RiskSettings, User
from ..routes.api_keys import get_decrypted_credentials
from ..routes.scalping import _log_order
from ..utils import current_environment, humanize_ticker_prefix, telegram_env_tag, ticker_display_name
from . import bot_credentials, bot_entry_filters, diagnostics, mailer, position_alerts, price_feed, price_watchdog, sector_diversity, telegram_notify
from .market_data_keys import get_decrypted_market_data_keys
from .strategy import microgrid_strategy
from .t212_client import T212APIError, T212Client

# BOT_ENVIRONMENT jako stała modułowa USUNIĘTA 2026-08-06 (była zawsze
# "demo" na sztywno) - zastąpiona per-userowym `utils.current_environment
# (user_id)` (patrz models.py::UserSettings.active_environment), Adam:
# "przełącz na live... i dodaj guzik przełącznik live demo". Stara teza
# "T212 nie wspiera zleceń LIMIT/STOP na koncie live" (patrz historia
# niżej) OBALONA empirycznie 2026-08-06/07 (LIMIT i STOP-LIMIT ręcznie
# potwierdzone działające na live przez Adama, patrz docstring modułu
# wyżej) - mimo to boty NIE są odblokowane na live automatycznie przez
# samo działanie tych typów zleceń, to osobna, świadoma decyzja Adama.

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

# Dodane 2026-07-29 (Adam: "niektore requesty powinny miec prio jak ochrona
# zysku, a nie doprowadzac do oczekiwan po 30 min") - realny przypadek
# ASML_US_EQ: KAŻDA próba wystawienia trailing STOP-a na PEŁNĄ trade.quantity
# dostawała 400 selling-equity-not-owned (świeżo wypełniona noga DCA jeszcze
# się rozliczała), a stary kod (_bump_retry) traktował to jak dowolny,
# niewyjaśniony błąd i wchodził w rosnący backoff (SELL_RETRY_BACKOFF_MINUTES,
# do 30 min) - pozycja została bez ochrony na 7+ godzin, mimo że treść błędu
# ("Selling more equities than owned, owned: X") WPROST mówi ile da się
# sprzedać JUŻ TERAZ. WAŻNE: to NIE jest prawdziwy stan posiadania (Adam
# potwierdził 29.07 - portfolio 0.3709 było poprawne, T212 tylko chwilowo nie
# pozwalał sprzedać nierozliczonej części) - używane WYŁĄCZNIE jako fallback
# rozmiaru NA JEDNĄ próbę zlecenia (patrz _manage_trailing_exit), nigdy do
# nadpisania trade.quantity.
_OWNED_QUANTITY_RE = re.compile(r"owned:\s*([\d.]+)")


def _extract_owned_quantity_from_error(exc: T212APIError) -> Decimal | None:
    """
    Zwraca ile T212 pozwala sprzedać JUŻ TERAZ, wyciągnięte z treści błędu
    selling-equity-not-owned - NIE jest to koniecznie pełny, prawdziwy stan
    posiadania (część pozycji może być po prostu jeszcze nierozliczona), więc
    wywołujący ma użyć tego WYŁĄCZNIE jako rozmiaru zlecenia na tę jedną
    próbę, nie jako nowej wartości trade.quantity. Zwraca None gdy błąd innego
    typu/nie da się sparsować - wtedy wywołujący ma zrobić zwykły _bump_retry
    (rosnący backoff nadal ma sens dla błędów, których przyczyny NIE znamy,
    np. 429).
    """
    if not isinstance(exc.payload, dict):
        return None
    if exc.payload.get("type") != SELLING_EQUITY_NOT_OWNED_ERROR_TYPE:
        return None
    match = _OWNED_QUANTITY_RE.search(str(exc.payload.get("detail", "")))
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None

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
    _AMSTERDAM_TZ, EU_SESSION_WINDOW, US_SESSION_WINDOW, held_by_other_engine,
    is_market_open as _market_open, is_position_management_hours as _position_hours,
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
#
# PRZENIESIONE 2026-08-03 z twardej stałej do RiskSettings.fx_cost_adjustment_
# enabled/fx_fee_pct (edytowalne w UI, ten sam mechanizm dodany też w Sygnale/
# EOD) - _manage_trailing_exit() NIE używa już tych stałych wprost, czyta z
# przekazanego `settings`. ZOSTAJĄ tutaj (nieużywane w tym pliku poza samą
# definicją) WYŁĄCZNIE bo `backtest/microgrid_runner.py` importuje
# FX_ROUND_TRIP_PCT wprost stąd (ten sam import, który dziś wcześniej już raz
# ucierpiał przy migracji MAX_CONCURRENT_POSITIONS - nie powtarzać tego błędu)
# - usunięcie zepsułoby backtest. Wartości = domyślne z migracji, dla
# spójności.
FX_FEE_PCT = Decimal("0.0015")
FX_ROUND_TRIP_PCT = FX_FEE_PCT * 2

# Francuski podatek od transakcji finansowych (FTT/TTF) - dodane 2026-08-07
# (Adam: "trzeba to doliczać do kosztów żeby nie tracić"). W ODRÓŻNIENIU od
# FX_ROUND_TRIP_PCT wyżej - nalicza się TYLKO przy KUPNIE (nie x2, sprzedaż
# jest wolna od podatku), więc bez FX_ROUND_TRIP_PCT-owego podwojenia. Stawka
# 0.4% (podniesiona z 0.3% w kwietniu 2025, potwierdzone wyszukiwaniem
# 2026-08-07 - Keytrade Bank/shares.io) dotyczy zakupu akcji francuskich
# spółek o kapitalizacji >1mld EUR przez T212. CELOWO twarda lista tickerów
# (nie heurystyka po samym sufiksie giełdy Paryż "p_EQ") - nie każda spółka
# na Euronext Paris ma kapitalizację >1mld€, więc zgadywanie po sufiksie
# dawałoby fałszywe pozytywy. Rozszerzać ręcznie w miarę dodawania kolejnych
# francuskich spółek do list botów (FPp_EQ=TotalEnergies, SUp_EQ=Schneider
# Electric - oba potwierdzone jako CAC 40, kwalifikują się).
FR_FTT_PCT = Decimal("0.004")
FR_FTT_TICKERS = frozenset({"FPp_EQ", "SUp_EQ"})

# Ciagly trailing (przeprojektowane 2026-07-22, patrz _manage_trailing_exit) -
# minimalna poprawa wzgledem AKTUALNEGO stop_target_price zeby w ogole
# oplacalo sie robic Cancel-Replace. Bez tego progu STOP probowalby sie
# przesuwac praktycznie co tick przy najmniejszym ruchu ceny w gore - przy
# kilkunastu jednoczesnie otwartych pozycjach zjadloby to caly i tak ciasny
# rate limit demo T212 na drobne, nieistotne poprawki. Polowa kroku
# take_profit_step_pct to kompromis: duzo czesciej niz dawny "tylko przy
# pelnym progu", ale nie przy kazdym centcie.
MIN_TRAIL_REQUOTE_FRACTION = Decimal("0.5")

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
#
# ZMIENIONE 2026-08-03 z 10 na 6 (Adam: realny budżet ~1000€ - pierwsza
# propozycja "1-2 pozycje" dała w backteście dużo wyższy drawdown (do 15% w
# porównaniu do 5-8% przy większej dywersyfikacji) z powodu koncentracji
# kapitału w niewielu pozycjach - Adam wybrał "zwiększ do 5-6 i sprawdź";
# 6 dało nieco niższy zwrot ORAZ niższy drawdown niż 5 (12.86%/5.15%dd vs
# 15.43%/6.15%dd na treningu), więc wybrane jako spokojniejsze) - przy
# max_dca_levels=7 i entry_amount=100 stare 10 dawało teoretyczne maksimum
# 10*7*100=7000€ jednocześnie zaangażowanego kapitału, wielokrotnie więcej
# niż realny budżet. Przy 6 pozycjach i entry_amount PRZELICZONYM na
# ~16.67€/nogę (patrz UPDATE BotAsset.entry_amount tego dnia) worst-case =
# 6*7*16.67≈700€, dokładnie budżet przypisany temu silnikowi (700€ z 1000€
# całości, reszta 300€ na Sygnał).
#
# PRZENIESIONE 2026-08-03 (wieczorem) ze stałej modułowej do
# RiskSettings.max_concurrent_positions (edytowalne w UI per-user, bez
# redeployu) - Adam: "to tylko ustawienia fabryczne", 6 zostaje jako DEFAULT
# nowej kolumny (patrz migrate_add_max_concurrent_positions.py), read z
# settings.max_concurrent_positions w miejscach użycia poniżej.

# Stop-loss oparty o realna zmiennosc instrumentu (ATR - Average True Range)
# ZAMIAST sztywnego % (RiskSettings.stop_loss_pct) - dodane 2026-07-22 na
# zyczenie Adama po analizie dnia, patrz docs/IDEAS_v2.md "Zarzadzanie
# ryzykiem": "Stop Loss: 1.8 x ATR(14)" - ten sam mnoznik. Dane: swiece
# dzienne z price_feed.get_mini_chart_ohlc (Finnhub->Yahoo, TEN SAM caly
# mechanizm co filtr trendu wyzej, wlacznie z jego wlasnym 30-min cache'em -
# zero nowego obciazenia zewnetrznych API na kazdy tick). Gdy danych brak
# (429, brak pokrycia symbolu, za krotka historia) - _get_atr_stop_distance
# zwraca None, a _manage_trailing_exit CICHO spada z powrotem na stary
# stop_loss_pct - zero twardej zaleznosci od tego nowego zrodla danych.
ATR_PERIOD = 14
ATR_LOOKBACK_DAYS = ATR_PERIOD + 5  # bufor - swiece dzienne maja dziury (weekendy/swieta)
ATR_STOP_MULTIPLIER = Decimal("1.8")

# Dedykowany fallback (CELOWO nie RiskSettings.stop_loss_pct) dla pozycji
# chronionych przez „Tylko stop-loss”/ręczną adopcję - dodane 2026-08-07,
# Adam znalazł że adoptowana FPp_EQ miała ZERO ochrony dopóki cena nie
# urośnie o 2*take_profit_step_pct (u niego 0.4%) - w tym trybie dca_level
# zostaje na zawsze 0 (DCA wyłączone), więc stary wyjątek „wyczerpane DCA”
# (dca_level == max_dca_levels-1) nigdy się nie odpala. Używany TYLKO gdy
# ATR niedostępny (normalnie floor idzie z ATR*1.8 - realna zmienność
# instrumentu, ten % to tylko siatka bezpieczeństwa gdy danych brak).
# Osobna stała od stop_loss_pct (2%, używana przez bota do własnych,
# algorytmicznych wejść/DCA, tuned/backtestowana) - inny cel: tu chodzi o
# jednorazową ręczną ochronę konkretnej, świadomie wskazanej pozycji, nie o
# parametr strategii scalpingowej. Adam poprosił o -5% po pytaniu „jak inni
# robią” - rozsądny, szeroki fallback dla pojedynczej blue-chip pozycji
# (TotalEnergies), zero wpływu na resztę bota.
MANUAL_PROTECTION_FALLBACK_PCT = Decimal("0.05")


def _compute_atr(candles: list[dict] | None, period: int = ATR_PERIOD) -> Decimal | None:
    """
    True Range dla swiecy i = max(high-low, |high-prev_close|, |low-prev_close|),
    ATR = prosta srednia (nie wygladzanie Wildera, dla prostoty) ostatnich
    `period` wartosci TR. Wymaga co najmniej period+1 swiec (potrzebny
    poprzedni close pierwszej liczonej swiecy) - None gdy za malo danych.

    PERF (2026-08-02, znalezione przy próbie grid searchu Sygnału na 58
    tickerach - zabite po 23 min bez postępu): stara wersja liczyła True
    Range dla CAŁEGO przekazanego `candles` (w backteście Micro-Gridu to
    cała historia do bieżącego dnia, rosnąca - realny O(n^2) na ticker), a
    dopiero na końcu brała ostatnie `period` wartości. Przycięcie do
    `period+1` PRZED pętlą daje IDENTYCZNY wynik (TR[i] zależy tylko od
    świec i/i-1, ostatnie `period` TR nie zależą od tego ile świec jest
    przed nimi) - zweryfikowane 200 losowymi testami + przypadkami
    brzegowymi przed zmianą.
    """
    if not candles or len(candles) < period + 1:
        return None

    window = candles[-(period + 1):]
    true_ranges = []
    for i in range(1, len(window)):
        high = Decimal(str(window[i]["h"]))
        low = Decimal(str(window[i]["l"]))
        prev_close = Decimal(str(window[i - 1]["c"]))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    return sum(true_ranges) / len(true_ranges)


def _get_atr_stop_distance(user_id: int, ticker: str) -> Decimal | None:
    """Dystans W WALUCIE INSTRUMENTU (nie %) = ATR(14) * ATR_STOP_MULTIPLIER, albo None gdy brak danych."""
    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
    candles = price_feed.get_mini_chart_ohlc(
        market_keys.get("finnhub_api_key"), ticker, days=ATR_LOOKBACK_DAYS,
        alpaca_api_key=market_keys.get("alpaca_api_key"),
        alpaca_api_secret=market_keys.get("alpaca_api_secret"),
    )
    atr = _compute_atr(candles)
    if atr is None:
        return None
    return atr * ATR_STOP_MULTIPLIER


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


# Backoff dla tick()::get_pending_orders po DOWOLNYM błędzie T212API PRZENIESIONY
# do t212_client.py::get_pending_orders_for_tick() (2026-07-29, Adam: "musisz
# jakos wspolnie korelowac te wejscia, nie moze byc ze boty nie widza o sobie
# i napierdlaja w ten sam czas") - był tu WŁASNY, silnik-specyficzny licznik
# (`_tick_error_backoff`/`TICK_ERROR_BACKOFF_MINUTES`), niezależny od tych
# samych w signal_engine.py/eod_engine.py, mimo że wszystkie trzy dobijają się
# o TEN SAM, wspólny, bardzo ciasny limit T212 demo - efekt: Sygnał mógł
# złapać serię 429 i wejść w 30-min backoff mimo że Micro-Grid/EOD w tym samym
# czasie ticowały bez błędu, bo każdy silnik "wiedział" tylko o WŁASNYCH
# nieudanych próbach. Teraz `client.get_pending_orders_for_tick()` zwraca None
# (skip, bez logowania) gdy WSPÓLNY backoff jest aktywny, albo listę/wyjątek
# jak dawny get_pending_orders() - patrz call site w tick() niżej.

# (user_id, ticker) -> (kolejnych nieudanych prób wejścia z rzędu, kiedy
# wolno spróbować znowu). W pamięci procesu, jak _shared_tick_backoff w
# t212_client.py (choć TEN konkretny licznik jest per-trade, nie wspólny).
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

# Cooldown dla sygnałów "kup ręcznie" na Telegramie (stop_loss_only_mode,
# dodane 2026-08-07) - bez tego ten sam najlepszy kandydat wysyłałby
# identyczny sygnał co tick (60s) dopóki Adam go ręcznie nie kupi/przekaże
# botowi (dopiero wtedy staje się "już otwarty" i znika z eligible). W
# pamięci procesu, per (user_id, ticker), restart zeruje - akceptowalne,
# najwyżej jeden dodatkowy sygnał zaraz po restarcie.
_entry_signal_sent_at: dict[tuple[int, str], dt.datetime] = {}
ENTRY_SIGNAL_COOLDOWN_MINUTES = 60

# "nie" na Telegramie (Adam, 2026-08-07: "jak napiszę nie niech mi go nie
# podpowiada przez 5min") - odrzucenie AKTUALNIE zasugerowanego kandydata na
# krótko, żeby _process_entries zaproponował na kolejnym ticku NASTĘPNEGO w
# kolejności zamiast wisieć na tym samym (60-minutowy ENTRY_SIGNAL_COOLDOWN
# wyżej to co innego - chroni przed spamem TEGO SAMEGO tickera, nie daje
# szansy zobaczyć alternatywy). _last_signal_ticker pamięta co ostatnio
# poszło na Telegram, żeby telegram_commands.py::poll_and_handle wiedziało
# do czego "nie" się odnosi (Telegram nie ma tu wątków/reply-context).
_last_signal_ticker: dict[int, str] = {}
_entry_signal_rejected_until: dict[tuple[int, str], dt.datetime] = {}
ENTRY_SIGNAL_REJECT_MINUTES = 5


def reject_current_signal(user_id: int) -> str | None:
    """
    Wołane z telegram_commands.py gdy Adam odpisze "nie" - wyklucza ostatnio
    zasugerowanego tickera z eligible (patrz _process_entries) na
    ENTRY_SIGNAL_REJECT_MINUTES. Zwraca display name odrzuconego tickera do
    potwierdzenia na Telegramie, albo None gdy nic nie było ostatnio
    zasugerowane (np. "nie" napisane bez wcześniejszego sygnału).
    """
    ticker = _last_signal_ticker.get(user_id)
    if ticker is None:
        return None
    _entry_signal_rejected_until[(user_id, ticker)] = dt.datetime.utcnow() + dt.timedelta(minutes=ENTRY_SIGNAL_REJECT_MINUTES)
    return ticker_display_name(ticker)


def adopt_confirmed_signal(user_id: int) -> tuple[bool, str]:
    """
    Wołane z telegram_commands.py gdy Adam odpisze "kupiłem" po sygnale
    (2026-08-07: "bot dał sygnał, kupiłem, niech on to zrozumie słowo
    kupiłem") - to samo co przycisk "Przekaż botowi" (routes/bot.py::
    adopt_position), tylko bez requestu/sesji przeglądarki (Telegram poll
    nie ma flask.g) - stąd master_key z bot_credentials, nie
    current_master_key(). Ticker brany z _last_signal_ticker (ten sam
    mechanizm co reject_current_signal) - "kupiłem" bez wcześniejszego
    sygnału nie ma do czego się odnieść. BotAsset MUSI już istnieć (sygnał
    leci tylko dla tickerów z listy bota), więc w odróżnieniu od
    routes/bot.py::adopt_position nie ma tu gałęzi "podaj entry_amount".
    """
    ticker = _last_signal_ticker.get(user_id)
    if ticker is None:
        return False, "Nie było żadnego świeżego sygnału do potwierdzenia."

    env = current_environment(user_id)
    instrument = Instrument.query.get(ticker)
    if instrument is None:
        return False, f"{ticker} nie znaleziony w lokalnej bazie instrumentów."

    if ActiveTrade.query.filter_by(user_id=user_id, ticker=ticker, status="OPEN", environment=env).first() is not None:
        return False, f"{ticker_display_name(ticker)} jest już zarządzany przez bota."

    other = held_by_other_engine(user_id, ticker, "bot")
    if other is not None:
        return False, f"{ticker_display_name(ticker)} jest już zarządzany przez {other} - zwolnij go tam najpierw."

    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return False, "Bot nie ma aktywnych poświadczeń (aktywuj go w appce)."
    creds = get_decrypted_credentials(user_id, master_key, env)
    if creds is None:
        return False, "Brak zapisanego klucza API."
    client = T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"], environment=env,
        engine="bot", user_id=user_id,
    )

    try:
        position = client.get_position(ticker)
    except T212APIError as exc:
        return False, f"Błąd T212: {exc}"

    if position is None:
        return False, f"Nie widzę {ticker_display_name(ticker)} w portfelu T212 - kupno jeszcze się nie rozliczyło? Spróbuj za chwilę."

    try:
        quantity = Decimal(str(position["quantity"]))
        avg_price = Decimal(str(position["averagePrice"]))
    except (KeyError, InvalidOperation, TypeError):
        return False, "Nieprawidłowe dane pozycji zwrócone przez T212."

    if quantity <= 0:
        return False, f"{ticker_display_name(ticker)}: ilość w portfelu wynosi 0."

    asset = BotAsset.query.filter_by(user_id=user_id, ticker=ticker, environment=env).first()
    if asset is None:
        return False, f"{ticker} nie jest już na liście bota (usunięty?) - dodaj go ręcznie w appce."

    position_group_id = str(uuid.uuid4())
    trade = ActiveTrade(
        user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
        ticker=ticker, currency=instrument.currency_code or "USD",
        buy_order_id=f"ADOPTED-TG-{uuid.uuid4()}",
        buy_price=avg_price, quantity=quantity, allocated_value=quantity * avg_price,
        average_price=avg_price, dca_level=0, grid_anchor_price=avg_price,
        baseline_owned_quantity=Decimal("0"),
        status="OPEN", is_paper=False, buy_confirmed=True,
        environment=env,
    )
    db.session.add(trade)
    db.session.commit()

    _log(
        user_id, "INFO",
        f"{ticker}: pozycja adoptowana ręcznie z portfela T212 przez Telegram ({quantity} @ ~{avg_price}) - "
        "od teraz zarządzana przez trailing exit bota.",
        position_group_id,
    )

    settings = RiskSettings.query.filter_by(user_id=user_id).first()
    if settings is not None:
        try:
            _manage_trailing_exit(user_id, client, settings)
        except Exception as exc:  # noqa: BLE001 - najlepsza proba, nie krytyczne
            _log(
                user_id, "ERROR",
                f"{ticker}: natychmiastowy trailing check po adopcji (Telegram) nie powiódł się ({exc}).",
                position_group_id,
            )

    _entry_signal_sent_at.pop((user_id, ticker), None)
    _last_signal_ticker.pop(user_id, None)
    return True, f"{ticker_display_name(ticker)}: przejąłem ({quantity} @ ~{avg_price}). Pilnuję teraz trailing stopu."


def close_active_trade_manual(user_id: int, client: T212Client, trade: ActiveTrade, price) -> None:
    """
    Zamknięcie pozycji Micro-Grid NA ŻĄDANIE (Telegram `/close`, Adam
    2026-08-09) - w odróżnieniu od `_finalize_closed_trade` (dla
    AUTOMATYCZNYCH wypełnień STOP-a, oczekuje `filled_via`
    "take-profit"/"stop-loss" i szuka "osieroconej drugiej nogi" do
    anulowania) tutaj JEDYNA aktywna noga (`stop_order_id`) jest anulowana
    TUTAJ, więc po Market-sellu nie ma już czego szukać - stąd bezpośredni
    zapis statusu zamiast wołania `_finalize_closed_trade` z niepasującym
    `filled_via`. Wzorowane na routes/signal.py::close_position (ten sam
    kształt: cancel stop -> market sell -> log -> finalize), ale klient
    T212 tu przychodzi już zbudowany przez wywołującego (Telegram nie ma
    sesji Flask - patrz adopt_confirmed_signal dla identycznego wzorca
    pozyskania poświadczeń przez bot_credentials.get_master_key).
    """
    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            _log(
                user_id, "INFO",
                f"{trade.ticker}: anulowanie stop-lossa przed ręczną sprzedażą nie powiodło się "
                f"(mógł się już wykonać) - {exc}",
                trade.position_group_id,
            )

    sell_result = client.place_market_order(trade.ticker, -trade.quantity)
    price_decimal = Decimal(str(price)) if price is not None else None
    _log_order(
        user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
        price_snapshot=price_decimal, status="sent", t212_order_id=sell_result.order_id,
    )
    _log(
        user_id, "INFO",
        f"{trade.ticker}: zamknięte ręcznie przez Telegram ({trade.quantity} @ ~{price_decimal}).",
        trade.position_group_id,
    )
    trade.close_price = price_decimal
    trade.status = "CLOSED"
    trade.closed_at = dt.datetime.utcnow()
    db.session.commit()


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


def _log_block_reason_throttled(user_id: int, key: str, message: str) -> None:
    """
    Dopisane 2026-08-09 (raport /why na Telegramie, patrz
    telegram_commands.py) - powody blokady wejścia (limit pozycji, cudza
    pozycja, backoff) potrafią trwać wiele ticków z rzędu; bez throttlingu
    zwykłe _log() zalałoby BotAuditLog identycznym wpisem co 60s. Dedupe
    (30 min/klucz) scalony w diagnostics.should_log_throttled - była to
    identyczna kopia w bot_engine.py/eod_engine.py/signal_engine.py, prefiks
    "bot:" żeby nie kolidować z tymi samymi kluczami ("max_concurrent" itd.)
    w pozostałych dwóch silnikach na dzielonym słowniku.
    """
    if diagnostics.should_log_throttled(user_id, f"bot:{key}"):
        _log(user_id, "INFO", message)


def _log(user_id: int, action_type: str, message: str, position_group_id: str | None = None) -> None:
    """
    ERROR leci do osobnego pliku tekstowego (_log_error_to_file), NIE do
    BotAuditLog/Dziennika w appce - patrz komentarz przy BOT_ERROR_LOG_FILENAME.
    Wszystko inne (BUY/INFO/WARN) zapisywane jak dotychczas, z commitem
    natychmiast (każdy wpis niezależny, ten sam styl co OrderLog).

    Dodane 2026-07-30: KAŻDE wywołanie (niezależnie od action_type) leci
    TAKŻE do diagnostics.py::log_diag() - ukryty log, nie zmienia niczego
    z powyższego (Dziennik/plik błędów zostają jak były), tylko dokłada
    kopię do wewnętrznej diagnostyki (instance/bot_diagnostics.log).
    """
    diagnostics.log_diag(user_id, "bot", f"[{action_type}] {message}")
    if action_type == "ERROR":
        _log_error_to_file(user_id, message)
        telegram_notify.send_telegram_message(
            current_app.config.get("TELEGRAM_BOT_TOKEN"), current_app.config.get("TELEGRAM_CHAT_ID"),
            f"🔴 [{telegram_env_tag(user_id)}] Micro-Grid ERROR (user {user_id}): {humanize_ticker_prefix(message)}",
        )
        return
    db.session.add(BotAuditLog(
        user_id=user_id, action_type=action_type, message=message,
        position_group_id=position_group_id, environment=current_environment(user_id),
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
    creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
    if creds is None:
        return None
    return T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
        engine="bot", user_id=user_id,
    )


def _get_current_equity(
    user_id: int, settings: RiskSettings, client: T212Client | None = None,
) -> Decimal | None:
    """
    Equity CAŁEGO konta T212 (gotówka+pozycje) - get_cash() to zawsze odczyt
    REALNEGO konta demo, NIEZALEŻNIE od trybu paper trading (to read-only
    zapytanie, bezpieczne niezależnie od trybu). Gdy `client` już istnieje
    (np. z tick()/reconcile(), gdzie jest budowany niezależnie od paper
    tradingu) - używamy go (zero dodatkowego odczytu poświadczeń). `client`
    bywa None dla paper trading (patrz _get_client_for_user), więc wtedy
    budujemy WŁASNY, dedykowany klient tylko do odczytu equity.

    Wołane RAZ na cały tick()/reconcile() (patrz equity_sizing_enabled tam) -
    budżet rate limitu /equity/account/summary (1 req/5.5s, dzielony między
    silnikami Micro-Grid/Sygnał/EOD) starcza z ogromnym zapasem przy jednym
    wywołaniu na 60s tick.

    None gdy: brak poświadczeń albo błąd T212/parsowania - wywołujący
    (compute_equity_scaled_amount) traktuje to jak "brak danych", zwracając
    bazową kwotę bez zmian (fail-safe, jak inne filtry w tym silniku).
    """
    if client is None:
        master_key = bot_credentials.get_master_key(user_id)
        if master_key is None:
            return None
        creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
        if creds is None:
            return None
        client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
            engine="bot", user_id=user_id,
        )
    try:
        raw = client.get_cash()
        return Decimal(str(raw["total"]))
    except (T212APIError, InvalidOperation, TypeError, KeyError) as exc:
        diagnostics.log_diag(
            user_id, "bot", f"equity sizing: nie udało się pobrać equity ({exc}) - baza bez skalowania.",
        )
        return None


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


def _portfolio_positions(client: T212Client) -> dict[str, dict]:
    """
    Jak _portfolio_quantities() wyżej, ale zwraca też averagePrice - dodane
    2026-08-07 dla _manage_trailing_exit() (Adam: "popraw żeby przeliczał
    average_price przy dokupieniu"). T212 sam liczy poprawną, uśrednioną
    cenę zakupu po dokupieniu (uwzględnia FX/opłaty lepiej niż my byśmy
    ręcznie przeliczali z samej ilości) - więc przy wykryciu wzrostu ilości
    (manualne dokupienie poza botem) bierzemy JEGO averagePrice wprost,
    zamiast liczyć własną średnią ważoną. Osobna funkcja (nie rozszerzenie
    _portfolio_quantities) żeby nie dotykać 3 pozostałych wywołań tamtej
    funkcji (_confirm_buy_fill/_retry_pending_buys/_confirm_dca_fills), które
    potrzebują tylko ilości.
    """
    portfolio = client.get_portfolio()
    result: dict[str, dict] = {}
    for p in portfolio:
        ticker = p.get("ticker")
        if not ticker:
            continue
        avg_price = p.get("averagePrice")
        result[ticker] = {
            "quantity": Decimal(str(p.get("quantity", 0))),
            "average_price": Decimal(str(avg_price)) if avg_price is not None else None,
        }
    return result


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
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False, environment=current_environment(user_id))
        .filter(db.or_(ActiveTrade.next_sell_retry_at.is_(None), ActiveTrade.next_sell_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if _position_hours(t.currency)]
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

    stop_loss_only_mode (2026-08-06, znalezione na żywo - Adam: "jakie
    gonienie ceny bot miał TYLKO SL stawiać!!!!!!!!") - cancel-then-replace
    tutaj to REALNE nowe zlecenie kupna (inna ilość, inna cena), więc w tym
    trybie jest tak samo zakazane jak _process_entries/_trigger_dca_buys.
    Zablokowane na starcie funkcji (nie per-call-site) - dotyczy to WSZYSTKICH
    wywołań, łącznie z reconcile(). Utknięte LIMIT BUY po prostu zostają
    w kolejce T212 na starym limicie, aż się wypełnią same albo user je
    ręcznie anuluje - żadnej automatycznej ingerencji.
    """
    if settings.stop_loss_only_mode:
        return
    now = dt.datetime.utcnow()
    candidates = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False, environment=current_environment(user_id))
        .filter(db.or_(ActiveTrade.next_buy_retry_at.is_(None), ActiveTrade.next_buy_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if _position_hours(t.currency)]
    if not candidates:
        return

    if pending is None:
        try:
            pending = client.get_pending_orders()
        except T212APIError as exc:
            _log(user_id, "ERROR", f"Retry LIMIT BUY: błąd pobierania pending orders - {exc}")
            return
    pending_by_id = {str(o.get("id")): o for o in pending}
    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))

    for trade in candidates:
        pending_order = pending_by_id.get(trade.buy_order_id)
        if pending_order is None:
            # Zlecenie już nie w kolejce - wypełnione albo anulowane skądinąd,
            # tym zajmuje się _retry_pending_sells (baseline-delta w portfolio).
            continue

        if Decimal(str(pending_order.get("filledQuantity", 0))) > 0:
            continue  # częściowo już wypełnione - nie anulujemy w połowie, niech dokończy

        current_price = price_feed.get_live_price(
            market_keys.get("finnhub_api_key"), trade.ticker,
            market_keys.get("alpaca_api_key"), market_keys.get("alpaca_api_secret"),
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
            new_result, new_quantity = _place_buy_with_precision_fallback(
                client, trade.ticker, new_quantity, current_price,
            )
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


def _auto_adopt_foreign_positions(user_id: int, client: T212Client, settings: RiskSettings) -> int:
    """
    Switch "zarządzaj wszystkim" (RiskSettings.manage_all_positions, pomysł
    #1 z docs/IDEAS_v2.md, zaimplementowany 27.07.2026 na prośbę Adama - "idę
    spać, a ty handluj"). Gdy włączony: każdy ticker posiadany na koncie T212
    (quantity > 0) BEZ otwartej ActiveTrade zostaje automatycznie "adoptowany"
    - dokładnie ten sam mechanizm co ręczne POST /bot/asset/adopt
    (routes/bot.py::adopt_position), tylko bez interakcji usera.

    Świadomie WYŁĄCZONE DCA dla tych pozycji (grid_anchor_price=0 - ten sam
    "legacy"/wyłączony-DCA mechanizm co stare pozycje sprzed migracji, patrz
    komentarz w _trigger_dca_buys) - bot nie zna kontekstu/celu ręcznego
    zakupu, więc "zarządzanie" ogranicza się do samego wyjścia (trailing
    stop), zgodnie z zastrzeżeniem zapisanym w IDEAS_v2.md przy tym pomyśle.
    Oznaczone auto_adopted=True, żeby wyłączenie switcha mogło je odróżnić od
    pozycji adoptowanych ręcznie (routes/bot.py::_release_auto_adopted_positions) -
    te ZAWSZE zostają pod botem do ręcznego "Zwolnij", niezależnie od switcha.

    BotAsset auto-tworzony jeśli ticker jeszcze nie jest na liście bota,
    entry_amount = aktualna wartość pozycji (quantity*avg_price) - wartość
    czysto formalna (kolumna NOT NULL), nigdy realnie nie użyta bo DCA
    wyłączone przez grid_anchor_price=0.

    Zwraca liczbę NOWO przejętych pozycji tym wywołaniem (dodane 2026-08-03,
    patrz wywołanie w tick() - pozwala od razu doliczyć _manage_trailing_exit
    dla świeżo przejętej pozycji W TYM SAMYM ticku, zamiast czekać na
    następny cykl - patrz uzasadnienie tam).
    """
    if not settings.manage_all_positions:
        return 0

    try:
        portfolio = client.get_portfolio()
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Zarządzaj wszystkim: błąd pobierania portfolio - {exc}")
        return 0

    open_tickers = {
        t.ticker for t in ActiveTrade.query.filter_by(
            user_id=user_id, status="OPEN", environment=current_environment(user_id),
        ).all()
    }
    adopted_count = 0

    for p in portfolio:
        ticker = p.get("ticker")
        if not ticker or ticker in open_tickers:
            continue
        other = held_by_other_engine(user_id, ticker, "bot")
        if other is not None:
            # Ticker realnie leży w portfelu, ale JUŻ zarządza nim inny
            # silnik (Sygnał/EOD) - "zarządzaj wszystkim" NIE powinno go
            # sobie zawłaszczać, inaczej odtworzylibyśmy kolizję SUp_EQ
            # tym razem przez auto-adopcję zamiast zwykłego wejścia.
            diagnostics.log_diag(
                user_id, "bot",
                f"{ticker}: pominięte auto-adopcją - już zarządzane przez {other}.",
            )
            continue
        try:
            quantity = Decimal(str(p.get("quantity", 0)))
            avg_price = Decimal(str(p.get("averagePrice", 0)))
        except InvalidOperation:
            continue
        if quantity <= 0 or avg_price <= 0:
            continue

        instrument = Instrument.query.get(ticker)
        currency = instrument.currency_code if instrument else "USD"

        env = current_environment(user_id)
        asset = BotAsset.query.filter_by(user_id=user_id, ticker=ticker, environment=env).first()
        if asset is None:
            asset = BotAsset(
                user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
                currency=currency, entry_amount=(quantity * avg_price).quantize(Decimal("0.01")),
                environment=env,
            )
            db.session.add(asset)
            db.session.flush()  # potrzebne asset.id do FK ActiveTrade.bot_asset_id ponizej

        position_group_id = str(uuid.uuid4())
        trade = ActiveTrade(
            user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
            ticker=ticker, currency=currency,
            buy_order_id=f"AUTOADOPTED-{uuid.uuid4()}",
            buy_price=avg_price, quantity=quantity, allocated_value=quantity * avg_price,
            average_price=avg_price, dca_level=0, grid_anchor_price=Decimal("0"),
            baseline_owned_quantity=Decimal("0"),
            status="OPEN", is_paper=False, buy_confirmed=True, auto_adopted=True,
            environment=env,
        )
        db.session.add(trade)
        db.session.commit()
        adopted_count += 1

        _log(
            user_id, "INFO",
            f"{ticker}: pozycja automatycznie przejęta przez tryb \"zarządzaj wszystkim\" "
            f"({quantity} @ ~{avg_price}) - od teraz pilnowana trailing exitem, bez DCA.",
            position_group_id,
        )

    return adopted_count


def _manage_trailing_exit(user_id: int, client: T212Client, settings: RiskSettings) -> None:
    """
    Trailing STOP - JEDNO zlecenie na raz (przeprojektowane 2026-07-21, patrz
    docs/IDEAS_v2.md pkt 4 - T212 nie pozwala trzymać LIMIT SELL + STOP
    równocześnie, 400 selling-equity-not-owned).

    CIĄGŁY trailing (przeprojektowane DRUGI RAZ 2026-07-22, na życzenie Adama
    po analizie realnych wyników - poprzednia wersja przesuwała STOP TYLKO
    przy pełnym minięciu kolejnego progu take_profit_step_pct, więc STOP
    zawsze siedział dokładnie jeden krok ZA ostatnim minionym progiem, a nie
    za faktycznym szczytem ceny. Realny przykład z 22.07: ASMLa_EQ doszło do
    progu ~4 (cena +0.9-1.2% od wejścia), po czym się cofnęło i wykonało na
    poziomie progu 3 (+0.3%) - oddaliśmy z powrotem prawie cały papierowy
    zysk, zanim ochrona w ogóle zadziałała):

    1. Bot NIC nie wystawia dopóki cena nie minie DWÓCH progów
       (2 * take_profit_step_pct powyżej ref_price) - BEZ ZMIAN, żeby zwykły
       szum tuż po zakupie nie wyciął pozycji.
    2. PIERWSZE uzbrojenie (przejście z brak-STOP-a na jest-STOP) siada na
       max(protective_floor, ciasny_target_teraz) - patrz 2a i 2c. Floor to
       DOLNA granica (nigdy nie schodzimy poniżej niej), nie sztywny target -
       ZMIANA 2026-07-27 (patrz 2c), wcześniej (do 27.07) siadało ZAWSZE
       wyłącznie na floorze.
    2a. protective_floor OPARTY O ATR ZAMIAST SZTYWNEGO % (dodane 2026-07-22,
        DRUGA zmiana tego samego dnia, na życzenie Adama - "boostowanie
        logiki stoploss") - gdy da się policzyć ATR(14) instrumentu (świece
        dzienne, patrz _get_atr_stop_distance), floor = ref_price - ATR*1.8
        (mnożnik z docs/IDEAS_v2.md). Skaluje się z REALNĄ zmiennością
        instrumentu zamiast jednego sztywnego % dla wszystkiego (ASML rusza
        się dziennie inaczej niż spokojna spółka dywidendowa). Gdy danych
        brak (429/brak pokrycia/za krótka historia) - CICHY fallback na
        stary ref_price * (1 - stop_loss_pct), zero twardej zależności.
    2b. KAŻDY KOLEJNY tick (już uzbrojony) liczy candidate = max(dotychczasowy
        stop_target_price, current_price * (1 - step)) - STOP goni szczyt,
        nigdy się nie cofa, floor z punktu 2 już się NIE przelicza ponownie.
        **Bug znaleziony i naprawiony 2026-07-22 przy dopinaniu ATR**: pierwsza
        wersja tej samej doby liczyła max(protective_floor, continuous_target)
        na KAŻDYM ticku od razu po uzbrojeniu - matematycznie, dla dowolnego
        realistycznego step<50%, current_price*(1-step) w momencie samego
        uzbrojenia jest ZAWSZE wyższy niż jakikolwiek floor poniżej ref_price
        (dowód: current_price >= ref_price*(1+2*step), więc current_price*
        (1-step) ≈ ref_price*(1+step) > ref_price > floor) - czyli floor
        (2% albo ATR) był martwym kodem, szeroki bufor "chroniący kapitał"
        tuż po uzbrojeniu w ogóle się nie włączał, STOP od razu był ciasny.
    2c. **ZMIANA 2026-07-27** (Adam, ochrona zysku > wszystko inne, patrz
        [[feedback_snajper_profit_protection_priority]] w pamięci Claude):
        punkt 2b opisuje TYLKO "już uzbrojony" - dla PIERWSZEGO uzbrojenia do
        27.07 kod celowo brał WYŁĄCZNIE floor (2a), NIGDY max() z ciasnym
        targetem - dokładnie ten sam dowód matematyczny co w buggu z 22.07
        (2b wyżej) oznacza, że max() przy SAMYM uzbrojeniu też niemal zawsze
        wybierze ciasny target zamiast floora. To był ŚWIADOMY kompromis
        "dać pozycji oddech tuż po zakupie" - ZŁAPANY NA ŻYWO jako realny
        problem 27.07: CRM_US_EQ uzbroiło się na floorze (159, daleko od
        ceny 173) i UTKNĘŁO tam na kilkanaście minut, bo dociągnięcie do
        ciasnego poziomu wymagało DRUGIEGO, osobnego zlecenia na kolejnym
        ticku - przy ciasnym rate limicie demo to podwójna szansa na 429
        zanim ochrona faktycznie dotrze tam gdzie powinna. Naprawione: PIERWSZE
        uzbrojenie liczy teraz `max(floor_candidate, current_price*(1-step))`
        - jeden strzał do najlepszego bezpiecznego poziomu zamiast dwóch.
        floor_candidate zostaje jako DOLNA granica (chroni przed nagłym
        cofnięciem tuż nad progiem 2, gdzie ciasny target mógłby wypaść
        niżej niż rozsądna ochrona kapitału).
    3. Żeby nie zarzynać ciasnego rate limitu demo Cancel-Replace'em przy
       KAŻDYM drobnym ruchu ceny w górę, STOP przesuwa się dopiero gdy nowy
       target jest o co najmniej MIN_TRAIL_REQUOTE_FRACTION * step wyższy niż
       obecny stop_target_price - "ciągły" w praktyce znaczy "dużo częściej
       niż dawniej", nie "dosłownie co tick".
    4. trail_milestone_steps (int(profit_pct / step)) zostaje wyliczane jak
       dawniej WYŁĄCZNIE do wyświetlenia w UI ("próg N") - nie steruje już
       SAMYM poziomem STOP-a, tylko bramką uzbrojenia (krok 1).
    5. WYJĄTEK od bramki uzbrojenia (dodane 2026-07-28, patrz
       microgrid_strategy.compute_exhausted_dca_floor) - pozycja BEZ amunicji
       do dalszego DCA (dca_level == max_dca_levels-1) i jeszcze nigdy nie
       uzbrojona dostaje natychmiastowy, ostatni-linii-obrony STOP zakotwiczony
       w AKTUALNEJ cenie (ATR floor / stop_loss_pct fallback), NIE czekając na
       2 progi zysku - bez tego taka pozycja mogła utknąć BEZ ŻADNEJ ochrony
       na czas nieokreślony (znalezione backtestem: PRXa_EQ/MCp_EQ/SAPd_EQ,
       dca_level=4, -27.7%/-18.1%/-13.7%, zero stop_target_price).
    5b. TEN SAM wyjątek rozszerzony (dodane 2026-08-07, Adam po ręcznej
        adopcji FPp_EQ na koncie LIVE) o `settings.stop_loss_only_mode` -
        w tym trybie DCA jest permanentnie wyłączone, więc dca_level ZAWSZE
        zostaje 0 i warunek "wyczerpane DCA" z punktu 5 nigdy się nie
        spełnia - pozycja adoptowana ręcznie (bez wcześniejszej pozycji
        bota) siedziałaby BEZ ŻADNEGO stopu do czasu aż cena sama urośnie
        o 2 progi, dokładnie ten sam problem co w punkcie 5, tylko innym
        mechanizmem. Fallback % dla tej gałęzi to CELOWO osobna stała
        (MANUAL_PROTECTION_FALLBACK_PCT, -5%), nie RiskSettings.stop_loss_pct
        (2%, tuned/backtestowany dla algorytmicznych wejść bota) - ATR*1.8
        nadal ma priorytet gdy dostępne, ten % to tylko siatka bezpieczeństwa.

    Migracja ze starego dwunożnego OCO: jeśli pozycja ma jeszcze
    trade.sell_order_id (LIMIT SELL założony PRZED przeprojektowaniem
    21.07), ten tick go najpierw anuluje i zeruje - dopiero potem liczy/
    wystawia pojedynczy STOP jak wyżej.
    """
    step = settings.take_profit_step_pct
    if step <= 0:
        return  # błędna konfiguracja (0 albo ujemny krok) - nie ma jak liczyć progów, nie zgaduj

    now = dt.datetime.utcnow()
    candidates = (
        ActiveTrade.query
        .filter_by(
            user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True, sell_blocked=False,
            sl_suspended_for_weekend=False, environment=current_environment(user_id),
        )
        .filter(db.or_(ActiveTrade.next_sell_retry_at.is_(None), ActiveTrade.next_sell_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if _position_hours(t.currency)]
    if not candidates:
        return

    # Wykrywanie pozycji sprzedanych RĘCZNIE poza appką (Adam, 2026-07-24:
    # "skoro go znasz, wyeliminuj ten błąd... jak sprzedam coś z ręki i nie
    # mam już, to niech bot tego nie próbuje sprzedać") - bez tego bot w
    # nieskończoność próbowałby uzbroić/przesunąć STOP na 0 akcji, dostając
    # błąd T212 i tylko odkładając retry z backoffem, nigdy nie rozwiązując
    # sprawy. Jedno zapytanie /equity/portfolio na cały tick (ten sam wzorzec
    # co _retry_pending_sells::_portfolio_quantities) - gdy się nie uda
    # (429/inny błąd), CICHY fallback: pomijamy tę detekcję na tym ticku,
    # reszta logiki działa jak dotychczas.
    # _portfolio_positions() (nie _portfolio_quantities) - potrzebujemy też
    # averagePrice z T212 do przeliczenia trade.average_price przy manualnym
    # dokupieniu, patrz sync niżej (dodane 2026-08-07).
    try:
        positions_map = _portfolio_positions(client)
    except T212APIError:
        positions_map = None
    owned_map = (
        {ticker: pos["quantity"] for ticker, pos in positions_map.items()}
        if positions_map is not None else None
    )

    # PRIORYTET wg pilności (dodane 2026-07-27, na życzenie Adama - "musisz
    # wymyślec jak zrobić żeby taka pozycja miała priorytet, to chroni
    # zyski") - znalezione na żywo na CRM_US_EQ: przy ciasnym rate limicie
    # demo T212 kolejność iteracji po `candidates` (bez sortowania = kolejność
    # z bazy) decydowała kto dostanie szansę na cancel/place tego ticku, więc
    # pozycja z NAJWIĘKSZĄ dziurą między obecnym stopem a tym co powinna mieć
    # (np. świeżo zresetowana po nieudanym cancel/replace) czekała tyle samo
    # co pozycja z drobną, kosmetyczną poprawką o ułamek procenta. Fix: DWIE
    # fazy zamiast jednej. Faza 1 (ta pętla) liczy candidate_stop dla KAŻDEJ
    # pozycji (koszt: tylko price_feed, ODDZIELNY budżet od zleceń T212, więc
    # liczenie z wyprzedzeniem dla wszystkich nic nie kosztuje) i zbiera je do
    # `pending`, ZAMIAST od razu wołać cancel/place. Faza 2 (niżej) sortuje
    # `pending` po wielkości luki (candidate_stop - obecny stop_target_price,
    # im większa tym pilniejsza - świeżo zresetowana szeroka ochrona zawsze
    # wygra z drobną kosmetyczną korektą) i DOPIERO WTEDY woła prawdziwe
    # zlecenia T212 w tej kolejności - pilne pozycje dostają pierwszy strzał
    # do ciasnego budżetu, zamiast czekać na przypadkową kolejność z bazy.
    pending: list[tuple[ActiveTrade, Decimal, int, Decimal]] = []  # (trade, candidate_stop, milestone_steps, current_price)

    for trade in candidates:
        if owned_map is not None and owned_map.get(trade.ticker, Decimal("0")) <= 0:
            # PRZED uznaniem za "sprzedane ręcznie" (cena nieznana) - sprawdź czy
            # to nie WŁASNY stop/take-profit bota, który właśnie się wykonał.
            # _detect_exit_fills()/_resolve_vanished_leg() robią dokładnie to
            # samo sprawdzenie (historia zleceń T212), ale wołane są PO tej
            # funkcji w reconcile()/tick() (celowo - patrz komentarz przy
            # wywołaniu _manage_trailing_exit, ochrona zysku ma priorytet nad
            # get_pending_orders()) - do tego czasu ta gałąź już zdążyłaby
            # zamknąć trade i wyzerować stop_order_id/sell_order_id, więc
            # _detect_exit_fills nie miałby już czego sprawdzić - realna cena
            # ginie bezpowrotnie. Znalezione 2026-08-03 na żywo: 31/52 (60%)
            # zamkniętych pozycji miało close_price=NULL, w tym 6/8 zamkniętych
            # jednego dnia - zdecydowanie za dużo jak na ręczną sprzedaż, to
            # niemal na pewno własne zamknięcia bota gubione przez ten wyścig.
            resolved = False
            for order_id, filled_via in (
                (trade.stop_order_id, "stop-loss"),
                (trade.sell_order_id, "take-profit"),
            ):
                if not order_id:
                    continue
                item = _lookup_recent_order(client, order_id)
                if item is None or (item.get("order") or {}).get("status") != _FILLED_ORDER_STATUS:
                    continue
                fill_price_raw = (item.get("fill") or {}).get("price")
                fill_price = Decimal(str(fill_price_raw)) if fill_price_raw is not None else None
                _log(
                    user_id, "WARN",
                    f"{trade.ticker}: 0 szt. w portfelu T212, zlecenie {order_id} potwierdzone jako "
                    f"WYKONANE w historii T212 (cena {fill_price if fill_price is not None else trade.stop_target_price}) "
                    "- zamykam z realną ceną, NIE jako 'sprzedane ręcznie'.",
                    trade.position_group_id,
                )
                _finalize_closed_trade(user_id, client, trade, filled_via, fill_price=fill_price)
                resolved = True
                break
            if resolved:
                continue

            trade.status = "CLOSED"
            trade.closed_at = dt.datetime.utcnow()
            trade.stop_order_id = None
            trade.sell_order_id = None
            db.session.commit()
            _log(
                user_id, "INFO",
                f"{trade.ticker}: 0 szt. w portfelu T212 (prawdopodobnie sprzedane ręcznie poza appką) - "
                "pozycja zamknięta lokalnie (cena zamknięcia nieznana), bot przestaje nią zarządzać.",
                trade.position_group_id,
            )
            continue

        # Synchronizacja ilości z rzeczywistym stanem T212 (dodane 2026-07-29,
        # na życzenie Adama - "niech boty maja pelna dostepnosc (testowo)...
        # niech dodaje to do logiki trailing stop") - portfolio GET jest
        # PRAWDZIWYM stanem posiadania (Adam potwierdził 29.07 wprost na
        # żywym przykładzie ASML_US_EQ: realna ilość to 0.3709, NIE 0.3075),
        # więc synchronizujemy w OBIE strony - i manualna sprzedaż, i
        # manualne dokupienie mają być od razu widoczne w trailing stopie.
        #
        # WAŻNE odróżnienie (znalezione na żywo tego samego dnia, patrz
        # _extract_owned_quantity_from_error przy place_stop_order niżej):
        # gdy T212 odrzuca SPRZEDAŻ błędem selling-equity-not-owned z
        # MNIEJSZĄ liczbą niż portfolio - to NIE jest dowód, że portfolio
        # kłamie, tylko że część pozycji (np. świeżo wypełniona noga DCA)
        # jeszcze się nie rozliczyła i chwilowo nie da się jej sprzedać.
        # Pierwsza wersja tego fixu (usunięta) błędnie brała tę MNIEJSZĄ,
        # "ile da się teraz sprzedać" liczbę za nowy stan PEŁNEGO posiadania
        # i na stałe zaniżała trade.quantity - realny błąd: po naprawieniu
        # rozjazdu wygasłoby ochranianie tej "zaginionej" części pozycji na
        # zawsze, mimo że faktycznie należy do usera. Rozwiązanie: TA
        # synchronizacja (portfolio, źródło prawdy o CAŁKOWITYM posiadaniu)
        # zostaje jak było; ograniczenie "ile da się sprzedać TERAZ" jest
        # obsługiwane WYŁĄCZNIE jako jednorazowy fallback przy samym
        # wystawianiu zlecenia (patrz niżej), bez dotykania trade.quantity.
        actual_owned = owned_map.get(trade.ticker) if owned_map is not None else None
        stop_undersized_after_sync = False
        if actual_owned is not None and actual_owned != trade.quantity:
            old_quantity = trade.quantity
            old_average_price = trade.average_price
            price_note = ""
            # Zlecenie STOP już wystawione na STARĄ (mniejszą) ilość, jeśli
            # user dokupił (2026-08-10, TSLA_US_EQ na żywo - Adam ręcznie
            # dokupił, ilość zsynchronizowała się 0.35->0.5, ale realny
            # resting STOP na T212 dalej wystawiony na 0.35, bo dopisanie
            # trade.quantity samo w sobie NIE wymusza Cancel-Replace, tylko
            # następna PRICE-DRIVEN poprawa stopu (compute_trailing_stop przy
            # is_first_arm=False zwraca None gdy cena się nie ruszyła) - do
            # tego czasu 0.15 akcji siedziało BEZ ŻADNEJ ochrony). Flaga niżej
            # wymusza natychmiastowy Cancel-Replace na TEJ SAMEJ cenie stopu,
            # tylko ze świeżą ilością, zamiast czekać na ruch ceny.
            if actual_owned > trade.quantity and trade.stop_order_id is not None:
                stop_undersized_after_sync = True
            # Przeliczenie average_price TYLKO przy wzroście (dokupienie) -
            # dodane 2026-08-07 (Adam: "popraw żeby przeliczał average_price
            # przy dokupieniu"). Przy spadku (częściowa sprzedaż) średnia
            # cena POZOSTAŁYCH udziałów się nie zmienia (standardowa
            # księgowość average-cost), więc zostaje bez zmian. Bierzemy
            # averagePrice WPROST z T212 (już uwzględnia FX/opłaty poprawnie)
            # zamiast liczyć własną średnią ważoną z nieznaną ceną dokupienia.
            if actual_owned > old_quantity and positions_map is not None:
                new_average_price = positions_map.get(trade.ticker, {}).get("average_price")
                if new_average_price is not None and new_average_price > 0:
                    trade.average_price = new_average_price
                    price_note = f", average_price {old_average_price} -> {new_average_price} (dokupienie)"
            trade.quantity = actual_owned
            trade.allocated_value = (actual_owned * trade.average_price).quantize(Decimal("0.01"))
            db.session.commit()
            _log(
                user_id, "INFO",
                f"{trade.ticker}: ilość zsynchronizowana z T212 ({old_quantity} -> {actual_owned}){price_note} - "
                "wykryto ręczną zmianę pozycji poza botem (albo rozjazd po cancel/replace), trailing "
                "STOP dalej liczony/wystawiany na aktualnej, prawdziwej ilości.",
                trade.position_group_id,
            )

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

        market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
        current_price = price_feed.get_live_price(
            market_keys.get("finnhub_api_key"), trade.ticker,
            market_keys.get("alpaca_api_key"), market_keys.get("alpaca_api_secret"),
        )
        if current_price is None or current_price <= 0:
            # Watchdog (dodany 2026-08-05 po buggu RHMd_EQ - zły symbol Yahoo
            # w TICKER_MAP sprawiał, że TA GAŁĄŹ wykonywała się cicho przez
            # >30h, zero śladu w jakimkolwiek logu) - po ALERT_THRESHOLD
            # ticków z rzędu bez ceny, jeden ERROR (trafia też na Telegram).
            if price_watchdog.note_price_result("bot", trade.ticker, False):
                _log(
                    user_id, "ERROR",
                    f"{trade.ticker}: brak ceny przez {price_watchdog.ALERT_THRESHOLD} ticków z rzędu - "
                    "trailing stop NIE działa dla tej pozycji! Sprawdź TICKER_MAP/mapowanie Yahoo "
                    "(finnhub_client.py) albo pokrycie instrumentu.",
                )
            continue  # brak ceny - spróbujemy przy kolejnym ticku, nic pilnego do zrobienia
        price_watchdog.note_price_result("bot", trade.ticker, True)
        position_alerts.check_move_alert(user_id, "bot", trade.ticker, trade.buy_price, current_price, trade.currency)

        # Dla USD: liczymy progi/STOP względem ref_price (average_price
        # podbite o round-trip FX), nie surowej average_price - koszt
        # przewalutowania na koncie EUR (konto Adama), patrz
        # RiskSettings.fx_cost_adjustment_enabled/fx_fee_pct. PRZENIESIONE
        # 2026-08-03 ze sztywnej stałej modułowej FX_ROUND_TRIP_PCT do
        # ustawień per-user (ten sam mechanizm dodany też w Sygnale/EOD,
        # gdzie tego wcześniej brakowało) - domyślnie WŁĄCZONE, więc
        # zachowanie Micro-Gridu bez zmian. Dla EUR (konto Adama jest w EUR,
        # zero konwersji) ref_price == average_price, zero zmiany zachowania.
        ref_price = trade.average_price
        if trade.currency == "USD" and settings.fx_cost_adjustment_enabled:
            ref_price = trade.average_price * (1 + settings.fx_fee_pct * 2)
        # Francuski FTT (2026-08-07, patrz FR_FTT_PCT/FR_FTT_TICKERS wyżej) -
        # jednorazowo (kupno, nie x2 jak FX round-trip), NIEZALEŻNIE od
        # powyższego bloku FX (obie stawki mogą się nałożyć, jeśli kiedyś
        # trafi się francuski ticker rozliczany nie w EUR - dziś nie ma
        # takiego przypadku, ale nie ma powodu tego wykluczać na sztywno).
        if trade.ticker in FR_FTT_TICKERS:
            ref_price = ref_price * (1 + FR_FTT_PCT)

        milestone_steps = microgrid_strategy.compute_milestone_steps(ref_price, current_price, step)
        if milestone_steps < 2:
            # WYJĄTEK 2026-07-28 (patrz compute_exhausted_dca_floor - znalezione
            # backtestem: PRXa_EQ/MCp_EQ/SAPd_EQ utknęły na dca_level=4, głęboko
            # pod wodą, ZERO ochrony) - pozycja bez amunicji do dalszego DCA
            # (dca_level == max_dca_levels-1) i jeszcze NIGDY nie uzbrojona
            # dostaje ostatnią linię obrony OD RAZU, bez czekania na 2 progi
            # zysku, które przy wyczerpanym DCA mogą nigdy nie nadejść.
            #
            # ROZSZERZONE 2026-08-07 (punkt 5b w docstringu wyżej) o
            # `settings.stop_loss_only_mode` - w tym trybie dca_level ZAWSZE
            # zostaje 0 (DCA wyłączone), więc warunek "wyczerpane DCA" sam z
            # siebie nigdy by się nie spełnił - ręcznie zaadoptowana pozycja
            # (np. "Przekaż botowi") zostawałaby BEZ ŻADNEGO stopu na czas
            # nieokreślony. Fallback % OSOBNY od stop_loss_pct - patrz
            # MANUAL_PROTECTION_FALLBACK_PCT.
            needs_immediate_floor = (
                trade.dca_level >= settings.max_dca_levels - 1 or settings.stop_loss_only_mode
            )
            if needs_immediate_floor and trade.stop_order_id is None:
                atr_distance = _get_atr_stop_distance(user_id, trade.ticker)
                fallback_pct = (
                    MANUAL_PROTECTION_FALLBACK_PCT if settings.stop_loss_only_mode else settings.stop_loss_pct
                )
                candidate_stop = microgrid_strategy.compute_exhausted_dca_floor(
                    current_price, atr_distance, fallback_pct,
                )
                pending.append((trade, candidate_stop, milestone_steps, current_price))
            elif stop_undersized_after_sync and trade.stop_order_id is not None:
                # Ta sama naprawa co przy is_first_arm=False nizej, ale dla
                # pozycji ktora akurat teraz jest ponizej 2 progow zysku -
                # dokupienie i tak zostawilo istniejacy STOP niedowymiarowany,
                # naprawiamy niezaleznie od tego gdzie akurat jest cena.
                pending.append((trade, trade.stop_target_price, milestone_steps, current_price))
            continue

        # Matematyka (floor ATR/fallback, max(floor, ciasny_target) przy
        # pierwszym uzbrojeniu, czysty ciagly trailing pozniej) wyciagnieta
        # 2026-07-28 do microgrid_strategy.compute_trailing_stop() - PELNE
        # uzasadnienie kazdego kroku (dlaczego floor tylko raz, dlaczego
        # floor_anchor=max(ref_price,current_price), dlaczego jeden strzal
        # do max(floor, ciasny_target) zamiast samego floora) zostaje w
        # docstringu tej funkcji wyzej - tu tylko wolanie juz zweryfikowanej
        # formuly.
        is_first_arm = trade.stop_order_id is None
        atr_distance = _get_atr_stop_distance(user_id, trade.ticker) if is_first_arm else None
        candidate_stop = microgrid_strategy.compute_trailing_stop(
            is_first_arm=is_first_arm,
            ref_price=ref_price,
            current_price=current_price,
            step=step,
            existing_stop_target=trade.stop_target_price,
            atr_distance=atr_distance,
            stop_loss_pct=settings.stop_loss_pct,
            min_requote_fraction=MIN_TRAIL_REQUOTE_FRACTION,
        )
        if candidate_stop is None:
            if stop_undersized_after_sync:
                # Cena nie dala normalnej poprawy, ale ilosc sie zmienila po
                # dokupieniu (patrz komentarz przy stop_undersized_after_sync
                # wyzej) - wymuszamy Cancel-Replace na TEJ SAMEJ cenie stopu,
                # zeby zlecenie natychmiast pokrylo CALA aktualna ilosc,
                # zamiast czekac az cena sama da powod do przesuniecia.
                pending.append((trade, trade.stop_target_price, milestone_steps, current_price))
            continue  # juz uzbrojony, ale poprawa za mala zeby placic Cancel-Replace'em z ciasnego rate limitu

        pending.append((trade, candidate_stop, milestone_steps, current_price))

    # Faza 2 - patrz komentarz "PRIORYTET wg pilnosci" wyzej. Gap = o ile
    # candidate_stop przebija obecny stop_target_price (0 gdy pozycja jeszcze
    # nigdy nie miala zadnego stopu - to TEZ pilne, brak ochrony w ogole).
    # Najpilniejsze (najwieksza luka) ida na sam poczatek kolejki zlecen T212.
    pending.sort(
        key=lambda item: item[1] - (item[0].stop_target_price or Decimal("0")),
        reverse=True,
    )

    # Cudze (nie-botowe) SELL-e na tickerze (2026-08-10, Adam po incydencie
    # TotalEnergies/SUp_EQ - ręcznie wystawiony limit sell z appki T212 na
    # telefonie, bot dobijał się co tick, żeby wystawić WŁASNY, kolidujący
    # STOP, dostając w kółko selling-equity-not-owned zamiast się zorientować
    # że user już samodzielnie sprzedaje: "bot powinien odczytać że to ręcznie
    # wystawiony SL i odpuścić, a nie się dobijać jak T-800"). Sprawdzane
    # RAZ na cały tick (współdzielony 50s cache T212Client, patrz
    # get_pending_orders() - nie kosztuje dodatkowego requestu poza pierwszym
    # w tym ticku), nie per-trade. Zlecenie uznajemy za "cudze" gdy jego id
    # NIE jest żadnym ze śledzonych przez tego trade'a (stop/sell/DCA-buy) -
    # solidniejsze niż poleganie na polu initiatedFrom (mogłoby się kiedyś
    # zmienić/nie zawsze być tym czego oczekujemy).
    try:
        live_orders = client.get_pending_orders()
    except T212APIError:
        live_orders = []  # cache miss/siec akurat nawaliła - bez tej ochrony na TEN jeden tick, nie blokujemy całej fazy 2
    foreign_sell_by_ticker: dict[str, dict] = {}
    for o in live_orders:
        if o.get("side") != "SELL":
            continue
        foreign_sell_by_ticker.setdefault(o.get("ticker"), o)

    for trade, candidate_stop, milestone_steps, current_price in pending:
        foreign_order = foreign_sell_by_ticker.get(trade.ticker)
        if foreign_order is not None and str(foreign_order.get("id")) not in (
            trade.stop_order_id, trade.sell_order_id, trade.dca_pending_buy_order_id,
        ):
            trade.stop_order_id = None
            trade.sell_order_id = None
            trade.dca_pending_buy_order_id = None
            trade.dca_pending_quantity = None
            trade.dca_pending_price = None
            trade.dca_pending_baseline_quantity = None
            trade.status = "RELEASED"
            db.session.commit()
            _log(
                user_id, "WARN",
                f"{trade.ticker}: wykryto na T212 zlecenie SELL ({foreign_order.get('id')}) spoza bota "
                f"(initiatedFrom={foreign_order.get('initiatedFrom')}) - pozycja zwolniona spod zarządzania "
                "automatycznie, żeby bot nie dobijał się o nią co tick. Udziały zostają na koncie.",
                trade.position_group_id,
            )
            continue

        if trade.stop_order_id:
            try:
                client.cancel_order(trade.stop_order_id)
            except T212APIError as exc:
                _bump_retry(
                    user_id, trade,
                    f"przesunięcie ciągłego trailing STOP - anulowanie starego "
                    f"({trade.stop_order_id}) nie powiodło się (mógł się już wykonać) - {exc}",
                )
                continue

        order_quantity = trade.quantity
        partial_sellable: Decimal | None = None
        try:
            stop_result = client.place_stop_order(trade.ticker, -order_quantity, candidate_stop)
        except T212APIError as exc:
            trade.stop_order_id = None
            # Fallback NA TĘ JEDNĄ próbę (dodane 2026-07-29, patrz [[feedback_
            # snajper_profit_protection_priority]] - Adam: "niektore requesty
            # powinny miec prio... a nie doprowadzac do oczekiwan po 30 min").
            # Realny przypadek ASML_US_EQ 29.07: T212 odrzucił sprzedaż PEŁNEJ
            # trade.quantity (0.3709, potwierdzone przez Adama jako PRAWDZIWY
            # stan posiadania) błędem selling-equity-not-owned, "owned: 0.3075"
            # - świeżo wypełniona noga DCA najwyraźniej jeszcze się rozlicza i
            # chwilowo nie da się jej sprzedać, NIE oznacza to że user ma mniej
            # akcji niż myślimy. Pierwsza wersja tego fixu (usunięta) błędnie
            # NADPISYWAŁA trade.quantity tą mniejszą liczbą na stałe - trwale
            # gubiąc ochronę "zaginionej" (w rzeczywistości wciąż posiadanej)
            # części pozycji. Naprawione: próbujemy OD RAZU, w TYM SAMYM
            # wywołaniu, wystawić STOP na to co błąd mówi że da się sprzedać
            # TERAZ - trade.quantity zostaje nietknięte (prawdziwy total), więc
            # przy KOLEJNYM ruchu ceny (Cancel-Replace) bot znów spróbuje
            # najpierw pełnej ilości - naturalnie złapie resztę, gdy tylko się
            # rozliczy, bez żadnego specjalnego mechanizmu "dogonienia".
            partial_sellable = _extract_owned_quantity_from_error(exc)
            if partial_sellable is None or not (0 < partial_sellable < order_quantity):
                _bump_retry(
                    user_id, trade,
                    f"uzbrojenie ciągłego trailing STOP (target {candidate_stop}) nie powiodło się - {exc}",
                )
                continue
            try:
                stop_result = client.place_stop_order(trade.ticker, -partial_sellable, candidate_stop)
            except T212APIError as exc2:
                _bump_retry(
                    user_id, trade,
                    f"uzbrojenie ciągłego trailing STOP nawet dla dostępnej ilości ({partial_sellable} "
                    f"z {order_quantity}, reszta prawdopodobnie jeszcze się rozlicza) nie powiodło się - {exc2}",
                )
                continue

        was_armed = trade.trail_milestone_steps > 0
        trade.stop_order_id = stop_result.order_id
        trade.stop_target_price = candidate_stop
        trade.trail_milestone_steps = milestone_steps
        trade.sell_retry_count = 0
        trade.next_sell_retry_at = None
        db.session.commit()
        if partial_sellable is not None:
            _log(
                user_id, "WARN",
                f"{trade.ticker}: trailing STOP {'uzbrojony' if not was_armed else 'przesunięty'} na "
                f"{candidate_stop} - CZĘŚCIOWO, tylko {partial_sellable} z {order_quantity} szt. (reszta "
                "prawdopodobnie jeszcze się rozlicza na T212, nie sprzedana ani zgubiona) - przy kolejnym "
                "ruchu ceny bot spróbuje ochronić całość ponownie.",
                trade.position_group_id,
            )
        else:
            _log(
                user_id, "INFO",
                f"{trade.ticker}: trailing STOP {'uzbrojony' if not was_armed else 'przesunięty'} "
                f"na {candidate_stop} (próg {milestone_steps}, cena teraz {current_price}).",
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


def _trigger_dca_buys(
    user_id: int, client: T212Client, settings: RiskSettings, current_equity: Decimal | None = None,
) -> None:
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

    stop_loss_only_mode (2026-08-06) - guard WEWNĄTRZ funkcji, nie tylko przy
    wywołaniu w tick() - reconcile() (restart appki/"Aktywuj") woła tę funkcję
    OSOBNO i był w niej dokładnie ten sam bug co w _retry_pending_buys (Adam,
    na żywo: "jakie gonienie ceny bot miał TYLKO SL stawiać!!!!!!!!") -
    zawiódłby dokładnie tak samo przy restarcie mimo włączonej flagi.
    """
    if settings.stop_loss_only_mode:
        return
    candidates = (
        ActiveTrade.query
        .filter_by(
            user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True,
            environment=current_environment(user_id),
        )
        .filter(ActiveTrade.dca_pending_buy_order_id.is_(None))
        .filter(ActiveTrade.dca_level < settings.max_dca_levels - 1)
        .all()
    )
    candidates = [t for t in candidates if _position_hours(t.currency)]
    if not candidates:
        return

    multipliers = _parse_dca_scenario(settings.dca_scenario)
    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))

    for trade in candidates:
        next_level = trade.dca_level + 1
        trigger_price = trade.grid_anchor_price * (Decimal("1") - settings.dca_trigger_pct * next_level)
        if trigger_price <= 0:
            continue  # grid_anchor_price=0 (np. stara pozycja sprzed migracji) - DCA celowo wyłączone

        current_price = price_feed.get_live_price(
            market_keys.get("finnhub_api_key"), trade.ticker,
            market_keys.get("alpaca_api_key"), market_keys.get("alpaca_api_secret"),
        )
        if current_price is None or current_price <= 0 or current_price > trigger_price:
            continue  # cena jeszcze nie spadła dość nisko (albo brak danych) - nic do zrobienia

        # Detektor "szoku" (patrz microgrid_strategy.is_shock) - domyślnie
        # WYŁĄCZONY, patrz docstring tamtego modułu. Gdy włączony: candles
        # żądane z days=ATR_LOOKBACK_DAYS - TA SAMA długość co fetch ATR w
        # _manage_trailing_exit (ten sam tick, wcześniejsza faza) więc to
        # zwykle trafienie w ciepły 30-min cache price_feed, nie nowe
        # zapytanie sieciowe.
        if microgrid_strategy.SHOCK_FILTER_ENABLED:
            shock_candles = price_feed.get_mini_chart_ohlc(
                market_keys.get("finnhub_api_key"), trade.ticker, days=ATR_LOOKBACK_DAYS,
                alpaca_api_key=market_keys.get("alpaca_api_key"),
                alpaca_api_secret=market_keys.get("alpaca_api_secret"),
            )
            atr = _compute_atr(shock_candles, ATR_PERIOD)
            atr_pct = (atr / current_price) if atr is not None and current_price > 0 else None
            max_drop = microgrid_strategy.compute_max_recent_single_day_drop_pct(shock_candles)
            if microgrid_strategy.is_shock(max_drop, atr_pct):
                _log(
                    user_id, "WARN",
                    f"{trade.ticker}: DCA poziom {next_level} POMINIĘTY - wykryty gwałtowny "
                    f"niedawny ruch (spadek {max_drop:.2%} vs ATR {atr_pct:.2%}), czeka na "
                    "trailing-stop/exhausted-DCA-floor zamiast dalszego dokupowania.",
                    trade.position_group_id,
                )
                continue

        asset = BotAsset.query.get(trade.bot_asset_id)
        if asset is None:
            continue  # aktywo usunięte z listy bota od czasu wejścia - nie dokupuj

        effective_entry_amount = asset.entry_amount
        if settings.equity_sizing_enabled and current_equity is not None:
            effective_entry_amount = microgrid_strategy.compute_equity_scaled_amount(
                asset.entry_amount, current_equity, settings.equity_sizing_baseline or Decimal("0"),
            )

        multiplier = _dca_multiplier(multipliers, next_level)
        dca_amount = effective_entry_amount * multiplier
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
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, environment=current_environment(user_id))
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


_FILLED_ORDER_STATUS = "FILLED"


def _lookup_recent_order(client: T212Client, order_id: str) -> dict | None:
    """
    Szuka zlecenia w historii T212 (GET /equity/history/orders, zwraca
    {"order": {..., "status": ...}, "fill": {"price": ..., ...}}) po jego id -
    pozwala _resolve_vanished_leg() rozróżnić faktyczne wykonanie (status
    FILLED) od anulowania/odrzucenia. Jedna strona (limit=50, najnowsze
    pierwsze) wystarcza - szukane zlecenie zniknęło z pending przed chwilą,
    więc jest na samej górze historii. Błąd API/brak w historii -> None,
    wywołujący traktuje to jako "jeszcze nie wiadomo", nie jako potwierdzenie.
    """
    try:
        page = client.get_order_history(limit=50)
    except T212APIError:
        return None
    for item in page.get("items", []):
        order = item.get("order") or {}
        if str(order.get("id")) == order_id:
            return item
    return None


def _finalize_closed_trade(
    user_id: int, client: T212Client, trade: ActiveTrade, filled_via: str, fill_price: float | None = None
) -> None:
    """
    Oznacza trade jako CLOSED i anuluje "osieroconą" drugą nogę (ręczne OCO -
    T212 nie ma natywnego one-cancels-other, patrz _manage_trailing_exit).
    filled_via: "take-profit" albo "stop-loss", tylko do logu/wyboru której
    nogi szukać jako osieroconej.

    close_price: preferuje rzeczywistą cenę wykonania z historii T212
    (fill_price, patrz _resolve_vanished_leg) - dopiero gdy ta niedostępna,
    fallback na trade.stop_target_price dla "stop-loss" (Market Order po
    przebiciu stopu może wykonać się z poślizgiem, więc to tylko
    PRZYBLIŻENIE) - dla "take-profit" bez fill_price zostaje NULL (nieznana).
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
    if fill_price is not None:
        trade.close_price = fill_price
    elif filled_via == "stop-loss":
        trade.close_price = trade.stop_target_price
    trade.status = "CLOSED"
    trade.closed_at = dt.datetime.utcnow()
    db.session.commit()


def _resolve_vanished_leg(
    user_id: int, client: T212Client, trade: ActiveTrade, order_id: str, filled_via: str
) -> bool:
    """
    Wołane z _detect_exit_fills() gdy sell_order_id/stop_order_id zniknęło
    z pending. Zniknięcie z pending SAMO W SOBIE nie dowodzi wykonania -
    zlecenie mogło też zostać anulowane/odrzucone (np. przez sam
    _manage_trailing_exit przy cancel/replace tej samej nogi, albo przez
    T212) - znaleziono 2026-07-23, gdy ASML pokazywał wciąż otwartą pozycję
    na koncie T212, mimo że bot już oznaczył ją CLOSED wyłącznie na
    podstawie zniknięcia z pending. Sprawdza realny status w historii T212
    (_lookup_recent_order) PRZED uznaniem pozycji za zamkniętą.
    Zwraca True jeśli pozycja została faktycznie zamknięta.
    """
    item = _lookup_recent_order(client, order_id)
    if item is None:
        _log(
            user_id, "INFO",
            f"{trade.ticker} (grupa {trade.position_group_id}): zlecenie {order_id} zniknęło z pending, "
            "ale nie udało się jeszcze zweryfikować jego statusu w historii T212 - sprawdzę ponownie "
            "na kolejnym ticku.",
            trade.position_group_id,
        )
        return False

    status = (item.get("order") or {}).get("status")
    if status != _FILLED_ORDER_STATUS:
        _log(
            user_id, "WARN",
            f"{trade.ticker} (grupa {trade.position_group_id}): zlecenie {order_id} zniknęło z pending, "
            f"ale status w historii T212 to {status}, NIE {_FILLED_ORDER_STATUS} - nie zostało "
            "wykonane (anulowane/odrzucone). Pozycja zostaje OPEN, noga zostanie wystawiona od nowa "
            "na kolejnym ticku.",
            trade.position_group_id,
        )
        if trade.sell_order_id == order_id:
            trade.sell_order_id = None
        if trade.stop_order_id == order_id:
            trade.stop_order_id = None
        db.session.commit()
        return False

    fill_price_raw = (item.get("fill") or {}).get("price")
    # BUG znaleziony i naprawiony 2026-07-23 (na żywo, XOM_US_EQ) - fill_price
    # z JSON-a T212 to zwykły float, ale trade.quantity/_log_order oczekują
    # Decimal (Decimal * float rzuca TypeError) - _log_order rzucało wyjątek
    # PRZED wywołaniem _finalize_closed_trade, więc pozycja nigdy nie
    # zostawała CLOSED - ten sam "STOP wykonany" log powtarzał się co tick
    # w nieskończoność, a _manage_trailing_exit (widząc dalej status="OPEN")
    # próbował wystawiać NOWE zlecenia STOP na już nieposiadane akcje.
    fill_price = Decimal(str(fill_price_raw)) if fill_price_raw is not None else None
    if filled_via == "take-profit":
        _log(
            user_id, "INFO",
            f"{trade.ticker} (grupa {trade.position_group_id}): LIMIT SELL (take-profit) wykonany "
            "- pozycja zamknięta.",
            position_group_id=trade.position_group_id,
        )
    else:
        # Ten sam mechanizm STOP obsługuje DWA różne etapy trailing exitu
        # (patrz _manage_trailing_exit): próg 2 to ochrona kapitału
        # (stop POD ceną wejścia, realna strata), próg 3+ to już
        # blokowanie ZYSKU (stop NAD ceną wejścia) - komunikat na sztywno
        # "ze stratą" mylił Adama dwukrotnie (2026-07-22, Siemens x2),
        # bo próg 3+ to w rzeczywistości zysk. Porównujemy fill_price
        # (gdzie się realnie wykonało, fallback stop_target_price gdy fill
        # niedostępny) z average_price (skąd weszliśmy) żeby podpisać
        # właściwie.
        reference_price = fill_price if fill_price is not None else trade.stop_target_price
        result_word = (
            "z ZYSKIEM" if reference_price is not None and reference_price > trade.average_price
            else "ze STRATĄ"
        )
        _log(
            user_id, "WARN",
            f"{trade.ticker} (grupa {trade.position_group_id}): STOP wykonany na "
            f"{fill_price if fill_price is not None else trade.stop_target_price} "
            f"(wejście {trade.average_price}) - pozycja zamknięta {result_word}.",
            position_group_id=trade.position_group_id,
        )

    # Dopisane 2026-07-23 - do tego dnia wyjścia (STOP/LIMIT SELL) w ogóle
    # nie trafiały do OrderLog (_log_order wołane tylko przy kupnie, patrz
    # _enter_position), więc strona Historia (routes/scalping.py::history_view)
    # nigdy nie pokazywała że/za ile bot sprzedał - Adam pytał "gdzie jest
    # historia że się sprzedało". Logujemy TYLKO potwierdzone wykonanie
    # (status="filled"), NIE każde przesunięcie STOP-a przy ciągłym trailingu
    # (patrz _manage_trailing_exit) - inaczej Historia zalałaby się dziesiątkami
    # wpisów "sent" per pozycja (10 przesunięć to normalka jednego dnia).
    _log_order(
        user_id=user_id, ticker=trade.ticker, side="sell",
        quantity=trade.quantity,
        price_snapshot=fill_price if fill_price is not None else trade.stop_target_price,
        status="filled", t212_order_id=order_id,
    )
    _finalize_closed_trade(user_id, client, trade, filled_via, fill_price=fill_price)
    return True


def _detect_exit_fills(user_id: int, client: T212Client, pending_ids: set[str]) -> int:
    """
    Sprawdza czy sell_order_id (take-profit) albo stop_order_id (stop-loss)
    jakiejś OPEN pozycji zniknęło z pending - jeśli tak, weryfikuje w
    historii T212 czy to faktyczne wykonanie czy anulowanie/odrzucenie
    (patrz _resolve_vanished_leg) zanim oznaczy CLOSED i anuluje osieroconą
    drugą nogę (patrz _finalize_closed_trade). Wołane z KAŻDEGO tick() (nie
    tylko reconcile() przy aktywacji) - inaczej pozycja wykonana W TRAKCIE
    gdy bot jest aktywny nigdy nie zostałaby lokalnie zamknięta, a druga
    noga wisiałaby na T212 bez końca. Zwraca liczbę zamkniętych pozycji.
    """
    open_trades = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, environment=current_environment(user_id))
        .filter(db.or_(ActiveTrade.sell_order_id.isnot(None), ActiveTrade.stop_order_id.isnot(None)))
        .all()
    )
    closed_count = 0
    for trade in open_trades:
        if trade.sell_order_id and trade.sell_order_id not in pending_ids:
            if _resolve_vanished_leg(user_id, client, trade, trade.sell_order_id, "take-profit"):
                closed_count += 1
        elif trade.stop_order_id and trade.stop_order_id not in pending_ids:
            if _resolve_vanished_leg(user_id, client, trade, trade.stop_order_id, "stop-loss"):
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

    creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
    if creds is None:
        _log(user_id, "ERROR", "Reconciliation: brak zapisanego klucza API demo, pomijam.")
        return

    settings = RiskSettings.query.filter_by(user_id=user_id).first()
    if not settings or not settings.is_bot_active:
        # Symetrycznie do tick() - bez tego gate'a user z autostartem w
        # bot_autostart_keys.json, ale świadomie WYŁĄCZONYM botem w UI,
        # dostawał pełne reconcile() (w tym realne zlecenia) przy KAŻDYM
        # restarcie appki. Znalezione 2026-08-06 przy incydencie migracji
        # prod->dev, patrz IDEAS_v2.md.
        return

    client = T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
        engine="bot", user_id=user_id,
    )

    # PRZED sprawdzeniem "czy jest cokolwiek do zrobienia" - switch "zarzadzaj
    # wszystkim" (patrz _auto_adopt_foreign_positions) moze wlasnie TERAZ
    # stworzyc pierwsze ActiveTrade z zera (np. swiezo wlaczony switch na
    # koncie gdzie bot dotad nie mial ZADNEJ wlasnej pozycji) - wczesny
    # return ponizej wyleciałby przed ta szansa, gdyby zostal przed tym wywolaniem.
    if settings is not None:
        _auto_adopt_foreign_positions(user_id, client, settings)

    # is_paper=False - pozycje papierowe nigdy nie trafily do T212, wiec nie
    # ma czego z nim uzgadniac (patrz models.py::ActiveTrade.is_paper).
    open_trades = ActiveTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, environment=current_environment(user_id),
    ).all()
    if not open_trades:
        _log(user_id, "INFO", "Reconciliation: brak otwartych pozycji (realnych) do sprawdzenia.")
        return

    # _manage_trailing_exit (ochrona zysku) CELOWO PRZED get_pending_orders()
    # ponizej - ten sam powod co w tick() (patrz komentarz tam, Adam
    # 2026-07-27) - nie korzysta z jego wyniku (brak `pending` w sygnaturze),
    # wiec nie powinna czekac ani obrywac wczesnym returnem, gdyby TO
    # konkretne zapytanie akurat zawiodlo.
    if settings is not None:
        _manage_trailing_exit(user_id, client, settings)

    # get_pending_orders_for_tick() zamiast get_pending_orders() - reconcile()
    # (start appki + ręczna aktywacja) wcześniej omijało WSPÓLNY backoff/cache
    # tick()/reconcile() między Micro-Grid/Sygnał/EOD (patrz t212_client.py),
    # więc restart appki albo ręczne "Aktywuj" mogło strzelić realny request
    # do T212 W TRAKCIE aktywnego backoffu ustawionego przez inny silnik, a
    # własny 429 stąd nie zasilał eskalacji - złapane na żywo 2026-08-04 (EOD
    # dostał 429 mimo że tick() był już w backoffie po wcześniejszym błędzie).
    try:
        pending = client.get_pending_orders_for_tick()
    except T212APIError as exc:
        consecutive, delay_seconds = client.tick_backoff_status() or (1, 60.0)
        _log(
            user_id, "ERROR",
            f"Reconciliation: błąd pobierania pending orders #{consecutive} z rzędu ({exc}) - "
            f"kolejna próba za {max(1, round(delay_seconds / 60))} min.",
        )
        return
    if pending is None:
        _log(user_id, "INFO", "Reconciliation: pomijam (wspólny backoff po wcześniejszych błędach T212).")
        return

    pending_ids = {str(o.get("id")) for o in pending}
    closed_count = _detect_exit_fills(user_id, client, pending_ids)
    if closed_count == 0:
        _log(user_id, "INFO", f"Reconciliation: {len(open_trades)} pozycji sprawdzonych, wszystkie nadal aktualne.")

    # Doliczenie świeżo wypełnionych nóg DCA MUSI polecieć PRZED weryfikacją
    # nadwyżek poniżej - inaczej trade.quantity dla pozycji, której dokupka
    # WŁAŚNIE się wykonała na T212 (ale jeszcze nie doliczona lokalnie), jest
    # zaniżone względem stanu na koncie i weryfikacja krzyczy fałszywą
    # "NADWYŻKĘ" (bug znaleziony 2026-07-27 na ASML - dokupka DCA 0.0605 szt.
    # wykonana, ale reconcile() sprawdzał nadwyżkę zanim _confirm_dca_fills()
    # zdążył ją doliczyć do trade.quantity).
    if settings is not None:
        _confirm_dca_fills(user_id, client, settings, pending=pending)

    # Stara weryfikacja nadwyżek (dodana 2026-07-22) USUNIĘTA 2026-07-29 -
    # porównywała baseline_owned_quantity + trade.quantity z rzeczywistym
    # portfelem, ale (a) łapała WYŁĄCZNIE nadwyżkę, nigdy niedobór (dokładnie
    # ten brak zablokował ASML_US_EQ na 7+ godzin bez żadnej ochrony STOP-a -
    # patrz historia 29.07), i (b) sam WARN nigdy się nie gois - tylko
    # informował, nie naprawiał. Zastąpione ciągłą synchronizacją ilości w
    # _manage_trailing_exit() (patrz tam, wołane kilka linii wyżej) - trade.
    # quantity jest tam na bieżąco ustawiane na to, co T212 faktycznie
    # pokazuje, więc ten blok liczyłby teraz nieaktualną (już zsynchronizowaną)
    # wartość i nie miałby czego wykrywać.

    if settings is not None:
        _retry_pending_buys(user_id, client, settings, pending=pending)
        _retry_pending_sells(user_id, client, settings, pending=pending)
        current_equity = _get_current_equity(user_id, settings, client) if settings.equity_sizing_enabled else None
        _trigger_dca_buys(user_id, client, settings, current_equity)


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

            diagnostics.log_diag(user_id, "bot", "tick start")

            # Bezpiecznik dziennej straty - SPRAWDZANY PRZED czymkolwiek innym
            # w tym ticku. Gdy próg przekroczony: bot się wyłącza (is_bot_active
            # =False) i tracimy poświadczenia, więc następne ticki go pominą.
            # Pozycje NIE są zamykane automatycznie - to świadoma decyzja,
            # panic-sell po przekroczeniu progu potrafi zrealizować stratę
            # dokładnie w dołku. Bot przestaje DOKŁADAĆ, resztą zarządzasz ręcznie.
            if not settings.is_paper_trading:
                _daily_loss_market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
                breach = bot_entry_filters.check_daily_loss_limit(
                    user_id,
                    settings,
                    lambda ticker: price_feed.get_live_price(
                        _daily_loss_market_keys.get("finnhub_api_key"), ticker,
                        _daily_loss_market_keys.get("alpaca_api_key"),
                        _daily_loss_market_keys.get("alpaca_api_secret"),
                    ),
                )
                if breach is not None:
                    bot_credentials.deactivate(user_id)
                    _log(
                        user_id, "WARN",
                        f"STOP: dzienny limit straty przekroczony. Zrealizowane "
                        f"{breach['realized']:+.2f}, niezrealizowane {breach['unrealized']:+.2f}, "
                        f"razem {breach['total']:+.2f} (limit {settings.max_daily_loss}). "
                        "Bot wyłączony - otwarte pozycje ZOSTAJĄ, zarządź nimi ręcznie.",
                    )
                    continue

            # Adam, 2026-07-27: "ty masz chronic zyski a nie sie skupiac na
            # kupnie akcji, kupno nie ma priorytetu" - _process_entries()
            # (nowe wejscia) byla wolana BEZWARUNKOWO na koncu tej funkcji,
            # NIEZALEZNIE od backoffu ponizej - jedyna czesc tick() ktora w
            # ogole nie ustepowala miejsca ochronie istniejacych pozycji,
            # mimo ze rzeczywiste zlecenia kupna (poza paper trading) licza
            # sie do TEGO SAMEGO, ciasnego budzetu T212 co cancel/place
            # trailing STOP-a. Flaga nizej: gdy konto jest w backoffie po
            # serii bledow, nowe wejscia (real, nie paper - paper nie dotyka
            # T212 wcale, patrz _enter_position) czekaja, zeby caly dostepny
            # budzet szedl na ochrone zysku, nie na otwieranie kolejnych pozycji.
            skip_new_entries = False

            client = _get_client_for_user(user_id, settings)

            # Equity RAZ na caly tick (nie per-kandydat) - patrz
            # _get_current_equity. ZERO nowego kosztu API gdy
            # equity_sizing_enabled=False (nie wolamy w ogole). Liczone PRZED
            # `if client is not None:` bo _get_current_equity buduje WLASNY
            # klient gdy trzeba (dziala tez dla paper trading, gdzie `client`
            # tutaj jest None).
            current_equity: Decimal | None = None
            if settings.equity_sizing_enabled:
                current_equity = _get_current_equity(user_id, settings, client)

            if client is not None:
                # _manage_trailing_exit (ochrona JUŻ zarobionego zysku) CELOWO
                # ODDZIELONA od backoffu ponizej (Adam, 2026-07-27: "musisz
                # jakos zmienic ta logike jakie 30min pauzy?? to niedopuszczalne"
                # - zlapane na zywo na CRM_US_EQ, stop utknal bo caly tick byl
                # wstrzymany do 30 min po nieudanym get_pending_orders(), mimo
                # ze trailing stop W OGOLE nie korzysta z jego wyniku, patrz
                # sygnatura _manage_trailing_exit(user_id, client, settings) -
                # brak `pending`). Ten sam mechanizm (cancel STOP + place STOP)
                # co reszta, wiec i tak nie hamruje limitu wiecej niz musi -
                # kazda POJEDYNCZA pozycja ma juz WLASNY backoff per-trade
                # (next_sell_retry_at, do 30 min) na wypadek gdyby jej wlasne
                # proby konsekwentnie failowaly - to zostaje bez zmian, tu
                # usuwamy TYLKO zbedne, calokontowe sprzezenie z backoffem
                # get_pending_orders(), ktory sluzy zupelnie innym funkcjom
                # (potwierdzanie kupna/sprzedazy nizej).
                _manage_trailing_exit(user_id, client, settings)

                # get_pending_orders_for_tick() = get_pending_orders() + backoff
                # WSPÓLNY między Micro-Grid/Sygnał/EOD, patrz t212_client.py -
                # None = wciąż w backoffie po poprzednich błędach (JAKIEGOKOLWIEK
                # z trzech silników), pomijamy CAŁY T212-zależny odcinek ticku bez
                # logowania (inaczej dokładnie ten sam spam co próbowaliśmy tu
                # zlikwidować). Nowe wejscia (ponizej) tez czekaja - patrz "kupno
                # nie ma priorytetu" wyzej.
                try:
                    pending = client.get_pending_orders_for_tick()
                except T212APIError as exc:
                    consecutive, delay_seconds = client.tick_backoff_status() or (1, 60.0)
                    _log(
                        user_id, "ERROR",
                        f"Tick: błąd pobierania pending orders #{consecutive} z rzędu ({exc}) - "
                        f"kolejna próba za {max(1, round(delay_seconds / 60))} min zamiast za 60s.",
                    )
                else:
                    if pending is None:
                        skip_new_entries = True
                    else:
                        pending_ids = {str(o.get("id")) for o in pending}
                        _detect_exit_fills(user_id, client, pending_ids)
                        _confirm_dca_fills(user_id, client, settings, pending=pending)
                        _retry_pending_buys(user_id, client, settings, pending=pending)
                        _retry_pending_sells(user_id, client, settings, pending=pending)
                        # stop_loss_only_mode (2026-08-06, patrz models.py::RiskSettings
                        # - "wylacz wszystko poza stoplossem") - auto-adopt i DCA to obie
                        # AUTONOMICZNE akcje (bot sam decyduje co przejac/dokupic), wiec
                        # obie pomijamy calkowicie w tym trybie; _manage_trailing_exit
                        # wyzej zostaje nietkniety, wiec juz zarzadzane/recznie adoptowane
                        # pozycje nadal dostaja przesuwany stop.
                        if not settings.stop_loss_only_mode:
                            adopted_count = _auto_adopt_foreign_positions(user_id, client, settings)
                            if adopted_count > 0:
                                # Świeżo przejęta pozycja (patrz komentarz przy
                                # _manage_trailing_exit wyżej) inaczej czekałaby
                                # na SWÓJ PIERWSZY trailing check aż do NASTĘPNEGO
                                # ticku (do 60s) - przy szybko rosnącej cenie
                                # oznacza to okno bez ŻADNEJ ochrony zysku mimo
                                # że cena mogła już dawno minąć próg uzbrojenia
                                # stopu. Znalezione na żywo 2026-08-03 (SAP: cena
                                # wejścia 157, w chwili przejęcia już 164 - stop
                                # uzbroił się dopiero na NASTĘPNYM ticku). Drugie
                                # wywołanie w TYM SAMYM ticku kosztuje tyle co
                                # nic dla pozycji już obsłużonych chwilę wcześniej
                                # (cache portfolio/ceny), a dla świeżo przejętej
                                # daje szansę na natychmiastowe uzbrojenie stopu.
                                _manage_trailing_exit(user_id, client, settings)
                            _trigger_dca_buys(user_id, client, settings, current_equity)

            if skip_new_entries:
                continue

            # stop_loss_only_mode (2026-08-07, Adam po serii nieudanych zleceń
            # z powodu za małego entry_amount: "niech tylko wysyła sygnały co
            # kupić, ja kupię ręcznie i mu przekażę do zarządzania") - w tym
            # trybie _process_entries dalej OCENIA kandydatów (scoring), ale
            # NIE wystawia żadnego zlecenia - patrz gałąź stop_loss_only_mode
            # na końcu tamtej funkcji, wysyła zamiast tego sygnał na Telegram.
            _process_entries(user_id, settings, current_equity)


def _process_entries(user_id: int, settings: RiskSettings, current_equity: Decimal | None = None) -> None:
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
    env = current_environment(user_id)
    open_count = ActiveTrade.query.filter_by(user_id=user_id, status="OPEN", is_paper=False, environment=env).count()
    if open_count >= settings.max_concurrent_positions:
        _log_block_reason_throttled(
            user_id, "max_concurrent",
            f"Wejścia: limit pozycji osiągnięty ({open_count}/{settings.max_concurrent_positions}) - nic nowego dziś.",
        )
        return  # limit otwartych pozycji osiągnięty (patrz RiskSettings.max_concurrent_positions) - nic nowego dziś

    now = dt.datetime.utcnow()

    # Krok 1: twarde, DARMOWE odsiewanie (zero zapytań do czegokolwiek) -
    # giełda zamknięta / pozycja już otwarta / asset w backoffie po serii
    # nieudanych prób. Dopiero to co zostanie idzie do scoringu.
    eligible = []
    for asset in BotAsset.query.filter_by(user_id=user_id, is_penny_stock=False, environment=env).all():
        if not _market_open(asset.currency):
            continue  # giełda właściwa dla tej waluty zamknięta - patrz _market_open
        if ActiveTrade.query.filter_by(user_id=user_id, ticker=asset.ticker, status="OPEN", environment=env).first():
            continue
        other = held_by_other_engine(user_id, asset.ticker, "bot")
        if other is not None:
            # Cudza pozycja (Sygnał/EOD) na tym samym tickerze - pomijamy,
            # zeby nie powtorzyc kolizji SUp_EQ (patrz market_hours.py::
            # held_by_other_engine). Throttlowany _log (2026-08-09, raport
            # /why) - _log() i tak dokłada kopię do ukrytego diag-logu
            # (patrz jej docstring), więc to zastępuje dawne bezpośrednie
            # diagnostics.log_diag, nie dubluje.
            _log_block_reason_throttled(
                user_id, f"held_by_other:{asset.ticker}",
                f"{asset.ticker}: pominięte wejście - już otwarte w {other}.",
            )
            continue
        sector_collision = sector_diversity.held_sector_ticker(user_id, env, asset.ticker)
        if sector_collision is not None:
            # Koncentracja sektorowa (2026-08-10) - patrz docstring
            # sector_diversity.py, znalezione na żywo (Sygnał, 4/6 pozycji w
            # tym samym sektorze). Skanuje wszystkie 3 silniki, więc kolizja
            # może pochodzić z DOWOLNEGO z nich, nie tylko Micro-Gridu.
            _log_block_reason_throttled(
                user_id, f"sector:{asset.ticker}",
                f"{asset.ticker}: pominięte wejście - już otwarta pozycja w tym samym sektorze ({sector_collision}).",
            )
            continue
        backoff = _entry_fail_backoff.get((user_id, asset.ticker))
        if backoff is not None and now < backoff[1]:
            _log_block_reason_throttled(
                user_id, f"backoff:{asset.ticker}",
                f"{asset.ticker}: pominięte wejście - w backoffie po nieudanych próbach do {backoff[1].strftime('%H:%M')} UTC.",
            )
            continue  # asset "w pauzie" po serii nieudanych prób
        rejected_until = _entry_signal_rejected_until.get((user_id, asset.ticker))
        if rejected_until is not None and now < rejected_until:
            continue  # Adam odpowiedział "nie" na sygnał - patrz reject_current_signal()
        eligible.append(asset)

    if not eligible:
        return

    # Krok 2: scoring (patrz services/bot_entry_filters.py). Do 23.07 o tym
    # który asset dostanie slot decydowała KOLEJNOŚĆ WIERSZY W BAZIE (brak
    # order_by) - przy 38 kandydatach i limicie 10 pozycji to była największa
    # strata potencjału w całym silniku. Teraz bot wchodzi w NAJLEPSZEGO.
    #
    # Koszt API: ZERO dodatkowych zapytań w domyślnym stanie. candles_getter
    # korzysta z tego samego 30-minutowego cache co filtr trendu i ATR, a
    # scoring celowo NIE potrzebuje żywej ceny (używa ceny zamknięcia
    # ostatniej świecy) - żywa cena jest pobierana dopiero w _enter_position,
    # dla zwycięzcy. Gdy bot_entry_filters.HURST_FILTER_ENABLED (domyślnie
    # WYŁĄCZONY, patrz docstring tamtego modułu) - fetch rośnie do
    # HURST_LOOKBACK_DAYS (ten sam pojedynczy request, większe okno, dalej
    # ZERO dodatkowych zapytań - tylko przy włączonym filtrze dłuższa
    # odpowiedź z tego samego źródła/cache).
    scoring_days = (
        bot_entry_filters.HURST_LOOKBACK_DAYS if bot_entry_filters.HURST_FILTER_ENABLED
        else bot_entry_filters.TREND_LOOKBACK_DAYS
    )
    _entries_market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
    scored, stats = bot_entry_filters.rank_candidates(
        eligible,
        settings,
        candles_getter=lambda ticker: price_feed.get_mini_chart_ohlc(
            _entries_market_keys.get("finnhub_api_key"),
            ticker,
            days=scoring_days,
            alpaca_api_key=_entries_market_keys.get("alpaca_api_key"),
            alpaca_api_secret=_entries_market_keys.get("alpaca_api_secret"),
        ),
    )

    if not scored:
        _log(user_id, "INFO", f"Wejścia: żaden kandydat nie przeszedł filtrów ({stats.summary()}).")
        return

    best_ticker = scored[0][0].ticker
    _log(
        user_id, "INFO",
        f"Wejścia: {stats.summary()}. Najlepszy kandydat: {best_ticker} "
        f"(score {scored[0][1]:.3f}).",
    )

    # stop_loss_only_mode (2026-08-07, Adam: "niech tylko wysyła sygnały co
    # kupić, ja kupię ręcznie i mu przekażę do zarządzania") - scoring wyżej
    # liczy się jak zwykle, ale ZAMIAST wysyłać zlecenie do T212 (realne
    # pieniądze, konto live) tylko informujemy na Telegramie o najlepszym
    # kandydacie. Adam kupuje ręcznie w T212/appce, potem "Przekaż botowi"
    # (routes/bot.py::adopt_position) oddaje pozycję pod trailing stop -
    # dokładnie ta sama ścieżka co ręczna adopcja dziś. Cooldown (patrz
    # ENTRY_SIGNAL_COOLDOWN_MINUTES) - bez niego identyczny sygnał leciałby
    # co tick (60s) dopóki ticker nie zniknie z eligible (czyli dopóki ktoś
    # go faktycznie nie kupi/przekaże).
    if settings.stop_loss_only_mode:
        best_asset = scored[0][0]
        key = (user_id, best_ticker)
        now = dt.datetime.utcnow()
        last_sent = _entry_signal_sent_at.get(key)
        if last_sent is None or (now - last_sent) >= dt.timedelta(minutes=ENTRY_SIGNAL_COOLDOWN_MINUTES):
            _entry_signal_sent_at[key] = now
            _last_signal_ticker[user_id] = best_ticker
            name = ticker_display_name(best_ticker)
            telegram_notify.send_telegram_message(
                current_app.config.get("TELEGRAM_BOT_TOKEN"), current_app.config.get("TELEGRAM_CHAT_ID"),
                f"🟢 [{telegram_env_tag(user_id)}] Micro-Grid SYGNAŁ (user {user_id}): {name} ({best_ticker}) - "
                f"score {scored[0][1]:.3f}, sugerowana kwota wejścia {best_asset.entry_amount}"
                f" {best_asset.currency}. Kup ręcznie, potem \"Przekaż botowi\" na stronie "
                f"instrumentu, żeby przejął trailing stop. Odpisz \"nie\", żeby nie "
                f"podpowiadał tego przez {ENTRY_SIGNAL_REJECT_MINUTES} min.",
            )
        return

    # Krok 3: próbujemy od najlepszego. NAJWYŻEJ JEDNO wejście na tick, ale
    # tylko jeśli faktycznie dotarło do T212 (return dopiero gdy
    # _enter_position zwróci True) - patrz docstring tej funkcji.
    for asset, _score in scored:
        if _enter_position(user_id, asset, settings, current_equity):
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
        alpaca_api_key=current_app.config.get("ALPACA_API_KEY"),
        alpaca_api_secret=current_app.config.get("ALPACA_API_SECRET"),
    )
    if not candles or len(candles) < 2:
        return True

    oldest_close = Decimal(str(candles[0]["c"]))
    newest_close = Decimal(str(candles[-1]["c"]))
    if oldest_close <= 0:
        return True

    drop_pct = (oldest_close - newest_close) / oldest_close
    return drop_pct <= ENTRY_TREND_MAX_DROP_PCT


def _enter_position(
    user_id: int, asset: BotAsset, settings: RiskSettings, current_equity: Decimal | None = None,
) -> bool:
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

    creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
    if creds is None:
        _log(user_id, "ERROR", f"{asset.ticker}: brak zapisanego klucza API demo.")
        return False

    market_keys = get_decrypted_market_data_keys(user_id, master_key)
    price = price_feed.get_live_price(
        market_keys.get("finnhub_api_key"), asset.ticker,
        market_keys.get("alpaca_api_key"), market_keys.get("alpaca_api_secret"),
    )
    if price is None or price <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: brak ceny (Finnhub i Yahoo zawiodły), pomijam ten tick.")
        return False

    effective_entry_amount = asset.entry_amount
    if settings.equity_sizing_enabled and current_equity is not None:
        effective_entry_amount = microgrid_strategy.compute_equity_scaled_amount(
            asset.entry_amount, current_equity, settings.equity_sizing_baseline or Decimal("0"),
        )

    try:
        entry_decision = microgrid_strategy.compute_entry_quantity(effective_entry_amount, price)
    except microgrid_strategy.EntryValidationError as exc:
        _log(user_id, "ERROR", f"{asset.ticker}: {exc}")
        return False
    quantity = entry_decision.quantity

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
            buy_confirmed=True, environment=current_environment(user_id),
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

    client = T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
        engine="bot", user_id=user_id,
    )

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
        environment=current_environment(user_id),
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

            env = current_environment(settings.user_id)
            closed = (
                ActiveTrade.query
                .filter_by(user_id=settings.user_id, is_paper=False, status="CLOSED", environment=env)
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

            open_trades = ActiveTrade.query.filter_by(user_id=settings.user_id, is_paper=False, status="OPEN", environment=env).all()
            unrealized_total = Decimal("0")
            unrealized_known = 0
            open_lines = []
            _report_market_keys = get_decrypted_market_data_keys(
                settings.user_id, bot_credentials.get_master_key(settings.user_id),
            )
            for t in open_trades:
                price = price_feed.get_live_price(
                    _report_market_keys.get("finnhub_api_key"), t.ticker,
                    _report_market_keys.get("alpaca_api_key"), _report_market_keys.get("alpaca_api_secret"),
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
