"""
backtest/microgrid_partial_runner.py
=======================================
Wariant Micro-Gridu z NIEZALEŻNYMI nogami DCA (Adam, 2026-08-10 - "kupuję po
200, leci na 100, czy lepiej uśrednić czy kupić nową paczkę po 100 i sprzedać
osobno przy odbiciu do 180?"). W odróżnieniu od produkcyjnego zachowania
(microgrid_runner.py - WSZYSTKIE nogi DCA łączą się w JEDNĄ pozycję ze
średnią ważoną ceną i JEDNYM wspólnym trailing stopem, sprzedawaną razem),
tu KAŻDA noga (level-0 i każdy DCA) to OSOBNY lot z WŁASNYM entry_price i
WŁASNYM trailing stopem - sprzedawana niezależnie, gdy TYLKO ONA odbije o
take_profit_step_pct, niezależnie od tego co robią pozostałe nogi tego
samego tickera.

Reużywa DOKŁADNIE tej samej matematyki co produkcja/microgrid_runner.py
(microgrid_strategy.compute_trailing_stop/compute_exhausted_dca_floor,
bot_entry_filters.rank_candidates) - jedyna różnica to co się dzieje z
WYNIKIEM tej matematyki: per-lot zamiast per-pozycja-zblendowana.

`grid_anchor_price` (próg następnej nogi DCA) liczony jak dotychczas od
CENY WEJŚCIA LEVEL-0 (niezmienny) - to nie się zmienia, zmienia się tylko
zarządzanie WYJŚCIEM.

UWAGA: to NIE jest zachowanie produkcyjnego bota - eksperymentalny silnik
wyłącznie do porównania w backteście, patrz run_partial_exit_backtest.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.services import bot_entry_filters
from app.services.bot_engine import (
    ATR_PERIOD,
    ATR_STOP_MULTIPLIER,
    FX_ROUND_TRIP_PCT,
    MIN_TRAIL_REQUOTE_FRACTION,
    _compute_atr,
    _dca_multiplier,
    _parse_dca_scenario,
)
from app.services.strategy import microgrid_strategy

from .microgrid_runner import MAX_CONCURRENT_POSITIONS, Asset, CANDLES_GETTER_WINDOW_ROWS, SettingsStub


@dataclass
class Lot:
    entry_day: int
    entry_price: Decimal
    quantity: Decimal
    dca_level: int
    stop_target_price: Decimal | None = None


@dataclass
class TickerState:
    ticker: str
    currency: str
    lots: list[Lot] = field(default_factory=list)
    grid_anchor_price: Decimal = Decimal("0")
    dca_level: int = 0  # ostatni ZAJĘTY poziom (level-0 = 0)


@dataclass
class ClosedLot:
    ticker: str
    entry_day: int
    exit_day: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    dca_level: int
    exit_reason: str
    pnl: Decimal
    pnl_pct: Decimal


class PartialPortfolio:
    def __init__(self, starting_cash: Decimal, slippage_pct: Decimal = Decimal("0.0005")):
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.slippage_pct = slippage_pct
        self.open_positions: dict[str, TickerState] = {}
        self.closed_trades: list[ClosedLot] = []
        self._equity_curve: list[Decimal] = [starting_cash]
        self._peak_equity = starting_cash
        self.max_drawdown_pct = Decimal("0")

    def open_level0(self, ticker: str, currency: str, day: int, price: Decimal, quantity: Decimal) -> None:
        fill_price = price * (1 + self.slippage_pct)
        cost = fill_price * quantity
        if cost > self.cash:
            return
        self.cash -= cost
        self.open_positions[ticker] = TickerState(
            ticker=ticker, currency=currency, grid_anchor_price=fill_price,
            lots=[Lot(entry_day=day, entry_price=fill_price, quantity=quantity, dca_level=0)],
        )

    def add_dca_leg(self, ticker: str, price: Decimal, amount: Decimal, day: int) -> None:
        state = self.open_positions[ticker]
        fill_price = price * (1 + self.slippage_pct)
        leg_qty = (amount / fill_price).quantize(Decimal("0.0001"))
        if leg_qty <= 0:
            return
        cost = leg_qty * fill_price
        if cost > self.cash:
            return
        self.cash -= cost
        state.dca_level += 1
        state.lots.append(Lot(entry_day=day, entry_price=fill_price, quantity=leg_qty, dca_level=state.dca_level))

    def close_lot(self, ticker: str, lot: Lot, day: int, price: Decimal, reason: str) -> None:
        state = self.open_positions[ticker]
        fill_price = price * (1 - self.slippage_pct)
        proceeds = fill_price * lot.quantity
        cost = lot.entry_price * lot.quantity
        self.cash += proceeds
        pnl = proceeds - cost
        pnl_pct = pnl / cost * 100
        self.closed_trades.append(ClosedLot(
            ticker=ticker, entry_day=lot.entry_day, exit_day=day,
            entry_price=lot.entry_price, exit_price=fill_price, quantity=lot.quantity,
            dca_level=lot.dca_level, exit_reason=reason, pnl=pnl, pnl_pct=pnl_pct,
        ))
        state.lots.remove(lot)
        if not state.lots:
            del self.open_positions[ticker]

    def mark_to_market(self, prices: dict[str, Decimal]) -> None:
        equity = self.cash
        for state in self.open_positions.values():
            price = prices.get(state.ticker)
            for lot in state.lots:
                equity += (price if price is not None else lot.entry_price) * lot.quantity
        self._equity_curve.append(equity)
        self._peak_equity = max(self._peak_equity, equity)
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - equity) / self._peak_equity * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown)

    @property
    def final_equity(self) -> Decimal:
        return self._equity_curve[-1]


def run_microgrid_partial_backtest(
    assets: list[Asset],
    candles_by_ticker: dict[str, list[dict]],
    portfolio: PartialPortfolio,
    settings: SettingsStub,
    exhausted_floor_atr_mult: Decimal | None = ATR_STOP_MULTIPLIER,
) -> None:
    """Lustro run_microgrid_backtest (microgrid_runner.py) - identyczne
    wyrównanie kalendarzowe/kolejność exit->DCA->nowe wejście, jedyna różnica
    to per-lot zamiast per-pozycja-zblendowana obsługa wyjścia (patrz
    docstring modułu).

    `exhausted_floor_atr_mult` (dodane 2026-08-10, eksperyment - Adam: "chcę
    zminimalizować liczbę stratnych pozycji zamykanych na minus"): mnożnik
    ATR dla awaryjnego stopu po wyczerpaniu wszystkich poziomów DCA bez
    odbicia. Domyślnie `ATR_STOP_MULTIPLIER` (1.8x, jak produkcja). Większa
    wartość (np. 5, 10) = szerszy bufor, rzadsze wymuszone cięcia, ale
    głębsza pojedyncza strata gdy JEDNAK się wykona. `None` = BRAK
    awaryjnego stopu - wyczerpana noga zostaje otwarta bez ograniczeń,
    czeka na własny target niezależnie jak długo to zajmie (żadnej siatki
    bezpieczeństwa - skrajny wariant do porównania, NIE zalecany na żywo
    bez tego backtestu).
    """
    min_bars = ATR_PERIOD + 1
    tickers = [a.ticker for a in assets]
    asset_by_ticker = {a.ticker: a for a in assets}
    total_days = max(len(c) for c in candles_by_ticker.values())
    offsets = {t: total_days - len(candles_by_ticker[t]) for t in tickers}

    for calendar_day in range(min_bars, total_days):
        day_prices: dict[str, Decimal] = {}
        day_lows: dict[str, Decimal] = {}
        windows: dict[str, list[dict]] = {}
        for ticker in tickers:
            local_day = calendar_day - offsets[ticker]
            if local_day < min_bars:
                continue
            window = candles_by_ticker[ticker][: local_day + 1]
            windows[ticker] = window
            day_prices[ticker] = Decimal(str(window[-1]["c"]))
            day_lows[ticker] = Decimal(str(window[-1]["l"]))

        # --- 1. Exit per LOT (nie per pozycja) ---
        for ticker in list(portfolio.open_positions.keys()):
            state = portfolio.open_positions[ticker]
            price = day_prices[ticker]
            fx_mult = (1 + FX_ROUND_TRIP_PCT) if state.currency == "USD" else Decimal("1")
            atr = _compute_atr(windows[ticker], ATR_PERIOD)
            arm_atr_distance = atr * ATR_STOP_MULTIPLIER if atr is not None else None
            floor_atr_distance = (
                atr * exhausted_floor_atr_mult if atr is not None and exhausted_floor_atr_mult is not None else None
            )
            exhausted = state.dca_level >= settings.max_dca_levels - 1

            for lot in list(state.lots):
                ref_price = lot.entry_price * fx_mult
                milestone_steps = microgrid_strategy.compute_milestone_steps(ref_price, price, settings.take_profit_step_pct)
                if milestone_steps >= 2:
                    is_first_arm = lot.stop_target_price is None
                    candidate_stop = microgrid_strategy.compute_trailing_stop(
                        is_first_arm=is_first_arm, ref_price=ref_price, current_price=price,
                        step=settings.take_profit_step_pct, existing_stop_target=lot.stop_target_price,
                        atr_distance=arm_atr_distance if is_first_arm else None, stop_loss_pct=settings.stop_loss_pct,
                        min_requote_fraction=MIN_TRAIL_REQUOTE_FRACTION,
                    )
                    if candidate_stop is not None:
                        lot.stop_target_price = candidate_stop
                elif exhausted and lot.stop_target_price is None and exhausted_floor_atr_mult is not None:
                    lot.stop_target_price = microgrid_strategy.compute_exhausted_dca_floor(
                        price, floor_atr_distance, settings.stop_loss_pct,
                    )

                if lot.stop_target_price is not None and day_lows[ticker] <= lot.stop_target_price:
                    reason = "trailing-stop" if milestone_steps >= 2 else "exhausted-floor"
                    portfolio.close_lot(ticker, lot, calendar_day, lot.stop_target_price, reason)

        # --- 2. DCA dla pozycji, ktore przezyly krok 1 ---
        for ticker in list(portfolio.open_positions.keys()):
            state = portfolio.open_positions[ticker]
            if state.dca_level >= settings.max_dca_levels - 1:
                continue
            next_level = state.dca_level + 1
            trigger_price = state.grid_anchor_price * (Decimal("1") - settings.dca_trigger_pct * next_level)
            if trigger_price <= 0 or day_lows[ticker] > trigger_price:
                continue

            if microgrid_strategy.SHOCK_FILTER_ENABLED:
                atr = _compute_atr(windows[ticker], ATR_PERIOD)
                atr_pct = (atr / trigger_price) if atr is not None and trigger_price > 0 else None
                max_drop = microgrid_strategy.compute_max_recent_single_day_drop_pct(windows[ticker])
                if microgrid_strategy.is_shock(max_drop, atr_pct):
                    continue

            multipliers = _parse_dca_scenario(settings.dca_scenario)
            multiplier = _dca_multiplier(multipliers, next_level)
            asset = asset_by_ticker[ticker]
            effective_entry_amount = asset.entry_amount
            if settings.equity_sizing_enabled:
                effective_entry_amount = microgrid_strategy.compute_equity_scaled_amount(
                    asset.entry_amount, portfolio.final_equity, portfolio.starting_cash,
                )
            portfolio.add_dca_leg(ticker, trigger_price, effective_entry_amount * multiplier, calendar_day)

        # --- 3. Jedno nowe wejscie level-0 do wolnego slotu ---
        if len(portfolio.open_positions) < MAX_CONCURRENT_POSITIONS:
            eligible = [
                a for a in assets
                if a.ticker not in portfolio.open_positions and a.ticker in day_prices
            ]
            if eligible:
                def candles_getter(ticker: str, _windows=windows) -> list[dict]:
                    w = _windows[ticker]
                    return w[-CANDLES_GETTER_WINDOW_ROWS:]

                scored, _stats = bot_entry_filters.rank_candidates(eligible, settings, candles_getter=candles_getter)
                if scored:
                    winner = scored[0][0]
                    price = day_prices[winner.ticker]
                    effective_entry_amount = winner.entry_amount
                    if settings.equity_sizing_enabled:
                        effective_entry_amount = microgrid_strategy.compute_equity_scaled_amount(
                            winner.entry_amount, portfolio.final_equity, portfolio.starting_cash,
                        )
                    try:
                        decision = microgrid_strategy.compute_entry_quantity(effective_entry_amount, price)
                    except microgrid_strategy.EntryValidationError:
                        decision = None
                    if decision is not None:
                        portfolio.open_level0(winner.ticker, winner.currency, calendar_day, price, decision.quantity)

        portfolio.mark_to_market(day_prices)
