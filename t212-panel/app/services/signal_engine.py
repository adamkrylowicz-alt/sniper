"""
app/services/signal_engine.py
================================
Strategia sygnałowa RSI/MA/ATR ("Normalny tryb handlu" z docs/IDEAS_v2.md) -
OSOBNA od Micro-Grid Bota (bot_engine.py), decyzja Adama 2026-07-24: działa
RÓWNOLEGLE, nie zastępuje siatki DCA. Własne tabele (models.py::SignalAsset/
SignalTrade/SignalSettings/SignalAuditLog), własny scheduler job (60s, patrz
app/__init__.py), własna strona (/signal/, routes/signal.py).

Warunek wejścia (PRD, na sztywno - NIE builder AND/OR, świadomie odłożony,
patrz docs/IDEAS_v2.md "Otwarte pytania"): RSI(14) < próg (domyślnie 35) ORAZ
cena > SMA(200), liczone ze świec DZIENNYCH (w odróżnieniu od modułu EOD,
który ma działać na świecach 1-min - ten moduł jeszcze nie zaimplementowany).
JEDNO wejście na sygnał, bez DCA.

Zarządzanie ryzykiem: Stop Loss = ATR(14) * stop_loss_atr_mult (domyślnie
1.8), Take Profit = ATR(14) * take_profit_atr_mult (domyślnie 3.0) - oba
liczone RAZ przy wejściu (nie trailing, w odróżnieniu od Micro-Grid) i
zostają stałe przez cały czas trwania pozycji - świadome uproszczenie na
start, "sprawdzimy w boju" (Adam, 2026-07-24) zanim dokładać complexity
trailing.

Mechanika wyjścia - TA SAMA przyczyna co przeprojektowanie Micro-Grid
21.07.2026 (T212 nie pozwala trzymać LIMIT SELL + STOP jednocześnie na te
same udziały, patrz docs/IDEAS_v2.md pkt 4): stop-loss to PRAWDZIWY resting
STOP na T212 (chroni nawet offline), take-profit pilnowany WYŁĄCZNIE w
softwarze (_manage_exits, Market Sell gdy żywa cena go dotknie) - jedyne
resting zlecenie na pozycję to ten jeden STOP.

Poświadczenia: reużywa services/bot_credentials.py (WSPÓLNY magazyn
odszyfrowanego master_key w pamięci procesu) zamiast własnego mechanizmu -
to ten sam demo klucz T212 co Micro-Grid, więc Adam nie powinien podawać
hasła dwa razy. Włączenie JEDNEGO z dwóch botów odblokowuje poświadczenia
dla OBU; wyłączenie danego bota czyści poświadczenia TYLKO gdy drugi też
jest wyłączony (patrz routes/signal.py::deactivate / routes/bot.py::deactivate).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytz
from flask import current_app

from ..extensions import db
from ..models import SignalAsset, SignalAuditLog, SignalSettings, SignalTrade
from ..routes.api_keys import get_decrypted_credentials
from ..routes.scalping import _log_order
from . import bot_credentials, market_hours, price_feed
from .bot_engine import _place_buy_with_precision_fallback
from .t212_client import T212APIError, T212Client

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

# Bot działa WYŁĄCZNIE na demo - ten sam powód co Micro-Grid (bot_engine.py):
# T212 nie wspiera zleceń LIMIT/STOP na koncie live.
SIGNAL_ENVIRONMENT = "demo"

RSI_PERIOD = 14
MA_PERIOD = 200
ATR_PERIOD = 14
# Bufor kalendarzowy dla MA(200): ~200 sesji giełdowych ≈ 280-290 dni
# kalendarzowych (weekendy/święta) - 250 zamawianych z Yahoo (patrz
# price_feed._yahoo_range_for_days) zostawia margines dla RSI(14) na ogonie.
SIGNAL_LOOKBACK_DAYS = 250

# Godziny wejścia - PRD: "Normalny tryb handlu (10:00-15:45/16:00)". Wyjścia
# (stop-loss/take-profit) NIE są ograniczone do tego okna, tylko do
# market_hours.is_market_open() - pozycja ma być chroniona przez CAŁĄ sesję,
# nie tylko rano.
ENTRY_WINDOW = (dt.time(10, 0), dt.time(15, 45))

# Rozszerzenie dla USD (Adam, 2026-07-24: dodane największe spółki USA do
# Sygnału) - sesja US w czasie Amsterdamu to 15:35-21:55 (patrz
# market_hours.US_SESSION_WINDOW), a stałe ENTRY_WINDOW 10:00-15:45 łapałoby
# praktycznie tylko pierwsze ~10 minut otwarcia USA. Dla USD osobne, szersze
# okno pokrywające prawie całą sesję NASDAQ/NYSE.
US_ENTRY_WINDOW = (dt.time(15, 35), dt.time(21, 45))


def _in_entry_window(currency: str) -> bool:
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:
        return False
    window = US_ENTRY_WINDOW if currency == "USD" else ENTRY_WINDOW
    return window[0] <= now_local.time() <= window[1]


def _log(user_id: int, action_type: str, message: str) -> None:
    if action_type == "ERROR":
        current_app.logger.error("[signal user=%s] %s", user_id, message)
    entry = SignalAuditLog(user_id=user_id, action_type=action_type, message=message)
    db.session.add(entry)
    db.session.commit()


def _compute_rsi(closes: list[Decimal], period: int = RSI_PERIOD) -> Decimal | None:
    """
    RSI PROSTY (nie wygładzanie Wildera) - ta sama filozofia prostoty co
    bot_engine.py::_compute_atr ("dla prostoty"). Wymaga co najmniej
    period+1 zamknięć (potrzeba `period` różnic dzień-do-dnia).
    """
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    deltas = [window[i] - window[i - 1] for i in range(1, len(window))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = (sum(gains) / period) if gains else Decimal("0")
    avg_loss = (sum(losses) / period) if losses else Decimal("0")
    if avg_loss == 0:
        return Decimal("100") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (1 + rs))


def _compute_sma(closes: list[Decimal], period: int = MA_PERIOD) -> Decimal | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def _compute_atr(candles: list[dict] | None, period: int = ATR_PERIOD) -> Decimal | None:
    """Identyczna formuła co bot_engine.py::_compute_atr - kopia celowa (patrz docstring modułu, "osobna strategia")."""
    if not candles or len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high = Decimal(str(candles[i]["h"]))
        low = Decimal(str(candles[i]["l"]))
        prev_close = Decimal(str(candles[i - 1]["c"]))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    last_n = true_ranges[-period:]
    return sum(last_n) / len(last_n)


def _finalize_closed_trade(user_id: int, trade: SignalTrade, via: str, fill_price: Decimal) -> None:
    trade.close_price = fill_price
    trade.status = "CLOSED"
    trade.closed_via = via
    trade.closed_at = dt.datetime.utcnow()
    db.session.commit()
    pnl = (fill_price - trade.buy_price) * trade.quantity
    _log(
        user_id, "INFO",
        f"{trade.ticker}: pozycja zamknięta ({via}) @ ~{fill_price}, "
        f"P/L ~{pnl:.2f} {trade.currency}.",
    )


def _enter_position(
    user_id: int, client: T212Client | None, asset: SignalAsset, settings: SignalSettings,
    price: Decimal, atr: Decimal,
) -> None:
    quantity = (asset.entry_amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: wyliczona ilość <= 0 (kwota {asset.entry_amount} / cena {price}).")
        return

    stop_loss_price = price - (atr * settings.stop_loss_atr_mult)
    take_profit_price = price + (atr * settings.take_profit_atr_mult)
    if stop_loss_price <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: wyliczony stop-loss <= 0 (ATR zbyt duże względem ceny), pomijam wejście.")
        return

    if settings.is_paper_trading:
        # Symulacja - zero requestow do T212, ten sam wzorzec co bot_engine.py::_enter_position.
        trade = SignalTrade(
            user_id=user_id, signal_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
            buy_order_id=f"PAPER-{uuid.uuid4()}",
            buy_price=price, quantity=quantity, allocated_value=quantity * price,
            atr_at_entry=atr, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            status="OPEN", is_paper=True, buy_confirmed=True,
        )
        db.session.add(trade)
        db.session.commit()
        _log(
            user_id, "BUY",
            f"[PAPER] {asset.ticker}: sygnał wejścia, {quantity} @ ~{price} - "
            f"SL {stop_loss_price:.4f} / TP {take_profit_price:.4f} (ATR={atr:.4f}).",
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
        _log(user_id, "ERROR", f"{asset.ticker}: błąd składania zlecenia kupna - {exc}")
        return

    trade = SignalTrade(
        user_id=user_id, signal_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
        buy_order_id=buy_result.order_id, baseline_owned_quantity=baseline,
        buy_price=price, quantity=quantity, allocated_value=quantity * price,
        atr_at_entry=atr, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
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
        f"{asset.ticker}: sygnał wejścia (RSI<{settings.rsi_threshold}, cena>MA{MA_PERIOD}), "
        f"{quantity} @ ~{price} - SL {stop_loss_price:.4f} / TP {take_profit_price:.4f} (ATR={atr:.4f}). "
        "Czeka na potwierdzenie kupna, dopiero potem uzbroi stop-loss.",
    )


def _process_entries(user_id: int, client: T212Client | None, settings: SignalSettings) -> None:
    assets = SignalAsset.query.filter_by(user_id=user_id).all()
    if not assets:
        return

    open_tickers = {
        t.ticker for t in
        SignalTrade.query.filter_by(user_id=user_id, status="OPEN").all()
    }

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for asset in assets:
        if asset.ticker in open_tickers:
            continue
        if not market_hours.is_market_open(asset.currency):
            continue
        if not _in_entry_window(asset.currency):
            continue

        candles = price_feed.get_mini_chart_ohlc(
            api_key, asset.ticker, days=SIGNAL_LOOKBACK_DAYS,
            alpaca_api_key=alpaca_key, alpaca_api_secret=alpaca_secret,
        )
        if not candles or len(candles) < MA_PERIOD:
            continue

        closes = [Decimal(str(c["c"])) for c in candles]
        rsi = _compute_rsi(closes)
        sma = _compute_sma(closes)
        atr = _compute_atr(candles)
        if rsi is None or sma is None or atr is None:
            continue

        price = price_feed.get_live_price(api_key, asset.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            continue

        if rsi < settings.rsi_threshold and price > sma:
            _enter_position(user_id, client, asset, settings, price, atr)


def _confirm_pending_entries(user_id: int, client: T212Client, settings: SignalSettings) -> None:
    """
    Sprawdza świeżo złożone zlecenia kupna (buy_confirmed=False) - gdy
    zniknęły z pending, uzbraja stop-loss (POJEDYNCZY resting STOP, patrz
    docstring modułu). Ten sam wzorzec co bot_engine.py przy potwierdzaniu
    wejścia Micro-Grid, uproszczony (brak DCA, brak trailing exitu).
    """
    pending_trades = SignalTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False,
    ).all()
    if not pending_trades:
        return

    try:
        pending_order_ids = {str(o.get("id")) for o in client.get_pending_orders()}
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Nie udało się pobrać zleceń oczekujących (potwierdzanie kupna) - {exc}")
        return

    for trade in pending_trades:
        if trade.buy_order_id in pending_order_ids:
            continue  # wciąż czeka na wypełnienie

        try:
            position = client.get_position(trade.ticker)
        except T212APIError as exc:
            _log(user_id, "ERROR", f"{trade.ticker}: błąd sprawdzenia portfela po zakupie - {exc}")
            continue

        current_owned = Decimal(str(position["quantity"])) if position else Decimal("0")
        filled = current_owned - trade.baseline_owned_quantity
        if filled <= 0:
            trade.status = "CLOSED"
            trade.closed_via = "never-filled"
            trade.closed_at = dt.datetime.utcnow()
            db.session.commit()
            _log(user_id, "ERROR", f"{trade.ticker}: zlecenie kupna zniknęło z kolejki bez wypełnienia (anulowane/odrzucone).")
            continue

        trade.quantity = filled
        trade.allocated_value = filled * trade.buy_price
        trade.buy_confirmed = True
        db.session.commit()

        try:
            stop_result = client.place_stop_order(trade.ticker, -filled, trade.stop_loss_price)
            trade.stop_order_id = stop_result.order_id
            db.session.commit()
            _log(user_id, "INFO", f"{trade.ticker}: kupno potwierdzone ({filled} szt.), stop-loss uzbrojony na {trade.stop_loss_price:.4f}.")
        except T212APIError as exc:
            _log(
                user_id, "ERROR",
                f"{trade.ticker}: kupno potwierdzone, ale NIE udało się uzbroić stop-loss - {exc}. "
                "Pozycja NIECHRONIONA żadnym resting orderem, sprawdź ręcznie.",
            )


def _manage_exits(user_id: int, client: T212Client, settings: SignalSettings) -> None:
    open_trades = SignalTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True,
    ).all()
    if not open_trades:
        return

    try:
        pending_order_ids = {str(o.get("id")) for o in client.get_pending_orders()}
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Nie udało się pobrać zleceń oczekujących (wyjścia) - {exc}")
        return

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for trade in open_trades:
        if not market_hours.is_position_management_hours(trade.currency):
            continue

        # Stop-loss sam sie wykonal na T212 (zniknal z pending) - zamykamy lokalnie.
        if trade.stop_order_id and trade.stop_order_id not in pending_order_ids:
            _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=trade.stop_loss_price)
            continue

        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            continue

        if price < trade.take_profit_price:
            continue

        # Take-profit pilnowany w softwarze (patrz docstring modulu) - najpierw
        # zdejmujemy STOP (jedyne resting zlecenie), dopiero potem Market Sell.
        if trade.stop_order_id:
            try:
                client.cancel_order(trade.stop_order_id)
            except T212APIError as exc:
                # Mogl sie wlasnie wykonac rownolegle (wyscig z T212) - kolejny
                # tick wykryje to wyzej (zniknie z pending_order_ids). Nie
                # sprzedajemy TERAZ, zeby nie zdublowac sprzedazy.
                _log(user_id, "INFO", f"{trade.ticker}: anulowanie stop-loss przed take-profit nie powiodło się (mógł się właśnie wykonać) - {exc}")
                continue

        try:
            sell_result = client.place_market_order(trade.ticker, -trade.quantity)
        except T212APIError as exc:
            _log(user_id, "ERROR", f"{trade.ticker}: take-profit osiągnięty, ale sprzedaż Market nie powiodła się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA, sprawdź ręcznie.")
            continue

        _log_order(
            user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
            price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
        )
        _finalize_closed_trade(user_id, trade, "take-profit", fill_price=price)


def _manage_paper_exits(user_id: int) -> None:
    """Pozycje papierowe nie maja zadnego zlecenia na T212 - stop-loss/take-profit sprawdzane WYLACZNIE tutaj, w softwarze."""
    open_trades = SignalTrade.query.filter_by(user_id=user_id, status="OPEN", is_paper=True).all()
    if not open_trades:
        return

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for trade in open_trades:
        if not market_hours.is_position_management_hours(trade.currency):
            continue
        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            continue
        if price <= trade.stop_loss_price:
            _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=price)
        elif price >= trade.take_profit_price:
            _finalize_closed_trade(user_id, trade, "take-profit", fill_price=price)


def reconcile(user_id: int) -> None:
    """Wołane raz zaraz po aktywacji (routes/signal.py::activate) - nadgania stan bez czekania na najbliższy tick co 60s."""
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        _log(user_id, "ERROR", "Reconciliation: brak poświadczeń w bot_credentials mimo aktywacji.")
        return
    creds = get_decrypted_credentials(user_id, master_key, SIGNAL_ENVIRONMENT)
    if creds is None:
        _log(user_id, "ERROR", "Reconciliation: brak zapisanego klucza API demo, pomijam.")
        return
    settings = SignalSettings.query.filter_by(user_id=user_id).first()
    if settings is None:
        return

    _manage_paper_exits(user_id)
    if not settings.is_paper_trading:
        client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=SIGNAL_ENVIRONMENT)
        _confirm_pending_entries(user_id, client, settings)
        _manage_exits(user_id, client, settings)


def tick(app) -> None:
    """Wołane cyklicznie przez APScheduler co 60s (patrz app/__init__.py) - ten sam wzorzec co bot_engine.py::tick."""
    with app.app_context():
        for user_id in bot_credentials.active_user_ids():
            settings = SignalSettings.query.filter_by(user_id=user_id).first()
            if not settings or not settings.is_active:
                continue

            _manage_paper_exits(user_id)

            if settings.is_paper_trading:
                # Wejscia papierowe nie dotykaja T212 wcale (patrz _enter_position -
                # sprawdza is_paper_trading PRZED jakimkolwiek uzyciem client), stad
                # bezpieczne None zamiast prawdziwego T212Client. Okno wejscia
                # sprawdzane per-aktywo/waluta wewnatrz _process_entries.
                _process_entries(user_id, None, settings)
                continue

            master_key = bot_credentials.get_master_key(user_id)
            if master_key is None:
                continue
            creds = get_decrypted_credentials(user_id, master_key, SIGNAL_ENVIRONMENT)
            if creds is None:
                _log(user_id, "ERROR", "Brak zapisanego klucza API demo, pomijam tick.")
                continue
            client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=SIGNAL_ENVIRONMENT)

            _confirm_pending_entries(user_id, client, settings)
            _manage_exits(user_id, client, settings)
            _process_entries(user_id, client, settings)
