"""
backtest/microgrid_runner.py
===============================
Portfolio-wide backtest Micro-Gridu (DCA + trailing STOP), 2026-07-28 - drugie
przejście po Sygnale (patrz signal_runner.py), na wyraźne życzenie Adama
("pełna symulacja portfela", nie uproszczony per-ticker). W odróżnieniu od
Sygnału, wejście level-0 Micro-Gridu to NIE niezależna decyzja per-ticker -
to KONKURS o max MAX_CONCURRENT_POSITIONS wspólnych slotów
(bot_entry_filters.rank_candidates, ten sam kod co produkcja), więc backtest
MUSI symulować całą listę tickerów naraz z jednym wspólnym portfelem, nie
osobno jak signal_runner.

Reużywa WPROST (zero duplikacji logiki, ten sam wzorzec co signal_runner.py):
- microgrid_strategy.compute_entry_quantity/compute_milestone_steps/
  compute_trailing_stop (app/services/strategy/microgrid_strategy.py)
- bot_entry_filters.rank_candidates/evaluate_candidate (scoring kandydatów,
  DOKŁADNIE ten sam kod co produkcja - już czysty, bierze candles_getter
  jako callback, zero I/O samo w sobie)
- bot_engine._compute_atr/_parse_dca_scenario/_dca_multiplier + stałe
  ATR_PERIOD/ATR_STOP_MULTIPLIER/MIN_TRAIL_REQUOTE_FRACTION/
  FX_ROUND_TRIP_PCT/MAX_CONCURRENT_POSITIONS

CELOWO POMINIĘTE (i dlaczego to nie zniekształca wyniku w istotny sposób):
- `_entry_trend_ok()` w bot_engine.py - MARTWY KOD, nigdz nie wołany z
  `_enter_position()` na produkcji (zweryfikowane grep-em) - filtr trendu
  dwustronny w bot_entry_filters już to pokrywa.
- Okna sesji giełdowej (`_market_open`) - dane dzienne nie mają godzin, a
  ten filtr wpływa na to KTÓRA GODZINA w ciągu dnia dostaje szansę, nie
  KTÓRY DZIEŃ - na siatce dziennej nie ma czego pomijać.
- Filtr spreadu (`quote_getter=None`) - fail-open, DOKŁADNIE jak na
  produkcji przy braku danych bid/ask z Finnhub free tier (patrz docstring
  bot_entry_filters.py - to jest już REALNY, nie hipotetyczny stan produkcji).
- Bezpiecznik dziennej straty (`check_daily_loss_limit`) - pole formularza,
  nigdy nie było aktywnie testowane w tym pilotowym przebiegu backtestu,
  osobne rozszerzenie później jeśli potrzebne.

UPROSZCZENIA WYNIKAJĄCE Z GRANULACJI DZIENNEJ (produkcja tickuje co 60s,
backtest ma tylko świece dzienne):
- NAJWYŻEJ JEDNO nowe wejście level-0 NA DZIEŃ (nie na tick) - fill po CENIE
  ZAMKNIĘCIA dnia, jak signal_runner.py.
- Fill nogi DCA po CENIE TRIGGERA (`grid_anchor_price*(1-dca_trigger_pct*
  level)`), NIE po dowolnej cenie live w środku dnia - wyzwalacz to
  `dzienne minimum <= trigger`, ten sam princip realizmu co detekcja
  stop-lossu w signal_runner.py (sprawdzanie WYŁĄCZNIE po close
  systematycznie gubiłoby dni, gdzie cena spika przez próg i wraca).
- Kandydaci do scoringu dostają jako okno `CANDLES_GETTER_WINDOW_ROWS` wierszy
  (patrz stała niżej) - `evaluate_candidate` sam przycina do `RECENT_WINDOW_ROWS`
  dla filtrów krótkoterminowych (trend/zakres dnia), pełne okno idzie do
  filtra regime'u (Hurst) gdy włączony.
- Kolejność w ramach jednego dnia: najpierw exit (trailing STOP) dla
  WSZYSTKICH otwartych pozycji, potem DCA dla tych co przeżyły, na końcu
  JEDNO nowe wejście do wolnego slotu - dokładnie kolejność z bot_engine.py
  tick() (_manage_trailing_exit -> _trigger_dca_buys -> _process_entries).
"""

