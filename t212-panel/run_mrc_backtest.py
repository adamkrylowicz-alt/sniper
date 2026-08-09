#!/usr/bin/env python3
"""
run_mrc_backtest.py
======================
Monte Carlo Reality Check dla Sygnalu (Adam, 2026-08-09 - trop z
financial-hacker.com "Why 90% of Backtests Fail"): czy dodatni backtest
Sygnalu (run_backtest.py) to prawdziwa przewaga z RSI-dip-buy timingu, czy
tylko efekt tego ze rynek w danym oknie ogolnie rosl (trend bias)?

Metoda: dla kazdego tickera zamieniamy KOLEJNOSC dziennych swiec na losowa,
zachowujac ksztalt kazdej swiecy (o/h/l/c WZGLEDEM poprzedniego zamkniecia)
i skladajac syntetyczna sciezke od tego samego punktu startowego. To niszczy
SEKWENCJE (autokorelacje, powrot po RSI-dolku w konkretnym miejscu w czasie)
ale zachowuje DOKLADNIE ten sam zbior dziennych zwrotow (wiec ten sam
calkowity dryf/trend rynku w tym oknie). Jesli strategia zarabia GLOWNIE
dzieki timingowi, real P&L powinien bic zdecydowana wiekszosc potasowanych
symulacji (niski p-value). Jesli zarabia glownie dzieki temu ze rynek rosl,
potasowane symulacje daja PODOBNY wynik co realne dane (wysoki p-value).

Wynik z 2026-08-09 (60 tickerow, parametry live SignalSettings RSI<35,
SL/TP=3xATR), udokumentowany w CLAUDE.md:
  400d:  real +184.47   vs potasowane srednia +21.99 (std 86.77)   p=0.020
  1300d: real +1242.49  vs potasowane srednia +578.64 (std 275.50) p=0.000
Oba okna p<0.05 - prawdziwy edge z timingu, nie sam trend bias.

Przyklad:
    python3 run_mrc_backtest.py --days 400 --n-sims 150
    python3 run_mrc_backtest.py --tickers AAPL_US_EQ,ASMLa_EQ --days 1300 --n-sims 50
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


def shuffle_candles(candles: list[dict], rng: random.Random) -> list[dict]:
    """Tasuje KOLEJNOSC dni, zachowujac ksztalt kazdej swiecy (o/h/l/c wzgledem
    poprzedniego zamkniecia) - patrz docstring modulu. Nierownosci h>=max(o,c)
    i l<=min(o,c) zachowane automatycznie (skalowanie przez ten sam dodatni
    wspolczynnik nie zmienia relacji)."""
    if len(candles) < 2:
        return list(candles)
    anchor = candles[0]
    ratios = []
    prev_c = candles[0]["c"]
    for day in candles[1:]:
        ratios.append((day["o"] / prev_c, day["h"] / prev_c, day["l"] / prev_c, day["c"] / prev_c))
        prev_c = day["c"]
    rng.shuffle(ratios)

    out = [anchor]
    prev_c = anchor["c"]
    for ro, rh, rl, rc in ratios:
        new_c = prev_c * rc
        out.append({"o": prev_c * ro, "h": prev_c * rh, "l": prev_c * rl, "c": new_c})
        prev_c = new_c
    return out


def total_pnl(
    candles_by_ticker: dict[str, list[dict]], entry_amount: Decimal,
    rsi_threshold: Decimal, sl_atr: Decimal, tp_atr: Decimal,
) -> Decimal:
    total = Decimal("0")
    for ticker, candles in candles_by_ticker.items():
        if not candles or len(candles) < 210:
            continue
        pf = Portfolio(starting_cash=Decimal("10000"))
        run_signal_backtest(
            ticker, candles, pf,
            entry_amount=entry_amount, rsi_threshold=rsi_threshold,
            stop_loss_atr_mult=sl_atr, take_profit_atr_mult=tp_atr,
        )
        total += sum((t.pnl for t in pf.closed_trades), Decimal("0"))
    return total


_worker_state: dict = {}


def _init_worker(candles_by_ticker: dict, entry_amount: Decimal, rsi_threshold: Decimal, sl_atr: Decimal, tp_atr: Decimal) -> None:
    """Wolane RAZ per worker process przy starcie Pool - unika przesylania
    (pickle) candles_by_ticker przy KAZDEJ symulacji, tylko raz na proces."""
    _worker_state["candles"] = candles_by_ticker
    _worker_state["entry_amount"] = entry_amount
    _worker_state["rsi_threshold"] = rsi_threshold
    _worker_state["sl_atr"] = sl_atr
    _worker_state["tp_atr"] = tp_atr


def _run_one_simulation(seed: int) -> float:
    rng = random.Random(seed)
    shuffled = {t: shuffle_candles(c, rng) for t, c in _worker_state["candles"].items()}
    return float(total_pnl(
        shuffled, _worker_state["entry_amount"], _worker_state["rsi_threshold"],
        _worker_state["sl_atr"], _worker_state["tp_atr"],
    ))


def run_mrc(
    tickers: list[str], days: int, n_sims: int, entry_amount: Decimal,
    rsi_threshold: Decimal, sl_atr: Decimal, tp_atr: Decimal, seed: int, workers: int,
) -> float:
    with app_context():
        candles_by_ticker = {}
        for t in tickers:
            try:
                candles_by_ticker[t] = fetch_candles(t, days)
            except RuntimeError:
                pass

    real_pnl = total_pnl(candles_by_ticker, entry_amount, rsi_threshold, sl_atr, tp_atr)

    # Symulacje sa od siebie calkowicie niezalezne (kazda tasuje wlasna kopie
    # danych, zero wspoldzielonego stanu) - idealny przypadek pod
    # multiprocessing.Pool, zamiast petli na jednym rdzeniu jak poprzednio
    # (Adam, 2026-08-09: "8 rdzeni zamiast 4 by pomoglo?" - dopiero jak kod
    # to wykorzysta, stad ta zmiana).
    t0 = time.monotonic()
    seeds = [seed + i for i in range(n_sims)]
    with Pool(processes=workers, initializer=_init_worker, initargs=(candles_by_ticker, entry_amount, rsi_threshold, sl_atr, tp_atr)) as pool:
        sim_results = pool.map(_run_one_simulation, seeds)
    elapsed = time.monotonic() - t0
    print(f"  [{days}d] {n_sims} symulacji na {workers} procesach w {elapsed:.1f}s ({elapsed/n_sims:.2f}s/symulacja efektywnie)")

    beats_or_equals = sum(1 for s in sim_results if s >= float(real_pnl))
    p_value = beats_or_equals / n_sims
    mean_sim = statistics.mean(sim_results)
    stdev_sim = statistics.stdev(sim_results) if len(sim_results) > 1 else 0

    print(f"\n########## MRC, okno {days} dni, {len(candles_by_ticker)} tickerow, {n_sims} symulacji ##########")
    print(f"Real P&L (chronologiczna kolejnosc):      {real_pnl:+9.2f}")
    print(f"Potasowane symulacje: srednia={mean_sim:+9.2f}  odch.std={stdev_sim:8.2f}  min={min(sim_results):+9.2f}  max={max(sim_results):+9.2f}")
    print(f"p-value (frakcja symulacji >= real):       {p_value:.3f}  ({beats_or_equals}/{n_sims})")
    if p_value < 0.05:
        print("=> p<0.05: real wynik BIJE zdecydowana wiekszosc losowych porzadkow - prawdopodobnie prawdziwy edge z timingu.")
    else:
        print("=> p>=0.05: real wynik NIE odstaje istotnie od losowego przetasowania - podejrzenie trend bias (rynek rosl, nie strategia wygrywa).")
    return p_value


def main() -> None:
    parser = argparse.ArgumentParser(description="Monte Carlo Reality Check dla strategii Sygnal.")
    parser.add_argument("--tickers", default=DEFAULT_TICKERS, help="Lista tickerow po przecinku")
    parser.add_argument("--days", type=int, default=400, help="Ile dni historii (min. ~210)")
    parser.add_argument("--n-sims", type=int, default=150, help="Liczba losowych przetasowan")
    parser.add_argument("--entry-amount", type=Decimal, default=Decimal("100"))
    parser.add_argument("--rsi-threshold", type=Decimal, default=Decimal("35"))
    parser.add_argument("--stop-loss-atr-mult", type=Decimal, default=Decimal("3.0"))
    parser.add_argument("--take-profit-atr-mult", type=Decimal, default=Decimal("3.0"))
    parser.add_argument("--seed", type=int, default=42, help="Seed RNG - reprodukowalnosc raportu")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="Liczba procesow rownoleglych (domyslnie wszystkie rdzenie)")
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    run_mrc(
        tickers, args.days, args.n_sims, args.entry_amount,
        args.rsi_threshold, args.stop_loss_atr_mult, args.take_profit_atr_mult, args.seed, args.workers,
    )


if __name__ == "__main__":
    main()
