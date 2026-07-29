"""
app/services/strategy/signal_strategy.py
==========================================
Czysta logika decyzyjna Sygnału (RSI/MA200 + trailing stop ATR), WYDZIELONA
2026-07-28 z `signal_engine.py` - pilot event-driven refaktoru (patrz plan
"Event-driven refactor - pilot na Sygnale"). Zero importów T212Client/db/
Flask - tylko Decimal in, Decimal/dataclass out. Cel: ta sama matematyka ma
dać się podłączyć zarówno pod żywy tick (signal_engine.py, jak dziś), jak i
docelowo pod backtester na danych historycznych, bez dotykania żadnego
prawdziwego API.

Wydzielone stąd, żeby skończyć z dublowaniem matematyki między ścieżką
real (`_trail_stop_loss`) i paper (`_trail_stop_loss_paper`) w
signal_engine.py - obie dawniej liczyły candidate_stop OSOBNO, tym samym
wzorem, tylko przepisanym dwa razy.

UWAGA: `_trail_stop_loss` (real) ma DODATKOWY próg `MIN_TRAIL_REQUOTE_ATR_
FRACTION` (nie przestawiaj stopu za drobną poprawę, żeby nie płacić
Cancel-Replace'em z ciasnego rate limitu demo) - `_trail_stop_loss_paper`
NIGDY go nie miał (nie dotyka T212, więc nie ma czego oszczędzać). Ta
różnica jest CELOWA i zostaje warstwą wyżej (signal_engine.py), NIE tutaj -
`compute_trailing_stop` poniżej daje tylko "czy jest poprawa i jaka", decyzję
"czy warto ją zrealizować" podejmuje wołający.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class EntryValidationError(Exception):
    """Wejście odrzucone (ilość <=0 albo SL<=0) - treść już gotowa do zalogowania."""


@dataclass(frozen=True)
class EntryDecision:
    quantity: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal


def compute_entry(
    entry_amount: Decimal,
    price: Decimal,
    atr: Decimal,
    stop_loss_atr_mult: Decimal,
    take_profit_atr_mult: Decimal,
) -> EntryDecision:
    """
    Wyciągnięte z `_enter_position()` w signal_engine.py (część PRZED
    rozgałęzieniem real/paper - tam liczone identycznie dla obu ścieżek).
    """
    quantity = (entry_amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        raise EntryValidationError(f"wyliczona ilość <= 0 (kwota {entry_amount} / cena {price}).")

    stop_loss_price = price - (atr * stop_loss_atr_mult)
    take_profit_price = price + (atr * take_profit_atr_mult)
    if stop_loss_price <= 0:
        raise EntryValidationError("wyliczony stop-loss <= 0 (ATR zbyt duże względem ceny), pomijam wejście.")

    return EntryDecision(quantity=quantity, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price)


def compute_trailing_stop(
    current_stop: Decimal,
    current_price: Decimal,
    atr_at_entry: Decimal | None,
    stop_loss_atr_mult: Decimal,
) -> Decimal | None:
    """
    Wyciągnięte ze zdublowanej matematyki w `_trail_stop_loss()`/
    `_trail_stop_loss_paper()`. Zwraca nowy poziom stopu TYLKO gdy to
    poprawa (nigdy nie cofa stopu w dół) - `None` gdy brak ATR z wejścia
    albo brak poprawy. Próg min. requote (real-only) NIE jest tu liczony,
    patrz docstring modułu.
    """
    if atr_at_entry is None or atr_at_entry <= 0:
        return None

    candidate_stop = (current_price - atr_at_entry * stop_loss_atr_mult).quantize(Decimal("0.0001"))
    if candidate_stop <= current_stop:
        return None

    return candidate_stop


def compute_armed_trailing_stop(
    current_stop: Decimal,
    current_price: Decimal,
    entry_price: Decimal,
    atr_at_entry: Decimal | None,
    arm_profit_atr_mult: Decimal,
    trail_atr_mult: Decimal,
) -> Decimal | None:
    """
    EKSPERYMENT dodany 2026-07-30 (Adam, po analizie backtestu Sygnału -
    "skup sie dlaczego sygnal sie tak kiepsci") - Sygnał od zawsze miał JEDEN
    dystans ATR (stop_loss_atr_mult) używany zarówno jako floor od razu przy
    wejściu, jak i do ciągłego trailingu - dokładnie ten sam wzorzec, który
    Micro-Grid PORZUCIŁ 22.07/27.07 po tym jak złapał na żywo identyczny
    problem (patrz bot_engine.py::_manage_trailing_exit, "protective floor" +
    "bramka uzbrojenia po 2 progach zysku"). Sygnał wchodzi w pozycję
    dokładnie wtedy, gdy RSI jest nisko (świeży dołek) - ciasny stop od dnia
    0 regularnie wywala pozycję zwykłym szumem, zanim spodziewane odbicie
    zdąży się rozwinąć (potwierdzone w logu backtestu - mnóstwo 1-dniowych
    strat).

    Analogiczna bramka: stop NIE zaciska się (zostaje na szerokim floorze
    wyliczonym RAZ przy wejściu - `compute_entry`, `stop_loss_atr_mult`)
    dopóki cena nie wyjdzie na plus o co najmniej `arm_profit_atr_mult *
    atr_at_entry` ponad `entry_price` - odpowiednik "2 progów zysku" z
    Micro-Gridu, tylko liczony w jednostkach ATR zamiast %. Po przekroczeniu
    progu, trailing przechodzi na CIASNY dystans `trail_atr_mult` (typowo
    < stop_loss_atr_mult) od bieżącej ceny - i, jak w oryginalnej wersji,
    nigdy się nie cofa.

    Zwraca None gdy: brak ATR, jeszcze nie uzbrojony (floor z wejścia
    zostaje bez zmian), albo brak poprawy względem obecnego stopu.
    """
    if atr_at_entry is None or atr_at_entry <= 0:
        return None

    arm_threshold = entry_price + atr_at_entry * arm_profit_atr_mult
    if current_price < arm_threshold:
        return None  # jeszcze nieuzbrojony - floor z wejścia zostaje bez zmian

    candidate_stop = (current_price - atr_at_entry * trail_atr_mult).quantize(Decimal("0.0001"))
    if candidate_stop <= current_stop:
        return None

    return candidate_stop