from __future__ import annotations

from dataclasses import dataclass
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

# PRZENIESIONE 2026-08-03 (wieczorem) - bot_engine.MAX_CONCURRENT_POSITIONS
# zniknęła (przeniesiona do RiskSettings.max_concurrent_positions, kolumna
# bazy per-user, patrz models.py), więc backtest dostaje WŁASNĄ, niezależną
# stałą (6 = obecny prod default) zamiast importu z produkcyjnego modułu.
# Testy chcące inny limit ustawiają to bezpośrednio (monkeypatch modułu, ten
# sam wzorzec co reszta dzisiejszej sesji).
MAX_CONCURRENT_POSITIONS = 6


@dataclass
class Asset:
    """Zastępuje BotAsset - tylko pola, których dotyka scoring/sizing."""
    ticker: str
    currency: str
    entry_amount: Decimal


@dataclass
class SettingsStub:
    """Zastępuje RiskSettings - tylko pola, których dotyka ten moduł."""
    max_dca_levels: int
    dca_trigger_pct: Decimal
    dca_scenario: str
    take_profit_step_pct: Decimal
    stop_loss_pct: Decimal
    max_spread_pct: Decimal = Decimal("0")  # bez znaczenia - quote_getter=None -> fail-open i tak
    equity_sizing_enabled: bool = False
    # Wariant testowy 2026-08-07 (Adam, po realnym przypadku Tesli - patrz
    # microgrid_strategy.compute_milestone_steps_abs) - gdy USTAWIONE, próg
    # uzbrojenia/trailing liczony jest w KWOCIE (np. 1 albo 2 w walucie
    # tickera) zamiast w %, patrz gałąź niżej w run_microgrid_backtest().
    # None (domyślnie) = stare zachowanie, zero zmiany.
    take_profit_step_abs: Decimal | None = None


@dataclass
class GridPosition:
    ticker: str
    currency: str
    entry_day: int
    quantity: Decimal
    average_price: Decimal
    allocated_value: Decimal
    dca_level: int = 0
    grid_anchor_price: Decimal = Decimal("0")
    stop_target_price: Decimal | None = None
    trail_milestone_steps: int = 0


@dataclass
class ClosedGridTrade:
    ticker: str
    entry_day: int
    exit_day: int
    average_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    dca_level: int
    exit_reason: str
    pnl: Decimal
    pnl_pct: Decimal


