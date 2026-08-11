"""
risk_guard.py
=============
Bezpieczniki wywoływane PRZED każdym zleceniem z Warp Mode (i normalnego trybu,
jeśli zechcesz). Ten moduł nic nie wie o Flasku ani o t212_client - dostaje
gotowe dane wejściowe i tylko mówi "wolno" / "nie wolno", żeby dało się go
testować w izolacji i używać z dowolnej warstwy (REST endpoint, WebSocket handler).

WAŻNE ZAŁOŻENIE ARCHITEKTONICZNE (ustalone wcześniej w rozmowie):
------------------------------------------------------------------
T212 API nie daje ceny live. Ty patrzysz na cenę na ekranie T212 (drugi
monitor) i to Ty wpisujesz/potwierdzasz szacowaną cenę w momencie kliknięcia -
albo appka w ogóle nie zna ceny przed wysłaniem zlecenia. Stąd Hard Cap
w tym module działa w DWÓCH trybach:

1. "pre_check" - masz jakąś szacowaną cenę (np. ręcznie wpisaną albo z ostatniej
   znanej pozycji w portfelu) -> liczymy Value = quantity * estimated_price
   i porównujemy z limitem PRZED wysłaniem zlecenia do T212.

2. "post_check" - nie masz żadnej ceny z góry (najczęstszy przypadek przy
   czystym Market Order bez feedu) -> zlecenie leci od razu, a dopiero PO
   otrzymaniu wyniku z T212 (rzeczywista cena wykonania) sprawdzamy czy
   przekroczyło limit i tylko o tym informujemy / logujemy - nie da się
   już go cofnąć, ale przynajmniej wiesz i możesz zareagować (np. tymczasowo
   zablokować dalsze zlecenia na ten ticker).

Cooldown działa niezależnie od trybu Hard Capa - zawsze można i warto go
stosować, żeby ograniczyć przypadkowe podwójne kliknięcia.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from ..utils import ticker_display_name


class GuardDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCKED_HARD_CAP = "BLOCKED_HARD_CAP"
    BLOCKED_COOLDOWN = "BLOCKED_COOLDOWN"


@dataclass
class GuardResult:
    decision: GuardDecision
    reason: Optional[str] = None
    estimated_value: Optional[Decimal] = None

    @property
    def allowed(self) -> bool:
        return self.decision == GuardDecision.ALLOW


@dataclass
class PostTradeWarning:
    """Wynik sprawdzenia PO fakcie, gdy nie było ceny z góry (post_check)."""
    exceeded: bool
    actual_value: Decimal
    limit: Decimal
    overshoot: Decimal  # o ile przekroczono limit (0 jeśli nie przekroczono)


class RiskGuard:
    """
    Stan bezpieczników per użytkownik. Jedna instancja na użytkownika
    (albo trzymana w słowniku {user_id: RiskGuard} w warstwie serwisowej) -
    cooldown musi pamiętać czas ostatniego kliknięcia PER TICKER, więc stan
    nie może być bezstanowy/globalny.
    """

    def __init__(
        self,
        max_order_value: Optional[Decimal] = None,
        cooldown_ms: int = 400,
        safety_margin: Decimal = Decimal("1.0"),
    ):
        """
        max_order_value: Hard Cap w walucie konta. None = brak limitu
            (odradzane, zwłaszcza w Warp Mode - lepiej ustawić coś, nawet
            hojnego, niż nic).
        cooldown_ms: minimalny odstęp między kliknięciami NA TEN SAM TICKER.
        safety_margin: mnożnik zapasu bezpieczeństwa dla pre_check, np. 1.02
            oznacza że realnie blokujemy już przy 98% limitu, żeby zostawić
            margines na ewentualny slippage między Twoją szacowaną ceną
            a rzeczywistą ceną wykonania. Domyślnie 1.0 = brak marginesu.
        """
        self.max_order_value = max_order_value
        self.cooldown_ms = cooldown_ms
        self.safety_margin = safety_margin
        self._last_click_at: dict[str, float] = {}  # {ticker: monotonic_timestamp}

    # -- Cooldown --------------------------------------------------------------

    def _check_cooldown(self, ticker: str) -> bool:
        """Zwraca True jeśli kliknięcie jest dozwolone (minął cooldown)."""
        now = time.monotonic()
        last = self._last_click_at.get(ticker)
        if last is not None and (now - last) * 1000 < self.cooldown_ms:
            return False
        self._last_click_at[ticker] = now
        return True

    # -- Hard Cap (pre-check, gdy znasz szacowaną cenę) ------------------------

    def check_before_order(
        self,
        ticker: str,
        quantity: Decimal,
        estimated_price: Optional[Decimal] = None,
    ) -> GuardResult:
        """
        Wywołuj TUŻ PRZED wysłaniem zlecenia do t212_client.place_market_order().

        Jeśli estimated_price=None (nie masz żadnej ceny referencyjnej),
        Hard Cap w tym wywołaniu jest pomijany (nie ma z czego liczyć Value) -
        w takim wypadku polegaj na check_after_order() jako jedynej linii
        obrony, ograniczonej do wykrycia po fakcie.
        """
        if not self._check_cooldown(ticker):
            return GuardResult(
                decision=GuardDecision.BLOCKED_COOLDOWN,
                reason=f"Cooldown {self.cooldown_ms}ms dla {ticker_display_name(ticker)} jeszcze nie minął.",
            )

        if estimated_price is None or self.max_order_value is None:
            # Brak danych do policzenia Hard Capa z góry - przepuszczamy,
            # kontrola przesuwa się na check_after_order().
            return GuardResult(decision=GuardDecision.ALLOW)

        estimated_value = abs(quantity) * estimated_price * self.safety_margin

        if estimated_value > self.max_order_value:
            return GuardResult(
                decision=GuardDecision.BLOCKED_HARD_CAP,
                reason=(
                    f"Szacowana wartość {estimated_value:.2f} przekracza "
                    f"limit {self.max_order_value:.2f} (z marginesem "
                    f"{self.safety_margin})."
                ),
                estimated_value=estimated_value,
            )

        return GuardResult(decision=GuardDecision.ALLOW, estimated_value=estimated_value)

    # -- Hard Cap (post-check, gdy dopiero po fill znasz cenę) -----------------

    def check_after_order(
        self,
        quantity: Decimal,
        actual_price: Decimal,
    ) -> PostTradeWarning:
        """
        Wywołuj PO otrzymaniu wyniku zlecenia z T212 (rzeczywista cena
        wykonania z odpowiedzi API / historii zleceń). Nie blokuje niczego -
        zlecenie już poszło - ale mówi Ci, czy i o ile przekroczyłeś limit,
        żebyś mógł np. ręcznie wstrzymać dalsze klikanie na ten instrument.
        """
        actual_value = abs(quantity) * actual_price

        if self.max_order_value is None or actual_value <= self.max_order_value:
            return PostTradeWarning(
                exceeded=False,
                actual_value=actual_value,
                limit=self.max_order_value or Decimal("0"),
                overshoot=Decimal("0"),
            )

        return PostTradeWarning(
            exceeded=True,
            actual_value=actual_value,
            limit=self.max_order_value,
            overshoot=actual_value - self.max_order_value,
        )


# ---------------------------------------------------------------------------
# Przykład użycia w warstwie routes/scalping.py:
#
#   guard = RiskGuard(max_order_value=Decimal("500"), cooldown_ms=400)
#
#   # --- Wariant z ręcznie wpisaną/przybliżoną ceną (pre-check) ---
#   result = guard.check_before_order("AAPL_US_EQ", Decimal("2"), estimated_price=Decimal("190.50"))
#   if not result.allowed:
#       play_sound("error.mp3"); show_tile_error(result.reason); return
#   order = client.place_market_order("AAPL_US_EQ", Decimal("2"))
#
#   # --- Wariant bez ceny z góry (post-check) ---
#   result = guard.check_before_order("AAPL_US_EQ", Decimal("2"))  # brak estimated_price
#   if not result.allowed:  # tu i tak sprawdzi tylko cooldown
#       return
#   order = client.place_market_order("AAPL_US_EQ", Decimal("2"))
#   fill_price = Decimal(str(order.raw.get("fillPrice", 0)))  # zależnie od faktycznego payloadu T212
#   warning = guard.check_after_order(Decimal("2"), fill_price)
#   if warning.exceeded:
#       log.warning("Przekroczono Hard Cap o %.2f", warning.overshoot)
# ---------------------------------------------------------------------------
