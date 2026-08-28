"""
app/services/yahoo_resolver.py
================================
Automatyczne, LENIWE rozwiazywanie symbolu Yahoo Finance dla tickerow T212
spoza automatycznego mapowania w finnhub_client.py::t212_to_finnhub()
(wzorzec _US_EQ + reczne wyjatki w TICKER_MAP) - w praktyce dotyczy to
wiekszosci instrumentow spoza gield USA (europejskich itp.), ktorych format
tickera T212 (litera waluty przed _EQ, np. "HASl_EQ") nie mapuje sie
jednoznacznie na sufiks gieldy Yahoo (".L", ".DE", ".PA"...).

Mechanizm: wyszukiwarka Yahoo (`/v1/finance/search?q=<nazwa spolki>`) zwraca
liste kandydatow z roznych gield (ta sama spolka bywa notowana w kilku
miejscach), a `_verify_symbol_has_data()` faktycznie sprawdza ktory z nich
zwraca prawdziwe dane cenowe zanim appka go zaakceptuje - samo trafienie
w wyszukiwarce nie wystarcza (bywaja duplikaty bez pokrycia danych).

Wynik (sukces LUB porazka) trzymany na stale w YahooSymbolMap (models.py) -
patrz resolve(), wywolywane z finnhub_client.py jako ostatni fallback.

2026-08-28: PRZESTAJE auto-cache'owac znaleziony kandydat bez potwierdzenia -
incydent Symrise/Bouygues na DEV, dopasowanie-po-samej-nazwie wybralo zle
papiery (US87155N1090.SG zamiast Symrise), bot wystawil limit-buy po
4-5x zlej cenie (nigdy sie nie wypelnil, ale mogl). Adam: "na przyszlosc nie
rob zadnych resolwow sam tylko zapytaj jak nie wiesz przez telegram" - teraz
znaleziony kandydat tylko PROPONUJEMY na Telegramie (patrz _notify_pending),
czeka w pamieci procesu (_pending) na "/resolve TICKER tak|nie" (patrz
telegram_commands.py) zanim cokolwiek wpadnie do bazy. "Brak dopasowania
wcale" (resolved_symbol=None) nadal cache'uje sie od razu jak dawniej - nie
ma czego potwierdzac.
"""

from __future__ import annotations

import time

import requests

from ..extensions import db
from ..models import Instrument, YahooSymbolMap

SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
CHART_URL_TMPL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
REQUEST_TIMEOUT = 6

# ticker -> (nazwa_spolki, kandydat_symbol) - kandydaci czekajacy na
# potwierdzenie przez Telegram. W pamieci procesu (ponytail: nie ma DB
# kolumny na "pending" stan - restart procesu zgubi oczekujacy wpis, appka
# po prostu zapyta ponownie przy kolejnej probie, nic straconego poza
# duplikatem powiadomienia).
_pending: dict[str, tuple[str, str]] = {}
_pending_notified_at: dict[str, float] = {}
_RENOTIFY_SECONDS = 3600  # nie spamuj Telegrama co tick bota, raz na godzine wystarczy
HEADERS = {"User-Agent": "Mozilla/5.0"}
MAX_CANDIDATES_TRIED = 4  # nie probuj w nieskonczonosc - kilka pierwszych (po sortowaniu wg waluty) wystarcza

# Ta sama spolka bywa notowana na kilku gieldach naraz (np. Hays: Londyn w GBX,
# ale tez Frankfurt/Stuttgart/Monachium w EUR) - samo trafienie w wyszukiwarce
# Yahoo nie mowi KTORE notowanie odpowiada walucie instrumentu w T212. Bez
# tego mapowania kod bralby pierwszy z brzegu zweryfikowany wynik, co
# potrafilo wybrac zle notowanie (inna waluta = inne ceny na wykresie).
# Kod gieldy ("exchange" w odpowiedzi Yahoo) -> jakim walutom T212 odpowiada.
EXCHANGE_TO_CURRENCIES = {
    "LSE": {"GBP", "GBX"},
    "EBS": {"CHF"}, "VTX": {"CHF"},
    "TOR": {"CAD"}, "TSX": {"CAD"}, "TSXV": {"CAD"},
    "GER": {"EUR"}, "FRA": {"EUR"}, "STU": {"EUR"}, "MUN": {"EUR"}, "BER": {"EUR"},
    "DUS": {"EUR"}, "HAM": {"EUR"}, "HAN": {"EUR"}, "PAR": {"EUR"}, "AMS": {"EUR"},
    "MIL": {"EUR"}, "BRU": {"EUR"}, "MCE": {"EUR"}, "LIS": {"EUR"},
}


