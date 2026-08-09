#!/usr/bin/env python3
"""
run_sizing_backtest.py
=========================
Backtest pomysłu #5 z listy usprawnień Sygnału (Adam, 2026-08-10) -
vol-adjusted sizing (patrz backtest/signal_runner.py::vol_target_atr_pct):
mniejsza pozycja gdy ATR/cena wysoki (chwiejny rynek), większa gdy niski
(spokojny rynek), zamiast dzisiejszego stałego entry_amount.

`--vol-target` domyślnie None = auto: MEDIANA zaobserwowanego ATR%/cena na
całym uniwersum/oknie (zamiast zgadywać liczbę - "dowody przed strojeniem").

Przykład:
    python3 run_sizing_backtest.py --days 1300
"""

from __future__ import annotations

import argparse
import statistics
from decimal import Decimal

from app.services.signal_engine import ATR_PERIOD, MA_PERIOD, RSI_PERIOD, _compute_atr, _compute_rsi, _compute_sma
from backtest.data import app_context, fetch_candles
from backtest.portfolio import Portfolio
from backtest.signal_runner import run_signal_backtest

DEFAULT_TICKERS = (
    "AAPL_US_EQ,ADa_EQ,AIRp_EQ,ALVd_EQ,AMZN_US_EQ,ASMa_EQ,ASMLa_EQ,ASML_US_EQ,BAC_US_EQ,BASd_EQ,"
    "BAYNd_EQ,BLK_US_EQ,BMWd_EQ,BNp_EQ,BNPp_EQ,COST_US_EQ,CRM_US_EQ,DAId_EQ,DBKd_EQ,DGp_EQ,"
    "DIS_US_EQ,DTEd_EQ,ENL1d_EQ,EOANd_EQ,FB_US_EQ,FPp_EQ,GOOGL_US_EQ,IBEe_EQ,IFXd_EQ,INGAa_EQ,"
    "IPOE_US_EQ,JNJ_US_EQ,JPM_US_EQ,KERp_EQ,KO_US_EQ,MA_US_EQ,MCp_EQ,MSFT_US_EQ,NESRd1_EQ,NFLX_US_EQ,"
    "NOTd1_EQ,NVDA_US_EQ,ORCL_US_EQ,ORp_EQ,PRXa_EQ,PYPL_US_EQ,RHOd_EQ,RWEd_EQ,SAFp_EQ,SANe_EQ,"
    "SAPd_EQ,SIEd_EQ,SPCX_US_EQ,SUp_EQ,TSLA_US_EQ,UNIAa_EQ,VOWd_EQ,V_US_EQ,WMT_US_EQ,XOM_US_EQ"
)

ENTRY_AMOUNT = Decimal("100")
RSI_THRESHOLD = Decimal("35")
SL_ATR = Decimal("3.0")
TP_ATR = Decimal("3.0")


def _median_atr_pct(candles_by_ticker: dict) -> Decimal:
    """Mediana ATR/cena na dniach GDZIE Sygnał faktycznie rozważa wejście
    (poza pozycją, po min_bars) - żeby cel odzwierciedlał zmienność w
    momencie decyzji, nie zmienność w ogóle (może się różnić - np. dni
    tuż po IPO są bardziej chwiejne, ale i tak odpadają przez min_bars)."""
    min_bars = max(MA_PERIOD, RSI_PERIOD + 1, ATR_PERIOD + 1)
    values = []
    for candles in candles_by_ticker.values():
        if len(candles) < min_bars + 1:
            continue
        all_closes = [Decimal(str(c["c"])) for c in candles]
        for day in range(min_bars, len(candles)):
            window = candles[max(0, day + 1 - min_bars - 5): day + 1]
            atr = _compute_atr(window, ATR_PERIOD)
            price = all_closes[day]
            if atr is not None and price > 0:
                values.append(atr / price)
    return Decimal(str(statistics.median(values))) if values else Decimal("0.02")


def run_variant(candles_by_ticker: dict, vol_target: Decimal | None):
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
            vol_target_atr_pct=vol_target,
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
    parser = argparse.ArgumentParser(description="Backtest vol-adjusted sizing dla Sygnału.")
    parser.add_argument("--tickers", default=DEFAULT_TICKERS)
    parser.add_argument("--days", type=int, default=1300)
    parser.add_argument("--vol-target", type=Decimal, default=None, help="Domyślnie: mediana ATR%% obserwowana w danych")
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    with app_context():
        candles_by_ticker = {t: fetch_candles(t, args.days) for t in tickers}

    vol_target = args.vol_target if args.vol_target is not None else _median_atr_pct(candles_by_ticker)
    print(f"Cel ATR%% (mediana z danych): {vol_target * 100:.2f}%")

    print(f"\n########## Okno {args.days} dni, {len(candles_by_ticker)} tickerów ##########")
    for label, target in (("BASELINE (stały entry_amount)", None), ("VOL-ADJUSTED SIZING", vol_target)):
        n, wr, pnl, dd = run_variant(candles_by_ticker, target)
        print(f"{label:32s} transakcji={n:4d}  win_rate={wr:5.1f}%  total_pnl={pnl:+9.2f}  śr.max_drawdown={dd:5.2f}%")


if __name__ == "__main__":
    main()