class GridPortfolio:
    """Jeden wspólny portfel (gotówka + wiele pozycji naraz) - w odróżnieniu od
    backtest/portfolio.py::Portfolio (jeden ticker = jeden niezależny portfel,
    bez DCA), bo tu pozycje NAPRAWDĘ konkurują o wspólny kapitał i sloty."""

    def __init__(
        self, starting_cash: Decimal,
        slippage_pct: Decimal = Decimal("0.0005"),
    ):
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.slippage_pct = slippage_pct
        self.open_positions: dict[str, GridPosition] = {}
        self.closed_trades: list[ClosedGridTrade] = []
        self._equity_curve: list[Decimal] = [starting_cash]
        self._peak_equity = starting_cash
        self.max_drawdown_pct = Decimal("0")

    def open_level0(self, ticker: str, currency: str, day: int, price: Decimal, quantity: Decimal) -> None:
        fill_price = price * (1 + self.slippage_pct)
        cost = fill_price * quantity
        if cost > self.cash:
            return  # brak gotówki (nie powinno się zdarzyć przy entry_amount=100/MAX_CONCURRENT_POSITIONS=10) - pomijamy cicho
        self.cash -= cost
        self.open_positions[ticker] = GridPosition(
            ticker=ticker, currency=currency, entry_day=day,
            quantity=quantity, average_price=fill_price, allocated_value=cost,
            grid_anchor_price=fill_price,
        )

    def add_dca_leg(self, ticker: str, price: Decimal, amount: Decimal) -> None:
        pos = self.open_positions[ticker]
        fill_price = price * (1 + self.slippage_pct)
        leg_qty = (amount / fill_price).quantize(Decimal("0.0001"))
        if leg_qty <= 0:
            return
        leg_cost = leg_qty * fill_price
        if leg_cost > self.cash:
            return  # jak wyżej - brak gotówki, cicho pomijamy tę nogę
        self.cash -= leg_cost
        pos.quantity += leg_qty
        pos.allocated_value = (pos.allocated_value + leg_cost).quantize(Decimal("0.01"))
        pos.average_price = (pos.allocated_value / pos.quantity).quantize(Decimal("0.0001"))
        pos.dca_level += 1
        pos.stop_target_price = None  # Cancel-Replace na produkcji - uzbraja się od nowa (_confirm_dca_fills)
        pos.trail_milestone_steps = 0

    def close(self, ticker: str, day: int, price: Decimal, reason: str) -> None:
        pos = self.open_positions.pop(ticker)
        fill_price = price * (1 - self.slippage_pct)
        proceeds = fill_price * pos.quantity
        self.cash += proceeds
        pnl = proceeds - pos.allocated_value
        pnl_pct = pnl / pos.allocated_value * 100
        self.closed_trades.append(ClosedGridTrade(
            ticker=ticker, entry_day=pos.entry_day, exit_day=day,
            average_price=pos.average_price, exit_price=fill_price,
            quantity=pos.quantity, dca_level=pos.dca_level, exit_reason=reason,
            pnl=pnl, pnl_pct=pnl_pct,
        ))

    def mark_to_market(self, prices: dict[str, Decimal]) -> None:
        equity = self.cash
        for pos in self.open_positions.values():
            equity += prices.get(pos.ticker, pos.average_price) * pos.quantity
        self._equity_curve.append(equity)
        self._peak_equity = max(self._peak_equity, equity)
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - equity) / self._peak_equity * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown)

    @property
    def final_equity(self) -> Decimal:
        return self._equity_curve[-1]


# Okno przekazywane do candles_getter - HURST_LOOKBACK_DAYS (120) zamiast
# dawnego przyblizenia TREND_LOOKBACK_DAYS~7 sesji, dodane 2026-07-31 przy
# filtrze regime'u (Hurst Exponent, patrz bot_entry_filters.py). ZERO KOSZTU
# w backteście - windows[ticker] to i tak PEŁNA historia już wczytana do
# pamięci, evaluate_candidate sam przycina do RECENT_WINDOW_ROWS dla
# _range_position/_trend_metrics (no-op względem starego zachowania), a
# Hurst dostaje pełne dłuższe okno gdy HURST_FILTER_ENABLED.
CANDLES_GETTER_WINDOW_ROWS = bot_entry_filters.HURST_LOOKBACK_DAYS


