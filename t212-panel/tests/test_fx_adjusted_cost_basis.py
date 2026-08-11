"""
Self-check dla app/utils.py::fx_adjusted_cost_basis (2026-08-11, Adam:
"otwiera i zamyka po API niech dolicza FX, bo inaczej będę robił za darmo",
potem "sprawdz ile pobiera kosztow gielda francuska" -> dolozony FR FTT).
Uruchom: python3 tests/test_fx_adjusted_cost_basis.py
"""
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils import fx_adjusted_cost_basis


@dataclass
class FakeSettings:
    fx_cost_adjustment_enabled: bool
    fx_fee_pct: Decimal


def demo() -> None:
    on = FakeSettings(fx_cost_adjustment_enabled=True, fx_fee_pct=Decimal("0.0015"))
    off = FakeSettings(fx_cost_adjustment_enabled=False, fx_fee_pct=Decimal("0.0015"))

    # USD + enabled -> koszt bazowy podbity o round-trip (2x fee)
    assert fx_adjusted_cost_basis(Decimal("100"), "USD", on) == Decimal("100") * Decimal("1.003")
    # EUR (konto Adama jest w EUR) -> zero zmiany niezaleznie od enabled
    assert fx_adjusted_cost_basis(Decimal("100"), "EUR", on) == Decimal("100")
    # wylaczone w ustawieniach -> zero zmiany nawet dla USD
    assert fx_adjusted_cost_basis(Decimal("100"), "USD", off) == Decimal("100")
    # brak ustawien (None) -> zero zmiany, fail-safe
    assert fx_adjusted_cost_basis(Decimal("100"), "USD", None) == Decimal("100")

    # francuski ticker (FPp_EQ=TotalEnergies) -> +0.4% NIEZALEZNIE od
    # fx_cost_adjustment_enabled (to nie opcjonalny prog jak FX, to realny
    # podatek naliczany zawsze przy kupnie - potwierdzone eksportem T212)
    assert fx_adjusted_cost_basis(Decimal("100"), "EUR", on, "FPp_EQ") == Decimal("100") * Decimal("1.004")
    assert fx_adjusted_cost_basis(Decimal("100"), "EUR", off, "FPp_EQ") == Decimal("100") * Decimal("1.004")
    assert fx_adjusted_cost_basis(Decimal("100"), "EUR", None, "SUp_EQ") == Decimal("100") * Decimal("1.004")
    # ticker spoza whitelisty FTT -> zero zmiany mimo sufiksu "p_EQ" (celowo
    # twarda lista, nie heurystyka po sufiksie gieldy - patrz komentarz w utils.py)
    assert fx_adjusted_cost_basis(Decimal("100"), "EUR", on, "XYZp_EQ") == Decimal("100")

    print("OK - fx_adjusted_cost_basis")


if __name__ == "__main__":
    demo()
