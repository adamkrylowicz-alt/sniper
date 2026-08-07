#!/usr/bin/env python3
"""
run_microgrid_backtest.py
============================
CLI do portfolio-wide backtestu Micro-Gridu (DCA + trailing STOP), patrz
backtest/microgrid_runner.py po pełny opis architektury i uproszczeń.
Domyślne wartości parametrów (--max-dca-levels, --dca-trigger-pct,
--dca-scenario, --take-profit-step-pct, --stop-loss-pct) i lista tickerów
(--tickers) ODZWIERCIEDLAJĄ aktualny stan produkcyjnej bazy (risk_settings/
bot_assets, sprawdzone ręcznie 2026-07-28) - domyślne uruchomienie bez flag
jest więc bezpośrednio porównywalne z tym co bot faktycznie robi na koncie
demo. Podanie flag nadpisuje pojedyncze parametry do eksperymentów.

--test-days N (dodane 2026-07-31, po lekturze serii "Build Better Strategies"
cz.3 o anti-patternie curve-fittingu): odcina ostatnie N dni jako
out-of-sample test, uruchamiany OSOBNO od treningu (własny świeży portfel).
Strojenie parametrów robimy patrząc WYŁĄCZNIE na raport IN-SAMPLE, test
odpalamy raz na końcu jako sprawdzian czy wynik trzyma się poza próbą - nie
jako kolejny cel do optymalizacji. Domyślnie 0 (wyłączone) - zachowanie
identyczne jak przed tą zmianą.

Przykład:
    python3 run_microgrid_backtest.py --days 400
    python3 run_microgrid_backtest.py --days 400 --dca-trigger-pct 0.03 --max-dca-levels 3
    python3 run_microgrid_backtest.py --days 400 --test-days 60
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from app.services import bot_entry_filters
from app.services.strategy import microgrid_strategy
from backtest.data import app_context, fetch_candles
from backtest.microgrid_runner import Asset, GridPortfolio, SettingsStub, run_microgrid_backtest

DEFAULT_TICKERS = [
    "AAPL_US_EQ", "AIRp_EQ", "ALVd_EQ", "AMZN_US_EQ", "ASML_US_EQ", "ASMLa_EQ",
    "BLK_US_EQ", "BNPp_EQ", "COST_US_EQ", "CRM_US_EQ", "DTEd_EQ", "FB_US_EQ",
    "FPp_EQ", "GOOGL_US_EQ", "IFXd_EQ", "INGAa_EQ", "IPOE_US_EQ", "JNJ_US_EQ",
    "JPM_US_EQ", "KO_US_EQ", "MA_US_EQ", "MCp_EQ", "MSFT_US_EQ", "NFLX_US_EQ",
    "NVDA_US_EQ", "ORCL_US_EQ", "PRXa_EQ", "PYPL_US_EQ", "SAFp_EQ", "SANe_EQ",
    "SAPd_EQ", "SIEd_EQ", "SPCX_US_EQ", "SUp_EQ", "TSLA_US_EQ", "V_US_EQ",
    "WMT_US_EQ", "XOM_US_EQ",
]


def _currency(ticker: str) -> str:
    return "USD" if ticker.endswith("_US_EQ") else "EUR"


def _split_candles(
    candles_by_ticker: dict[str, list[dict]], test_days: int,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """
    Tnie świece każdego tickera na (trening, test) wg ostatnich test_days dni.
    Krótsze historie (np. SPCX_US_EQ, niedawny debiut) dają pustą listę po
    stronie treningu zamiast wyjątku - run_microgrid_backtest już to
    bezpiecznie pomija (offset wychodzi poza zakres kalendarza, ticker po
    prostu nie handluje żadnego dnia), patrz docstring microgrid_runner.py
    o wyrównaniu kalendarzowym. Zero dodatkowego guarda tam potrzebne.
    """
    if test_days <= 0:
        raise ValueError("test_days musi być > 0")  # candles[:-0] ucięłoby WSZYSTKO, nie nic
    train: dict[str, list[dict]] = {}
    test: dict[str, list[dict]] = {}
    for ticker, candles in candles_by_ticker.items():
        train[ticker] = candles[:-test_days] if len(candles) > test_days else []
        test[ticker] = candles[-test_days:]
    return train, test


def _run_and_report(
    label: str,
    assets: list[Asset],
    candles_by_ticker: dict[str, list[dict]],
    starting_cash: Decimal,
    settings: SettingsStub,
    csv_rows: list[tuple] | None,
    segment_tag: str,
) -> None:
    """
    Jeden przebieg silnika (świeży portfel) + wydruk konsolowy pod nagłówkiem
    `label`. csv_rows zbierane z zewnątrz (main() pisze JEDEN plik na końcu,
    z obu przebiegów train/test gdy dotyczy) zamiast dwóch osobnych plików do
    ręcznego łączenia - segment_tag ("train"/"test"/"full") trafia jako
    ostatnia kolumna każdego wiersza.
    """
    portfolio = GridPortfolio(starting_cash=starting_cash)
    run_microgrid_backtest(assets, candles_by_ticker, portfolio, settings)

    trades = portfolio.closed_trades
    total_return_pct = (portfolio.final_equity - starting_cash) / starting_cash * 100
    wins = [t for t in trades if t.pnl > 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0

    print(f"\n=== {label} ===")
    print(f"Tickerów: {len(candles_by_ticker)} | Transakcji zamkniętych: {len(trades)} | Wciąż otwartych: {len(portfolio.open_positions)}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Total return: {total_return_pct:.2f}%")
    print(f"Max drawdown: {portfolio.max_drawdown_pct:.2f}%")
    print(f"Kapitał końcowy: {portfolio.final_equity:.2f} (start {starting_cash:.2f})")
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print(f"Powody zamknięcia: {by_reason}")

    if csv_rows is not None:
        for t in trades:
            csv_rows.append((
                t.ticker, t.entry_day, t.exit_day, t.average_price, t.exit_price,
                t.quantity, t.dca_level, t.exit_reason, t.pnl, t.pnl_pct, segment_tag,
            ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest portfolio-wide Micro-Gridu (DCA + trailing STOP).")
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS), help="Lista tickerów po przecinku")
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--entry-amount", type=Decimal, default=Decimal("100"))
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--max-dca-levels", type=int, default=5)
    parser.add_argument("--dca-trigger-pct", type=Decimal, default=Decimal("0.05"))
    parser.add_argument("--dca-scenario", default="1,1,1,1,1")
    parser.add_argument("--take-profit-step-pct", type=Decimal, default=Decimal("0.003"))
    parser.add_argument(
        "--take-profit-step-abs", type=Decimal, default=None,
        help="Testowy wariant (2026-08-07): próg uzbrojenia/trailing w KWOCIE "
             "(np. 1 albo 2, w walucie tickera) zamiast w %. Gdy podane, "
             "NADPISUJE --take-profit-step-pct dla tego przebiegu - patrz "
             "microgrid_strategy.compute_milestone_steps_abs/compute_trailing_stop_abs.",
    )
    parser.add_argument("--stop-loss-pct", type=Decimal, default=Decimal("0.02"))
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--csv", default=None)
    parser.add_argument(
        "--test-days", type=int, default=0,
        help="Odetnij ostatnie N dni jako out-of-sample test (walk-forward). "
             "0 = wyłączone, jeden przebieg na całej historii jak dziś.",
    )
    parser.add_argument(
        "--hurst-filter", action="store_true",
        help="Włącz eksperymentalny filtr regime'u (Hurst Exponent, domyślnie "
             "wyłączony na produkcji - patrz bot_entry_filters.HURST_FILTER_ENABLED) "
             "do porównania A/B względem obecnego zachowania.",
    )
    parser.add_argument(
        "--shock-filter", action="store_true",
        help="Włącz eksperymentalny detektor szoku przed DCA (domyślnie "
             "wyłączony na produkcji - patrz microgrid_strategy.SHOCK_FILTER_ENABLED) "
             "do porównania A/B względem obecnego zachowania.",
    )
    parser.add_argument(
        "--equity-scaling", action="store_true",
        help="Włącz eksperymentalne skalowanie √equity kwoty wejścia/DCA (domyślnie "
             "wyłączone - patrz microgrid_strategy.compute_equity_scaled_amount) do "
             "porównania A/B względem obecnego (stałego entry_amount) zachowania. "
             "Equity odniesienia = --starting-cash TEGO przebiegu (portfolio.starting_cash).",
    )
    args = parser.parse_args()

    # Ustawiane PRZED jakimkolwiek fetchem/przebiegiem - to proste stałe
    # modułów (jak reszta progów filtrów wejścia), nie pola SettingsStub, więc
    # nadpisujemy je bezpośrednio na modułach współdzielonych z produkcją.
    bot_entry_filters.HURST_FILTER_ENABLED = args.hurst_filter
    microgrid_strategy.SHOCK_FILTER_ENABLED = args.shock_filter

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    assets = [Asset(ticker=t, currency=_currency(t), entry_amount=args.entry_amount) for t in tickers]
    settings = SettingsStub(
        max_dca_levels=args.max_dca_levels, dca_trigger_pct=args.dca_trigger_pct,
        dca_scenario=args.dca_scenario, take_profit_step_pct=args.take_profit_step_pct,
        stop_loss_pct=args.stop_loss_pct, equity_sizing_enabled=args.equity_scaling,
        take_profit_step_abs=args.take_profit_step_abs,
    )

    with app_context():
        candles_by_ticker = {t: fetch_candles(t, args.days, force_refresh=args.force_refresh) for t in tickers}

    csv_rows: list[tuple] | None = [] if args.csv else None

    if args.test_days > 0:
        train, test = _split_candles(candles_by_ticker, args.test_days)
        _run_and_report(
            f"IN-SAMPLE (trening, dni 0..-{args.test_days})",
            assets, train, args.starting_cash, settings, csv_rows, "train",
        )
        _run_and_report(
            f"OUT-OF-SAMPLE (test, ostatnie {args.test_days} dni, NIE używać do strojenia)",
            assets, test, args.starting_cash, settings, csv_rows, "test",
        )
    else:
        _run_and_report(
            f"WYNIK (pełna historia, {args.days} dni)",
            assets, candles_by_ticker, args.starting_cash, settings, csv_rows, "full",
        )

    if csv_rows is not None:
        import csv
        with open(args.csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "ticker", "entry_day", "exit_day", "average_price", "exit_price",
                "quantity", "dca_level", "exit_reason", "pnl", "pnl_pct", "segment",
            ])
            writer.writerows(csv_rows)
        print(f"\nZapisano transakcje do {args.csv}")


if __name__ == "__main__":
    main()
