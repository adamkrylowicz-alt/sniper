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
(get_live_price) to Alpaca Market Data API (REST, klucz+secret w
ALPACA_API_KEY/ALPACA_API_SECRET), Finnhub->Yahoo zostaje jako fallback gdy
Alpaca zawiedzie (brak klucza, błąd sieci, symbol spoza pokrycia). Rozszerzone
2026-07-27 (Adam: "masz api alpaki dlaczego go nie używasz?") o świece -
get_mini_chart_ohlc (dzienne, RSI/SMA/ATR) i get_eod_intraday_1m (1-min, EOD)
też próbują Alpaca NAJPIERW dla `*_US_EQ`, zamiast wyłącznie na Finnhub
(zablokowany /stock/candle na darmowym planie, patrz TICKER_MAP) / Yahoo
(nieoficjalne, bez SLA). Dla tickerów spoza USD (EUR itd.) Alpaca w ogóle nie
jest próbowane - zero zmiany zachowania, wciąż Finnhub->Yahoo jak dotychczas.
Endpoint Market Data API jest WSPÓLNY dla kluczy paper i live trading
(inaczej niż endpoint do składania zleceń) - klucz zaczynający się na "PK"
(paper) działa tu identycznie jak klucz live.
"""

from __future__ import annotations

import datetime as dt
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

# Cache dla wykresu 1-min na stronie instrumentu (get_intraday_1m_chart) -
# CELOWO OSOBNY od get_eod_intraday_1m (decyzje bota EOD, zero cache'u) - to
# widok dla czlowieka, nie decyzja tradingowa co do sekundy, wiec krotki
# cache jest OK i oszczedza budzet Alpaca gdy ktos odswieza/przelacza karty.
CACHE_1M_TTL_SECONDS = 60
_cache_1m: dict[str, tuple[float, list[dict] | None]] = {}  # {"AAPL_US_EQ:5m": (monotonic_ts, candles)}

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


def get_eod_intraday_1m(
    ticker: str, alpaca_api_key: str | None = None, alpaca_api_secret: str | None = None,
) -> list[dict] | None:
    """
    Świece 1-minutowe DZISIEJSZEJ sesji - do modułu EOD (services/eod_engine.py,
    detekcja ostrych spadków w 1-5 minut pod koniec sesji).

    Dla tickerów `*_US_EQ`: Alpaca Market Data API jako GŁÓWNE źródło (dodane
    2026-07-27, na życzenie Adama - "masz api alpaki dlaczego go nie
    używasz?"; wcześniej tylko get_live_price go używał, ten moduł jechał
    wyłącznie na Yahoo mimo że Alpaca daje realne, dokumentowane 1-min bary
    dla US), Yahoo jako fallback. Dla reszty (EUR itd.) bez zmian - Yahoo
    Chart API (`range=1d&interval=1m`), NIEOFICJALNE, bez SLA - świadoma
    decyzja Adama (2026-07-24, patrz docs/IDEAS_v2.md pkt 3): sprawdzone 5
    płatnych alternatyw (Twelve Data, Alpha Vantage, Polygon, EOD Historical
    Data, IEX Cloud) i żadna nie dawała taniego, prawdziwego 1-min dla
    Europy - docelowo IBKR API gdy dostępne.

    ZERO cache'u (w odróżnieniu od get_mini_chart_ohlc) - to dane do decyzji
    tradingowej sprzed sekund, nie do mini-wykresu, ten sam powód co
    get_live_price. Zwraca listę {"t": unix_timestamp, "o","h","l","c"}
    najstarsza -> najnowsza, albo None (brak danych/błąd/poza sesją -
    Yahoo dla `range=1d` poza godzinami handlu zwraca pustą/krótką listę).
    """
    if ticker.endswith(_US_SUFFIX) and alpaca_api_key and alpaca_api_secret:
        candles = _fetch_alpaca_bars_1m(alpaca_api_key, alpaca_api_secret, ticker)
        if candles:
            return candles

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
        if resp.status_code != 200:
            return None
        result = resp.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        opens, highs, lows, closes = quote["open"], quote["high"], quote["low"], quote["close"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None

    candles = [
        {"t": t, "o": round(float(o), 4), "h": round(float(h), 4), "l": round(float(l), 4), "c": round(float(c), 4)}
        for t, o, h, l, c in zip(timestamps, opens, highs, lows, closes)
        if None not in (o, h, l, c)
    ]
    return candles if len(candles) >= 2 else None


def get_mini_chart_ohlc(
    api_key: str | None, ticker: str, days: int = 30,
    alpaca_api_key: str | None = None, alpaca_api_secret: str | None = None,
) -> list[dict] | None:
    """
    Zwraca listę OHLC (open/high/low/close, najstarsza -> najnowsza) dla
    ostatnich `days` dni, albo None gdy brak danych/klucza/połączenia - do
    rysowania świec w Smart Virtual Pie (routes/pie.py::charts) oraz do
    RSI/SMA/ATR w signal_engine.py i bot_engine.py.

    Kolejność źródeł dla tickerów `*_US_EQ`: Alpaca (dodane 2026-07-27, ten
    sam klucz co get_live_price - Finnhub /stock/candle jest zablokowany na
    darmowym planie, patrz TICKER_MAP/finnhub_client.py) -> Finnhub -> Yahoo.
    Dla reszty tickerów (EUR itd.): Finnhub POMIJANY całkowicie -> od razu
    Yahoo. /stock/candle na darmowym planie Finnhub zwraca 403 dla KAŻDEGO
    tickera spoza US bez wyjątku (potwierdzone w logach 2026-07-29 dla
    wszystkich EU tickerów botów - AIRp_EQ, ASMLa_EQ, BNPp_EQ, DTEd_EQ,
    FPp_EQ, IFXd_EQ, INGAa_EQ, MCp_EQ, PRXa_EQ, SAFp_EQ, SANe_EQ, SAPd_EQ,
    SIEd_EQ, SUp_EQ) - dla tych tickerów Alpaca nigdy nie jest próbowane
    (poza US), więc zapytanie do Finnhuba tu ZAWSZE kończyło się 403 i
    wyłącznie zaśmiecało log przed spadkiem do Yahoo.
    """
    key = f"{ticker}:{days}"
    now = time.monotonic()
    cached = _cache_ohlc.get(key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    candles = None
    if ticker.endswith(_US_SUFFIX) and alpaca_api_key and alpaca_api_secret:
        candles = _fetch_alpaca_bars_daily(alpaca_api_key, alpaca_api_secret, ticker, days)

    if not candles and api_key and ticker.endswith(_US_SUFFIX):
        candles = _fetch_candles_ohlc(api_key, ticker, days)
    if not candles:
        candles = _fetch_yahoo_candles_ohlc(ticker, days)
    _cache_ohlc[key] = (now, candles)
    return candles


def get_mini_charts_ohlc(
    api_key: str | None, tickers: list[str], days: int = 30,
    alpaca_api_key: str | None = None, alpaca_api_secret: str | None = None,
) -> dict[str, list[dict] | None]:
    """Wygodny batch - jedno wywołanie JS->Flask na cały widok Pie zamiast N osobnych requestów."""
    return {t: get_mini_chart_ohlc(api_key, t, days, alpaca_api_key, alpaca_api_secret) for t in tickers}


# Zakladki ponizej "1min" na stronie instrumentu (dodane 2026-07-27, Adam:
# "dodaj tez inne timestampy oprocz tych co juz sa") - kazdy wpis to
# (timeframe Alpaca, ile dni wstecz pobrac). Krotsze interwaly = krotszy
# lookback (nie ma sensu 700 swiec 1-min z tygodnia, wykres byłby nieczytelny
# w drugą stronę), dluzsze interwaly = dluzszy lookback (garstka świec 1h z
# jednego dnia to za mało, żeby cokolwiek pokazać).
_INTRADAY_INTERVALS: dict[str, tuple[str, int]] = {
    "1m": ("1Min", 1),
    "5m": ("5Min", 5),
    "15m": ("15Min", 14),
    "1h": ("1Hour", 60),
}

# Odpowiednik _INTRADAY_INTERVALS dla IBKR (barSizeSetting ma inny format
# stringów niż Alpaca) - SAME okna dni wstecz, już przetestowane 2026-07-28
# jako szybkie i niezawodne w backtest/ibkr_data.py (60 dni tylko dla 1h -
# dużo mniej punktów danych niż 60 dni 1-min, które się wieszało).
_IBKR_BAR_SIZES: dict[str, str] = {
    "1m": "1 min",
    "5m": "5 mins",
    "15m": "15 mins",
    "1h": "1 hour",
}

# Mapowanie T212 -> kontrakt IBKR (symbol, exchange, currency) dla EU -
# ten sam wzorzec co finnhub_client.TICKER_MAP ("ręcznie uzupełniany gdy
# automatyczne nie działa"), ale IBKR potrzebuje TRZECH pól osobno, nie
# jednego symbolu. Tradegate (TGATE, pokrywa większość europejskich
# blue-chipów, darmowe w "Alternative European Equities") jako domyślna
# giełda; kilka nazw (głównie hiszpańskie + Tenaris) nie ma notowania na
# TGATE, dla nich SMART routing (primaryExchange BM/BVME/AEB) - oznaczone
# niżej. Pierwotne 15 tickerów zweryfikowanych na żywo 2026-07-28.

# ROZSZERZONE 2026-08-06 (Adam: "rozszerz IBKR na resztę tickerów EU") -
# doszło 121 kolejnych, WSZYSTKIE 136 tickerów EU z list Micro-Grid/Sygnał/
# EOD teraz pokryte. Metoda (żeby uniknąć zgadywania symbolu - różne EU
# giełdy nie zawsze mają oczywisty symbol wynikający z tickera T212):
# 1) pobrane ISIN każdego tickera z T212 `/equity/metadata/instruments`
# (lokalna tabela `instruments` go nie trzyma, T212 API tak); 2) każdy ISIN
# rozwiązany przez IBKR `reqContractDetails` (secIdType=ISIN) - zwraca
# WSZYSTKICH kandydatów na różnych giełdach, wybrany TGATE gdy dostępny;
# 3) dla 16 tickerów bez TGATE (głównie hiszpańskie: CaixaBank, Amadeus,
# Sabadell, Indra, Bankinter, Acciona, Cellnex, Mapfre, Naturgy, Enagas,
# ACS, Merlin, Grifols + ArcelorMittal/APERAM/Tenaris) - SMART routing,
# zweryfikowane że qualifyContracts daje DOKŁADNIE JEDEN wynik (nie
# niejednoznaczny). KAŻDY z 121 nowych wpisów zweryfikowany DWA razy na
# żywo: (a) dokładnie ta sama konstrukcja Contract() co `_fetch_ibkr_
# intraday` niżej (symbol+exchange+currency, BEZ ISIN) daje jednoznaczny
# wynik - 121/121 OK, zero niejednoznacznych; (b) end-to-end test
# `reqHistoricalData` na 4 próbkach z różnych giełd (Niemcy/Belgia/Austria/
# SMART-Amsterdam) zwrócił realne świece 1-min z sensownymi cenami.
IBKR_TICKER_MAP: dict[str, tuple[str, str, str]] = {
    "1COVd_EQ": ("1COV", "TGATE", "EUR"),  # Covestro
    "2FEd_EQ": ("RACE", "TGATE", "EUR"),  # Ferrari
    "58Hd_EQ": ("CPR", "TGATE", "EUR"),  # Campari
    "ABI_BE_EQ": ("ABI", "TGATE", "EUR"),  # AB InBev
    "ACAp_EQ": ("ACA", "TGATE", "EUR"),  # Credit Agricole
    "ACSe_EQ": ("ACS", "SMART", "EUR"),  # ACS Actividades de Construccion y Servicios
    "ADSd_EQ": ("ADS", "TGATE", "EUR"),  # Adidas
    "ADYENa_EQ": ("ADYEN", "TGATE", "EUR"),  # Adyen
    "ADa_EQ": ("AD", "TGATE", "EUR"),  # Ahold Delhaize
    "AENAe_EQ": ("AENA", "TGATE", "EUR"),  # Aena SME
    "AGNa_EQ": ("AGN", "TGATE", "EUR"),  # Aegon
    "AGS_BE_EQ": ("AGS", "TGATE", "EUR"),  # Ageas
    "AIp_EQ": ("AI", "TGATE", "EUR"),  # Air Liquide
    "AIRp_EQ": ("AIR", "TGATE", "EUR"),
    "AKZAa_EQ": ("AKZA", "TGATE", "EUR"),  # Akzo Nobel
    "ALOp_EQ": ("ALO", "TGATE", "EUR"),  # Alstom
    "ALVd_EQ": ("ALV", "TGATE", "EUR"),
    "AMSe_EQ": ("AMS", "SMART", "EUR"),  # Amadeus IT
    "ANAe_EQ": ("ANA", "SMART", "EUR"),  # Acciona
    "APAMa_EQ": ("APAM", "SMART", "EUR"),  # APERAM
    "ASGd_EQ": ("G", "TGATE", "EUR"),  # GENERALI
    "ASMLa_EQ": ("ASML", "TGATE", "EUR"),
    "ASMa_EQ": ("ASM", "TGATE", "EUR"),  # ASM International
    "BASd_EQ": ("BAS", "TGATE", "EUR"),  # BASF
    "BAYNd_EQ": ("BAYN", "TGATE", "EUR"),  # Bayer
    "BBVAe_EQ": ("BBVA", "TGATE", "EUR"),  # Banco Bilbao Vizcaya Argentaria
    "BEId_EQ": ("BEI", "TGATE", "EUR"),  # Beiersdorf
    "BKTe_EQ": ("BKT", "SMART", "EUR"),  # Bankinter
    "BMWd_EQ": ("BMW", "TGATE", "EUR"),  # Bayerische Motoren Werke
    "BNPp_EQ": ("BNP", "TGATE", "EUR"),
    "BNRd_EQ": ("BNR", "TGATE", "EUR"),  # Brenntag
    "BNp_EQ": ("BN", "TGATE", "EUR"),  # Danone
    "CABKe_EQ": ("CABK", "SMART", "EUR"),  # CaixaBank
    "CAPp_EQ": ("CAP", "TGATE", "EUR"),  # Capgemini
    "CAp_EQ": ("CA", "TGATE", "EUR"),  # Carrefour
    "CBKd_EQ": ("CBK", "TGATE", "EUR"),  # Commerzbank
    "CLNXe_EQ": ("CLNX", "SMART", "EUR"),  # Cellnex Telecom
    "COLR_BE_EQ": ("COLR", "TGATE", "EUR"),  # Colruyt Group
    "CONd_EQ": ("CON", "TGATE", "EUR"),  # Continental
    "CSp_EQ": ("CS", "TGATE", "EUR"),  # AXA
    "DAId_EQ": ("MBG", "TGATE", "EUR"),  # Mercedes-Benz
    "DB1d_EQ": ("DB1", "TGATE", "EUR"),  # Deutsche Boerse
    "DBKd_EQ": ("DBK", "TGATE", "EUR"),  # Deutsche Bank
    "DGp_EQ": ("DG", "TGATE", "EUR"),  # Vinci
    "DPWd_EQ": ("DHL", "TGATE", "EUR"),  # DHL Group
    "DSMa_EQ": ("DSFIR", "TGATE", "EUR"),  # DSM-Firmenich
    "DSYp_EQ": ("DSY", "TGATE", "EUR"),  # Dassault Systemes
    "DTEd_EQ": ("DTE", "TGATE", "EUR"),
    "EDENp_EQ": ("EDEN", "TGATE", "EUR"),  # Edenred
    "ELEe_EQ": ("ELE", "TGATE", "EUR"),  # Endesa
    "ELI_BE_EQ": ("ELI", "TGATE", "EUR"),  # Elia Group
    "ELp_EQ": ("EL", "TGATE", "EUR"),  # EssilorLuxottica
    "ENGIp_EQ": ("ENGI", "TGATE", "EUR"),  # Engie
    "ENGe_EQ": ("ENG", "SMART", "EUR"),  # Enagas
    "ENI_BE_EQ": ("ENI", "TGATE", "EUR"),  # Eni
    "ENL1d_EQ": ("ENEL", "TGATE", "EUR"),  # Enel
    "ENp_EQ": ("EN", "TGATE", "EUR"),  # Bouygues
    "EOANd_EQ": ("EOAN", "TGATE", "EUR"),  # E.ON
    "FMEd_EQ": ("FME", "TGATE", "EUR"),  # Fresenius Medical Care
    "FPp_EQ": ("TTE", "TGATE", "EUR"),  # TotalEnergies - T212 trzyma stary ticker FP sprzed rebrandingu
    "FREd_EQ": ("FRE", "TGATE", "EUR"),  # Fresenius
    "GLEp_EQ": ("GLE", "TGATE", "EUR"),  # Societe Generale
    "GRFe_EQ": ("GRF", "SMART", "EUR"),  # Grifols
    "HEIAa_EQ": ("HEIA", "TGATE", "EUR"),  # Heineken
    "HEId_EQ": ("HEI", "TGATE", "EUR"),  # Heidelberg Materials
    "HENd_EQ": ("HEN", "TGATE", "EUR"),  # Henkel
    "HNR1d_EQ": ("HNR1", "TGATE", "EUR"),  # Hannover Rueck
    "HOp_EQ": ("HO", "TGATE", "EUR"),  # Thales
    "IBEe_EQ": ("IBE", "TGATE", "EUR"),  # Iberdrola
    "IDRe_EQ": ("IDR", "SMART", "EUR"),  # Indra Sistemas
    "IESd_EQ": ("ISP", "TGATE", "EUR"),  # Intesa Sanpaolo
    "IFXd_EQ": ("IFX", "TGATE", "EUR"),
    "INGAa_EQ": ("INGA", "TGATE", "EUR"),
    "ITXe_EQ": ("ITX", "TGATE", "EUR"),  # Industria de Diseno Textil
    "KBC_BE_EQ": ("KBC", "TGATE", "EUR"),  # KBC Group
    "KERp_EQ": ("KER", "TGATE", "EUR"),  # Kering
    "KPNa_EQ": ("KPN", "TGATE", "EUR"),  # KPN
    "LIGHTa_EQ": ("LIGHT", "TGATE", "EUR"),  # Signify
    "LRp_EQ": ("LR", "TGATE", "EUR"),  # Legrand
    "MAPe_EQ": ("MAP", "SMART", "EUR"),  # Mapfre
    "MCp_EQ": ("MC", "TGATE", "EUR"),
    "MELE_BE_EQ": ("MELE", "TGATE", "EUR"),  # Melexis
    "MLp_EQ": ("ML", "TGATE", "EUR"),  # Cie Generale des Etablissements Michelin
    "MRKd_EQ": ("MRK", "TGATE", "EUR"),  # Merck
    "MRLe_EQ": ("MRL", "SMART", "EUR"),  # Merlin Properties Socimi
    "MTXd_EQ": ("MTX", "TGATE", "EUR"),  # MTU Aero Engines
    "MTa_EQ": ("MT", "SMART", "EUR"),  # ArcelorMittal
    "MUV2d_EQ": ("MUV2", "TGATE", "EUR"),  # Muenchener Rueckversicherungs-Gesellschaft
    "NESRd1_EQ": ("NESR", "TGATE", "EUR"),  # Nestlé
    "NNa_EQ": ("NN", "TGATE", "EUR"),  # NN Group
    "NOTd1_EQ": ("NOT", "TGATE", "EUR"),  # Novartis
    "NTGYe_EQ": ("NTGY", "SMART", "EUR"),  # Naturgy Energy
    "OMV_AT_EQ": ("OMV", "TGATE", "EUR"),  # OMV
    "ORp_EQ": ("OR", "TGATE", "EUR"),  # L'Oreal
    "P911d_EQ": ("P911", "TGATE", "EUR"),  # Porsche
    "PHIAa_EQ": ("PHIA", "TGATE", "EUR"),  # Philips
    "PROX_BE_EQ": ("PROX", "TGATE", "EUR"),  # Proximus
    "PRXa_EQ": ("PRX", "TGATE", "EUR"),
    "PUBp_EQ": ("PUB", "TGATE", "EUR"),  # Publicis Groupe
    "PUMd_EQ": ("PUM", "TGATE", "EUR"),  # Puma
    "QIAd_EQ": ("QIA", "TGATE", "EUR"),  # QIAGEN
    "RANDa_EQ": ("RAND", "TGATE", "EUR"),  # Randstad
    "REPe_EQ": ("REP", "TGATE", "EUR"),  # Repsol
    "RHMd_EQ": ("RHM", "TGATE", "EUR"),  # Rheinmetall
    "RHOd_EQ": ("RHO", "TGATE", "EUR"),  # Roche
    "RIp_EQ": ("RI", "TGATE", "EUR"),  # Pernod Ricard
    "RMSp_EQ": ("RMS", "TGATE", "EUR"),  # Hermes International
    "RNOp_EQ": ("RNO", "TGATE", "EUR"),  # Renault
    "RWEd_EQ": ("RWE", "TGATE", "EUR"),  # RWE
    "SABe_EQ": ("SAB1", "SMART", "EUR"),  # Banco de Sabadell
    "SAFp_EQ": ("SAF", "TGATE", "EUR"),
    "SANe_EQ": ("SAN", "TGATE", "EUR"),
    "SANp_EQ": ("SAN1", "TGATE", "EUR"),  # Sanofi
    "SAPd_EQ": ("SAP", "TGATE", "EUR"),
    "SGOp_EQ": ("SGO", "TGATE", "EUR"),  # Cie de Saint-Gobain
    "SIEd_EQ": ("SIE", "TGATE", "EUR"),
    "SOLB_BE_EQ": ("SOLB", "TGATE", "EUR"),  # Solvay
    "SRTd1_EQ": ("SRT", "TGATE", "EUR"),  # Sartorius
    "STLAPp_EQ": ("STLAP", "TGATE", "EUR"),  # Stellantis
    "STMpp_EQ": ("STMPA", "TGATE", "EUR"),  # STMicroelectronics
    "SUp_EQ": ("SU", "TGATE", "EUR"),
    "SY1d_EQ": ("SY1", "TGATE", "EUR"),  # Symrise
    "TEFe_EQ": ("TEF", "TGATE", "EUR"),  # Telefonica
    "TEPp_EQ": ("TEP", "TGATE", "EUR"),  # Teleperformance
    "TW10d_EQ": ("TEN", "SMART", "EUR"),  # Tenaris
    "UCB_BE_EQ": ("UCB", "TGATE", "EUR"),  # UCB
    "UMG1a_EQ": ("UMG", "TGATE", "EUR"),  # Universal Music
    "UMI_BE_EQ": ("UMI", "TGATE", "EUR"),  # Umicore
    "UNIAa_EQ": ("UNVB", "TGATE", "EUR"),  # Unilever
    "URWa_EQ": ("URW", "TGATE", "EUR"),  # Unibail-Rodamco-Westfield
    "VIEp_EQ": ("VIE", "TGATE", "EUR"),  # Veolia Environnement
    "VIVp_EQ": ("VIV", "TGATE", "EUR"),  # Vivendi
    "VOWd_EQ": ("VOW", "TGATE", "EUR"),  # Volkswagen
    "WKLa_EQ": ("WKL", "TGATE", "EUR"),  # Wolters Kluwer
    "WLNp_EQ": ("WLN", "TGATE", "EUR"),  # Worldline
    "ZALd_EQ": ("ZAL", "TGATE", "EUR"),  # Zalando
}

IB_GATEWAY_HOST = "127.0.0.1"
IB_GATEWAY_PORT = 4002  # PAPER API - patrz docker-compose.yml::ib-gateway


def _fetch_ibkr_intraday(
    symbol: str, exchange: str, currency: str, bar_size: str, lookback_days: int,
    ibkr_host: str | None = None, ibkr_port: int | None = None,
) -> list[dict] | None:
    """
    Świece śróddzienne z IBKR (ten sam kontener/gateway co backtest/ibkr_data.py,
    ale timeouty KRÓTKIE - to blokuje wątek żądania Flask, strona nie może
    czekać wiele minut jak jednorazowy skrypt backtestu gdy gateway
    padnie/zwolni). Fail-open jak Finnhub/Yahoo wszędzie indziej w tym pliku -
    KAŻDY błąd (brak połączenia, timeout, brak kontraktu) -> None, żeby
    przejściowa awaria źródła nie wywalała strony instrumentu.

    ibkr_host/ibkr_port: WŁASNA bramka usera (10.08.2026, opcjonalne, patrz
    models.py::MarketDataKeySet.ibkr_host) - None (brak własnej) spada na
    IB_GATEWAY_HOST/PORT, wspólną bramkę jak dotychczas. Świadomie łagodniej
    niż Finnhub/Alpaca - to tylko wykresy, nie decyzje tradingowe.
    """
    host = ibkr_host or IB_GATEWAY_HOST
    port = ibkr_port or IB_GATEWAY_PORT
    try:
        from ib_insync import IB, Contract
        import random

        ib = IB()
        try:
            ib.connect(host, port, clientId=random.randint(100, 999999), readonly=True, timeout=8)
        except Exception:
            return None
        try:
            contract = Contract(symbol=symbol, secType="STK", exchange=exchange, currency=currency)
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(
                contract, endDateTime="", durationStr=f"{lookback_days} D",
                barSizeSetting=bar_size, whatToShow="TRADES", useRTH=True, formatDate=1, timeout=20,
            )
        finally:
            ib.disconnect()

        if not bars:
            return None
        candles = [
            {"o": round(b.open, 4), "h": round(b.high, 4), "l": round(b.low, 4), "c": round(b.close, 4), "t": int(b.date.timestamp())}
            for b in bars
        ]
        return candles if len(candles) >= 2 else None
    except Exception:
        return None


def get_intraday_chart(
    ticker: str, interval: str, alpaca_api_key: str | None = None, alpaca_api_secret: str | None = None,
    ibkr_host: str | None = None, ibkr_port: int | None = None,
) -> list[dict] | None:
    """
    Świece śróddzienne (1min/5min/15min/1h) do wykresu na stronie szczegółów
    instrumentu (routes/scalping.py::candles, instrument.js - zakładki obok
    1T/1M/3M/1R/MAX). Dodane 2026-07-27 na życzenie Adama: "popracuj nad
    świeczkami 1min na wykresach, tam gdzie się da poki co czyli usa z
    alpaca", potem rozszerzone o kolejne interwały: "dodaj tez inne
    timestampy oprocz tych co juz sa".

    Dla `*_US_EQ` -> Alpaca. Dla EU -> IBKR (dodane 2026-07-28, patrz
    IBKR_TICKER_MAP/_fetch_ibkr_intraday wyżej), TYLKO dla tickerów z
    IBKR_TICKER_MAP (ręczna mapa, jak finnhub_client.TICKER_MAP -
    reszta EU tickerów po prostu nie ma jeszcze wpisu, `None` jak dawniej).
    TYLKO dla `interval` z `_INTRADAY_INTERVALS` - `None` dla wszystkiego
    innego.

    CELOWO OSOBNA funkcja od get_eod_intraday_1m (get_eod_intraday_1m ma
    ZERO cache'u i ZERO IBKR - to dane do REALNEJ decyzji tradingowej bota
    EOD sprzed sekund, świadomie zostaje na Yahoo, patrz plan "IBKR jako
    źródło 1-min świec dla EU") - tutaj to widok dla człowieka, więc krótki
    cache (CACHE_1M_TTL_SECONDS - nazwa historyczna, dotyczy teraz
    wszystkich interwałów śróddziennych, nie tylko 1m) jest pożądany, nie
    problemem - oszczędza budżet Alpaca/IBKR gdy ktoś odświeża/przełącza
    zakładki na stronie instrumentu.
    """
    config = _INTRADAY_INTERVALS.get(interval)
    if config is None:
        return None

    cache_key = f"{ticker}:{interval}"
    now = time.monotonic()
    cached = _cache_1m.get(cache_key)
    if cached and (now - cached[0]) < CACHE_1M_TTL_SECONDS:
        return cached[1]

    timeframe, lookback_days = config
    if ticker.endswith(_US_SUFFIX) and alpaca_api_key and alpaca_api_secret:
        if lookback_days <= 1:
            start = dt.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
        else:
            start = (dt.datetime.utcnow() - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00Z")
        candles = _fetch_alpaca_bars(alpaca_api_key, alpaca_api_secret, ticker, timeframe, start)
    else:
        ibkr_contract = IBKR_TICKER_MAP.get(ticker)
        if ibkr_contract is None:
            return None
        symbol, exchange, currency = ibkr_contract
        bar_size = _IBKR_BAR_SIZES[interval]
        candles = _fetch_ibkr_intraday(symbol, exchange, currency, bar_size, lookback_days, ibkr_host, ibkr_port)

    _cache_1m[cache_key] = (now, candles)
    return candles


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


def _fetch_alpaca_bars(
    api_key: str, api_secret: str, ticker: str, timeframe: str, start: str,
) -> list[dict] | None:
    """
    Świece z Alpaca Market Data API /v2/stocks/{symbol}/bars - WSPÓLNA
    implementacja dla dziennych (get_mini_chart_ohlc) i 1-minutowych
    (get_eod_intraday_1m), tylko dla tickerów `*_US_EQ` (Alpaca nie ma
    pokrycia poza US). Bez parametru `feed` - domyślny feed konta (IEX na
    darmowym planie) jest wystarczający, ten sam kompromis co Yahoo
    (opóźnione dane, patrz docstring get_eod_intraday_1m).
    """
    symbol = _to_finnhub_symbol(ticker)
    try:
        resp = requests.get(
            f"{ALPACA_DATA_BASE_URL}/stocks/{symbol}/bars",
            params={"timeframe": timeframe, "start": start, "limit": 10000, "adjustment": "raw"},
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Alpaca bars (%s): błąd sieci dla %s: %s", timeframe, ticker, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Alpaca bars (%s): HTTP %s dla %s", timeframe, resp.status_code, ticker)
        return None

    try:
        bars = resp.json().get("bars") or []
    except ValueError:
        return None

    candles = []
    for b in bars:
        try:
            o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        except (KeyError, TypeError, ValueError):
            continue
        candle = {"o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4)}
        # "t" dodawane dla KAZDEGO interwalu (nie tylko "1Min" jak wczesniej) -
        # od 2026-07-27 wykres uzywa TradingView Lightweight Charts, ktora
        # wymaga prawdziwego czasu na osi X niezaleznie od interwalu (patrz
        # get_intraday_chart nizej - dziala teraz dla 1m/5m/15m/1h).
        try:
            ts = dt.datetime.strptime(b["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except (KeyError, ValueError):
            continue
        candle["t"] = int(ts.timestamp())
        candles.append(candle)
    return candles if len(candles) >= 2 else None


def _fetch_alpaca_bars_daily(api_key: str, api_secret: str, ticker: str, days: int) -> list[dict] | None:
    """
    `days` to liczba SESJI GIEŁDOWYCH żądanych przez wywołującego (patrz
    get_mini_chart_ohlc), nie dni kalendarzowych - stąd mnożnik *1.6 (+20 dni
    marginesu na święta), żeby okno kalendarzowe do Alpaca dawało co najmniej
    `days` świec handlowych. Ten sam problem i to samo podejście co Yahoo
    (_yahoo_range_for_days: dla 250 sesji żąda "1y" = 365 dni kalendarzowych,
    czyli mnożnik ~1.46) - pierwsza wersja tej funkcji (dni+10) dawała
    Alpace tylko ~176 świec dla żądanych 250 (SMA(200) nigdy by nie policzyło),
    znalezione testem przed wdrożeniem 2026-07-27.
    """
    calendar_days = int(days * 1.6) + 20
    start = (dt.datetime.utcnow() - dt.timedelta(days=calendar_days)).strftime("%Y-%m-%dT00:00:00Z")
    candles = _fetch_alpaca_bars(api_key, api_secret, ticker, "1Day", start)
    return candles[-days:] if candles else None


def _fetch_alpaca_bars_1m(api_key: str, api_secret: str, ticker: str) -> list[dict] | None:
    start = dt.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
    return _fetch_alpaca_bars(api_key, api_secret, ticker, "1Min", start)


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
    Alpaca zawiodło. Dla wszystkich innych tickerów (EUR itd.): Finnhub
    POMIJANY całkowicie -> od razu Yahoo (ten sam powód co
    get_mini_chart_ohlc - darmowy plan Finnhub odmawia dla każdego tickera
    spoza US, więc zapytanie tylko zjadało limit 60/min i zaśmiecało log
    429-kami, patrz 2026-07-29). Zwraca None jeśli WSZYSTKIE źródła zawiodą -
    wywołujący (bot_engine.py) ma wtedy pominąć wejście, nie zgadywać ceny.
    """
    if ticker.endswith(_US_SUFFIX) and alpaca_api_key and alpaca_api_secret:
        price = _fetch_alpaca_quote(alpaca_api_key, alpaca_api_secret, ticker)
        if price is not None:
            return price

    if api_key and ticker.endswith(_US_SUFFIX):
        price = _fetch_finnhub_quote(api_key, ticker)
        if price is not None:
            return price

    return _fetch_yahoo_quote(ticker)
