#!/usr/bin/env python3
"""
run_eod_backtest.py
======================
CLI do backtestu strategii EOD (sharp-drop scalper) na świecach 1-minutowych
z IBKR (patrz backtest/eod_runner.py i plan "Backtester EOD na danych 1-min
z IBKR", 2026-07-28). WYMAGA działającego kontenera ib-gateway (patrz
backtest/ibkr_data.py) przy pierwszym pobraniu danego tickera - kolejne
uruchomienia czytają z lokalnego cache (backtest/data/*_ibkr.json).

Domyślne stop_loss_pct/take_profit_pct odzwierciedlają aktualną produkcyjną
bazę (eod_settings, sprawdzone 2026-07-28: 0.4%/0.6%).

Tickery podawane jako TRÓJKI symbol:exchange:currency (IBKR potrzebuje
osobnych pól kontraktu, nie jeden string jak T212 - patrz ibkr_data.py).

Przykład:
    python3 run_eod_backtest.py --tickers IFX:TGATE:EUR,AAPL:IEX:USD --duration-days 5
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from backtest.eod_runner import run_eod_backtest
from backtest.ibkr_data import fetch_ibkr_candles
from backtest.portfolio import Portfolio
from backtest.report import print_report, write_csv


def _parse_ticker_spec(spec: str) -> tuple[str, str, str]:
    symbol, exchange, currency = spec.split(":")
    return symbol, exchange, currency


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest strategii EOD na 1-min świecach z IBKR.")
    parser.add_argument(
        "--tickers", required=True,
        help="Lista symbol:exchange:currency po przecinku, np. IFX:TGATE:EUR,AAPL:IEX:USD",
    )
    parser.add_argument("--duration-days", type=int, default=5, help="Ile dni 1-min historii pobrać przez IBKR")
    parser.add_argument("--entry-amount", type=Decimal, default=Decimal("100"))
    parser.add_argument("--stop-loss-pct", type=Decimal, default=Decimal("0.004"))
    parser.add_argument("--take-profit-pct", type=Decimal, default=Decimal("0.006"))
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--force-refresh", action="store_true", help="Ignoruj lokalny cache, pobierz dane od nowa z IBKR")
    parser.add_argument("--csv", default=None)
    args = parser.parse_args()

    specs = [_parse_ticker_spec(s.strip()) for s in args.tickers.split(",") if s.strip()]

    candles_by_symbol: dict[str, list[dict]] = {}
    for symbol, exchange, currency in specs:
        candles_by_symbol[symbol] = fetch_ibkr_candles(
            symbol, exchange, currency, bar_size="1 min",
            duration_days=args.duration_days, force_refresh=args.force_refresh,
        )

    results: dict[str, Portfolio] = {}
    for symbol, exchange, currency in specs:
        portfolio = Portfolio(starting_cash=args.starting_cash)
        run_eod_backtest(
            symbol, candles_by_symbol[symbol], portfolio,
            entry_amount=args.entry_amount, stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
        )
        results[symbol] = portfolio
        print_report(symbol, portfolio)

    if args.csv:
        write_csv(args.csv, results)
        print(f"\nZapisano transakcje do {args.csv}")


if __name__ == "__main__":
    main()
