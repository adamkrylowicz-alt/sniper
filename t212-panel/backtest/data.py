"""
backtest/data.py
==================
Fetch + lokalny cache historii OHLC do backtestu Sygnału. Owija
app/services/price_feed.get_mini_chart_ohlc() - dla tickerów spoza US ta
funkcja idzie przez yahoo_resolver.py, który potrzebuje PRAWDZIWEJ bazy
(tabela YahooSymbolMap/Instrument) - goły Flask.config bez create_app() NIE
wystarcza (zweryfikowane na żywo: RuntimeError "current Flask app is not
registered with this SQLAlchemy instance" na SAFp_EQ).

`app_context()` woła więc PRAWDZIWY `create_app()`, ale BEZPIECZNIE -
patchuje `bot_credentials.load_autostart` na no-op PRZED wywołaniem (żeby
`_register_scheduler` w ogóle nie próbował załadować poświadczeń i odpalić
reconcile() trzech silników) i natychmiast zatrzymuje sam scheduler jako
drugie zabezpieczenie. To dokładnie ten sam wzorzec, który uratował sytuację
przy jednorazowych skryptach diagnostycznych 2026-07-28 - bez niego osobny
proces Pythona wołający create_app() zaczyna bić we WSPÓLNY, bardzo ciasny
rate limit T212 razem z prawdziwym `run.py`, mimo że backtester w ogóle nie
potrzebuje T212 (tylko Finnhub/Yahoo/Alpaca po dane historyczne).

get_mini_chart_ohlc() NIE zwraca dat (tylko o/h/l/c) - backtester referuje
świece po INDEKSIE dnia w oknie (patrz signal_runner.py), nie po prawdziwej
dacie kalendarzowej - uczciwe odzwierciedlenie tego co dane faktycznie
dają, zamiast zmyślać daty.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "data"


@contextmanager
def app_context():
    """Bezpieczny kontekst appki (baza + config), scheduler/autostart zablokowane - patrz docstring modułu."""
    from unittest.mock import patch

    from app.services import bot_credentials

    with patch.object(bot_credentials, "load_autostart", return_value=[]):
        from app import create_app
        app = create_app()

    from app.extensions import scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)

    with app.app_context():
        yield app


def _cache_path(ticker: str, days: int) -> Path:
    return CACHE_DIR / f"{ticker}_{days}d.json"


def fetch_candles(ticker: str, days: int, *, force_refresh: bool = False) -> list[dict]:
    """
    Zwraca listę świec (o/h/l/c, najstarsza->najnowsza) dla `ticker`, z
    lokalnego cache jeśli istnieje (chyba że force_refresh=True) - inaczej
    pobiera przez price_feed.get_mini_chart_ohlc() (WYMAGA aktywnego
    app_context() u wołającego - patrz run_backtest.py) i zapisuje do cache.

    Rzuca RuntimeError gdy żadne źródło (Alpaca/Finnhub/Yahoo) nie da
    danych - lepiej głośno przerwać niż cicho backtestować na pustce.
    """
    cache_file = _cache_path(ticker, days)
    if not force_refresh and cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)

    from flask import current_app

    from app.services import price_feed

    candles = price_feed.get_mini_chart_ohlc(
        current_app.config.get("FINNHUB_API_KEY"), ticker, days=days,
        alpaca_api_key=current_app.config.get("ALPACA_API_KEY"),
        alpaca_api_secret=current_app.config.get("ALPACA_API_SECRET"),
    )
    if not candles:
        raise RuntimeError(f"Brak danych historycznych dla {ticker} (Finnhub/Yahoo/Alpaca zawiodły).")

    CACHE_DIR.mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(candles, f)
    return candles
