"""
app/services/logo_cache.py
============================
Logotypy spółek - pobierane RAZ z zewnętrznego serwisu (Logo.dev, ticker-based
stock logo API), zapisywane lokalnie w app/static/logos/, i od tej pory ZAWSZE
serwowane z własnego dysku - zero kolejnych zapytań na zewnątrz dla tego
samego tickera.

DLACZEGO LOGO.DEV, A NIE CLEARBIT (poprzednie źródło w tym pliku):
Clearbit Logo API (logo.clearbit.com) zostało wygaszone przez HubSpot - od
grudnia 2025 nie zwraca już nic sensownego. Logo.dev to oficjalnie polecany
następca, z dedykowanym endpointem PO TICKERZE (nie po domenie firmy) -
`img.logo.dev/ticker/{symbol}` - więc znika też cała poprzednia heurystyka
zgadywania domeny z nazwy firmy (TICKER_DOMAINS/_guess_domain), zastąpiona
prostym mapowaniem "ticker T212 -> symbol giełdowy" (ten sam pomysł co
services/price_feed.py::_to_finnhub_symbol).

OGRANICZENIE (świadome, nie do naprawienia bez ręcznej pracy):
Logo.dev pokrywa akcje/ETF-y na głównych giełdach po ICH realnych symbolach.
Europejskie ETF-y UCITS (T212 nadaje im WEWNĘTRZNE kody, np. "XNASl_EQ" dla
Xtrackers NASDAQ 100) nie mają odpowiednika w tym mapowaniu - dla nich zawsze
zostanie kolorowy awatar z inicjałem (patrz watchlist.html/settings.py). To
ograniczenie danych, nie błąd w tym module.

Żeby dodać ręczny wyjątek dla tickera, który T212 nazywa inaczej niż realny
symbol giełdowy: dopisz go do TICKER_SYMBOL_OVERRIDES poniżej.
"""

from __future__ import annotations

import os
import threading
import time

import requests

TICKER_SYMBOL_OVERRIDES: dict[str, str] = {
    # Meta Platforms - T212 wciąż używa starego symbolu sprzed rebrandingu
    # z Facebooka, ale realny/aktualny symbol giełdowy (i to czego szuka
    # Logo.dev) to META.
    "FB_US_EQ": "META",
}

_KNOWN_SUFFIXES = ("_US_EQ", "_EQ")


def _to_market_symbol(ticker: str) -> str | None:
    """
    Mapuje ticker T212 na realny symbol giełdowy, którego oczekuje Logo.dev.
    Działa pewnie dla spółek US (najczęstszy przypadek); dla tickerów spoza
    tego wzorca (europejskie ETF-y UCITS z wewnętrznymi kodami T212) zwraca
    None - fetch_and_cache_logo wtedy nie robi żadnego requestu, appka od
    razu pokazuje fallback (kolorowy awatar).
    """
    if ticker in TICKER_SYMBOL_OVERRIDES:
        return TICKER_SYMBOL_OVERRIDES[ticker]

    for suffix in _KNOWN_SUFFIXES:
        if ticker.endswith(suffix):
            symbol = ticker[: -len(suffix)]
            # Same litery/cyfry - jeśli zostało coś innego (np. wewnętrzny
            # kod UCITS w stylu "XNASl"), to i tak prawdopodobnie nie jest
            # prawdziwym symbolem giełdowym, ale niech Logo.dev sam
            # zdecyduje (fallback=404 i tak bezpiecznie nic nie zwróci).
            return symbol if symbol else None

    return None


LOGO_SERVICE_URL = "https://img.logo.dev/ticker/{symbol}"
LOGO_DIR_NAME = "logos"  # podfolder w app/static/
REQUEST_TIMEOUT = 8


