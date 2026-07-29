"""
backtest/ibkr_data.py
========================
Fetch + lokalny cache historii świec ŚRÓDDZIENNYCH (1-min i inne) z IBKR -
patrz plan "IBKR jako źródło danych historycznych" (2026-07-28). Domyka lukę,
której `backtest/data.py` (Alpaca/Finnhub/Yahoo, WYŁĄCZNIE świece dzienne) nie
domyka: `get_intraday_chart` w `app/services/price_feed.py` daje dane
śróddzienne TYLKO dla tickerów `_US_EQ` i z absurdalnie krótkim lookbackiem
(1 dzień dla 1-min), a tickery europejskie (Xetra/Euronext) nie mają ŻADNEGO
źródła intraday w ogóle.

WYŁĄCZNIE MOST DO DANYCH, NIE DO TRADINGU - żadna funkcja tego modułu nie
woła `placeOrder`/`reqAccountUpdates` ani niczego transakcyjnego. Kontener
IB Gateway (`docker-compose.yml::ib-gateway`) ma `READ_ONLY_API=yes` jako
dodatkowe zabezpieczenie na poziomie samego API - nawet błąd w tym kodzie nie
mógłby złożyć zlecenia.

Używa `ib_insync` (NIE aktywnie utrzymywanego następcy `ib_async`) - `ib_async`
wymaga Python>=3.10, ten NAS ma tylko 3.8 (brak nowszego interpretera w
Package Center, zweryfikowane). `ib_insync` jest archiwalny, ale wspiera
Python>=3.6, a samo API do danych historycznych (`reqHistoricalData`) jest
stabilne od lat - akceptowalne ryzyko dla wąskiego, nietransakcyjnego użycia
tutaj. Gdy NAS kiedyś dostanie Python 3.10+, warto przejść na `ib_async`.

WYMAGA działającego kontenera `ib-gateway` (patrz docker-compose.yml w
korzeniu /volume1/docker/) nasłuchującego na 127.0.0.1:4002 (PAPER API) -
pierwsze logowanie kontenera wymaga RĘCZNEGO zatwierdzenia w apce IBKR
Mobile (IBC nie wspiera automatycznego 2FA przez zapisany sekret TOTP -
zweryfikowane wprost w źródłowym config.ini IBC, tylko push-based IBKR
Mobile), potem sesja sama się utrzymuje/odnawia.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "data"
IB_GATEWAY_HOST = "127.0.0.1"
IB_GATEWAY_PORT = 4002  # PAPER API (host) - patrz docker-compose.yml::ib-gateway

# Pacing limits IBKR (patrz TWS API docs "Historical Data Limitations") - max
# 6 identycznych zapytań/2s, 60 zapytań/10min. Ten prosty sleep między
# kolejnymi tickerami w jednym przebiegu wystarcza z dużym marginesem dla
# skali, jakiej tu używamy (kilka-kilkanaście tickerów na sesję backtestu),
# ten sam duch co throttle w app/services/t212_client.py, ale to OSOBNY,
# niepowiązany rate limit IBKR.
MIN_REQUEST_INTERVAL_SECONDS = 2.0


def _cache_path(symbol: str, bar_size: str, duration_days: int) -> Path:
    safe_bar_size = bar_size.replace(" ", "")
    return CACHE_DIR / f"{symbol}_{safe_bar_size}_{duration_days}d_ibkr.json"


def fetch_ibkr_candles(
    symbol: str,
    exchange: str,
    currency: str,
    *,
    bar_size: str = "1 min",
    duration_days: int = 30,
    force_refresh: bool = False,
) -> list[dict]:
    """
    Zwraca listę świec (o/h/l/c, najstarsza->najnowsza) dla kontraktu IBKR
    (symbol/exchange/currency - NIE ticker T212, mapowanie NIE jest 1:1, patrz
    docstring modułu) z lokalnego cache jeśli istnieje (chyba że
    force_refresh=True), inaczej pobiera przez `reqHistoricalData` i zapisuje.

    `exchange` przykłady: "SMART" (US, routing IBKR), "IBIS" (Xetra/Niemcy),
    "SBF" (Euronext Paris) - musisz podać właściwą dla danego instrumentu,
    IBKR nie zgaduje.

    Rzuca RuntimeError gdy się nie połączy z IB Gateway albo zapytanie nie
    zwróci danych - lepiej głośno przerwać niż cicho backtestować na pustce
    (ten sam princip co `backtest/data.py::fetch_candles`).
    """
    cache_file = _cache_path(symbol, bar_size, duration_days)
    if not force_refresh and cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)

    from ib_insync import IB, Contract

    # clientId LOSOWY, nie stały - znalezione na żywo 2026-07-28 (bulk fetch
    # 30 tickerów w JEDNYM długo działającym procesie): stały clientId=7
    # przy szybkim rozłącz/połącz w tej samej sesji Pythona wieszał się na
    # DRUGIM połączeniu (Gateway nie zdążył zwolnić poprzedniego slotu) -
    # pojedyncze wywołania w osobnych procesach tego nie łapały, bo cały
    # proces (i socket) kończył się między wywołaniami. Losowy clientId per
    # zapytanie eliminuje kolizję całkowicie, kosztem żadnej wady tutaj (to
    # tylko odczyt danych historycznych, nie sesja handlowa do śledzenia).
    import random
    ib = IB()
    try:
        ib.connect(IB_GATEWAY_HOST, IB_GATEWAY_PORT, clientId=random.randint(100, 999999), readonly=True, timeout=20)
    except Exception as exc:
        raise RuntimeError(
            f"Nie połączono z IB Gateway na {IB_GATEWAY_HOST}:{IB_GATEWAY_PORT} - "
            f"czy kontener 'ib-gateway' działa i jest zalogowany (docker logs ib-gateway)? {exc}"
        ) from exc

    try:
        contract = Contract(symbol=symbol, secType="STK", exchange=exchange, currency=currency)
        ib.qualifyContracts(contract)

        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=f"{duration_days} D",
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            # ib_insync default (60s) nie starczał dla wiekszych zapytan (np.
            # 60 dni 1-min) - znalezione na zywo 2026-07-28
            # ("reqHistoricalData: Timeout"). 180s z duzym marginesem.
            timeout=180,
        )
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS)
    finally:
        ib.disconnect()

    if not bars:
        raise RuntimeError(
            f"Brak danych historycznych z IBKR dla {symbol}/{exchange}/{currency} "
            f"(bar_size={bar_size}, duration_days={duration_days})."
        )

    candles = [
        {"o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close, "t": str(bar.date)}
        for bar in bars
    ]

    CACHE_DIR.mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(candles, f)
    return candles
