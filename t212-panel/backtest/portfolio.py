"""
backtest/portfolio.py
=======================
Symulacja portfela dla backtestu - gotówka, otwarte pozycje, poślizg
(slippage), prowizja, krzywa kapitału i max drawdown. Zero zależności od
Flask/T212/bazy - czysty stan w pamięci, jak `signal_strategy.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class ClosedTrade:
    ticker: str
    entry_day: int
    exit_day: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    exit_reason: str
    pnl: Decimal
    pnl_pct: Decimal


@dataclass
class OpenPosition:
    ticker: str
    entry_day: int
    entry_price: Decimal
    quantity: Decimal
    stop_loss_price: Decimal
    # Opcjonalne - tylko EOD (eod_runner.py) ma osobny take-profit (rynkowa
    # sprzedaż natychmiast po dotarciu do poziomu, nie resting order jak SL);
    # Sygnał (signal_runner.py) go nie ustawia (wychodzi WYŁĄCZNIE trailing
    # stopem, patrz signal_engine.py - sztywny TP świadomie usunięty 28.07).
    take_profit_price: Decimal | None = None


class Portfolio:
    """
    `starting_cash`: kapitał startowy. `slippage_pct`: niekorzystny poślizg
    zastosowany do KAŻDEGO fillu (kupno drożej, sprzedaż taniej) - domyślnie
    0.05%, konserwatywny szacunek dla płynnych blue-chipów. `commission_pct`:
    prowizja per transakcja, domyślnie 0 (T212 equities bez prowizji w tej
    appce - patrz docstring t212_client.py, nic tu nie zmyślamy).
    """

    def __init__(
        self, starting_cash: Decimal,
        slippage_pct: Decimal = Decimal("0.0005"),
        commission_pct: Decimal = Decimal("0"),
    ):
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.slippage_pct = slippage_pct
        self.commission_pct = commission_pct
        self.open_positions: dict[str, OpenPosition] = {}
        self.closed_trades: list[ClosedTrade] = []
        self._equity_curve: list[Decimal] = [starting_cash]
        self._peak_equity = starting_cash
        self.max_drawdown_pct = Decimal("0")

    def buy(
        self, ticker: str, day: int, price: Decimal, quantity: Decimal, stop_loss_price: Decimal,
        take_profit_price: Decimal | None = None,
    ) -> None:
        if ticker in self.open_positions:
            raise ValueError(f"{ticker}: już otwarta pozycja, nie mogę kupić drugiej (Sygnał/EOD nie robią DCA).")
        fill_price = price * (1 + self.slippage_pct)
        cost = fill_price * quantity
        commission = cost * self.commission_pct
        self.cash -= (cost + commission)
        self.open_positions[ticker] = OpenPosition(
            ticker=ticker, entry_day=day, entry_price=fill_price,
            quantity=quantity, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
        )

    def sell(self, ticker: str, day: int, price: Decimal, reason: str) -> None:
        pos = self.open_positions.pop(ticker)
        fill_price = price * (1 - self.slippage_pct)
        proceeds = fill_price * pos.quantity
        commission = proceeds * self.commission_pct
        self.cash += (proceeds - commission)
        pnl = (fill_price - pos.entry_price) * pos.quantity
        pnl_pct = (fill_price - pos.entry_price) / pos.entry_price * 100
        self.closed_trades.append(ClosedTrade(
            ticker=ticker, entry_day=pos.entry_day, exit_day=day,
            entry_price=pos.entry_price, exit_price=fill_price,
            quantity=pos.quantity, exit_reason=reason, pnl=pnl, pnl_pct=pnl_pct,
        ))

    def mark_to_market(self, prices: dict[str, Decimal]) -> None:
        """Wołane raz na koniec każdego dnia - aktualizuje krzywą kapitału/drawdown."""
        equity = self.cash
        for pos in self.open_positions.values():
            equity += prices.get(pos.ticker, pos.entry_price) * pos.quantity
        self._equity_curve.append(equity)
        self._peak_equity = max(self._peak_equity, equity)
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - equity) / self._peak_equity * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown)

    @property
    def final_equity(self) -> Decimal:
        return self._equity_curve[-1]
