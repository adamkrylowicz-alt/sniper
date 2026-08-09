"""
backtest/shuffle.py
======================
Tasowanie kolejności dziennych świec do Monte Carlo Reality Check (patrz
run_mrc_backtest.py dla Sygnału, run_mrc_microgrid.py dla Micro-Gridu) -
wyciągnięte 2026-08-09 z run_mrc_backtest.py, żeby nie duplikować identycznej
funkcji po raz drugi.
"""

from __future__ import annotations

import random


def shuffle_candles(candles: list[dict], rng: random.Random) -> list[dict]:
    """
    Tasuje KOLEJNOŚĆ dni, zachowując kształt każdej świecy (o/h/l/c względem
    poprzedniego zamknięcia) i odtwarzając syntetyczną ścieżkę ceny przez
    składanie od tego samego punktu startowego - niszczy SEKWENCJE
    (autokorelacje, powrót po dołku w konkretnym miejscu w czasie) ale
    zachowuje DOKŁADNIE ten sam zbiór dziennych zwrotów (ten sam całkowity
    dryf/trend). Nierówności h>=max(o,c) i l<=min(o,c) zachowane automatycznie
    (skalowanie przez ten sam dodatni współczynnik nie zmienia relacji).
    """
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
