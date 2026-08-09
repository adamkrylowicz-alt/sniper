"""
backtest/regime.py
=====================
Filtr rezimu rynkowego (Adam, 2026-08-09 - pomysl #1 z listy usprawnien,
artykul "The Market Regime Filter" na financial-hacker.com, oryginalnie
Di Prima/Baruffa). 3 niezalezne, market-wide sygnaly (NIE per-ticker jak
`signal_engine.py::_process_entries`, ktore filtruje tylko cena>SMA200
danego tickera):

  1. Trend:      SPY > SMA200(SPY)
  2. Zmiennosc:  VIX < VIX3M (contango = spokojny rynek, backwardation = stres)
  3. Kredyt:     z-score(HYG/IEF, lookback 100d) > -2 (spready sie nie rozjezdzaja)

Ile z 3 sygnalow sie zgadza -> mnoznik ekspozycji: 3/3 zielony=1.0, 2/3
pomaranczowy=0.5, <2 czerwony=0.0. Uzywane jako `entry_multiplier` w
`run_signal_backtest()` (backtest/signal_runner.py) - mnozy `entry_amount`
per dzien, wiec 0.0 = brak nowych wejsc tego dnia (zero exposure).

WAZNE OGRANICZENIE: backtester referuje swiece po INDEKSIE dnia w oknie
tickera, NIE po prawdziwej dacie (patrz data.py). Ten modul zaklada ze
SPY/VIX/VIX3M/HYG/IEF pobrane z tym samym `days` w tym samym momencie maja
TEN SAM kalendarz sesji co dowolny ticker `*_US_EQ` pobrany rowniez z tym
samym `days` (obie strony - NYSE) - PRAWDZIWE tylko dla tickerow US, NIE
dla europejskich (inne swieta/kalendarz gieldowy). Dlatego
`run_regime_backtest.py` domyslnie testuje TYLKO na podzbiorze `_US_EQ`.
"""

from __future__ import annotations

from decimal import Decimal

import requests

from app.services.signal_engine import _compute_sma

REQUEST_TIMEOUT = 10
_YAHOO_SYMBOLS = {"spy": "SPY", "vix": "%5EVIX", "vix3m": "%5EVIX3M", "hyg": "HYG", "ief": "IEF"}


def _fetch_yahoo_closes(symbol: str, days: int) -> list[Decimal] | None:
    """Surowy fetch Yahoo Chart API po DOKLADNYM symbolu (SPY/^VIX/HYG/...) -
    w odroznieniu od price_feed._fetch_yahoo_candles_ohlc, ktora oczekuje
    tickera T212 i idzie przez t212_to_finnhub() (rozwiazanie na symbol
    Yahoo) - VIX/VIX3M to indeksy bez odpowiednika T212, wiec ta sciezka
    tu nie pasuje."""
    yahoo_range = "5y" if days > 600 else ("2y" if days > 250 else "1y")
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": yahoo_range, "interval": "1d"},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return None
        quote = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
        closes = [Decimal(str(c)) for c in quote["close"] if c is not None]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None
    return closes[-days:] if closes else None


def fetch_regime_multipliers(days: int) -> list[Decimal] | None:
    """
    Zwraca liste mnoznikow ekspozycji (Decimal 0.0/0.5/1.0), najstarszy
    -> najnowszy, dlugosci min(dlugosci wszystkich 5 serii) - albo None gdy
    ktorekolwiek zrodlo zawiedzie (lepiej glosno przerwac niz cicho
    backtestowac bez filtra).
    """
    series = {}
    for key, symbol in _YAHOO_SYMBOLS.items():
        closes = _fetch_yahoo_closes(symbol, days + 210)  # +bufor pod SMA200/z-score(100)
        if not closes:
            return None
        series[key] = closes

    n = min(len(s) for s in series.values())
    spy, vix, vix3m, hyg, ief = (series[k][-n:] for k in ("spy", "vix", "vix3m", "hyg", "ief"))

    ratio = [h / i for h, i in zip(hyg, ief)]
    multipliers: list[Decimal] = []
    for day in range(n):
        signals = 0

        sma200 = _compute_sma(spy[: day + 1], 200)
        if sma200 is not None and spy[day] > sma200:
            signals += 1

        if vix[day] < vix3m[day]:
            signals += 1

        lookback = ratio[max(0, day - 99): day + 1]
        if len(lookback) >= 20:  # zbyt krotkie okno na poczatku serii - pomijamy sygnal (nie liczymy jako zgode)
            mean = sum(lookback) / len(lookback)
            variance = sum((r - mean) ** 2 for r in lookback) / len(lookback)
            stdev = variance.sqrt() if variance > 0 else Decimal("0")
            if stdev > 0 and (ratio[day] - mean) / stdev > Decimal("-2"):
                signals += 1

        multipliers.append(Decimal("1.0") if signals == 3 else Decimal("0.5") if signals == 2 else Decimal("0.0"))

    return multipliers[-days:] if len(multipliers) > days else multipliers
