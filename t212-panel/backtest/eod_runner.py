"""
backtest/eod_runner.py
=========================
Event loop backtestu EOD (sharp-drop scalper) na świecach 1-MINUTOWYCH z
IBKR (backtest/ibkr_data.py) - trzeci i ostatni z trzech silników, patrz
plan "Backtester EOD na danych 1-min z IBKR" (2026-07-28).

Per-ticker NIEZALEŻNA pętla (jak signal_runner.py, NIE jak
microgrid_runner.py) - produkcyjny `_process_entries()` w eod_engine.py
iteruje WSZYSTKIE EODAsset co tick i otwiera KAŻDY co spełni trigger, zero
wspólnego limitu slotów (w odróżnieniu od Micro-Gridu), więc backtest może
traktować tickery niezależnie, reużywając wprost `backtest/portfolio.py`
(Sygnał/EOD: jedna pozycja na ticker, bez DCA - pasuje 1:1).

Reużywa WPROST (zero duplikacji logiki):
- `eod_engine._worst_recent_drop`/`_size_multiplier_for_drop` (już czyste
  funkcje w produkcyjnym module - trigger detekcji ostrego spadku i sizing
  tieru, patrz ich docstringi w eod_engine.py)
- `app.services.strategy.eod_strategy.compute_entry`/`compute_trailing_stop`
  (wyciągnięte z eod_engine.py w tym samym przejściu co ten plik, sprawdzone
  1:1 na 3 realnych transakcjach z bazy `eod_trades`)

GRANICA DNIA SESYJNEGO: świece z IBKR (`useRTH=True` w ibkr_data.py) są
tylko z godzin sesji, ale wielodniowe zapytanie w JEDNYM wywołaniu
(`duration_days`) może mieć przerwę nocną między dniami - `_worst_recent_drop`
liczony na surowej, płaskiej liście pomyliłby ostatnią świecę wczoraj z
pierwszą dziś jako "spadek w 1 minutę" (fałszywy sygnał na luce otwarcia).
Runner grupuje świece po DACIE (prefiks pola `t`, które `ibkr_data.py` już
zapisuje) i liczy rolling-drop WYŁĄCZNIE w obrębie tego samego dnia - okno
restartuje się na granicy dni (dokładnie jak `_worst_recent_drop` na
produkcji, gdzie świece z Yahoo to zawsze TYLKO dzisiejsza, jednodniowa
sesja - backtest odtwarza ten sam, jednodniowy zasięg okna, tylko dla wielu
dni pod rząd).

Fill wejścia: cena ZAMKNIĘCIA minuty triggera (jak signal_runner.py).
Detekcja wykonania stop-loss/take-profit: względem MINUTOWEGO minimum/
maksimum (nie tylko close) - ten sam princip realizmu co day_low w
signal_runner.py. W tej samej minucie stop-loss sprawdzany PRZED
take-profit (produkcja: SL to prawdziwy resting order na T212, niezależny
od price-tick logiki TP - patrz `_manage_exits` w eod_engine.py).

`force_close_enabled` (domyślnie WYŁĄCZONE na produkcji) - POMINIĘTE w tej
wersji, zgodnie z planem.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.eod_engine import _size_multiplier_for_drop, _worst_recent_drop
from app.services.strategy import eod_strategy

from .portfolio import Portfolio

MIN_TRAIL_REQUOTE_EOD_FRACTION = Decimal("0.1")  # ten sam wzorzec co eod_engine.py


def _group_by_day(candles: list[dict]) -> list[list[dict]]:
    """Dzieli płaską listę świec na listy per dzień kalendarzowy (prefiks pola 't', patrz docstring modułu)."""
    days: list[list[dict]] = []
    current_date = None
    for candle in candles:
        date = candle["t"][:10]
        if date != current_date:
            days.append([])
            current_date = date
        days[-1].append(candle)
    return days


def run_eod_backtest(
    ticker: str,
    candles: list[dict],
    portfolio: Portfolio,
    entry_amount: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
) -> None:
    """Odtwarza logikę EOD minuta po minucie na `candles` (o/h/l/c/t, rosnąco) - patrz docstring modułu."""
    global_idx = -1
    for day_candles in _group_by_day(candles):
        for i in range(5, len(day_candles)):
            global_idx += 1
            window = day_candles[: i + 1]
            candle = window[-1]
            price = Decimal(str(candle["c"]))
            minute_low = Decimal(str(candle["l"]))
            minute_high = Decimal(str(candle["h"]))

            if ticker in portfolio.open_positions:
                pos = portfolio.open_positions[ticker]
                if minute_low <= pos.stop_loss_price:
                    portfolio.sell(ticker, global_idx, pos.stop_loss_price, "stop-loss")
                elif pos.take_profit_price is not None and minute_high >= pos.take_profit_price:
                    portfolio.sell(ticker, global_idx, pos.take_profit_price, "take-profit")
                elif pos.take_profit_price is None or price < pos.take_profit_price:
                    candidate_stop = eod_strategy.compute_trailing_stop(
                        pos.stop_loss_price, price, pos.entry_price, stop_loss_pct, MIN_TRAIL_REQUOTE_EOD_FRACTION,
                    )
                    if candidate_stop is not None:
                        pos.stop_loss_price = candidate_stop
            else:
                drop_result = _worst_recent_drop(window)
                if drop_result is not None:
                    drop_pct, reference_price = drop_result
                    multiplier = _size_multiplier_for_drop(drop_pct)
                    if multiplier is not None:
                        try:
                            decision = eod_strategy.compute_entry(
                                entry_amount, price, multiplier, reference_price, stop_loss_pct, take_profit_pct,
                            )
                        except eod_strategy.EntryValidationError:
                            decision = None
                        if decision is not None:
                            portfolio.buy(
                                ticker, global_idx, price, decision.quantity,
                                decision.stop_loss_price, decision.take_profit_price,
                            )

            portfolio.mark_to_market({ticker: price})
