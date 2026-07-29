"""
app/services/strategy/microgrid_strategy.py
=============================================
Czysta logika decyzyjna Micro-Grid Bota (progi/trailing STOP, sizing wejścia),
WYDZIELONA 2026-07-28 z `bot_engine.py` - druga część event-driven refaktoru
(pierwsza: `signal_strategy.py`, pilot na Sygnale). Zero importów
T212Client/db/Flask - tylko Decimal in, Decimal/dataclass/int out.

Wydzielone WYŁĄCZNIE: sizing wejścia (`_enter_position`) i matematyka
ciągłego trailing STOP (`_manage_trailing_exit`, Faza 1). CELOWO nie
obejmuje DCA-triggerów (`_trigger_dca_buys`) ani scoringu kandydatów
(`_process_entries`) - te zostają w `bot_engine.py` na razie, osobne
przejście później jeśli potrzebne.

Pełne uzasadnienie każdego kroku matematyki (dlaczego floor liczony TYLKO
przy pierwszym uzbrojeniu, dlaczego floor_anchor=max(ref_price,current_price)
a nie goły ref_price, dlaczego max(floor, ciasny_target) zamiast samego
floora) zostaje w docstringu `_manage_trailing_exit()` w bot_engine.py -
tu tylko sama, już zweryfikowana formuła.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class EntryValidationError(Exception):
    """Wejście odrzucone (wyliczona ilość <= 0) - treść już gotowa do zalogowania."""


@dataclass(frozen=True)
class EntryDecision:
    quantity: Decimal


def compute_entry_quantity(entry_amount: Decimal, price: Decimal) -> EntryDecision:
    """Wyciągnięte z `_enter_position()` w bot_engine.py."""
    quantity = (entry_amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        raise EntryValidationError(f"wyliczona ilość <= 0 (kwota {entry_amount} / cena {price}).")
    return EntryDecision(quantity=quantity)


def compute_milestone_steps(ref_price: Decimal, current_price: Decimal, step: Decimal) -> int:
    """Ile PEŁNYCH progów `step` minęła cena powyżej ref_price (0 gdy na minusie/płasko)."""
    profit_pct = (current_price - ref_price) / ref_price
    return int(profit_pct / step) if profit_pct > 0 else 0


def compute_exhausted_dca_floor(
    current_price: Decimal,
    atr_distance: Decimal | None,
    stop_loss_pct: Decimal,
) -> Decimal:
    """
    Dodane 2026-07-28 - ostatnia linia obrony dla pozycji, która wyczerpała
    WSZYSTKIE poziomy DCA (`dca_level == max_dca_levels-1`) a cena wciąż
    siedzi poniżej average_price (`compute_milestone_steps` < 2, więc
    normalny warunek uzbrojenia w `_manage_trailing_exit` nigdy się nie
    spełni - do tej zmiany taka pozycja zostawała UZBROJONA-NIGDY, bez
    żadnego stopu, dopóki cena sama nie wróci nad breakeven+2*step; znalezione
    backtestem 2026-07-28: PRXa_EQ/MCp_EQ/SAPd_EQ na dca_level=4, -27.7%/
    -18.1%/-13.7%, `stop_target_price=None`).

    Ta sama matematyka co floor przy PIERWSZYM uzbrojeniu (ATR*1.8 gdy
    dostępne, inaczej stop_loss_pct - patrz compute_trailing_stop), ale
    zakotwiczona w AKTUALNEJ cenie, nie w ref_price/average - average jest
    już wysoko nad nami (stąd brak amunicji), więc floor liczony od niej
    wypadłby jeszcze wyżej niż obecna cena i wykonałby się od razu.
    Zakotwiczenie w current_price daje realny, sensowny bufor OD TEGO
    miejsca w dół, zamiast próbować odtworzyć nieosiągalny już breakeven.

    Zwraca ZAWSZE realną cenę (nigdy None) - w odróżnieniu od
    compute_trailing_stop przy już uzbrojonej pozycji, tu nie ma czego
    porównywać (brak poprzedniego stopu), więc każdy wynik jest z definicji
    poprawą względem braku ochrony.
    """
    if atr_distance is not None:
        return (current_price - atr_distance).quantize(Decimal("0.0001"))
    return (current_price * (1 - stop_loss_pct)).quantize(Decimal("0.0001"))


def compute_trailing_stop(
    is_first_arm: bool,
    ref_price: Decimal,
    current_price: Decimal,
    step: Decimal,
    existing_stop_target: Decimal | None,
    atr_distance: Decimal | None,
    stop_loss_pct: Decimal,
    min_requote_fraction: Decimal,
) -> Decimal | None:
    """
    Wyciągnięte z `_manage_trailing_exit()` (Faza 1) w bot_engine.py.
    Zwraca nowy poziom STOP-a, albo `None` gdy nic do zrobienia (dla
    "już uzbrojony" - albo brak poprawy, albo poprawa za mała żeby
    opłacało się Cancel-Replace'em).

    `is_first_arm=True`: PIERWSZE uzbrojenie - `max(floor, ciasny_target_teraz)`,
    floor z ATR gdy dostępne, inaczej fallback na `stop_loss_pct`.

    `is_first_arm=False`: już uzbrojony - czysty ciągły trailing względem
    WŁASNEGO poprzedniego poziomu (nigdy w dół), z progiem min. requote
    (`existing_stop_target` MUSI być podane, nie może być `None`).
    """
    if is_first_arm:
        floor_anchor = max(ref_price, current_price)
        if atr_distance is not None:
            floor_candidate = (floor_anchor - atr_distance).quantize(Decimal("0.0001"))
        else:
            floor_candidate = (floor_anchor * (1 - stop_loss_pct)).quantize(Decimal("0.0001"))
        tight_target_now = (current_price * (1 - step)).quantize(Decimal("0.0001"))
        return max(floor_candidate, tight_target_now)

    assert existing_stop_target is not None, "already-armed trade must have a stop_target_price"
    continuous_target = (current_price * (1 - step)).quantize(Decimal("0.0001"))
    candidate_stop = max(existing_stop_target, continuous_target)

    min_requote_threshold = existing_stop_target * (1 + step * min_requote_fraction)
    if candidate_stop < min_requote_threshold:
        return None

    return candidate_stop
