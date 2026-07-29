"""
backtest/signal_runner.py
===========================
Event loop backtestu Sygnału - dla KAŻDEGO dnia N (po kolei, bez
lookaheadu) liczy RSI/SMA/ATR z okna świec [0..N] (te same funkcje co
produkcja: signal_engine._compute_rsi/_compute_sma/_compute_atr), woła
signal_strategy.compute_entry()/compute_trailing_stop() (DOKŁADNIE ta sama
logika decyzyjna co żywy tick, patrz app/services/strategy/signal_strategy.py)
i wykonuje decyzje na Portfolio (backtest/portfolio.py).

Fill entry / przeliczenie trailing stopu: cena ZAMKNIĘCIA dnia N
(market-on-close, udokumentowane uproszczenie - patrz plan "Backtester v1").

Detekcja "czy stop-loss się wykonał" jest WYJĄTKIEM od tego uproszczenia -
sprawdzana względem DZIENNEGO MINIMUM (`l`), nie samego close. Powód:
prawdziwy STOP na T212 wykonuje się gdy cena W CIĄGU DNIA dotknie poziomu,
niezależnie gdzie zamknie sesję - sprawdzanie tylko po close systematycznie
NIE WYŁAPYWAŁOBY dni, gdzie cena spikuje w dół przez stop i odbija się z
powrotem przed zamknięciem, co sztucznie zawyżałoby wynik backtestu
(fałszywy optymizm - dokładnie czego chcemy uniknąć).
"""

from __future__ import annotations

from decimal import Decimal

from app.services.signal_engine import ATR_PERIOD, MA_PERIOD, RSI_PERIOD, _compute_atr, _compute_rsi, _compute_sma
from app.services.strategy import signal_strategy

from .portfolio import Portfolio


def run_signal_backtest(
    ticker: str,
    candles: list[dict],
    portfolio: Portfolio,
    entry_amount: Decimal,
    rsi_threshold: Decimal,
    stop_loss_atr_mult: Decimal,
    take_profit_atr_mult: Decimal,
) -> None:
    """Odtwarza logikę Sygnału dzień po dniu na `candles` (o/h/l/c, rosnąco)."""
    min_bars = max(MA_PERIOD, RSI_PERIOD + 1, ATR_PERIOD + 1)

    for day in range(min_bars, len(candles)):
        window = candles[: day + 1]
        closes = [Decimal(str(c["c"])) for c in window]
        price = closes[-1]
        day_low = Decimal(str(window[-1]["l"]))

        if ticker in portfolio.open_positions:
            pos = portfolio.open_positions[ticker]
            atr = _compute_atr(window, ATR_PERIOD)
            if atr is not None:
                candidate_stop = signal_strategy.compute_trailing_stop(
                    pos.stop_loss_price, price, atr, stop_loss_atr_mult,
                )
                if candidate_stop is not None:
                    pos.stop_loss_price = candidate_stop

            if day_low <= pos.stop_loss_price:
                portfolio.sell(ticker, day, pos.stop_loss_price, "stop-loss")
        else:
            rsi = _compute_rsi(closes, RSI_PERIOD)
            sma = _compute_sma(closes, MA_PERIOD)
            atr = _compute_atr(window, ATR_PERIOD)
            if rsi is not None and sma is not None and atr is not None and rsi < rsi_threshold and price > sma:
                try:
                    decision = signal_strategy.compute_entry(
                        entry_amount, price, atr, stop_loss_atr_mult, take_profit_atr_mult,
                    )
                except signal_strategy.EntryValidationError:
                    decision = None
                if decision is not None:
                    portfolio.buy(ticker, day, price, decision.quantity, decision.stop_loss_price)

        portfolio.mark_to_market({ticker: price})
