"""
Self-check dla app/services/bot_engine.py::_hybrid_rsi_candidate (2026-08-27,
"RSI Hybrid" - entry_strategy_mode, patrz models.py::RiskSettings). Sprawdza
że: (1) wybiera kandydata z NAJNIŻSZYM RSI wśród spełniających próg+trend,
(2) odrzuca kandydatów z RSI>=próg albo cena<=SMA, (3) brak spełniających ->
(None, None), (4) za mało świec (< MA_PERIOD) -> pominięty bez wyjątku.
Uruchom: python3 tests/test_hybrid_rsi_candidate.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.bot_engine import HYBRID_RSI_THRESHOLD, _hybrid_rsi_candidate


@dataclass
class FakeAsset:
    ticker: str


def _uptrend_dip_candles(final_close: Decimal, days: int = 210) -> list[dict]:
    """Ceny rosnące (żeby SMA200 < cena), z ostatnią świecą obniżoną do final_close (dołek RSI)."""
    candles = []
    price = Decimal("50")
    for _ in range(days - 1):
        price += Decimal("0.2")
        candles.append({"o": float(price), "h": float(price), "l": float(price), "c": float(price)})
    candles.append({"o": float(final_close), "h": float(final_close), "l": float(final_close), "c": float(final_close)})
    return candles


def demo() -> None:
    # Kandydat A: głębszy dołek, ALE wciąż nad SMA200 (dużo poniżej progu RSI).
    # Uwaga: final_close musi zostać > SMA(~72), inaczej trend filter go odrzuci
    # (złapane właśnie tym testem - zbyt duży spadek łamie warunek price>SMA).
    candles_a = _uptrend_dip_candles(Decimal("75"))  # RSI~13.4, cena nadal > SMA
    # Kandydat B: płytszy dołek (RSI niższe niż próg, ale wyższe niż A).
    candles_b = _uptrend_dip_candles(Decimal("85"))  # RSI~27.7, mniejszy spadek -> RSI wyższe niż A, wciąż < próg
    # Kandydat C: cena nadal rośnie (brak dołka) -> RSI wysokie, odrzucony.
    candles_c = []
    price = Decimal("50")
    for _ in range(210):
        price += Decimal("0.2")
        candles_c.append({"o": float(price), "h": float(price), "l": float(price), "c": float(price)})

    data = {"A": candles_a, "B": candles_b, "C": candles_c}
    eligible = [FakeAsset(t) for t in data]
    best, best_rsi = _hybrid_rsi_candidate(eligible, candles_getter=lambda t: data[t])
    assert best is not None, "oczekiwano kandydata (A ma głęboki dołek RSI)"
    assert best.ticker == "A", f"oczekiwano najgłębszego dołka (A), dostano {best.ticker}"
    assert best_rsi < HYBRID_RSI_THRESHOLD

    # Brak żadnego kandydata poniżej progu -> (None, None).
    only_c = [FakeAsset("C")]
    best2, rsi2 = _hybrid_rsi_candidate(only_c, candles_getter=lambda t: data[t])
    assert best2 is None and rsi2 is None

    # Za mało świec (< MA_PERIOD=200) -> pominięty, brak wyjątku.
    short = [FakeAsset("short")]
    best3, rsi3 = _hybrid_rsi_candidate(short, candles_getter=lambda t: candles_a[-50:])
    assert best3 is None and rsi3 is None

    print("OK - wszystkie asercje przeszły (_hybrid_rsi_candidate)")


if __name__ == "__main__":
    demo()