def _search_candidates(company_name: str, currency_code: str | None) -> list[str]:
    try:
        resp = requests.get(
            SEARCH_URL,
            params={"q": company_name, "quotesCount": 8, "newsCount": 0},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        quotes = [
            q for q in resp.json().get("quotes", [])
            if q.get("quoteType") == "EQUITY" and q.get("symbol")
        ]
    except (requests.RequestException, ValueError):
        return []

    # Notowania z gieldy pasujacej do waluty instrumentu ida na przod
    # kolejnosci (ale reszta zostaje jako fallback, nie jest odrzucana -
    # lepszy przyblizony wynik niz zaden, jesli akurat nie ma dopasowania).
    def matches_currency(q: dict) -> bool:
        expected = EXCHANGE_TO_CURRENCIES.get(q.get("exchange"))
        return bool(currency_code and expected and currency_code in expected)

    quotes.sort(key=lambda q: 0 if matches_currency(q) else 1)
    return [q["symbol"] for q in quotes][:MAX_CANDIDATES_TRIED]


def _verify_symbol_has_data(symbol: str) -> bool:
    """Samo trafienie w wyszukiwarce nie wystarcza - sprawdz ze Yahoo faktycznie ma dla niego notowania."""
    try:
        resp = requests.get(
            CHART_URL_TMPL.format(symbol=symbol),
            params={"range": "5d", "interval": "1d"},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return False
        result = resp.json()["chart"]["result"]
        return bool(result and result[0].get("indicators", {}).get("quote"))
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return False


def resolve(t212_ticker: str) -> str | None:
    """
    Zwraca zbuforowany symbol Yahoo dla tickera T212, None jesli nie ma
    jeszcze potwierdzonego mapowania - albo dlatego ze nic nie znaleziono
    (cache'owane od razu jak dawniej), albo dlatego ze kandydat CZEKA na
    potwierdzenie Adama przez Telegram (patrz modul docstring/_notify_pending
    /confirm_pending). W obu przypadkach wolajacy dostaje po prostu "brak
    ceny" i pomija ten tick - dokladnie jak przy prawdziwym braku pokrycia.
    """
    cached = YahooSymbolMap.query.get(t212_ticker)
    if cached is not None:
        return cached.yahoo_symbol

    if t212_ticker in _pending:
        _notify_pending(t212_ticker)
        return None

    instrument = Instrument.query.get(t212_ticker)
    company_name = instrument.name if instrument else t212_ticker
    currency_code = instrument.currency_code if instrument else None

    resolved_symbol = None
    for candidate in _search_candidates(company_name, currency_code):
        if _verify_symbol_has_data(candidate):
            resolved_symbol = candidate
            break

    if resolved_symbol is None:
        db.session.add(YahooSymbolMap(ticker=t212_ticker, yahoo_symbol=None))
        db.session.commit()
        return None

    _pending[t212_ticker] = (company_name, resolved_symbol)
    _notify_pending(t212_ticker)
    return None


def _notify_pending(t212_ticker: str) -> None:
    now = time.monotonic()
    last = _pending_notified_at.get(t212_ticker, 0.0)
    if now - last < _RENOTIFY_SECONDS:
        return
    _pending_notified_at[t212_ticker] = now

    from flask import current_app

    from . import telegram_notify

    company_name, candidate = _pending[t212_ticker]
    telegram_notify.send_telegram_message(
        current_app.config.get("TELEGRAM_BOT_TOKEN"), current_app.config.get("TELEGRAM_CHAT_ID"),
        f"❓ Nowy ticker bez mapowania cen: {company_name} ({t212_ticker})\n"
        f"Znaleziony kandydat: {candidate}\n\n"
        f"Odpowiedz: /resolve {t212_ticker} tak (albo: /resolve {t212_ticker} nie)",
    )


def confirm_pending(t212_ticker: str, accepted: bool) -> str | None:
    """
    Wolane z telegram_commands.py po "/resolve TICKER tak|nie". Zwraca
    zaakceptowany symbol (albo None gdy odrzucone/nic nie czekalo) - do
    wiadomosci potwierdzajacej.
    """
    pending = _pending.pop(t212_ticker, None)
    _pending_notified_at.pop(t212_ticker, None)
    if pending is None:
        return None
    _, candidate = pending
    symbol = candidate if accepted else None
    db.session.add(YahooSymbolMap(ticker=t212_ticker, yahoo_symbol=symbol))
    db.session.commit()
    return symbol if accepted else None
