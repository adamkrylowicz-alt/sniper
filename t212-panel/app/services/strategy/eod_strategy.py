"""
app/services/strategy/eod_strategy.py
========================================
Czysta logika decyzyjna EOD (sizing wejścia + trailing stop-loss),
WYDZIELONA 2026-07-28 z `eod_engine.py` - TRZECIA część event-driven
refaktoru (pierwsza: `signal_strategy.py`, druga: `microgrid_strategy.py`).
Zero importów T212Client/db/Flask - tylko Decimal in, Decimal/dataclass out.

`_worst_recent_drop()`/`_size_multiplier_for_drop()` w `eod_engine.py` SĄ już
czyste (biorą listę świec, zwracają Decimal/tuple, zero I/O) - CELOWO
zostają tam, importowane wprost stąd i z `backtest/eod_runner.py`, żeby nie
duplikować.

Pełne uzasadnienie matematyki (dlaczego TP = reference_price zamiast
sztywnego %, dlaczego trailing SL to stały % nie ATR jak w Sygnale/
Micro-Gridzie) zostaje w docstringu `eod_engine.py` - tu tylko sama, już
zweryfikowana formuła.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class EntryValidationError(Exception):
    """Wejście odrzucone (wyliczona ilość <= 0) - treść już gotowa do zalogowania."""


@dataclass(frozen=True)
class EntryDecision:
    quantity: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal


def compute_entry(
    entry_amount: Decimal,
    price: Decimal,
    multiplier: Decimal,
    reference_price: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
) -> EntryDecision:
    """Wyciągnięte z `_enter_position()` w eod_engine.py."""
    amount = entry_amount * multiplier
    quantity = (amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        raise EntryValidationError(f"wyliczona ilość <= 0 (kwota {amount} / cena {price}).")

    stop_loss_price = price * (1 - stop_loss_pct)
    # TP = powrót do ceny SPRZED spadku (reference_price z _worst_recent_drop) -
    # fallback na sztywny % TYLKO gdyby reference_price wypadł <= cenie wejścia
    # (żeby TP nigdy nie był na/poniżej wejścia - patrz eod_engine.py).
    take_profit_price = reference_price if reference_price > price else price * (1 + take_profit_pct)

    return EntryDecision(quantity=quantity, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price)


def compute_trailing_stop(
    current_stop: Decimal,
    current_price: Decimal,
    buy_price: Decimal,
    stop_loss_pct: Decimal,
    min_requote_fraction: Decimal,
) -> Decimal | None:
    """
    Wyciągnięte z `_trail_stop_loss()` w eod_engine.py. Zwraca nowy poziom
    stopu TYLKO gdy to poprawa (nigdy nie cofa) I wystarczająco duża żeby
    opłacało się Cancel-Replace'em (`min_requote_fraction` * dystans przy
    wejściu) - inaczej `None`. W odróżnieniu od Sygnału/Micro-Gridu dystans
    jest STAŁYM % (`stop_loss_pct`) liczonym od BIEŻĄCEJ ceny, bez ATR -
    EOD to krótki scalp, celowo prostsze.
    """
    candidate_stop = (current_price * (1 - stop_loss_pct)).quantize(Decimal("0.0001"))
    if candidate_stop <= current_stop:
        return None

    distance = buy_price * stop_loss_pct
    min_requote_threshold = current_stop + (distance * min_requote_fraction)
    if candidate_stop < min_requote_threshold:
        return None

    return candidate_stop
