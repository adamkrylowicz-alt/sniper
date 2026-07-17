"""
app/services/finnhub_client.py
================================
Klient Finnhub API - ceny live, dane historyczne (sparkline 7d), logo firm.

MAPOWANIE T212 -> FINNHUB:
- US: "AAPL_US_EQ" -> "AAPL" (ucinamy suffix _US_EQ / _US_EQ_...)
- EU/inne: przez TICKER_MAP (ręcznie uzupełniany gdy automatyczne nie działa)
- Fallback: gdy Finnhub nie zna symbolu -> None, UI pokazuje awatar bez ceny/wykresu

CACHE:
- Ceny live: RAM, TTL = interwał odświeżania (przekazywany przez wywołującego)
- Dane historyczne (sparkline): RAM, TTL = 1h (dane dzienne, nie zmieniają się co minutę)
- Logo: dysk (static/logos/), pobierane raz, nigdy nie wygasają

LIMIT DARMOWEGO TIERU FINNHUB: 60 req/min
Dynamiczny interwał odświeżania w JS: ceil(liczba_kafelków * 1.2) sekund
Przy 1 kafelku: 1.2s -> ceil = 2s (bezpieczny margines)
Przy 9 kafelkach: 10.8s -> ceil = 11s
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

FINNHUB_BASE = "https://finnhub.io/api/v1"
REQUEST_TIMEOUT = 6

# Ręczna mapa wyjątków T212 -> symbol Finnhub.
# Uzupełniaj gdy automatyczne mapowanie nie działa (np. europejskie spółki
# z niestandardowymi tickerami T212). Format: "TICKER_T212": "SYMBOL_FINNHUB"
TICKER_MAP: dict[str, str] = {
    "RHMd_EQ": "RHM.XETRA",
    "1YD_EQ": "AVGO",           # Broadcom, T212 używa lokalnego symbolu Frankfurt
    "SPCX_US_EQ": "SPCX",      # SpaceX - IPO czerwiec 2026
}


def _fetch_yahoo_candles(symbol: str, days: int) -> list[float] | None:
    """
    Fallback dla get_sparkline() gdy Finnhub /stock/candle nie jest dostepny
    na danym planie (potwierdzone 17.07.2026 - darmowy klucz dostaje
    "You don't have access to this resource"). Yahoo Finance Chart API, bez
    klucza - ten sam symbol co t212_to_finnhub() (US ticker bez sufiksu).
    """
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": "1mo", "interval": "1d"},
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


def _fetch_yahoo_ohlc(symbol: str, days: int) -> list[dict] | None:
    """
    Jak _fetch_yahoo_candles(), ale zwraca pelne OHLC (open/high/low/close)
    zamiast samych zamkniec - do rysowania swiec (get_candles()), nie
    liniowego sparkline. Yahoo Finance Chart API, bez klucza.
    """
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": "1mo", "interval": "1d"},
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


def t212_to_finnhub(ticker: str) -> str | None:
    """
    Konwertuje ticker T212 na symbol Finnhub.
    Priorytet: TICKER_MAP (ręczne wyjątki) -> automatyczne dla US -> None dla reszty.
    """
    if ticker in TICKER_MAP:
        return TICKER_MAP[ticker]

    # Automatyczne: US spółki mają suffix _US_EQ
    if "_US_EQ" in ticker:
        return ticker.split("_US_EQ")[0]

    # Nieznane - zwracamy None, UI pokaże fallback
    return None


class FinnhubClient:
    """
    Klient Finnhub z wbudowanym cache'em w pamięci.
    Jedna instancja na aplikację (tworzona w extensions.py lub przy pierwszym użyciu).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._quote_cache: dict[str, tuple[dict, float]] = {}   # symbol -> (data, timestamp)
        self._candle_cache: dict[str, tuple[list, float]] = {}  # symbol -> (data, timestamp)
        self._ohlc_cache: dict[str, tuple[list, float]] = {}    # symbol -> (data, timestamp)
        self._profile_cache: dict[str, tuple[dict, float]] = {} # symbol -> (data, timestamp)

        self.QUOTE_TTL = 2       # sekundy - nadpisywane przez JS z dynamicznym interwałem
        self.CANDLE_TTL = 3600   # 1h - dane dzienne nie zmieniają się co chwilę
        self.PROFILE_TTL = 86400 # 24h - profil firmy zmienia się rzadko

    def _get(self, endpoint: str, params: dict) -> dict | None:
        params["token"] = self.api_key
        try:
            resp = requests.get(
                f"{FINNHUB_BASE}/{endpoint}",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.RequestException:
            return None

    def get_quote(self, t212_ticker: str) -> dict | None:
        """
        Zwraca aktualną cenę i zmianę % dla tickera T212.
        Struktura odpowiedzi Finnhub /quote:
        {
            "c": 150.12,   # current price
            "d": 1.23,     # change
            "dp": 0.83,    # percent change
            "h": 151.0,    # high today
            "l": 149.5,    # low today
            "o": 149.8,    # open
            "pc": 148.89,  # previous close
        }
        """
        symbol = t212_to_finnhub(t212_ticker)
        if not symbol:
            return None

        cached, ts = self._quote_cache.get(symbol, (None, 0))
        if cached and time.time() - ts < self.QUOTE_TTL:
            return cached

        data = self._get("quote", {"symbol": symbol})
        if data and data.get("c", 0) != 0:  # c=0 oznacza brak danych
            self._quote_cache[symbol] = (data, time.time())
            return data
        return None

    def get_sparkline(self, t212_ticker: str, days: int = 7) -> list[float] | None:
        """
        Zwraca listę cen zamknięcia z ostatnich `days` dni - do rysowania sparkline.
        Dane dzienne z /stock/candle, resolution=D.
        """
        symbol = t212_to_finnhub(t212_ticker)
        if not symbol:
            return None

        cached, ts = self._candle_cache.get(symbol, (None, 0))
        if cached and time.time() - ts < self.CANDLE_TTL:
            return cached

        now = int(time.time())
        from_ts = now - days * 86400

        data = self._get("stock/candle", {
            "symbol": symbol,
            "resolution": "D",
            "from": from_ts,
            "to": now,
        })

        if data and data.get("s") == "ok" and data.get("c"):
            closes = data["c"]
            self._candle_cache[symbol] = (closes, time.time())
            return closes

        # Finnhub /stock/candle odmowil (darmowy tier) - Yahoo Finance jako
        # zapasowe zrodlo, bez klucza.
        closes = _fetch_yahoo_candles(symbol, days)
        if closes:
            self._candle_cache[symbol] = (closes, time.time())
        return closes

    def get_candles(self, t212_ticker: str, days: int = 30) -> list[dict] | None:
        """
        Zwraca liste OHLC ({"o","h","l","c"}) do rysowania swiec w Focus Mode
        (patrz focus.js::drawCandles). Finnhub /stock/candle jako glowne
        zrodlo (zwraca tez open/high/low, nie tylko close jak get_sparkline
        wykorzystuje), Yahoo jako fallback - identyczny wzorzec co
        get_sparkline(). NIE dla wszystkich tickerow - jesli Yahoo tez nie ma
        pokrycia (mniej plynne/egzotyczne spolki), zwraca None i UI Focus
        Mode ma pokazac czytelny brak danych zamiast pustego wykresu.
        """
        symbol = t212_to_finnhub(t212_ticker)
        if not symbol:
            return None

        cached, ts = self._ohlc_cache.get(symbol, (None, 0))
        if cached and time.time() - ts < self.CANDLE_TTL:
            return cached

        now = int(time.time())
        from_ts = now - days * 86400

        data = self._get("stock/candle", {
            "symbol": symbol,
            "resolution": "D",
            "from": from_ts,
            "to": now,
        })

        if data and data.get("s") == "ok" and data.get("c"):
            candles = [
                {"o": round(float(o), 4), "h": round(float(h), 4), "l": round(float(l), 4), "c": round(float(c), 4)}
                for o, h, l, c in zip(data["o"], data["h"], data["l"], data["c"])
            ]
            self._ohlc_cache[symbol] = (candles, time.time())
            return candles

        candles = _fetch_yahoo_ohlc(symbol, days)
        if candles:
            self._ohlc_cache[symbol] = (candles, time.time())
        return candles

    def get_profile(self, t212_ticker: str) -> dict | None:
        """
        Zwraca profil firmy: nazwa, logo URL, branża, waluta, giełda.
        Używane do pobierania i cache'owania logo lokalnie.
        """
        symbol = t212_to_finnhub(t212_ticker)
        if not symbol:
            return None

        cached, ts = self._profile_cache.get(symbol, (None, 0))
        if cached and time.time() - ts < self.PROFILE_TTL:
            return cached

        data = self._get("stock/profile2", {"symbol": symbol})
        if data and data.get("name"):
            self._profile_cache[symbol] = (data, time.time())
            return data
        return None

    def get_quote_batch(self, t212_tickers: list[str]) -> dict[str, dict]:
        """
        Pobiera ceny dla listy tickerów - używane przy wielu aktywnych kafelkach.
        Zwraca {t212_ticker: quote_data} dla tych, które się udały.
        UWAGA: Finnhub nie ma batch endpoint - to N osobnych requestów,
        każdy liczy się do limitu 60 req/min. Używaj z rozwagą.
        """
        results = {}
        for ticker in t212_tickers:
            quote = self.get_quote(ticker)
            if quote:
                results[ticker] = quote
        return results

    def invalidate_quote_cache(self, t212_ticker: str) -> None:
        """Wymuś odświeżenie ceny przy następnym zapytaniu (np. po złożeniu zlecenia)."""
        symbol = t212_to_finnhub(t212_ticker)
        if symbol and symbol in self._quote_cache:
            del self._quote_cache[symbol]
