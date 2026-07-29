#!/usr/bin/env python3
"""
run_backtest.py
=================
CLI do backtestu strategii Sygnał na historycznych świecach dziennych
(patrz backtest/ i plan "Backtester v1 - Sygnal na danych historycznych",
2026-07-28). Odtwarza dokładnie tę samą logikę decyzyjną co produkcja
(app/services/strategy/signal_strategy.py) - żeby weryfikować zmiany
strategii w sekundy zamiast czekać na żywy rynek.

Przykład:
    python3 run_backtest.py --tickers SAFp_EQ,GOOGL_US_EQ --days 400
    python3 run_backtest.py --tickers ASMa_EQ --days 250 --csv wyniki.csv
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from backtest.data import app_context, fetch_candles
from backtest.portfolio import Portfolio
from backtest.report import print_report, write_csv
from backtest.signal_runner import run_signal_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest strategii Sygnał na historii dziennej.")
    parser.add_argument("--tickers", required=True, help="Lista tickerów po przecinku, np. SAFp_EQ,GOOGL_US_EQ")
    parser.add_argument("--days", type=int, default=400, help="Ile dni historii pobrać (min. ~210 dla MA200+bufor)")
    parser.add_argument("--entry-amount", type=Decimal, default=Decimal("100"))
    parser.add_argument("--rsi-threshold", type=Decimal, default=Decimal("35"))
    parser.add_argument("--stop-loss-atr-mult", type=Decimal, default=Decimal("1"))
    parser.add_argument("--take-profit-atr-mult", type=Decimal, default=Decimal("3"))
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--force-refresh", action="store_true", help="Ignoruj lokalny cache, pobierz dane od nowa")
    parser.add_argument("--csv", default=None, help="Ścieżka do zapisu listy transakcji jako CSV")
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    with app_context():
        candles_by_ticker = {t: fetch_candles(t, args.days, force_refresh=args.force_refresh) for t in tickers}

    results: dict[str, Portfolio] = {}
    for ticker in tickers:
        portfolio = Portfolio(starting_cash=args.starting_cash)
        run_signal_backtest(
            ticker, candles_by_ticker[ticker], portfolio,
            entry_amount=args.entry_amount, rsi_threshold=args.rsi_threshold,
            stop_loss_atr_mult=args.stop_loss_atr_mult, take_profit_atr_mult=args.take_profit_atr_mult,
        )
        results[ticker] = portfolio
        print_report(ticker, portfolio)

    if args.csv:
        write_csv(args.csv, results)
        print(f"\nZapisano transakcje do {args.csv}")


if __name__ == "__main__":
    main()
