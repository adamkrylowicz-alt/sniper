"""
app/services/eod_engine.py
=============================
Moduł EOD (End of Day) - TRZECI, znowu CAŁKOWICIE OSOBNY silnik obok
Micro-Grid (bot_engine.py) i strategii sygnałowej (signal_engine.py). Patrz
docs/IDEAS_v2.md ("Specjalny moduł EOD") i models.py (komentarz nad
EODAsset) po pełne uzasadnienie niezależności.

Działa WYŁĄCZNIE pod koniec sesji EUR (16:00-17:30 Amsterdamu, EOD_WINDOW
niżej) - cel: szybka reakcja na NAGŁE, OSTRE spadki w krótkim czasie (1-5
minut), nie na powolne pełzanie w dół (to już robi Micro-Grid/Sygnał na
świecach dziennych). Dane: świece 1-MINUTOWE dzisiejszej sesji
(price_feed.get_eod_intraday_1m, Yahoo nieoficjalne - patrz docs/IDEAS_v2.md
pkt 3, decyzja 2026-07-24: żaden tani dostawca nie dawał prawdziwego 1-min
dla Europy, docelowo IBKR API gdy dostępne).

Trigger: dla każdego tickera na liście EODAsset, licz % zmiany od ceny sprzed
1/2/3/4/5 minut do teraz - bierz NAJGORSZY (najbardziej ujemny) z tych 5
okien jako "ostry ruch". Mapuj go na tier wielkości pozycji (PRD):
    -2.0% do -3.49%  -> 0.6x entry_amount
    -3.5% do -5.49%  -> 1.0x entry_amount
    -5.5% i więcej   -> 1.5x entry_amount
Poniżej -2.0% - brak triggera.

Wyjście - TA SAMA mechanika co signal_engine.py (T212 nie pozwala na dwa
resting ordery na te same udziały, docs/IDEAS_v2.md pkt 4): stop-loss to
PRAWDZIWY resting STOP (ciasny, domyślnie 0.4%), take-profit pilnowany
WYŁĄCZNIE w softwarze (ciasny, domyślnie 0.6%). Wymuszone zamknięcie przed
końcem sesji (PRD: "nie przenoszą się na kolejny dzień") jest OPCJONALNE -
`EODSettings.force_close_enabled`, domyślnie WYŁĄCZONE (Adam 2026-07-24:
najpierw zaimplementowane jako sztywne zachowanie, potem odrzucone jako
"kto ci kazał nie trzymać ich na drugi dzień" - zamiast usuwać funkcję,
przerobione na przełącznik w Ustawieniach ryzyka, wyłączony domyślnie).

Poświadczenia WSPÓLNE z Micro-Grid/Sygnał (services/bot_credentials.py) -
ten sam demo klucz T212, jedno hasło odblokowuje wszystkie trzy silniki.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytz
from flask import current_app

from ..extensions import db
from ..models import EODAsset, EODAuditLog, EODSettings, EODTrade
from ..routes.api_keys import get_decrypted_credentials
from ..routes.scalping import _log_order
from . import bot_credentials, market_hours, price_feed
from .bot_engine import _place_buy_with_precision_fallback
from .t212_client import T212APIError, T212Client

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

# Bot działa WYŁĄCZNIE na demo - ten sam powód co Micro-Grid/Sygnał (T212 nie
# wspiera zleceń LIMIT/STOP na koncie live).
EOD_ENVIRONMENT = "demo"

# PRD: "Działa tylko pod koniec sesji (od ok. 16:00)" - Adam 2026-07-24:
# niech normalnie próbuje łapać sygnały do 17:30 (nie 17:25 jak pierwotnie).
EOD_WINDOW = (dt.time(16, 0), dt.time(17, 30))
# 5 min bufora przed twardym zamknięciem sesji - używane WYŁĄCZNIE gdy
# EODSettings.force_close_enabled=True (opcjonalny przełącznik, domyślnie
# wyłączony, patrz docstring modułu).
FORCE_CLOSE_TIME = dt.time(17, 20)

DROP_LOOKBACK_MINUTES = range(1, 6)  # 1..5 minut wstecz

# Tiery wielkości pozycji (PRD, tabela "Przykładowa siatka dokupywania") -
# uproszczone do STAŁYCH mnożników zamiast zakresu % kapitału (spójne z
# entry_amount-per-ticker już używanym w BotAsset/SignalAsset, unika
# konieczności odpytywania całkowitej wartości konta na każdy tick).
SIZE_TIERS = (
    (Decimal("0.055"), Decimal("1.5")),   # -5.5% i więcej
    (Decimal("0.035"), Decimal("1.0")),   # -3.5% do -5.49%
    (Decimal("0.02"), Decimal("0.6")),    # -2.0% do -3.49%
)


def _in_eod_window() -> bool:
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:
        return False
    return EOD_WINDOW[0] <= now_local.time() <= EOD_WINDOW[1]


def _should_force_close(settings: EODSettings) -> bool:
    """Tylko gdy settings.force_close_enabled=True (opcjonalny przełącznik, patrz docstring modułu)."""
    if not settings.force_close_enabled:
        return False
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:
        return False
    return now_local.time() >= FORCE_CLOSE_TIME


def _log(user_id: int, action_type: str, message: str) -> None:
    if action_type == "ERROR":
        current_app.logger.error("[eod user=%s] %s", user_id, message)
    entry = EODAuditLog(user_id=user_id, action_type=action_type, message=message)
    db.session.add(entry)
    db.session.commit()


def _worst_recent_drop_pct(candles: list[dict] | None) -> Decimal | None:
    """
    Najbardziej ujemna zmiana % między ceną sprzed N minut (N=1..5) a
    ostatnią świecą - "ostry ruch" niezależnie od DOKŁADNEGO okna czasowego
    w którym się wydarzył (PRD: "reaguje na ostry spadek w krótkim czasie
    (1-5 minut)"). None gdy za mało świec (dopiero co otworzyła się sesja).
    """
    if not candles or len(candles) < 2:
        return None
    latest_close = Decimal(str(candles[-1]["c"]))
    worst = None
    for n in DROP_LOOKBACK_MINUTES:
        idx = len(candles) - 1 - n
        if idx < 0:
            continue
        reference = Decimal(str(candles[idx]["c"]))
        if reference <= 0:
            continue
        pct = (latest_close - reference) / reference
        if worst is None or pct < worst:
            worst = pct
    return worst


def _size_multiplier_for_drop(drop_pct: Decimal | None) -> Decimal | None:
    """Zwraca mnożnik entry_amount dla danego spadku (ujemny %), albo None gdy poniżej progu triggera (-2.0%)."""
    if drop_pct is None or drop_pct >= 0:
        return None
    drop_abs = abs(drop_pct)
    for threshold, multiplier in SIZE_TIERS:
        if drop_abs >= threshold:
            return multiplier
    return None


def _finalize_closed_trade(user_id: int, trade: EODTrade, via: str, fill_price: Decimal) -> None:
    trade.close_price = fill_price
    trade.status = "CLOSED"
    trade.closed_via = via
    trade.closed_at = dt.datetime.utcnow()
    db.session.commit()
    pnl = (fill_price - trade.buy_price) * trade.quantity
    _log(
        user_id, "INFO",
        f"{trade.ticker}: pozycja EOD zamknięta ({via}) @ ~{fill_price}, "
        f"P/L ~{pnl:.2f} {trade.currency}.",
    )


def _enter_position(
    user_id: int, client: T212Client | None, asset: EODAsset, settings: EODSettings,
    price: Decimal, drop_pct: Decimal, multiplier: Decimal,
) -> None:
    amount = asset.entry_amount * multiplier
    quantity = (amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: wyliczona ilość <= 0 (kwota {amount} / cena {price}).")
        return

    stop_loss_price = price * (1 - settings.stop_loss_pct)
    take_profit_price = price * (1 + settings.take_profit_pct)

    if settings.is_paper_trading:
        trade = EODTrade(
            user_id=user_id, eod_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
            buy_order_id=f"PAPER-{uuid.uuid4()}",
            buy_price=price, quantity=quantity, allocated_value=quantity * price,
            drop_pct_at_entry=drop_pct, size_multiplier=multiplier,
            stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            status="OPEN", is_paper=True, buy_confirmed=True,
        )
        db.session.add(trade)
        db.session.commit()
        _log(
            user_id, "BUY",
            f"[PAPER] {asset.ticker}: ostry spadek {drop_pct*100:.2f}% (tier x{multiplier}), "
            f"{quantity} @ ~{price} - SL {stop_loss_price:.4f} / TP {take_profit_price:.4f}.",
        )
        return

    try:
        existing_position = client.get_position(asset.ticker)
    except T212APIError:
        existing_position = None
    baseline = Decimal(str(existing_position["quantity"])) if existing_position else Decimal("0")

    try:
        buy_result, quantity = _place_buy_with_precision_fallback(client, asset.ticker, quantity, price)
    except T212APIError as exc:
        _log(user_id, "ERROR", f"{asset.ticker}: błąd składania zlecenia kupna EOD - {exc}")
        return

    trade = EODTrade(
        user_id=user_id, eod_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
        buy_order_id=buy_result.order_id, baseline_owned_quantity=baseline,
        buy_price=price, quantity=quantity, allocated_value=quantity * price,
        drop_pct_at_entry=drop_pct, size_multiplier=multiplier,
        stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
        status="OPEN", is_paper=False, buy_confirmed=False,
    )
    db.session.add(trade)
    db.session.commit()

    _log_order(
        user_id=user_id, ticker=asset.ticker, side="buy", quantity=quantity,
        price_snapshot=price, status="sent", t212_order_id=buy_result.order_id,
    )
    _log(
        user_id, "BUY",
        f"{asset.ticker}: ostry spadek {drop_pct*100:.2f}% (tier x{multiplier}), "
        f"{quantity} @ ~{price} - SL {stop_loss_price:.4f} / TP {take_profit_price:.4f}. "
        "Czeka na potwierdzenie kupna, dopiero potem uzbroi stop-loss.",
    )


def _process_entries(user_id: int, client: T212Client | None, settings: EODSettings) -> None:
    assets = EODAsset.query.filter_by(user_id=user_id).all()
    if not assets:
        return

    open_tickers = {t.ticker for t in EODTrade.query.filter_by(user_id=user_id, status="OPEN").all()}

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for asset in assets:
        if asset.ticker in open_tickers:
            continue
        if not market_hours.is_market_open(asset.currency):
            continue

        candles = price_feed.get_eod_intraday_1m(asset.ticker)
        drop = _worst_recent_drop_pct(candles)
        multiplier = _size_multiplier_for_drop(drop)
        if multiplier is None:
            continue

        price = price_feed.get_live_price(api_key, asset.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            continue

        _enter_position(user_id, client, asset, settings, price, drop, multiplier)


def _confirm_pending_entries(user_id: int, client: T212Client, settings: EODSettings) -> None:
    pending_trades = EODTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False,
    ).all()
    if not pending_trades:
        return

    try:
        pending_order_ids = {str(o.get("id")) for o in client.get_pending_orders()}
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Nie udało się pobrać zleceń oczekujących (potwierdzanie kupna EOD) - {exc}")
        return

    for trade in pending_trades:
        if trade.buy_order_id in pending_order_ids:
            continue

        try:
            position = client.get_position(trade.ticker)
        except T212APIError as exc:
            _log(user_id, "ERROR", f"{trade.ticker}: błąd sprawdzenia portfela po zakupie EOD - {exc}")
            continue

        current_owned = Decimal(str(position["quantity"])) if position else Decimal("0")
        filled = current_owned - trade.baseline_owned_quantity
        if filled <= 0:
            trade.status = "CLOSED"
            trade.closed_via = "never-filled"
            trade.closed_at = dt.datetime.utcnow()
            db.session.commit()
            _log(user_id, "ERROR", f"{trade.ticker}: zlecenie kupna EOD zniknęło z kolejki bez wypełnienia.")
            continue

        trade.quantity = filled
        trade.allocated_value = filled * trade.buy_price
        trade.buy_confirmed = True
        db.session.commit()

        try:
            stop_result = client.place_stop_order(trade.ticker, -filled, trade.stop_loss_price)
            trade.stop_order_id = stop_result.order_id
            db.session.commit()
            _log(user_id, "INFO", f"{trade.ticker}: kupno EOD potwierdzone ({filled} szt.), stop-loss uzbrojony na {trade.stop_loss_price:.4f}.")
        except T212APIError as exc:
            _log(
                user_id, "ERROR",
                f"{trade.ticker}: kupno EOD potwierdzone, ale NIE udało się uzbroić stop-loss - {exc}. "
                "Pozycja NIECHRONIONA, sprawdź ręcznie.",
            )


def _force_close_real(user_id: int, client: T212Client, trade: EODTrade) -> None:
    """Wymuszone zamknięcie - TYLKO gdy EODSettings.force_close_enabled=True (patrz _should_force_close). Anuluje STOP, Market Sell."""
    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            _log(user_id, "INFO", f"{trade.ticker}: anulowanie stop-loss przy wymuszonym zamknięciu EOD nie powiodło się (mógł się właśnie wykonać) - {exc}")
            return  # kolejny tick wykryje ewentualne wykonanie STOP-a (zniknie z pending)

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
    price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)

    try:
        sell_result = client.place_market_order(trade.ticker, -trade.quantity)
    except T212APIError as exc:
        _log(user_id, "ERROR", f"{trade.ticker}: wymuszone zamknięcie EOD (koniec sesji) nie powiodło się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA, sprawdź ręcznie.")
        return

    _log_order(
        user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
        price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
    )
    _finalize_closed_trade(user_id, trade, "eod-forced", fill_price=price if price is not None else trade.buy_price)


def _manage_exits(user_id: int, client: T212Client, settings: EODSettings) -> None:
    open_trades = EODTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True,
    ).all()
    if not open_trades:
        return

    force_close = _should_force_close(settings)

    try:
        pending_order_ids = {str(o.get("id")) for o in client.get_pending_orders()}
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Nie udało się pobrać zleceń oczekujących (wyjścia EOD) - {exc}")
        return

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for trade in open_trades:
        if trade.stop_order_id and trade.stop_order_id not in pending_order_ids:
            _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=trade.stop_loss_price)
            continue

        if force_close:
            _force_close_real(user_id, client, trade)
            continue

        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            continue
        if price < trade.take_profit_price:
            continue

        if trade.stop_order_id:
            try:
                client.cancel_order(trade.stop_order_id)
            except T212APIError as exc:
                _log(user_id, "INFO", f"{trade.ticker}: anulowanie stop-loss przed take-profit EOD nie powiodło się (mógł się właśnie wykonać) - {exc}")
                continue

        try:
            sell_result = client.place_market_order(trade.ticker, -trade.quantity)
        except T212APIError as exc:
            _log(user_id, "ERROR", f"{trade.ticker}: take-profit EOD osiągnięty, ale sprzedaż Market nie powiodła się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA.")
            continue

        _log_order(
            user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
            price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
        )
        _finalize_closed_trade(user_id, trade, "take-profit", fill_price=price)


def _manage_paper_exits(user_id: int, settings: EODSettings) -> None:
    open_trades = EODTrade.query.filter_by(user_id=user_id, status="OPEN", is_paper=True).all()
    if not open_trades:
        return

    force_close = _should_force_close(settings)
    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for trade in open_trades:
        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            if force_close:
                _finalize_closed_trade(user_id, trade, "eod-forced", fill_price=trade.buy_price)
            continue
        if price <= trade.stop_loss_price:
            _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=price)
        elif price >= trade.take_profit_price:
            _finalize_closed_trade(user_id, trade, "take-profit", fill_price=price)
        elif force_close:
            _finalize_closed_trade(user_id, trade, "eod-forced", fill_price=price)


def reconcile(user_id: int) -> None:
    """Wołane raz zaraz po aktywacji (routes/eod.py::activate) - nadgania stan bez czekania na najbliższy tick."""
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        _log(user_id, "ERROR", "Reconciliation: brak poświadczeń w bot_credentials mimo aktywacji.")
        return
    creds = get_decrypted_credentials(user_id, master_key, EOD_ENVIRONMENT)
    if creds is None:
        _log(user_id, "ERROR", "Reconciliation: brak zapisanego klucza API demo, pomijam.")
        return
    settings = EODSettings.query.filter_by(user_id=user_id).first()
    if settings is None:
        return

    _manage_paper_exits(user_id, settings)
    if not settings.is_paper_trading:
        client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=EOD_ENVIRONMENT)
        _confirm_pending_entries(user_id, client, settings)
        _manage_exits(user_id, client, settings)


def tick(app) -> None:
    """Wołane cyklicznie przez APScheduler co 60s (PRD: 'co 1 minutę sprawdza świece 1-minutowe') - patrz app/__init__.py."""
    with app.app_context():
        for user_id in bot_credentials.active_user_ids():
            settings = EODSettings.query.filter_by(user_id=user_id).first()
            if not settings or not settings.is_active:
                continue

            _manage_paper_exits(user_id, settings)

            if settings.is_paper_trading:
                if _in_eod_window():
                    _process_entries(user_id, None, settings)
                continue

            master_key = bot_credentials.get_master_key(user_id)
            if master_key is None:
                continue
            creds = get_decrypted_credentials(user_id, master_key, EOD_ENVIRONMENT)
            if creds is None:
                _log(user_id, "ERROR", "Brak zapisanego klucza API demo, pomijam tick.")
                continue
            client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=EOD_ENVIRONMENT)

            _confirm_pending_entries(user_id, client, settings)
            _manage_exits(user_id, client, settings)
            if _in_eod_window():
                _process_entries(user_id, client, settings)
