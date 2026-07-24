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

MAPOWANIE TICKERA T212 -> FINNHUB/YAHOO:
T212 używa własnych symboli (np. "AAPL_US_EQ"), Finnhub/Yahoo swoich ("AAPL").
_to_finnhub_symbol() ucina typowe sufiksy T212 - działa dla popularnych spółek
US, ale NIE jest uniwersalne (spółki spoza US mają inne konwencje symboli) -
używane WYŁĄCZNIE do zapytań Finnhub (który i tak w praktyce nie ma pokrycia
poza US, patrz finnhub_client.py::t212_to_finnhub). Fallback Yahoo (funkcje
_fetch_yahoo_*) używa zamiast tego t212_to_finnhub() z finnhub_client.py, który
dla tickerów spoza US rozwiązuje właściwy symbol Yahoo przez yahoo_resolver.py
(np. "ASMLa_EQ" -> "ASML.AS") - bez tego bot nigdy nie dostawał ceny dla
żadnego nie-amerykańskiego tickera (znaleziony realny bug produkcyjny,
2026-07-20 - patrz historia w bot_engine.py).

ALPACA (dodane 2026-07-22, na życzenie Adama - "na rynki usa mam nowego
dostawce danych alpaca"): dla tickerów `*_US_EQ` GŁÓWNE źródło ceny "na żywo"
(get_live_price) to teraz Alpaca Market Data API (REST, klucz+secret w
ALPACA_API_KEY/ALPACA_API_SECRET), Finnhub->Yahoo zostaje jako fallback gdy
Alpaca zawiedzie (brak klucza, błąd sieci, symbol spoza pokrycia). Dla
tickerów spoza USD (EUR itd.) Alpaca w ogóle nie jest próbowane - zero zmiany
zachowania, wciąż Finnhub->Yahoo jak dotychczas. Endpoint Market Data API jest
WSPÓLNY dla kluczy paper i live trading (inaczej niż endpoint do składania
zleceń) - klucz zaczynający się na "PK" (paper) działa tu identycznie jak
klucz live.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import requests

from .finnhub_client import t212_to_finnhub

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets/v2"
REQUEST_TIMEOUT = 10  # sekund

CACHE_TTL_SECONDS = 30 * 60  # dane do MINI-wykresu, nie do decyzji tradingowych
_cache_ohlc: dict[str, tuple[float, list[dict] | None]] = {}  # {"AAPL_US_EQ:30": (monotonic_ts, candles)}

_KNOWN_SUFFIXES = ("_US_EQ", "_EQ")
_US_SUFFIX = "_US_EQ"


def _to_finnhub_symbol(t212_ticker: str) -> str:
    """
    Dla tickerów `*_US_EQ` sprawdza NAJPIERW finnhub_client.py::TICKER_MAP
    (wyjątki gdzie T212 trzyma stary/martwy symbol - np. FB_US_EQ (Meta
    Platforms sprzed rebrandingu z FB na META) czy IPOE_US_EQ (SoFi
    Technologies, stary kod SPAC-a)) - bez tego auto-obcięcie sufiksu
    dawałoby BŁĘDNY, ale technicznie istniejący symbol (np. "FB" to inny,
    martwy byt na Alpaca - zwraca prawdziwą, ale sprzed dni cenę zamiast
    błędu "brak danych", co jest dużo groźniejsze - realny bug znaleziony
    2026-07-22, bot wszedł w pozycję po cenie $44.61 zamiast ~$635).
    Ograniczone do tickerów US - reszta TICKER_MAP to symbole w przestrzeni
    Yahoo (np. "RHM.XETRA"), których Finnhub/Alpaca by nie zrozumiały.
    """
    if t212_ticker.endswith(_US_SUFFIX):
        from .finnhub_client import TICKER_MAP
        if t212_ticker in TICKER_MAP:
            return TICKER_MAP[t212_ticker]

    symbol = t212_ticker
    for suffix in _KNOWN_SUFFIXES:
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _fetch_candles_ohlc(api_key: str, ticker: str, days: int) -> list[dict] | None:
    """Pobiera pełne OHLC (open/high/low/close) z Finnhub /stock/candle - do świec."""
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
        logger.warning("Finnhub OHLC: błąd sieci dla %s: %s", ticker, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Finnhub OHLC: HTTP %s dla %s", resp.status_code, ticker)
        return None

    try:
        payload = resp.json()
    except ValueError:
        return None

    if payload.get("s") != "ok":
        return None

    opens, highs, lows, closes = payload.get("o"), payload.get("h"), payload.get("l"), payload.get("c")
    if not (opens and highs and lows and closes):
        return None
    return [
        {"o": round(float(o), 4), "h": round(float(h), 4), "l": round(float(l), 4), "c": round(float(c), 4)}
        for o, h, l, c in zip(opens, highs, lows, closes)
    ]


def _yahoo_range_for_days(days: int) -> str:
    """
    Mapuje żądaną liczbę dni na parametr `range` Yahoo Chart API - dawniej
    było to na sztywno "3mo" niezależnie od `days` (wystarczające dla
    dotychczasowych wywołań: ATR/filtr trendu w bot_engine.py, oba <=30 dni).
    Strategia sygnałowa RSI/MA/ATR (services/signal_engine.py, 2026-07-24)
    potrzebuje SMA(200) - grubo ponad 3 miesiące dziennych świec - stąd
    dynamiczny dobór zamiast stałej. Progi z marginesem (SMA200 potrzebuje
    ~200 sesji giełdowych ≈ 280 dni kalendarzowych, stąd próg 200 dni -> "1y",
    nie "6mo").
    """
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    if days <= 300:
        return "1y"
    if days <= 600:
        return "2y"
    return "5y"


def _fetch_yahoo_candles_ohlc(ticker: str, days: int) -> list[dict] | None:
    """
    Fallback OHLC (Yahoo Finance Chart API, bez klucza) gdy Finnhub odmówi.
    Symbol przez t212_to_finnhub() (finnhub_client.py), NIE _to_finnhub_symbol()
    - dla tickerów spoza US ta pierwsza rozwiązuje właściwy symbol Yahoo przez
    yahoo_resolver.py (np. "ASMLa_EQ" -> "ASML.AS"), naiwne ucinanie sufiksu
    dałoby nieistniejący symbol ("ASMLa") i zawsze 404.
    """
    yahoo_symbol = t212_to_finnhub(ticker)
    if yahoo_symbol is None:
        return None
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
            params={"range": _yahoo_range_for_days(days), "interval": "1d"},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return None
        quote = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
        opens, highs, lows, closes = quote["open"], quote["high"], quote["low"], quote["close"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None

    candles = [
        {"o": round(float(o), 4), "h": round(float(h), 4), "l": round(float(l), 4), "c": round(float(c), 4)}
        for o, h, l, c in zip(opens, highs, lows, closes)
        if None not in (o, h, l, c)
    ]
    if len(candles) < 2:
        return None
    return candles[-days:]


def get_mini_chart_ohlc(api_key: str | None, ticker: str, days: int = 30) -> list[dict] | None:
    """
    Zwraca listę OHLC (open/high/low/close, najstarsza -> najnowsza) dla
    ostatnich `days` dni, albo None gdy brak danych/klucza/połączenia - do
    rysowania świec w Smart Virtual Pie (routes/pie.py::charts).
    """
    if not api_key:
        logger.warning("FINNHUB_API_KEY nie ustawiony w .env - mini-wykresy wyłączone.")
        return None

    key = f"{ticker}:{days}"
    now = time.monotonic()
    cached = _cache_ohlc.get(key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    candles = _fetch_candles_ohlc(api_key, ticker, days)
    if not candles:
        candles = _fetch_yahoo_candles_ohlc(ticker, days)
    _cache_ohlc[key] = (now, candles)
    return candles


def get_mini_charts_ohlc(
    api_key: str | None, tickers: list[str], days: int = 30
) -> dict[str, list[dict] | None]:
    """Wygodny batch - jedno wywołanie JS->Flask na cały widok Pie zamiast N osobnych requestów."""
    return {t: get_mini_chart_ohlc(api_key, t, days) for t in tickers}


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
    ale szeroko używane, bez klucza). Symbol przez t212_to_finnhub()
    (finnhub_client.py) - NIE lokalne _to_finnhub_symbol() - dla tickerów US
    to i tak to samo (ucięty sufiks), ale dla tickerów spoza US
    t212_to_finnhub() rozwiązuje właściwy symbol Yahoo przez yahoo_resolver.py
    (np. "ASMLa_EQ" -> "ASML.AS"); bez tego bot nigdy nie dostawał ceny dla
    żadnego nie-amerykańskiego tickera (potwierdzone realnym błędem
    "brak ceny" na koncie produkcyjnym, 2026-07-20).
    """
    yahoo_symbol = t212_to_finnhub(ticker)
    if yahoo_symbol is None:
        return None
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
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


def _fetch_alpaca_quote(api_key: str, api_secret: str, ticker: str) -> Decimal | None:
    """
    Ostatnia zawarta transakcja (latest trade) z Alpaca Market Data API -
    tylko dla tickerów `*_US_EQ` (patrz get_live_price), symbol identyczny
    jak dla Finnhub (_to_finnhub_symbol ucina sufiks T212).
    """
    try:
        resp = requests.get(
            f"{ALPACA_DATA_BASE_URL}/stocks/{_to_finnhub_symbol(ticker)}/trades/latest",
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Alpaca latest trade: błąd sieci dla %s: %s", ticker, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Alpaca latest trade: HTTP %s dla %s", resp.status_code, ticker)
        return None

    try:
        price = resp.json().get("trade", {}).get("p")
    except ValueError:
        return None

    if not price:
        return None
    return Decimal(str(price))


def get_live_price(
    api_key: str | None,
    ticker: str,
    alpaca_api_key: str | None = None,
    alpaca_api_secret: str | None = None,
) -> Decimal | None:
    """
    Cena "na żywo" do decyzji bota. Dla tickerów `*_US_EQ`: Alpaca Market Data
    API jako GŁÓWNE źródło (dodane 2026-07-22, na życzenie Adama - nowy
    dostawca danych dla rynków USA), Finnhub -> Yahoo jako fallback gdyby
    Alpaca zawiodło. Dla wszystkich innych tickerów (EUR itd.): bez zmian,
    Finnhub jako główne (PRD: "Domyślnie Finnhub"), Yahoo Finance jako
    fallback. Zwraca None jeśli WSZYSTKIE źródła zawiodą - wywołujący
    (bot_engine.py) ma wtedy pominąć wejście, nie zgadywać ceny.
    """
    if ticker.endswith(_US_SUFFIX) and alpaca_api_key and alpaca_api_secret:
        price = _fetch_alpaca_quote(alpaca_api_key, alpaca_api_secret, ticker)
        if price is not None:
            return price

    if api_key:
        price = _fetch_finnhub_quote(api_key, ticker)
        if price is not None:
            return price

    return _fetch_yahoo_quote(ticker)
