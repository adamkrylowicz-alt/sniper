"""
app/services/price_feed.py
============================
Dane do mini-wykresów w Smart Virtual Pie - z Finnhub (PDF sekcja 3.1: "Źródło
ceny: Domyślnie Finnhub"), CAŁKOWICIE NIEZALEŻNE od T212 API (zero obciążenia
jego wąskiego rate limitu - patrz t212_client.py/instrument_cache.py).

Darmowy tier Finnhub ma limit 60 zapytań/min - dużo hojniejszy niż T212, ale
i tak NIE odpytujemy go przy każdym renderowaniu strony: prosty cache TTL w
pamięci procesu, ten sam kompromis co services/session_store.py i
routes/scalping.py::_guards - restart appki czyści cache, akceptowalne przy
jednym procesie dev-server na NAS-ie (patrz run.py - brak gunicorn/wielu workerów).

api_key jest przekazywany jawnie jako argument (nie current_app.config w środku
modułu) - ten sam styl co services/logo_cache.py (static_folder jako parametr),
żeby serwis dało się wywołać/testować bez kontekstu aplikacji Flask.

MAPOWANIE TICKERA T212 -> FINNHUB:
T212 używa własnych symboli (np. "AAPL_US_EQ"), Finnhub swoich ("AAPL").
_to_finnhub_symbol() ucina typowe sufiksy T212 - działa dla popularnych spółek
US, NIE jest uniwersalne (spółki spoza US mają inne konwencje symboli na
Finnhub) - jawnie udokumentowane ograniczenie Etapu 1.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import requests

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
REQUEST_TIMEOUT = 10  # sekund

CACHE_TTL_SECONDS = 30 * 60  # dane do MINI-wykresu, nie do decyzji tradingowych
_cache: dict[str, tuple[float, list[float] | None]] = {}  # {"AAPL_US_EQ:30": (monotonic_ts, closes)}

_KNOWN_SUFFIXES = ("_US_EQ", "_EQ")


def _to_finnhub_symbol(t212_ticker: str) -> str:
    symbol = t212_ticker
    for suffix in _KNOWN_SUFFIXES:
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _fetch_candles(api_key: str, ticker: str, days: int) -> list[float] | None:
    now = int(time.time())
    frm = now - days * 86400

    try:
        resp = requests.get(
            f"{FINNHUB_BASE_URL}/stock/candle",
            params={
                "symbol": _to_finnhub_symbol(ticker),
                "resolution": "D",
                "from": frm,
                "to": now,
                "token": api_key,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Finnhub: błąd sieci dla %s: %s", ticker, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Finnhub: HTTP %s dla %s", resp.status_code, ticker)
        return None

    try:
        payload = resp.json()
    except ValueError:
        return None

    # "no_data" - Finnhub nie ma pokrycia dla tego symbolu/giełdy (darmowy tier
    # bywa ograniczony do wybranych rynków) - traktujemy jak brak danych, nie błąd.
    if payload.get("s") != "ok":
        return None

    closes = payload.get("c") or []
    return [round(float(v), 4) for v in closes]


def _fetch_yahoo_candles(ticker: str, days: int) -> list[float] | None:
    """
    Fallback dla get_mini_chart() gdy Finnhub /stock/candle nie jest
    dostepny na danym planie (potwierdzone 17.07.2026 - darmowy klucz
    dostaje "You don't have access to this resource"). Yahoo Finance Chart
    API, bez klucza - ten sam mapping tickera co _fetch_yahoo_quote nizej.
    """
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{_to_finnhub_symbol(ticker)}",
            params={"range": "3mo", "interval": "1d"},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return None
        result = resp.json()["chart"]["result"][0]
        raw_closes = result["indicators"]["quote"][0]["close"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None

    closes = [round(float(c), 4) for c in raw_closes if c is not None]
    if len(closes) < 2:
        return None
    return closes[-days:]


def get_mini_chart(api_key: str | None, ticker: str, days: int = 30) -> list[float] | None:
    """
    Zwraca listę cen zamknięcia (najstarsza -> najnowsza) dla ostatnich `days`
    dni, albo None gdy brak danych/klucza/połączenia - wywołujący (routes/pie.py)
    pokazuje wtedy "brak danych" zamiast wywalać cały widok koszyka.
    """
    if not api_key:
        logger.warning("FINNHUB_API_KEY nie ustawiony w .env - mini-wykresy wyłączone.")
        return None

    key = f"{ticker}:{days}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    closes = _fetch_candles(api_key, ticker, days)
    if not closes:
        # Finnhub /stock/candle odmowil (darmowy tier) - Yahoo Finance jako
        # zapasowe zrodlo, bez klucza.
        closes = _fetch_yahoo_candles(ticker, days)
    _cache[key] = (now, closes)
    return closes


def get_mini_charts(
    api_key: str | None, tickers: list[str], days: int = 30
) -> dict[str, list[float] | None]:
    """Wygodny batch - jedno wywołanie JS->Flask na cały widok Pie zamiast N osobnych requestów."""
    return {t: get_mini_chart(api_key, t, days) for t in tickers}


# -- Cena "na żywo" do decyzji bota (Etap 2, services/bot_engine.py) --------
# CELOWO osobna od get_mini_chart wyżej: zero cache'u TTL (bot ma dostać
# możliwie świeżą cenę w momencie wejścia, nie dane sprzed 30 minut) i dwa
# źródła w kolejności z PRD (sekcja 3.1) - Finnhub jako główne, Yahoo Finance
# jako fallback "z opóźnieniem" gdy Finnhub zawiedzie (brak klucza, błąd
# sieci, brak pokrycia symbolu).

def _fetch_finnhub_quote(api_key: str, ticker: str) -> Decimal | None:
    try:
        resp = requests.get(
            f"{FINNHUB_BASE_URL}/quote",
            params={"symbol": _to_finnhub_symbol(ticker), "token": api_key},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Finnhub quote: błąd sieci dla %s: %s", ticker, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Finnhub quote: HTTP %s dla %s", resp.status_code, ticker)
        return None

    try:
        price = resp.json().get("c")
    except ValueError:
        return None

    # "c" == 0 (albo brak pola) to typowy sygnał Finnhub "brak notowania dla
    # tego symbolu" - traktujemy jak brak ceny, nie błąd.
    if not price:
        return None
    return Decimal(str(price))


def _fetch_yahoo_quote(ticker: str) -> Decimal | None:
    """
    Fallback gdy Finnhub zawiedzie. Yahoo Finance Chart API (nieoficjalne,
    ale szeroko używane, bez klucza) - ten sam mapping tickera co Finnhub
    (_to_finnhub_symbol) - obie usługi adresują "gołe" symbole giełdowe
    (AAPL, MSFT), nie kody T212.
    """
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{_to_finnhub_symbol(ticker)}",
            params={"range": "1d", "interval": "1m"},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    except requests.RequestException as exc:
        logger.warning("Yahoo quote: błąd sieci dla %s: %s", ticker, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Yahoo quote: HTTP %s dla %s", resp.status_code, ticker)
        return None

    try:
        price = resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None

    if not price:
        return None
    return Decimal(str(price))


def get_live_price(api_key: str | None, ticker: str) -> Decimal | None:
    """
    Cena "na żywo" do decyzji bota - Finnhub /quote jako główne źródło (PRD:
    "Domyślnie Finnhub"), Yahoo Finance jako fallback. Zwraca None jeśli OBA
    źródła zawiodą - wywołujący (bot_engine.py) ma wtedy pominąć wejście,
    nie zgadywać ceny.
    """
    if api_key:
        price = _fetch_finnhub_quote(api_key, ticker)
        if price is not None:
            return price

    return _fetch_yahoo_quote(ticker)
