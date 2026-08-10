#!/usr/bin/env python3
"""
run_partial_exit_backtest.py
===============================
Adam, 2026-08-10: "kupuję po 200, leci na 100 - lepiej uśrednić (dzisiejszy
Micro-Grid, jeden blendowany stop na całość) czy kupić nową paczkę po 100 i
sprzedać ją OSOBNO przy odbiciu, niezależnie od starych akcji?"

Porównuje produkcyjny Micro-Grid (backtest/microgrid_runner.py - wszystkie
nogi DCA łączą się w jedną pozycję ze średnią ważoną, jeden wspólny trailing
stop) z wariantem "niezależne nogi" (backtest/microgrid_partial_runner.py -
każda noga to osobny lot, własny trailing stop, sprzedawana osobno).

Parametry ODZWIERCIEDLAJĄ produkcję (żywy RiskSettings, sprawdzone
2026-08-09): max_dca_levels=7, dca_trigger_pct=0.02, take_profit_step_pct=
0.002, stop_loss_pct=0.02.

Przykład:
    python3 run_partial_exit_backtest.py --days 1300
"""

from __future__ import annotations

import argparse
import statistics
from decimal import Decimal

from backtest.data import app_context, fetch_candles
from backtest.microgrid_partial_runner import PartialPortfolio, run_microgrid_partial_backtest
from backtest.microgrid_runner import Asset, GridPortfolio, SettingsStub, run_microgrid_backtest

DEFAULT_TICKERS = (
    "AAPL_US_EQ,AIRp_EQ,ALVd_EQ,AMZN_US_EQ,ASML_US_EQ,ASMLa_EQ,BLK_US_EQ,BNPp_EQ,COST_US_EQ,"
    "CRM_US_EQ,DTEd_EQ,FB_US_EQ,FPp_EQ,GOOGL_US_EQ,IFXd_EQ,INGAa_EQ,IPOE_US_EQ,JNJ_US_EQ,"
    "JPM_US_EQ,KO_US_EQ,MA_US_EQ,MCp_EQ,MSFT_US_EQ,NFLX_US_EQ,NVDA_US_EQ,ORCL_US_EQ,PRXa_EQ,"
    "PYPL_US_EQ,SAFp_EQ,SANe_EQ,SAPd_EQ,SIEd_EQ,SPCX_US_EQ,SUp_EQ,TSLA_US_EQ,V_US_EQ,WMT_US_EQ,XOM_US_EQ"
)


def _currency(ticker: str) -> str:
    return "USD" if ticker.endswith("_US_EQ") else "EUR"


def main() -> None:
    parser = argparse.ArgumentParser(description="Micro-Grid: uśrednianie (blended) vs niezależne nogi DCA (partial exit).")
    parser.add_argument("--tickers", default=DEFAULT_TICKERS)
    parser.add_argument("--days", type=int, default=1300)
    parser.add_argument("--entry-amount", type=Decimal, default=Decimal("100"))
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--max-dca-levels", type=int, default=7)
    parser.add_argument("--dca-trigger-pct", type=Decimal, default=Decimal("0.02"))
    parser.add_argument("--dca-scenario", default="1,1,1,1,1,1,1")
    parser.add_argument("--take-profit-step-pct", type=Decimal, default=Decimal("0.002"))
    parser.add_argument("--stop-loss-pct", type=Decimal, default=Decimal("0.02"))
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    assets = [Asset(ticker=t, currency=_currency(t), entry_amount=args.entry_amount) for t in tickers]
    settings = SettingsStub(
        max_dca_levels=args.max_dca_levels, dca_trigger_pct=args.dca_trigger_pct,
        dca_scenario=args.dca_scenario, take_profit_step_pct=args.take_profit_step_pct,
        stop_loss_pct=args.stop_loss_pct,
    )

    with app_context():
        candles_by_ticker = {t: fetch_candles(t, args.days) for t in tickers}

    print(f"\n########## Okno {args.days} dni, {len(tickers)} tickerów ##########")

    blended = GridPortfolio(starting_cash=args.starting_cash)
    run_microgrid_backtest(assets, candles_by_ticker, blended, settings)
    b_trades = blended.closed_trades
    b_wins = [t for t in b_trades if t.pnl > 0]
    b_return = (blended.final_equity - args.starting_cash) / args.starting_cash * 100
    print(f"{'BLENDED (dzisiejszy Micro-Grid)':38s} transakcji={len(b_trades):4d}  win_rate={(len(b_wins)/len(b_trades)*100 if b_trades else 0):5.1f}%  "
          f"return={b_return:+7.2f}%  kapitał_końc={blended.final_equity:9.2f}  max_dd={blended.max_drawdown_pct:5.2f}%")

    partial = PartialPortfolio(starting_cash=args.starting_cash)
    run_microgrid_partial_backtest(assets, candles_by_ticker, partial, settings)
    p_trades = partial.closed_trades
    p_wins = [t for t in p_trades if t.pnl > 0]
    p_return = (partial.final_equity - args.starting_cash) / args.starting_cash * 100
    print(f"{'PARTIAL (niezależne nogi, osobny exit)':38s} transakcji={len(p_trades):4d}  win_rate={(len(p_wins)/len(p_trades)*100 if p_trades else 0):5.1f}%  "
          f"return={p_return:+7.2f}%  kapitał_końc={partial.final_equity:9.2f}  max_dd={partial.max_drawdown_pct:5.2f}%")

    by_reason: dict[str, int] = {}
    for t in p_trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print(f"PARTIAL powody zamknięcia: {by_reason}")

    dca_depths = [t.dca_level for t in b_trades]
    print(f"\nGłębokość DCA przy zamknięciu (BLENDED): średnia={statistics.mean(dca_depths) if dca_depths else 0:.2f}, max={max(dca_depths) if dca_depths else 0}")


if __name__ == "__main__":
    main()
