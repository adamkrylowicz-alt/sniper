#!/usr/bin/env python3
"""
run_regime_backtest.py
=========================
Backtest pomysłu #1 z listy usprawnień Sygnału (Adam, 2026-08-09) - filtr
reżimu rynkowego (patrz backtest/regime.py, artykuł "The Market Regime
Filter" na financial-hacker.com) jako mnożnik `entry_amount`: 1.0 gdy
trend+zmienność+kredyt się zgadzają (SPY>SMA200, VIX<VIX3M, z-score HYG/IEF
>-2), 0.5 gdy 2 z 3, 0.0 (brak wejść) gdy <2.

OGRANICZENIE (patrz docstring regime.py): dopasowanie dzień-do-dnia między
serią rynkową (SPY/VIX/...) a świecami tickera jest po INDEKSIE, nie
kalendarzu - wiarygodne tylko dla tickerów `*_US_EQ` (ten sam kalendarz
sesji NYSE). Domyślna lista tickerów tu to WYŁĄCZNIE podzbiór US z
uniwersum run_mrc_backtest.py.

Przykład:
    python3 run_regime_backtest.py --days 1300
"""

from __future__ import annotations

import argparse
import statistics
from decimal import Decimal

from backtest.data import app_context, fetch_candles
from backtest.portfolio import Portfolio
from backtest.regime import fetch_regime_multipliers
from backtest.signal_runner import run_signal_backtest

DEFAULT_US_TICKERS = (
    "AAPL_US_EQ,AMZN_US_EQ,ASML_US_EQ,BAC_US_EQ,BLK_US_EQ,COST_US_EQ,CRM_US_EQ,DIS_US_EQ,"
    "FB_US_EQ,GOOGL_US_EQ,IPOE_US_EQ,JNJ_US_EQ,JPM_US_EQ,KO_US_EQ,MA_US_EQ,MSFT_US_EQ,"
    "NFLX_US_EQ,NVDA_US_EQ,ORCL_US_EQ,PYPL_US_EQ,SPCX_US_EQ,TSLA_US_EQ,V_US_EQ,WMT_US_EQ,XOM_US_EQ"
)

ENTRY_AMOUNT = Decimal("100")
RSI_THRESHOLD = Decimal("35")
SL_ATR = Decimal("3.0")
TP_ATR = Decimal("3.0")


def run_variant(candles_by_ticker: dict, multiplier: list[Decimal] | None):
    total_trades = 0
    total_wins = 0
    total_pnl = Decimal("0")
    drawdowns = []
    for ticker, candles in candles_by_ticker.items():
        if not candles or len(candles) < 210:
            continue
        pf = Portfolio(starting_cash=Decimal("10000"))
        run_signal_backtest(
            ticker, candles, pf,
            entry_amount=ENTRY_AMOUNT, rsi_threshold=RSI_THRESHOLD,
            stop_loss_atr_mult=SL_ATR, take_profit_atr_mult=TP_ATR,
            entry_multiplier=multiplier,
        )
        trades = pf.closed_trades
        wins = [t for t in trades if t.pnl > 0]
        total_trades += len(trades)
        total_wins += len(wins)
        total_pnl += sum((t.pnl for t in trades), Decimal("0"))
        drawdowns.append(float(pf.max_drawdown_pct))
    win_rate = (total_wins / total_trades * 100) if total_trades else 0
    avg_dd = statistics.mean(drawdowns) if drawdowns else 0
    return total_trades, win_rate, total_pnl, avg_dd


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest filtru reżimu rynkowego dla Sygnału (US tickery).")
    parser.add_argument("--tickers", default=DEFAULT_US_TICKERS, help="Lista tickerów US po przecinku")
    parser.add_argument("--days", type=int, default=1300)
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    with app_context():
        candles_by_ticker = {}
        for t in tickers:
            try:
                candles_by_ticker[t] = fetch_candles(t, args.days)
            except RuntimeError:
                pass

        print("Pobieranie serii reżimu rynkowego (SPY/VIX/VIX3M/HYG/IEF)...")
        multipliers = fetch_regime_multipliers(args.days)

    if multipliers is None:
        print("BŁĄD: nie udało się pobrać jednego z SPY/VIX/VIX3M/HYG/IEF z Yahoo.")
        return

    green = multipliers.count(Decimal("1.0"))
    orange = multipliers.count(Decimal("0.5"))
    red = multipliers.count(Decimal("0.0"))
    print(f"Reżim na {len(multipliers)} dniach: zielony(1.0x)={green} ({green/len(multipliers)*100:.0f}%), "
          f"pomarańczowy(0.5x)={orange} ({orange/len(multipliers)*100:.0f}%), "
          f"czerwony(0.0x)={red} ({red/len(multipliers)*100:.0f}%)")

    print(f"\n########## Okno {args.days} dni, {len(candles_by_ticker)} tickerów US ##########")
    for label, mult in (("BASELINE (bez filtra)", None), ("Z FILTREM REŻIMU", multipliers)):
        n, wr, pnl, dd = run_variant(candles_by_ticker, mult)
        print(f"{label:25s} transakcji={n:4d}  win_rate={wr:5.1f}%  total_pnl={pnl:+9.2f}  śr.max_drawdown={dd:5.2f}%")


if __name__ == "__main__":
    main()