def _logo_dir(static_folder: str) -> str:
    path = os.path.join(static_folder, LOGO_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def get_cached_logo_filename(static_folder: str, ticker: str) -> str | None:
    """
    Zwraca nazwę pliku (np. "AAPL_US_EQ.png") jeśli logo już jest w cache'u
    na dysku, inaczej None. NIE robi żadnego requestu sieciowego - to czysto
    odczyt z dysku, bezpieczne do wołania przy każdym renderowaniu strony.
    """
    logo_dir = _logo_dir(static_folder)
    for ext in ("png", "jpg", "jpeg"):
        candidate = f"{ticker}.{ext}"
        if os.path.exists(os.path.join(logo_dir, candidate)):
            return candidate
    return None


def fetch_and_cache_logo(
    static_folder: str, ticker: str, api_key: str | None, timeout: int = REQUEST_TIMEOUT
) -> str | None:
    """
    Pobiera logo z Logo.dev TYLKO jeśli:
    1. nie ma go jeszcze w lokalnym cache'u (patrz get_cached_logo_filename), ORAZ
    2. znamy klucz API (api_key), ORAZ
    3. ticker mapuje się na jakiś symbol giełdowy (patrz _to_market_symbol)

    ?fallback=404 każe Logo.dev zwrócić prawdziwe 404 dla nieznanych symboli,
    zamiast wygenerowanego domyślnego monogramu - inaczej appka zapisałaby
    sobie na stałe cudzy, generyczny obrazek zamiast pokazać WŁASNY, spójny
    z resztą UI kolorowy awatar (patrz avatar_hue() w utils.py).

    Zwraca nazwę zapisanego pliku, albo None jeśli się nie udało (brak klucza,
    nieznany symbol, błąd sieci, Logo.dev nie ma logo dla tego symbolu itd.) -
    appka ma wtedy pokazać fallback (kolorowy awatar), NIE błąd.
    """
    existing = get_cached_logo_filename(static_folder, ticker)
    if existing:
        return existing

    if not api_key:
        return None

    symbol = _to_market_symbol(ticker)
    if not symbol:
        return None

    try:
        resp = requests.get(
            LOGO_SERVICE_URL.format(symbol=symbol),
            params={"token": api_key, "fallback": "404"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None

    if resp.status_code != 200 or not resp.content:
        return None

    content_type = resp.headers.get("Content-Type", "")
    ext = "png"
    if "jpeg" in content_type or "jpg" in content_type:
        ext = "jpg"

    filename = f"{ticker}.{ext}"
    filepath = os.path.join(_logo_dir(static_folder), filename)
    with open(filepath, "wb") as f:
        f.write(resp.content)

    return filename


def fetch_missing_logos(static_folder: str, api_key: str | None, tickers: list[str]) -> dict[str, bool]:
    """
    Woła fetch_and_cache_logo dla listy tickerów - używane przez przycisk
    "Pobierz wszystkie logotypy" (bez limitu, może potrwać dłużej przy wielu
    brakujących naraz - to świadome, bo to JAWNA akcja usera).
    Zwraca {ticker: czy_sie_udalo}.
    """
    results = {}
    for ticker in tickers:
        filename = fetch_and_cache_logo(static_folder, ticker, api_key)
        results[ticker] = filename is not None
    return results


def ensure_logos_auto(
    static_folder: str,
    api_key: str | None,
    tickers: list[str],
    max_fetches: int = 5,
    timeout: int = 4,
) -> None:
    """
    Automatyczne, OGRANICZONE pobieranie brakujących logo - wołane przy
    KAŻDYM wejściu na stronę Watchlist (bez klikania czegokolwiek).

    Ograniczenie max_fetches (domyślnie 5) + krótszy timeout (4s) to celowy
    kompromis: strona ma się ładować szybko nawet gdy masz sporo nowych,
    jeszcze niepobranych ulubionych - pobierze pierwsze 5 brakujących teraz,
    resztę przy kolejnych wizytach (albo od razu przyciskiem "Pobierz
    wszystkie" jeśli nie chce Ci się czekać na kilka odświeżeń).
    """
    fetched_count = 0
    for ticker in tickers:
        if fetched_count >= max_fetches:
            break
        if get_cached_logo_filename(static_folder, ticker):
            continue  # już jest w cache - nie liczy się do limitu
        fetch_and_cache_logo(static_folder, ticker, api_key, timeout=timeout)
        fetched_count += 1


# -- Bulk-fetch dla CAŁEJ bazy instrumentów (nie tylko ulubionych) ----------
#
# ensure_logos_auto/fetch_missing_logos wyżej operują tylko na ulubionych -
# to świadomie zostaje (mało kosztowne, dzieje się przy zwykłym przeglądaniu).
# Ale skoro zakładki kategorii (routes/settings.py) pozwalają teraz przeglądać
# WSZYSTKIE ~15800 zsynchronizowanych instrumentów, użytkownik może chcieć
# dociągnąć logo dla całej bazy naraz - to zbyt długa operacja (dziesiątki
# minut) żeby robić ją w jednym request/response, więc leci w tle w osobnym
# wątku. Stan trzymany w pamięci procesu (ten sam kompromis co
# routes/scalping.py::_guards, services/session_store.py) - restart appki
# czyści postęp, ale sam plikowy cache logo na dysku ZOSTAJE, więc kolejne
# uruchomienie i tak tylko dociąga brakujące.

_bulk_state: dict[str, int | bool] = {"running": False, "done": 0, "total": 0}
_bulk_lock = threading.Lock()

BULK_FETCH_DELAY_SECONDS = 0.15  # odstęp między zapytaniami do Logo.dev - nie chcemy ich zalewać


def bulk_fetch_status() -> dict:
    """Bezstanowy odczyt - do pollowania przez JS co kilka sekund."""
    return dict(_bulk_state)


def start_bulk_fetch(app, static_folder: str, api_key: str | None) -> bool:
    """
    Odpala pobieranie logo dla WSZYSTKICH instrumentów w lokalnym cache (nie
    tylko ulubionych) w osobnym wątku w tle - funkcja wraca NATYCHMIAST,
    appka nie czeka na koniec (dziesiątki minut przy ~15800 instrumentach).

    Zwraca False (i niczego nie odpala) jeśli poprzedni bulk-fetch jeszcze
    trwa - celowo brak kolejki, jedno zadanie naraz wystarczy.

    `app` musi być prawdziwym obiektem Flask (current_app._get_current_object()
    w warstwie routes), NIE proxy current_app - wątek w tle potrzebuje
    własnego app_context(), a proxy current_app działa tylko wewnątrz
    aktywnego requestu/kontekstu wywołującego.
    """
    with _bulk_lock:
        if _bulk_state["running"]:
            return False
        _bulk_state["running"] = True
        _bulk_state["done"] = 0
        _bulk_state["total"] = 0

    def worker():
        with app.app_context():
            from ..models import Instrument  # lokalny import - unikamy zależności na poziomie modułu

            tickers = [row.ticker for row in Instrument.query.with_entities(Instrument.ticker)]
            _bulk_state["total"] = len(tickers)

            for ticker in tickers:
                fetch_and_cache_logo(static_folder, ticker, api_key)
                _bulk_state["done"] = int(_bulk_state["done"]) + 1
                time.sleep(BULK_FETCH_DELAY_SECONDS)

        _bulk_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True