def run_microgrid_backtest(
    assets: list[Asset],
    candles_by_ticker: dict[str, list[dict]],
    portfolio: GridPortfolio,
    settings: SettingsStub,
    skip_gap_through_stop: bool = False,
    realistic_gap_fill: bool = False,
    gap_threshold_pct: Decimal = Decimal("0.01"),
) -> None:
    """
    Odtwarza kolejność bot_engine.py::tick() dzień po dniu: exit -> DCA -> jedno
    nowe wejście.

    WYRÓWNANIE KALENDARZOWE (znalezione i naprawione 2026-07-28 przy pierwszym
    pełnym przebiegu na 38 tickerach - SPCX_US_EQ miał tylko 31 wierszy zamiast
    400, bo to niedawny debiut/SPAC z krótką historią, i przyciął CAŁĄ
    symulację do 31 "dni" zamiast pominąć go do czasu aż dorobi się historii).
    `get_mini_chart_ohlc` zwraca świece PRAWOSTRONNIE wyrównane do "dziś"
    (ostatni wiersz = najnowsza sesja, NIEZALEŻNIE ile wierszy jest - patrz
    docstring price_feed.get_mini_chart_ohlc), więc ticker z krótszą historią
    ma swój wiersz 0 bliżej "dziś", nie 400 dni temu. Iterujemy więc po
    WSPÓLNYM kalendarzu (`calendar_day`, 0=najstarszy dzień NAJDŁUŻSZEGO
    tickera, total_days-1=dziś dla WSZYSTKICH) i dla każdego tickera z osobna
    przeliczamy na jego WŁASNY indeks (`local_day = calendar_day - offset`,
    offset = total_days - len(jego świec)) - ticker bez jeszcze własnej
    historii tego dnia jest pomijany (nie ma jak wejść w pozycję, której
    jeszcze nie mógł mieć), zamiast obcinać wszystkim innym horyzont.

    `skip_gap_through_stop` (dodane 2026-08-10, eksperyment - Adam: luka na
    otwarciu weekendowym może "wyciąć" SL, chce to przeczekać zamiast
    realizować stratę na chwilowym dołku): gdy dzień otwiera się PONIŻEJ
    stop_target_price O WIĘCEJ NIŻ `gap_threshold_pct` (patrz niżej - zwykła
    noc-do-nocy zmienność NIE liczy się jako "luka", tylko realny skok),
    ta sama noga NIE jest zamykana tego dnia - pozycja jedzie dalej,
    zwykły trailing/floor wraca następnego dnia. Domyślnie False - stare
    zachowanie bez zmian.

    `gap_threshold_pct` (domyślnie 1%) - próg wielkości luki żeby w ogóle
    liczyła się jako "luka" (dla `skip_gap_through_stop`/`realistic_gap_fill`).
    WAŻNE odkrycie przy budowie tego eksperymentu: przy dzisiejszym
    take_profit_step_pct=0.2% stop siedzi TAK ciasno że zwykła, codzienna
    zmienność nocna (mediana ~0.4% na AAPL, 70% dni ma gap >0.2%) prawie
    ZAWSZE technicznie "gapuje przez" stop - bez tego progu test mierzyłby
    głównie zwykły szum, nie prawdziwe zdarzenia weekendowe/newsowe.

    `realistic_gap_fill` (dodane razem z powyższym, OSOBNA flaga - domyślnie
    False, zero zmiany zachowania dla żadnego innego wołającego tej funkcji):
    gdy True, fill = min(stop_target_price, day_open) zamiast zawsze
    idealnego stop_target_price - rzeczywisty resting STOP na T212 wypełnia
    się na dostępnej cenie, nie tam gdzie stał, gdy rynek otwiera się
    poniżej niego. Osobna flaga (nie połączona z `skip_gap_through_stop`)
    żeby dało się uczciwie porównać "uszanuj stop" vs "przeczekaj lukę" z
    tym samym, realistycznym modelem wypełnień po obu stronach.
    """
    min_bars = ATR_PERIOD + 1
    tickers = [a.ticker for a in assets]
    asset_by_ticker = {a.ticker: a for a in assets}
    total_days = max(len(c) for c in candles_by_ticker.values())
    offsets = {t: total_days - len(candles_by_ticker[t]) for t in tickers}

    for calendar_day in range(min_bars, total_days):
        day_prices: dict[str, Decimal] = {}
        day_lows: dict[str, Decimal] = {}
        day_opens: dict[str, Decimal] = {}
        windows: dict[str, list[dict]] = {}
        for ticker in tickers:
            local_day = calendar_day - offsets[ticker]
            if local_day < min_bars:
                continue  # ten ticker jeszcze nie ma (u siebie) dosc historii na ten dzien kalendarzowy
            window = candles_by_ticker[ticker][: local_day + 1]
            windows[ticker] = window
            day_prices[ticker] = Decimal(str(window[-1]["c"]))
            day_lows[ticker] = Decimal(str(window[-1]["l"]))
            day_opens[ticker] = Decimal(str(window[-1]["o"]))

        # --- 1. Exit (trailing STOP) dla wszystkich otwartych pozycji ---
        for ticker in list(portfolio.open_positions.keys()):
            pos = portfolio.open_positions[ticker]
            price = day_prices[ticker]
            ref_price = (
                pos.average_price * (1 + FX_ROUND_TRIP_PCT) if pos.currency == "USD"
                else pos.average_price
            )
            if settings.take_profit_step_abs is not None:
                milestone_steps = microgrid_strategy.compute_milestone_steps_abs(
                    ref_price, price, settings.take_profit_step_abs,
                )
            else:
                milestone_steps = microgrid_strategy.compute_milestone_steps(ref_price, price, settings.take_profit_step_pct)
            if milestone_steps >= 2:
                is_first_arm = pos.stop_target_price is None
                atr_distance = None
                if is_first_arm:
                    atr = _compute_atr(windows[ticker], ATR_PERIOD)
                    if atr is not None:
                        atr_distance = atr * ATR_STOP_MULTIPLIER
                if settings.take_profit_step_abs is not None:
                    candidate_stop = microgrid_strategy.compute_trailing_stop_abs(
                        is_first_arm=is_first_arm, ref_price=ref_price, current_price=price,
                        step_abs=settings.take_profit_step_abs, existing_stop_target=pos.stop_target_price,
                        atr_distance=atr_distance, stop_loss_pct=settings.stop_loss_pct,
                        min_requote_fraction=MIN_TRAIL_REQUOTE_FRACTION,
                    )
                else:
                    candidate_stop = microgrid_strategy.compute_trailing_stop(
                        is_first_arm=is_first_arm, ref_price=ref_price, current_price=price,
                        step=settings.take_profit_step_pct, existing_stop_target=pos.stop_target_price,
                        atr_distance=atr_distance, stop_loss_pct=settings.stop_loss_pct,
                        min_requote_fraction=MIN_TRAIL_REQUOTE_FRACTION,
                    )
                if candidate_stop is not None:
                    pos.stop_target_price = candidate_stop
                    pos.trail_milestone_steps = milestone_steps
            elif pos.dca_level >= settings.max_dca_levels - 1 and pos.stop_target_price is None:
                # Ostatnia linia obrony dla wyczerpanego DCA - patrz
                # microgrid_strategy.compute_exhausted_dca_floor i punkt 5
                # docstringu bot_engine.py::_manage_trailing_exit (dodane
                # 2026-07-28 po tym jak PIERWSZY przebieg tego backtestu
                # pokazał PRXa_EQ/MCp_EQ/SAPd_EQ utknięte bez ochrony).
                atr = _compute_atr(windows[ticker], ATR_PERIOD)
                atr_distance = atr * ATR_STOP_MULTIPLIER if atr is not None else None
                pos.stop_target_price = microgrid_strategy.compute_exhausted_dca_floor(
                    price, atr_distance, settings.stop_loss_pct,
                )

            if pos.stop_target_price is not None and day_lows[ticker] <= pos.stop_target_price:
                gapped_through = day_opens[ticker] < pos.stop_target_price * (1 - gap_threshold_pct)
                if skip_gap_through_stop and gapped_through:
                    pass  # przeczekujemy luke, patrz docstring skip_gap_through_stop
                else:
                    fill_price = pos.stop_target_price
                    if realistic_gap_fill and gapped_through:
                        fill_price = day_opens[ticker]
                    portfolio.close(ticker, calendar_day, fill_price, "trailing-stop")

        # --- 2. DCA dla pozycji, ktore przezyly krok 1 ---
        for ticker in list(portfolio.open_positions.keys()):
            pos = portfolio.open_positions[ticker]
            if pos.dca_level >= settings.max_dca_levels - 1:
                continue
            next_level = pos.dca_level + 1
            trigger_price = pos.grid_anchor_price * (Decimal("1") - settings.dca_trigger_pct * next_level)
            if trigger_price <= 0 or day_lows[ticker] > trigger_price:
                continue

            # Detektor "szoku" (microgrid_strategy.is_shock) - domyślnie
            # WYŁĄCZONY (patrz docstring modułu), zero kosztu tutaj (ATR
            # liczony z windows[ticker] już w pamięci).
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
            portfolio.add_dca_leg(ticker, trigger_price, effective_entry_amount * multiplier)

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
