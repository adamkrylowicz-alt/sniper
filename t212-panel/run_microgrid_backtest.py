#!/usr/bin/env python3
"""
run_microgrid_backtest.py
============================
CLI do portfolio-wide backtestu Micro-Gridu (DCA + trailing STOP), patrz
backtest/microgrid_runner.py po pełny opis architektury i uproszczeń.
Domyślne wartości parametrów (--max-dca-levels, --dca-trigger-pct,
--dca-scenario, --take-profit-step-pct, --stop-loss-pct) i lista tickerów
(--tickers) ODZWIERCIEDLAJĄ aktualny stan produkcyjnej bazy (risk_settings/
bot_assets, sprawdzone ręcznie 2026-07-28) - domyślne uruchomienie bez flag
jest więc bezpośrednio porównywalne z tym co bot faktycznie robi na koncie
demo. Podanie flag nadpisuje pojedyncze parametry do eksperymentów.

Przykład:
    python3 run_microgrid_backtest.py --days 400
    python3 run_microgrid_backtest.py --days 400 --dca-trigger-pct 0.03 --max-dca-levels 3
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from backtest.data import app_context, fetch_candles
from backtest.microgrid_runner import Asset, GridPortfolio, SettingsStub, run_microgrid_backtest

DEFAULT_TICKERS = [
    "AAPL_US_EQ", "AIRp_EQ", "ALVd_EQ", "AMZN_US_EQ", "ASML_US_EQ", "ASMLa_EQ",
    "BLK_US_EQ", "BNPp_EQ", "COST_US_EQ", "CRM_US_EQ", "DTEd_EQ", "FB_US_EQ",
    "FPp_EQ", "GOOGL_US_EQ", "IFXd_EQ", "INGAa_EQ", "IPOE_US_EQ", "JNJ_US_EQ",
    "JPM_US_EQ", "KO_US_EQ", "MA_US_EQ", "MCp_EQ", "MSFT_US_EQ", "NFLX_US_EQ",
    "NVDA_US_EQ", "ORCL_US_EQ", "PRXa_EQ", "PYPL_US_EQ", "SAFp_EQ", "SANe_EQ",
    "SAPd_EQ", "SIEd_EQ", "SPCX_US_EQ", "SUp_EQ", "TSLA_US_EQ", "V_US_EQ",
    "WMT_US_EQ", "XOM_US_EQ",
]


def _currency(ticker: str) -> str:
    return "USD" if ticker.endswith("_US_EQ") else "EUR"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest portfolio-wide Micro-Gridu (DCA + trailing STOP).")
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS), help="Lista tickerów po przecinku")
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--entry-amount", type=Decimal, default=Decimal("100"))
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--max-dca-levels", type=int, default=5)
    parser.add_argument("--dca-trigger-pct", type=Decimal, default=Decimal("0.05"))
    parser.add_argument("--dca-scenario", default="1,1,1,1,1")
    parser.add_argument("--take-profit-step-pct", type=Decimal, default=Decimal("0.003"))
    parser.add_argument("--stop-loss-pct", type=Decimal, default=Decimal("0.02"))
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--csv", default=None)
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    assets = [Asset(ticker=t, currency=_currency(t), entry_amount=args.entry_amount) for t in tickers]
    settings = SettingsStub(
        max_dca_levels=args.max_dca_levels, dca_trigger_pct=args.dca_trigger_pct,
        dca_scenario=args.dca_scenario, take_profit_step_pct=args.take_profit_step_pct,
        stop_loss_pct=args.stop_loss_pct,
    )

    with app_context():
        candles_by_ticker = {t: fetch_candles(t, args.days, force_refresh=args.force_refresh) for t in tickers}

    portfolio = GridPortfolio(starting_cash=args.starting_cash)
    run_microgrid_backtest(assets, candles_by_ticker, portfolio, settings)

    trades = portfolio.closed_trades
    total_return_pct = (portfolio.final_equity - portfolio.starting_cash) / portfolio.starting_cash * 100
    wins = [t for t in trades if t.pnl > 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0

    print(f"Tickerów: {len(tickers)} | Transakcji zamkniętych: {len(trades)} | Wciąż otwartych: {len(portfolio.open_positions)}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Total return: {total_return_pct:.2f}%")
    print(f"Max drawdown: {portfolio.max_drawdown_pct:.2f}%")
    print(f"Kapitał końcowy: {portfolio.final_equity:.2f} (start {portfolio.starting_cash:.2f})")
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print(f"Powody zamknięcia: {by_reason}")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "ticker", "entry_day", "exit_day", "average_price", "exit_price",
                "quantity", "dca_level", "exit_reason", "pnl", "pnl_pct",
            ])
            for t in trades:
                writer.writerow([
                    t.ticker, t.entry_day, t.exit_day, t.average_price, t.exit_price,
                    t.quantity, t.dca_level, t.exit_reason, t.pnl, t.pnl_pct,
                ])
        print(f"\nZapisano transakcje do {args.csv}")


if __name__ == "__main__":
    main()
