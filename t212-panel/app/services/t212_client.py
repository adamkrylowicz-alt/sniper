"""
t212_client.py
===============
Wrapper na oficjalne Trading 212 Public API (beta).

WAŻNE OGRANICZENIA (ustalone researchem, stan na 2026):
--------------------------------------------------------
1. API NIE udostępnia endpointu z ceną/quote na żywo. Nie ma REST-owego
   ani WebSocketowego strumienia cen. Cena pojawia się wyłącznie:
   - w /equity/portfolio, ale TYLKO dla instrumentów które już posiadasz
   - jako LTP (Last Traded Price) używane wewnętrznie przez zlecenia
     Stop/Stop-Limit jako punkt wyzwalający - nie jest to endpoint do
     odpytania "jaka jest teraz cena X".

   Konsekwencja architektoniczna: ten moduł NIE próbuje dawać Ci ceny.
   Zgodnie z ustaleniami, cenę oglądasz sam na ekranie/appce T212 -
   ten klient jest czystym "wykonawcą" zleceń, nie źródłem danych rynkowych.

2. Konto Invest = tylko long. Możesz kupować i sprzedawać posiadane
   instrumenty, bez shortowania i bez marginu.

3. W becie na koncie LIVE wspierane są WYŁĄCZNIE Market Orders.
   Limit/Stop/Stop-Limit działają na demo, ale NIE są dostępne na live -
   place_limit_order() istnieje (używa go Micro-Grid Bot, services/bot_engine.py),
   ale warstwa wyżej (routes/bot.py) twardo blokuje aktywację bota na
   environment="live", więc w praktyce ta metoda dziś woła wyłącznie demo.

4. Autoryzacja: Basic Auth, para "Klucz:Sekret" zakodowana w base64
   (nagłówek "Authorization: Basic <base64(API_KEY:API_SECRET)>").
   UWAGA: to zmiana względem pierwszej wersji tego pliku - pierwotnie
   zaimplementowałem pojedynczy token w nagłówku Authorization, bo część
   dokumentacji T212 tak sugerowała. W praktyce (potwierdzone testem na
   koncie demo - błąd 401 przy samym tokenie, sukces po dodaniu sekretu)
   T212 wymaga PEŁNEJ pary klucz+sekret w formacie Basic Auth.

5. Market Order może się wykonać po innej cenie niż oczekiwana
   (slippage) - T212 wprost to zaznacza w dokumentacji. Ten klient
   tego nie łagodzi - to zadanie dla risk_guard.py + Twojej własnej oceny.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import requests

from . import diagnostics

logger = logging.getLogger(__name__)

Environment = Literal["demo", "live"]

BASE_URLS: dict[Environment, str] = {
    "demo": "https://demo.trading212.com/api/v0",
    "live": "https://live.trading212.com/api/v0",
}

DEFAULT_TIMEOUT = 10  # sekund na request

# Kolejka/throttle PER-ENDPOINT na wszystkie zapytania do T212 (dodane
# 2026-07-28, Adam: "to musi byc jakos kolejkowane a nie faktycznie wszystkie
# zapytania napierdlaja jednoczesnie"; PRZEROBIONE na per-endpoint 2026-07-30,
# Adam podrzucil oficjalny plik specyfikacji API T212 (pliki/api.json/yaml) z
# limitami wpisanymi wprost w opis kazdej operacji - "skoro niektore mozna
# postowac co 1s to na chuja czekac 5s". Micro-Grid/Sygnal/EOD kazdy tworzy
# WLASNY T212Client co tick (3 osobne instancje), a APScheduler odpala
# wszystkie trzy joby (interval=60s) w praktycznie tej samej sekundzie - bez
# throttle'a 3 silniki potrafily wystrzelic po kilka zapytan do TEGO SAMEGO,
# bardzo ciasnego limitu (potwierdzone spec'iem I zmierzone recznie 2026-07-30
# - patrz tabela w _rate_limit_key_and_interval nizej) w oknie <1s, gwarantujac
# 429 nawet gdy pojedynczy silnik sam w sobie nie przekraczalby limitu.
#
# Lock jest MODULOWY (nie per-instancja T212Client) wlasnie dlatego, ze
# wszystkie trzy silniki dzialaja w TYM SAMYM procesie Pythona (APScheduler =
# watki w jednym `run.py`, nie osobne procesy) - dzieki temu serializuje
# zapytania NIEZALEZNIE od tego, ktory silnik/instancja je wysyla. Trzyma lock
# przez CALY czas zapytania (wlacznie z oczekiwaniem na odpowiedz), wiec w
# danej chwili do T212 leci co najwyzej JEDNO zapytanie z calej appki - ale
# odstep MINIMALNY wyliczany jest teraz OSOBNO per endpoint (slownik zamiast
# pojedynczego float), zamiast jednego wspolnego zegara dla wszystkiego. Dzieki
# temu np. Skasuj/nowe zlecenie Market (limit T212: 50/60s) nie czeka juz na
# ten sam, ciasny 5.5s odstep co odczyt listy zlecen (limit: 1/5s) - kazdy
# endpoint dostaje WLASNY, poprawny odstep.
_rate_limit_lock = threading.Lock()
_last_request_by_key: dict[str, float] = {}

# Limity WPROST ze specyfikacji OpenAPI T212 (pliki/api.json/api.yaml,
# dostarczone przez Adama 2026-07-30 - jeden wspolny spec dla demo i live,
# bez rozroznienia per-srodowisko). Wartosc = period/limit + maly zapas (dla
# limitow >1 to bezpieczny odstep przy CIAGLYM uzyciu, nie realne wykorzystanie
# calej pojemnosci "wybuchu" - swiadomy kompromis prostoty). "portfolio" nie
# jest w spec'ie wcale (widocznie starszy/nieudokumentowany alias) - zmierzone
# recznie 2026-07-30 (zatrzymany Snajper, 10x bez opoznienia): identyczny wzor
# jak account/summary i orders (1/5s), stad taka sama wartosc.
#   GET    /equity/orders (lista)         -> 1 / 5s
#   GET    /equity/orders/{id}            -> 1 / 1s
#   DELETE /equity/orders/{id}            -> 50 / 60s
#   POST   /equity/orders/market          -> 50 / 60s
#   POST   /equity/orders/limit           -> 1 / 2s
#   POST   /equity/orders/stop            -> 1 / 2s
#   GET    /equity/account/summary        -> 1 / 5s
#   GET    /equity/portfolio              -> niedokumentowany, zmierzony jako 1 / 5s
#   GET    /equity/metadata/instruments   -> 1 / 50s
#   GET    /equity/history/orders         -> 6 / 60s
DEFAULT_MIN_INTERVAL_SECONDS = 5.5  # bezpieczny fallback dla niewymienionych wprost


def _rate_limit_key_and_interval(method: str, path: str) -> tuple[str, float]:
    """
    Mapuje (method, path) na (klucz throttle'a, minimalny bezpieczny odstep
    w sekundach) wg tabeli wyzej. Kolejnosc sprawdzania WAZNA - najpierw
    najbardziej specyficzne sciezki (np. /equity/orders/market), dopiero potem
    ogolniejsze wzorce z tym samym prefiksem (np. /equity/orders/{id}),
    inaczej te pierwsze zlapalyby sie w zly, ogolniejszy przypadek.
    """
    if method == "POST" and path == "/equity/orders/market":
        return "orders/market", 1.3
    if method == "POST" and path == "/equity/orders/limit":
        return "orders/limit", 2.2
    if method == "POST" and path == "/equity/orders/stop":
        return "orders/stop", 2.2
    if method == "DELETE" and path.startswith("/equity/orders/"):
        return "orders/cancel", 1.3
    if method == "GET" and path.startswith("/equity/orders/"):
        return "orders/by_id", 1.1
    if method == "GET" and path == "/equity/orders":
        return "orders/list", 5.5
    if method == "GET" and path == "/equity/account/summary":
        return "account/summary", 5.5
    if method == "GET" and path == "/equity/portfolio":
        return "portfolio", 5.5
    if method == "GET" and path == "/equity/metadata/instruments":
        return "metadata/instruments", 50.5
    if method == "GET" and path == "/equity/history/orders":
        return "history/orders", 10.5
    return f"{method} {path}", DEFAULT_MIN_INTERVAL_SECONDS

# Cache dzielony MIĘDZY silnikami dla get_portfolio()/get_pending_orders()
# (dodane 2026-07-29 - throttle wyżej rozstrzelał zapytania w czasie, ale
# NIE zmniejszył ich LICZBY: Micro-Grid/Sygnał/EOD nadal wołają te same dwa
# endpointy OSOBNO w swoim własnym tick(), a wszystkie trzy joby APScheduler
# mają interval=60s - więc mimo throttle'a i tak leciały 3 realne zapytania
# do TEGO SAMEGO, bardzo ciasnego limitu (x-ratelimit-limit=1) w ciągu paru
# sekund, gwarantując 429 dla 2 z 3 silników w KAŻDYM cyklu (potwierdzone w
# run.log 2026-07-29 - non-stop "błąd pobierania pending orders" narastającym
# backoffem cały dzień). Cache kluczowany (environment, api_key), TTL krótszy
# niż interwał ticku (60s) - pierwszy silnik w danym cyklu robi realny
# fetch, kolejne dwa dostają ten sam wynik z cache, a NASTĘPNY cykl (60s
# później) i tak dostanie świeży fetch.
#
# WAŻNE: cache'owany jest też WYNIK BŁĘDU (T212APIError), nie tylko sukces -
# pierwsza wersja tego fixu cache'owała tylko sukces, więc gdy limit był już
# wyczerpany i pierwszy silnik dostawał 429, nic się nie zapisywało do cache'a
# i drugi/trzeci silnik i tak strzelał WŁASNYM realnym zapytaniem (od razu
# odtwarzając ten sam problem - potwierdzone w run.log 2026-07-29 tuz po
# restarcie: signal ORAZ eod dostały każdy swój 429 w tym samym cyklu).
# Trzymanie błędu w cache'u i re-raise'owanie go kolejnym callerom w tym
# samym oknie TTL gwarantuje NAJWYŻEJ jedno realne zapytanie na (endpoint,
# user) na cały cykl, niezależnie od tego czy się powiedzie czy nie.
_shared_cache_lock = threading.Lock()
_portfolio_cache: dict[str, tuple[float, list[dict] | None, Exception | None]] = {}
_pending_orders_cache: dict[str, tuple[float, list[dict] | None, Exception | None]] = {}
SHARED_CACHE_TTL_SECONDS = 50.0

# Backoff po błędach get_pending_orders() DZIELONY między Micro-Grid/Sygnał/
# EOD (przeniesione tu 2026-07-29 - Adam: "musisz jakos wspolnie korelowac
# te wejscia, nie moze byc ze boty nie widza o sobie i napierdlaja w ten sam
# czas"). Wcześniej KAŻDY z trzech silników miał WŁASNY, osobny
# `_tick_error_backoff` (w bot_engine.py/signal_engine.py/eod_engine.py) -
# trzy niezależne "zegary", każdy odliczający OD MOMENTU WŁASNEJO pierwszego
# błędu. Skutek na żywo (2026-07-29, PO already wdrożonym SHARED_CACHE_TTL
# powyżej): Sygnał złapał 5 kolejnych 429 i wyszedł w 30-min backoff, mimo że
# Micro-Grid i EOD w tym samym czasie ticowały bez błędu - zegar Sygnału był
# całkowicie rozjechany względem tego, kiedy limit T212 faktycznie miał
# jakiś budżet, bo "wie" tylko o WŁASNYCH nieudanych próbach, nie o tym, że
# inny silnik właśnie zjadł jedyny dostępny slot. Backoff kluczowany
# _cache_key (jak cache wyżej) - TA SAMA skala eskalacji (1,2,5,15,30 min),
# ale JEDNO wspólne źródło prawdy: kto pierwszy w danym cyklu zobaczy błąd,
# ten ustawia zegar dla WSZYSTKICH trzech silników, i wszystkie trzy czekają
# do tego samego `retry_at`, zamiast każdy próbować według własnego uznania.
TICK_ERROR_BACKOFF_MINUTES = (1, 2, 5, 15, 30)
_shared_tick_backoff: dict[str, tuple[int, float]] = {}  # {cache_key: (consecutive, monotonic_retry_at)}


def _next_tick_error_delay_seconds(consecutive_errors: int) -> float:
    idx = min(consecutive_errors - 1, len(TICK_ERROR_BACKOFF_MINUTES) - 1)
    return TICK_ERROR_BACKOFF_MINUTES[idx] * 60.0


class T212APIError(Exception):
    """
    Podniesiony przy każdym błędzie odpowiedzi API (status >= 400).
    Przechowuje status_code i treść odpowiedzi, żeby warstwa wyżej
    (routes/scalping.py) mogła np. rozróżnić 429 (rate limit) od 400
    (błędne dane zlecenia) i zareagować inaczej.
    """

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"T212 API error {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


@dataclass
class OrderResult:
    """Znormalizowany wynik złożenia zlecenia, niezależny od surowego JSON-a T212."""
    order_id: str | None
    ticker: str
    quantity: Decimal
    status: str
    raw: dict


class T212Client:
    """
    Cienki, synchroniczny klient REST na Trading 212 Public API.

    Użycie:
        client = T212Client(api_key="...", environment="demo")
        cash = client.get_cash()
        result = client.place_market_order("AAPL_US_EQ", quantity=Decimal("1"))
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        environment: Environment = "demo",
        timeout: int = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
        engine: str = "?",
        user_id: int | None = None,
    ):
        if environment not in BASE_URLS:
            raise ValueError(f"Nieznane środowisko: {environment!r} (oczekiwano 'demo' albo 'live')")

        self.environment = environment
        self.base_url = BASE_URLS[environment]
        self.timeout = timeout
        self._session = session or requests.Session()
        # engine/user_id - WYŁĄCZNIE do otagowania wpisów w ukrytym logu
        # diagnostycznym (diagnostics.py, 2026-07-30) - nie wpływają na żadne
        # zachowanie/cache/rate-limit, patrz _cache_key niżej (ten zostaje
        # bez zmian, po environment+api_key).
        self.engine = engine
        self.user_id = user_id

        credentials = f"{api_key}:{api_secret}".encode("utf-8")
        basic_token = base64.b64encode(credentials).decode("utf-8")
        self._session.headers.update({
            "Authorization": f"Basic {basic_token}",
            "Content-Type": "application/json",
        })
        # Klucz do _portfolio_cache/_pending_orders_cache - environment+api_key,
        # żeby dwóch różnych userów (albo demo/live tego samego usera) nie
        # dzielili cache'a między sobą.
        self._cache_key = f"{environment}:{api_key}"

    # -- Niskopoziomowa obsługa requestów -----------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        rl_key, min_interval = _rate_limit_key_and_interval(method, path)

        with _rate_limit_lock:
            last = _last_request_by_key.get(rl_key, 0.0)
            wait = min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                _last_request_by_key[rl_key] = time.monotonic()
                logger.error("T212 request failed: %s %s -> %s", method, url, exc)
                diagnostics.log_diag(
                    self.user_id, self.engine, f"T212 {method} {path} -> błąd sieci: {exc}",
                )
                raise T212APIError(0, f"Błąd sieci: {exc}") from exc
            _last_request_by_key[rl_key] = time.monotonic()

        self._log_rate_limit(resp)
        diagnostics.log_diag(
            self.user_id, self.engine,
            f"T212 {method} {path} -> {resp.status_code}, "
            f"rate_limit remaining={resp.headers.get('x-ratelimit-remaining', '?')}"
            f"/{resp.headers.get('x-ratelimit-limit', '?')}",
        )

        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text
            raise T212APIError(resp.status_code, str(payload), payload)

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _cached_request(
        self, path: str, cache: dict[str, tuple[float, list[dict] | None, Exception | None]]
    ) -> list[dict]:
        """GET z cache'em dzielonym między silnikami - patrz _cached_request_ex()."""
        result, error, _ = self._cached_request_ex(path, cache)
        if error is not None:
            raise error
        return result

    def _cached_request_ex(
        self, path: str, cache: dict[str, tuple[float, list[dict] | None, Exception | None]]
    ) -> tuple[list[dict] | None, Exception | None, bool]:
        """
        Jak _cached_request(), ale zwraca też `was_fresh` (czy TO konkretne
        wołanie zrobiło realny request, czy trafiło w cache) - potrzebne
        get_pending_orders_for_tick() do eskalacji WSPÓLNEGO backoffu
        wyłącznie na podstawie świeżego błędu, nie każdego re-raise'u
        cache'owanego błędu (inaczej 2-3 silniki obserwujące TEN SAM
        cache'owany błąd w tym samym oknie TTL podbiłyby licznik 2-3x
        zamiast 1x, przez co backoff eskalowałby szybciej niż powinien).

        Cache'uje TAKŻE błąd (re-raise tego samego wyjątku kolejnym
        callerom w oknie TTL), nie tylko sukces.

        Znalezione na żywo 2026-07-30 (IFXd_EQ, 429 nieprzerwanie przez
        godziny mimo tego cache'a): lock był trzymany TYLKO przy sprawdzeniu
        cache'a i TYLKO przy zapisie wyniku, NIE przez cały czas trwania
        samego _request() - trzy silniki (Micro-Grid/Sygnał/EOD) wołające to
        niemal jednocześnie (np. wszystkie trzy przy starcie appki/autostart)
        każde widziały "cache pusty/przeterminowany" PRZED tym, jak
        którekolwiek zdążyło zapisać świeży wynik, więc wszystkie trzy i tak
        strzelały osobnym, realnym requestem - efektywnie zerując sens tego
        cache'a właśnie w chwilach największego obciążenia ciasnego limitu
        demo. Fix: lock trzymany przez CAŁY check-then-fetch-then-store -
        tylko PIERWSZY caller robi realny request, reszta czeka na ten sam
        lock i dostaje to, co on właśnie zapisał, zamiast dublować zapytanie.
        """
        with _shared_cache_lock:
            cached = cache.get(self._cache_key)
            if cached and (time.monotonic() - cached[0]) < SHARED_CACHE_TTL_SECONDS:
                _, cached_result, cached_error = cached
                age = time.monotonic() - cached[0]
                diagnostics.log_diag(
                    self.user_id, self.engine, f"T212 GET {path} -> cache hit (wiek {age:.1f}s)",
                )
                return cached_result, cached_error, False

            result: list[dict] | None = None
            error: Exception | None = None
            try:
                result = self._request("GET", path) or []
            except T212APIError as exc:
                error = exc

            cache[self._cache_key] = (time.monotonic(), result, error)

        return result, error, True

    def get_pending_orders_for_tick(self) -> list[dict] | None:
        """
        Używane przez tick() Micro-Grid/Sygnał/EOD ZAMIAST get_pending_orders()
        + własny, silnik-specyficzny backoff - patrz komentarz przy
        _shared_tick_backoff. Zwraca None, gdy jesteśmy we WSPÓLNYM backoffie
        po poprzednich błędach (caller powinien pominąć CAŁY T212-zależny
        odcinek ticku bez logowania, tak jak dotychczas). W przeciwnym razie
        zachowuje się jak get_pending_orders() - zwraca listę albo podnosi
        T212APIError (caller loguje treść błędu + woła tick_backoff_status()
        po (consecutive, delay) do komunikatu).
        """
        with _shared_cache_lock:
            backoff = _shared_tick_backoff.get(self._cache_key)
            if backoff is not None and time.monotonic() < backoff[1]:
                return None

        result, error, was_fresh = self._cached_request_ex("/equity/orders", _pending_orders_cache)

        if error is not None:
            if was_fresh:
                self._escalate_tick_backoff()
            raise error

        with _shared_cache_lock:
            _shared_tick_backoff.pop(self._cache_key, None)
        return result

    def tick_backoff_status(self) -> tuple[int, float] | None:
        """
        (consecutive, delay_seconds) aktualnego WSPÓLNEGO backoffu (patrz
        _shared_tick_backoff), albo None gdy żaden silnik go nie eskalował.
        Wołaj PO otrzymaniu T212APIError z get_pending_orders_for_tick() - do
        zbudowania komunikatu logu z tymi samymi liczbami niezależnie od
        tego, czy TEN caller zrobił świeży request, czy trafił w cache'owany
        błąd zapisany przez inny silnik chwilę wcześniej.
        """
        with _shared_cache_lock:
            backoff = _shared_tick_backoff.get(self._cache_key)
        if backoff is None:
            return None
        consecutive, retry_at = backoff
        return consecutive, max(retry_at - time.monotonic(), 0.0)

    def _escalate_tick_backoff(self) -> None:
        with _shared_cache_lock:
            backoff = _shared_tick_backoff.get(self._cache_key)
            consecutive = (backoff[0] if backoff else 0) + 1
            delay = _next_tick_error_delay_seconds(consecutive)
            _shared_tick_backoff[self._cache_key] = (consecutive, time.monotonic() + delay)

    @staticmethod
    def _log_rate_limit(resp: requests.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        limit = resp.headers.get("x-ratelimit-limit")
        if remaining is not None and limit is not None and int(remaining) <= 3:
            logger.warning(
                "T212 rate limit prawie wyczerpany: %s/%s pozostało (endpoint %s)",
                remaining, limit, resp.request.url if resp.request else "?",
            )

    # -- Konto ---------------------------------------------------------------

    def get_cash(self) -> dict:
        """
        Zwraca stan gotówki/konta, znormalizowany do prostego kształtu
        {"free": ..., "total": ..., "reserved": ..., "investments": ...}.

        UWAGA: pierwotnie używałem /equity/account/cash, co dawało 403
        (zweryfikowane empirycznie na koncie demo - zły/nieaktualny endpoint).
        Poprawny endpoint to /equity/account/summary, z polami zagnieżdżonymi
        w cash.* i investments.* - patrz mapowanie niżej.
        """
        raw = self._request("GET", "/equity/account/summary") or {}
        cash = raw.get("cash", {}) or {}
        investments = raw.get("investments", {}) or {}
        return {
            "free": cash.get("availableToTrade"),
            "total": raw.get("totalValue"),
            "reserved": cash.get("reservedForOrders"),
            "investments": investments.get("currentValue"),
            "raw": raw,
        }

    def get_account_summary(self) -> dict:
        """Pełny, surowy summary konta (cash + investments) - patrz get_cash()."""
        return self._request("GET", "/equity/account/summary")

    # -- Portfolio -------------------------------------------------------------

    def get_portfolio(self) -> list[dict]:
        """
        Lista aktualnie otwartych pozycji: ticker, quantity, averagePrice,
        currentPrice, ppl (profit/loss) - dla instrumentów, które POSIADASZ.
        Nie zwraca cen dla instrumentów, których nie masz w portfelu.

        Wynik cache'owany do SHARED_CACHE_TTL_SECONDS i dzielony między
        Micro-Grid/Sygnał/EOD (patrz komentarz przy _portfolio_cache) -
        NIE wołaj tego tam, gdzie potrzebujesz gwarantowanie świeżego stanu
        (np. zaraz po własnoręcznie złożonym zleceniu w tym samym ticku).
        """
        return self._cached_request("/equity/portfolio", _portfolio_cache)

    def get_position(self, ticker: str) -> dict | None:
        """
        Wygodny skrót: zwraca pozycję dla danego tickera z portfela,
        albo None jeśli go nie posiadasz. Przydatne przed sprzedażą -
        żeby sprawdzić czy w ogóle masz co sprzedać i ile.
        """
        for position in self.get_portfolio():
            if position.get("ticker") == ticker:
                return position
        return None

    # -- Instrumenty -----------------------------------------------------------

    def get_instruments(self) -> list[dict]:
        """
        Lista wszystkich dostępnych do handlu instrumentów (ticker, name,
        currencyCode, maxOpenQuantity, isin, type). Warto to cache'ować
        lokalnie (np. raz dziennie), zamiast odpytywać przy każdym starcie -
        lista rzadko się zmienia, a to spory response.
        """
        return self._request("GET", "/equity/metadata/instruments")

    # -- Zlecenia ---------------------------------------------------------------

    def place_market_order(
        self,
        ticker: str,
        quantity: Decimal,
        extended_hours: bool = False,
    ) -> OrderResult:
        """
        Składa zlecenie Market. Kierunek (kupno/sprzedaż) wynika ZE ZNAKU
        quantity: dodatnia = buy, ujemna = sell. To zachowanie API T212,
        nie konwencja tego klienta - warto to wprost widzieć w warstwie
        UI (dwa przyciski Buy/Sell powinny tylko ustawiać znak).

        extended_hours=True pozwala na wykonanie poza standardową sesją -
        zlecenie wtedy czeka w kolejce do otwarcia rynku, jeśli jest zamknięty.

        Zwraca OrderResult ze znormalizowanymi polami + surowym JSON-em (raw)
        na wszelki wypadek, gdyby wyżej potrzebne było coś spoza normalizacji.
        """
        body = {
            "ticker": ticker,
            "quantity": float(quantity),
            "extendedHours": extended_hours,
        }
        raw = self._request("POST", "/equity/orders/market", json=body)

        return OrderResult(
            order_id=str(raw.get("id")) if raw and raw.get("id") is not None else None,
            ticker=ticker,
            quantity=quantity,
            status=raw.get("status", "UNKNOWN") if raw else "UNKNOWN",
            raw=raw or {},
        )

    def place_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        limit_price: Decimal,
        time_validity: str = "GOOD_TILL_CANCEL",
    ) -> OrderResult:
        """
        Składa zlecenie Limit - execute'uje po limit_price LUB LEPSZEJ cenie.
        Kierunek jak w place_market_order: znak quantity (dodatnia=buy,
        ujemna=sell). Używane przez Micro-Grid Bota (services/bot_engine.py)
        do natychmiastowego LIMIT SELL po każdym wejściu.

        time_validity: "DAY" albo "GOOD_TILL_CANCEL" (oficjalny schemat T212,
        docs.trading212.com/api/orders/placelimitorder.md) - "GOOD_TILL_CANCEL"
        bo bot potrzebuje zlecenia WISZĄCEGO bez limitu czasowego (nie "DAY",
        które wygasłoby z końcem sesji).

        UWAGA - ZWERYFIKOWANE NA ŻYWO (17.07.2026, konto demo): pierwsza wersja
        tej metody wysyłała też pole "extendedHours" (kopiując wzorzec z
        place_market_order) - T212 odrzucał to jako 400 "Invalid payload".
        Oficjalny schemat tego endpointu ma TYLKO 4 pola: limitPrice, quantity,
        ticker, timeValidity - "extendedHours" NIE jest tu obsługiwane (istnieje
        wyłącznie dla Market Order). Nie dodawaj go z powrotem bez sprawdzenia.
        """
        body = {
            "ticker": ticker,
            "quantity": float(quantity),
            "limitPrice": float(limit_price),
            "timeValidity": time_validity,
        }
        raw = self._request("POST", "/equity/orders/limit", json=body)

        return OrderResult(
            order_id=str(raw.get("id")) if raw and raw.get("id") is not None else None,
            ticker=ticker,
            quantity=quantity,
            status=raw.get("status", "UNKNOWN") if raw else "UNKNOWN",
            raw=raw or {},
        )

    def place_stop_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        time_validity: str = "GOOD_TILL_CANCEL",
    ) -> OrderResult:
        """
        Składa zlecenie Stop (CZYSTY stop, nie Stop-Limit) - gdy Last Traded
        Price osiągnie stop_price, T212 wystawia w to miejsce Market Order.
        Kierunek jak w innych metodach: znak quantity (dodatnia=buy,
        ujemna=sell). Używane jako noga ochronna (stop-loss) trailing exitu
        - patrz services/bot_engine.py::_manage_trailing_exit.

        Endpoint POST /equity/orders/stop, schemat (docs.trading212.com/api/
        orders/placestoporder_1.md, zweryfikowane 2026-07-21): quantity,
        stopPrice, ticker, timeValidity - TYLKO te 4 pola, ten sam kształt co
        place_limit_order tylko stopPrice zamiast limitPrice (żadnego
        limitPrice tutaj - to byłby już Stop-Limit, inny endpoint).
        """
        body = {
            "ticker": ticker,
            "quantity": float(quantity),
            "stopPrice": float(stop_price),
            "timeValidity": time_validity,
        }
        raw = self._request("POST", "/equity/orders/stop", json=body)

        return OrderResult(
            order_id=str(raw.get("id")) if raw and raw.get("id") is not None else None,
            ticker=ticker,
            quantity=quantity,
            status=raw.get("status", "UNKNOWN") if raw else "UNKNOWN",
            raw=raw or {},
        )

    def get_pending_orders(self, *, force_refresh: bool = False) -> list[dict]:
        """
        Zlecenia jeszcze niewykonane / nieanulowane / niewygasłe.

        Wynik cache'owany do SHARED_CACHE_TTL_SECONDS i dzielony między
        Micro-Grid/Sygnał/EOD, patrz get_portfolio() i komentarz przy
        _pending_orders_cache - te same zasady dot. świeżości.

        force_refresh=True pomija cache całkowicie (prawdziwy request do
        T212) - dodane 2026-07-30 po realnym buggu: cancel_one()/
        reprice_order() w routes/scalping.py sprawdzały istnienie
        KONKRETNEGO, świeżo utworzonego zlecenia przez ten sam 50s cache co
        boty - jeśli cache zdążył się zapełnić TUZ PRZED utworzeniem tego
        zlecenia (np. przez tick jednego z silników), "Skasuj"/edycja
        dostawały 404 "zlecenie nie istnieje" mimo ze ono realnie tam było
        (Adam: "nie da się skasować orderu", "skasuj działa jak anuluj" -
        czyli cichcem trafialo w galaz "nie ok", front cofal linie).
        Świadomie NIE zmieniamy tu domyślnego zachowania (boty nadal
        korzystają z cache'a przy każdym ticku) - to pojedyncza, rzadka
        akcja user-initiated (klik), nie okresowe pollowanie, więc jeden
        dodatkowy realny request jest do przyjęcia.
        """
        if force_refresh:
            return self._request("GET", "/equity/orders") or []
        return self._cached_request("/equity/orders", _pending_orders_cache)

    def cancel_order(self, order_id: str) -> None:
        """Próbuje anulować aktywne, niewykonane zlecenie po jego ID."""
        self._request("DELETE", f"/equity/orders/{order_id}")

    def get_order_history(self, limit: int = 20, cursor: str | None = None) -> dict:
        """
        Historia zamkniętych zleceń (do audytu i do sprawdzania rzeczywistej
        ceny wykonania po fakcie). Zwraca {"items": [...], "nextPagePath": str|None}.
        Do paginacji użyj wprost wartości nextPagePath jako kolejnego zapytania.
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/equity/history/orders", params=params)


# ---------------------------------------------------------------------------
# Przykład użycia (do testów na koncie DEMO, żeby sprawdzić czy w ogóle "zatrybi"):
#
#   from decimal import Decimal
#   client = T212Client(api_key="TWÓJ_DEMO_KEY", api_secret="TWÓJ_DEMO_SECRET", environment="demo")
#
#   print(client.get_cash())
#   print(client.get_portfolio())
#
#   result = client.place_market_order("AAPL_US_EQ", Decimal("1"))   # kupno 1 sztuki
#   print(result)
#
#   result = client.place_market_order("AAPL_US_EQ", Decimal("-1"))  # sprzedaż 1 sztuki
#   print(result)
# ---------------------------------------------------------------------------
