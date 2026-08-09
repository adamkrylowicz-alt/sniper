#!/usr/bin/env python3
"""
run_mrc_microgrid.py
=======================
Monte Carlo Reality Check dla Micro-Gridu (Adam, 2026-08-09 - pomysł #3 z
listy usprawnień, ten sam trop co run_mrc_backtest.py dla Sygnału: czy
dodatni backtest DCA-grida to prawdziwa przewaga (kupowanie dołków +
trailing exit dobrze wyczasowane), czy tylko efekt tego że rynek w oknie
ogólnie rósł? Micro-Grid jest z natury BARDZIEJ podatny na to pytanie niż
Sygnał - dokupuje (DCA) do pozycji która idzie przeciwko niemu, więc w
trwałej bessie mógłby tracić znacznie mocniej niż Sygnał (który ma twardy
per-ticker stop-loss i nie dokupuje).

Metoda identyczna jak run_mrc_backtest.py (patrz backtest/shuffle.py) -
tasujemy KOLEJNOŚĆ dziennych świec per ticker (zachowując kształt każdej),
niszcząc sekwencje ale zachowując dokładnie ten sam zbiór dziennych zwrotów.
RÓŻNICA względem Sygnału: Micro-Grid to JEDEN WSPÓLNY portfel (konkurs o
sloty, patrz microgrid_runner.py), więc real_pnl/symulacje liczone jako
(final_equity - starting_cash) tego jednego portfela, nie suma osobnych
portfeli per ticker.

Domyślne parametry ODZWIERCIEDLAJĄ produkcję (sprawdzone w żywym
RiskSettings, 2026-08-09): max_dca_levels=7, dca_trigger_pct=0.02,
take_profit_step_pct=0.002, stop_loss_pct=0.02 - w odróżnieniu od
run_microgrid_backtest.py, którego defaulty są STARE (5/0.05/0.003/0.02,
sprzed strojenia z 2026-08-02/03) - tu podane jawnie żeby nie powielić tej
nieaktualności.

Przykład:
    python3 run_mrc_microgrid.py --days 400 --n-sims 100
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import time
from decimal import Decimal
from multiprocessing import Pool

from backtest.data import app_context, fetch_candles
from backtest.microgrid_runner import Asset, GridPortfolio, SettingsStub, run_microgrid_backtest
from backtest.shuffle import shuffle_candles

DEFAULT_TICKERS = (
    "AAPL_US_EQ,AIRp_EQ,ALVd_EQ,AMZN_US_EQ,ASML_US_EQ,ASMLa_EQ,BLK_US_EQ,BNPp_EQ,COST_US_EQ,"
    "CRM_US_EQ,DTEd_EQ,FB_US_EQ,FPp_EQ,GOOGL_US_EQ,IFXd_EQ,INGAa_EQ,IPOE_US_EQ,JNJ_US_EQ,"
    "JPM_US_EQ,KO_US_EQ,MA_US_EQ,MCp_EQ,MSFT_US_EQ,NFLX_US_EQ,NVDA_US_EQ,ORCL_US_EQ,PRXa_EQ,"
    "PYPL_US_EQ,SAFp_EQ,SANe_EQ,SAPd_EQ,SIEd_EQ,SPCX_US_EQ,SUp_EQ,TSLA_US_EQ,V_US_EQ,WMT_US_EQ,XOM_US_EQ"
)


def _currency(ticker: str) -> str:
    return "USD" if ticker.endswith("_US_EQ") else "EUR"


def run_once(
    tickers: list[str], candles_by_ticker: dict, entry_amount: Decimal,
    starting_cash: Decimal, settings: SettingsStub,
) -> Decimal:
    assets = [Asset(ticker=t, currency=_currency(t), entry_amount=entry_amount) for t in tickers]
    portfolio = GridPortfolio(starting_cash=starting_cash)
    run_microgrid_backtest(assets, candles_by_ticker, portfolio, settings)
    return portfolio.final_equity - starting_cash


_worker_state: dict = {}


def _init_worker(tickers, candles_by_ticker, entry_amount, starting_cash, settings) -> None:
    _worker_state["tickers"] = tickers
    _worker_state["candles"] = candles_by_ticker
    _worker_state["entry_amount"] = entry_amount
    _worker_state["starting_cash"] = starting_cash
    _worker_state["settings"] = settings


def _run_one_simulation(seed: int) -> float:
    rng = random.Random(seed)
    shuffled = {t: shuffle_candles(c, rng) for t, c in _worker_state["candles"].items()}
    return float(run_once(
        _worker_state["tickers"], shuffled, _worker_state["entry_amount"],
        _worker_state["starting_cash"], _worker_state["settings"],
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Monte Carlo Reality Check dla Micro-Gridu.")
    parser.add_argument("--tickers", default=DEFAULT_TICKERS)
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--n-sims", type=int, default=100)
    parser.add_argument("--entry-amount", type=Decimal, default=Decimal("100"))
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--max-dca-levels", type=int, default=7)
    parser.add_argument("--dca-trigger-pct", type=Decimal, default=Decimal("0.02"))
    parser.add_argument("--dca-scenario", default="1,1,1,1,1,1,1")
    parser.add_argument("--take-profit-step-pct", type=Decimal, default=Decimal("0.002"))
    parser.add_argument("--stop-loss-pct", type=Decimal, default=Decimal("0.02"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    settings = SettingsStub(
        max_dca_levels=args.max_dca_levels, dca_trigger_pct=args.dca_trigger_pct,
        dca_scenario=args.dca_scenario, take_profit_step_pct=args.take_profit_step_pct,
        stop_loss_pct=args.stop_loss_pct,
    )

    with app_context():
        candles_by_ticker = {t: fetch_candles(t, args.days) for t in tickers}

    real_pnl = run_once(tickers, candles_by_ticker, args.entry_amount, args.starting_cash, settings)

    t0 = time.monotonic()
    seeds = [args.seed + i for i in range(args.n_sims)]
    with Pool(
        processes=args.workers, initializer=_init_worker,
        initargs=(tickers, candles_by_ticker, args.entry_amount, args.starting_cash, settings),
    ) as pool:
        sim_results = pool.map(_run_one_simulation, seeds)
    elapsed = time.monotonic() - t0
    print(f"  [{args.days}d] {args.n_sims} symulacji na {args.workers} procesach w {elapsed:.1f}s ({elapsed/args.n_sims:.2f}s/symulację)")

    beats_or_equals = sum(1 for s in sim_results if s >= float(real_pnl))
    p_value = beats_or_equals / args.n_sims
    mean_sim = statistics.mean(sim_results)
    stdev_sim = statistics.stdev(sim_results) if len(sim_results) > 1 else 0

    print(f"\n########## MRC Micro-Grid, okno {args.days} dni, {len(tickers)} tickerów, {args.n_sims} symulacji ##########")
    print(f"Real P&L (chronologiczna kolejność):       {real_pnl:+9.2f}")
    print(f"Potasowane symulacje: średnia={mean_sim:+9.2f}  odch.std={stdev_sim:8.2f}  min={min(sim_results):+9.2f}  max={max(sim_results):+9.2f}")
    print(f"p-value (frakcja symulacji >= real):        {p_value:.3f}  ({beats_or_equals}/{args.n_sims})")
    if p_value < 0.05:
        print("=> p<0.05: real wynik BIJE zdecydowaną większość losowych porządków - prawdopodobnie prawdziwy edge, nie tylko trend bias.")
    else:
        print("=> p>=0.05: real wynik NIE odstaje istotnie od losowego przetasowania - podejrzenie trend bias (rynek rósł, nie strategia wygrywa).")


if __name__ == "__main__":
    main()
