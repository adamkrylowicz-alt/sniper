"""
backtest/report.py
====================
Podsumowanie wyniku backtestu Sygnału - stdout + opcjonalny CSV z listą
transakcji (jeden wiersz per zamknięta pozycja, per ticker).
"""

from __future__ import annotations

import csv

from .portfolio import Portfolio


def print_report(ticker: str, portfolio: Portfolio) -> None:
    trades = portfolio.closed_trades
    total_return_pct = (portfolio.final_equity - portfolio.starting_cash) / portfolio.starting_cash * 100
    wins = [t for t in trades if t.pnl > 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0

    print(f"\n=== {ticker} ===")
    print(f"Transakcji: {len(trades)}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Total return: {total_return_pct:.2f}%")
    print(f"Max drawdown: {portfolio.max_drawdown_pct:.2f}%")
    print(f"Kapitał końcowy: {portfolio.final_equity:.2f} (start {portfolio.starting_cash:.2f})")
    for t in trades:
        print(
            f"  dzień {t.entry_day}->{t.exit_day}: {t.entry_price:.4f}->{t.exit_price:.4f} "
            f"({t.exit_reason}) P/L {t.pnl:+.2f} ({t.pnl_pct:+.2f}%)"
        )


def write_csv(path: str, results: dict[str, Portfolio]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ticker", "entry_day", "exit_day", "entry_price", "exit_price",
            "quantity", "exit_reason", "pnl", "pnl_pct",
        ])
        for ticker, portfolio in results.items():
            for t in portfolio.closed_trades:
                writer.writerow([
                    ticker, t.entry_day, t.exit_day, t.entry_price, t.exit_price,
                    t.quantity, t.exit_reason, t.pnl, t.pnl_pct,
                ])
