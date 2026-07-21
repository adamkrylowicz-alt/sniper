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
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import requests

logger = logging.getLogger(__name__)

Environment = Literal["demo", "live"]

BASE_URLS: dict[Environment, str] = {
    "demo": "https://demo.trading212.com/api/v0",
    "live": "https://live.trading212.com/api/v0",
}

DEFAULT_TIMEOUT = 10  # sekund na request


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
    ):
        if environment not in BASE_URLS:
            raise ValueError(f"Nieznane środowisko: {environment!r} (oczekiwano 'demo' albo 'live')")

        self.environment = environment
        self.base_url = BASE_URLS[environment]
        self.timeout = timeout
        self._session = session or requests.Session()

        credentials = f"{api_key}:{api_secret}".encode("utf-8")
        basic_token = base64.b64encode(credentials).decode("utf-8")
        self._session.headers.update({
            "Authorization": f"Basic {basic_token}",
            "Content-Type": "application/json",
        })

    # -- Niskopoziomowa obsługa requestów -----------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            logger.error("T212 request failed: %s %s -> %s", method, url, exc)
            raise T212APIError(0, f"Błąd sieci: {exc}") from exc

        self._log_rate_limit(resp)

        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text
            raise T212APIError(resp.status_code, str(payload), payload)

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

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
        """
        return self._request("GET", "/equity/portfolio")

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

    def get_pending_orders(self) -> list[dict]:
        """Zlecenia jeszcze niewykonane / nieanulowane / niewygasłe."""
        return self._request("GET", "/equity/orders")

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
