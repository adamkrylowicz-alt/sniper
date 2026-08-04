"""
app/routes/signal.py
======================
Panel strategii sygnałowej RSI/MA/ATR (services/signal_engine.py) - OSOBNA
od Micro-Grid Bota (routes/bot.py), decyzja Adama 2026-07-24 (patrz
docs/IDEAS_v2.md). Aktywacja wymaga PONOWNEGO podania hasła, ten sam wzorzec
bezpieczeństwa co routes/bot.py::activate - i ten sam WSPÓLNY magazyn
poświadczeń (services/bot_credentials.py), żeby Adam nie podawał hasła dwa
razy dla dwóch botów działających na tym samym koncie demo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

import pytz
from flask import Blueprint, current_app, jsonify, render_template, request

from .. import cipher
from ..extensions import db
from ..models import ApiKeySet, Instrument, RiskSettings, SignalAsset, SignalAuditLog, SignalSettings, SignalTrade, User
from ..services import bot_credentials, price_feed, signal_engine
from ..services.t212_client import T212APIError, T212Client
from ..utils import avatar_hue, current_master_key, current_user_id, friendly_name, login_required
from .api_keys import get_decrypted_credentials

signal_bp = Blueprint("signal", __name__, url_prefix="/signal")

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")


def _get_or_create_settings(user_id: int) -> SignalSettings:
    settings = SignalSettings.query.filter_by(user_id=user_id).first()
    if settings is None:
        settings = SignalSettings(user_id=user_id)
        db.session.add(settings)
        db.session.commit()
    return settings


def _get_owned_asset(asset_id: int) -> SignalAsset:
    return SignalAsset.query.filter_by(id=asset_id, user_id=current_user_id()).first_or_404()


@signal_bp.route("/", methods=["GET"])
@login_required
def view():
    user_id = current_user_id()
    settings = _get_or_create_settings(user_id)

    logs_raw = (
        SignalAuditLog.query
        .filter_by(user_id=user_id)
        .order_by(SignalAuditLog.created_at.desc())
        .limit(50)
        .all()
    )
    logs = [
        {
            "action_type": l.action_type,
            "message": l.message,
            "created_at_local": pytz.utc.localize(l.created_at).astimezone(_AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for l in logs_raw
    ]

    all_assets = SignalAsset.query.filter_by(user_id=user_id).order_by(SignalAsset.created_at.desc()).all()
    open_trades = (
        SignalTrade.query
        .filter_by(user_id=user_id, status="OPEN")
        .order_by(SignalTrade.created_at.desc())
        .all()
    )

    tickers = list({a.ticker for a in all_assets} | {t.ticker for t in open_trades})
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(tickers)).all()}
        if tickers else {}
    )
    display_ticker_by_asset_id = {a.id: a.display_ticker for a in all_assets}

    from ..services.market_hours import is_market_open

    signal_assets = [
        {
            "id": a.id,
            "ticker": a.ticker,
            "display_ticker": a.display_ticker,
            "name": friendly_name(instruments_by_ticker[a.ticker].name) if a.ticker in instruments_by_ticker else "",
            "currency": a.currency,
            "entry_amount": str(a.entry_amount),
            "hue": avatar_hue(a.ticker),
            "market_open": is_market_open(a.currency),
        }
        for a in all_assets
    ]

    open_positions = [
        {
            "id": t.id,
            "ticker": t.ticker,
            "display_ticker": display_ticker_by_asset_id.get(t.signal_asset_id, t.ticker),
            "name": friendly_name(instruments_by_ticker[t.ticker].name) if t.ticker in instruments_by_ticker else "",
            "currency": t.currency,
            "quantity": str(t.quantity),
            "buy_price": str(t.buy_price),
            "stop_loss_price": str(t.stop_loss_price),
            "take_profit_price": str(t.take_profit_price),
            "buy_confirmed": t.buy_confirmed,
            "is_paper": t.is_paper,
            "created_at_local": pytz.utc.localize(t.created_at).astimezone(_AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "hue": avatar_hue(t.ticker),
            "market_open": is_market_open(t.currency),
        }
        for t in open_trades
    ]

    return render_template(
        "signal.html",
        settings=settings,
        credentials_active=bot_credentials.is_active(user_id),
        logs=logs,
        signal_assets=signal_assets,
        open_positions=open_positions,
    )


@signal_bp.route("/assets/add", methods=["POST"])
@login_required
def add_asset():
    user_id = current_user_id()
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip()

    instrument = Instrument.query.get(ticker) if ticker else None
    if instrument is None:
        return jsonify(ok=False, error=f"{ticker or '(brak)'} nie znaleziony w lokalnej bazie instrumentów."), 400

    try:
        entry_amount = Decimal(str(payload.get("entry_amount")))
        if entry_amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Podaj kwotę wejścia (musi być > 0)."), 400

    if SignalAsset.query.filter_by(user_id=user_id, ticker=ticker).first() is not None:
        return jsonify(ok=False, error=f"{ticker} jest już na liście strategii sygnałowej."), 400

    asset = SignalAsset(
        user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
        currency=instrument.currency_code or "USD", entry_amount=entry_amount,
    )
    db.session.add(asset)
    db.session.commit()
    return jsonify(ok=True, id=asset.id)


@signal_bp.route("/asset/<int:asset_id>/remove", methods=["POST"])
@login_required
def remove_asset(asset_id):
    asset = _get_owned_asset(asset_id)
    db.session.delete(asset)
    db.session.commit()
    return jsonify(ok=True)


@signal_bp.route("/asset/<int:asset_id>/entry-amount", methods=["POST"])
@login_required
def update_entry_amount(asset_id):
    asset = _get_owned_asset(asset_id)
    payload = request.get_json(silent=True) or {}

    try:
        entry_amount = Decimal(str(payload.get("entry_amount")))
        if entry_amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Nieprawidłowa kwota (musi być > 0)."), 400

    asset.entry_amount = entry_amount
    db.session.commit()
    return jsonify(ok=True, entry_amount=str(asset.entry_amount))


@signal_bp.route("/prices", methods=["GET"])
@login_required
def asset_prices():
    """Cena na żywo + przybliżona liczba akcji dla WSZYSTKICH aktywów strategii naraz - ten sam wzorzec co routes/bot.py::bot_asset_prices."""
    user_id = current_user_id()
    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    result = {}
    for asset in SignalAsset.query.filter_by(user_id=user_id).all():
        price = price_feed.get_live_price(api_key, asset.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            result[asset.ticker] = {"price": None, "implied_quantity": None}
            continue
        implied_quantity = asset.entry_amount / price
        result[asset.ticker] = {
            "price": str(price),
            "implied_quantity": str(implied_quantity.quantize(Decimal("0.000001"))),
        }
    return jsonify(ok=True, prices=result)


@signal_bp.route("/settings", methods=["POST"])
@login_required
def update_settings():
    settings = _get_or_create_settings(current_user_id())
    payload = request.get_json(silent=True) or {}

    try:
        rsi_threshold = Decimal(str(payload.get("rsi_threshold", settings.rsi_threshold)))
        stop_loss_atr_mult = Decimal(str(payload.get("stop_loss_atr_mult", settings.stop_loss_atr_mult)))
        take_profit_atr_mult = Decimal(str(payload.get("take_profit_atr_mult", settings.take_profit_atr_mult)))
        max_concurrent_positions = int(payload.get("max_concurrent_positions", settings.max_concurrent_positions))
        fx_fee_pct = Decimal(str(payload.get("fx_fee_pct", settings.fx_fee_pct)))

        if (
            rsi_threshold <= 0 or rsi_threshold >= 100 or stop_loss_atr_mult <= 0 or take_profit_atr_mult <= 0
            or max_concurrent_positions <= 0 or fx_fee_pct < 0
        ):
            raise ValueError("Wartości poza dozwolonym zakresem.")
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Nieprawidłowe dane w ustawieniach ryzyka."), 400

    settings.rsi_threshold = rsi_threshold
    settings.stop_loss_atr_mult = stop_loss_atr_mult
    settings.take_profit_atr_mult = take_profit_atr_mult
    settings.max_concurrent_positions = max_concurrent_positions
    settings.is_paper_trading = bool(payload.get("is_paper_trading", settings.is_paper_trading))
    settings.fx_cost_adjustment_enabled = bool(payload.get("fx_cost_adjustment_enabled", settings.fx_cost_adjustment_enabled))
    settings.fx_fee_pct = fx_fee_pct

    # Money management √equity - ten sam wzorzec auto-capture co routes/bot.py
    # (2026-07-31), dodany tutaj 2026-08-03 (Adam: "dodaj do obu").
    equity_sizing_enabled = bool(payload.get("equity_sizing_enabled", settings.equity_sizing_enabled))
    equity_sizing_turned_on = equity_sizing_enabled and not settings.equity_sizing_enabled
    equity_sizing_baseline = settings.equity_sizing_baseline

    if equity_sizing_turned_on:
        creds = get_decrypted_credentials(current_user_id(), current_master_key(), signal_engine.SIGNAL_ENVIRONMENT)
        if creds is None:
            return jsonify(
                ok=False,
                error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API) - potrzebny do "
                      "odczytania bieżącego equity jako punktu odniesienia dla skalowania.",
            ), 400
        client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=signal_engine.SIGNAL_ENVIRONMENT,
            engine="signal", user_id=current_user_id(),
        )
        try:
            cash = client.get_cash()
            equity_sizing_baseline = Decimal(str(cash["total"]))
        except (T212APIError, InvalidOperation, TypeError, KeyError) as exc:
            return jsonify(ok=False, error=f"Nie udało się odczytać equity z T212: {exc}"), 502
        if equity_sizing_baseline is None or equity_sizing_baseline <= 0:
            return jsonify(ok=False, error="Odczytane equity <= 0 - nie mogę ustawić punktu odniesienia."), 400

    settings.equity_sizing_enabled = equity_sizing_enabled
    settings.equity_sizing_baseline = equity_sizing_baseline
    db.session.commit()

    return jsonify(
        ok=True,
        equity_sizing_baseline=str(equity_sizing_baseline) if equity_sizing_baseline is not None else None,
    )


@signal_bp.route("/activate", methods=["POST"])
@login_required
def activate():
    """
    JSON {"password": "..."} - patrz routes/bot.py::activate, identyczna
    bramka bezpieczeństwa. bot_credentials.activate() to WSPÓLNY magazyn ze
    strategią Micro-Grid - jeśli Micro-Grid jest już aktywny, ten wywołanie
    tylko nadpisuje ten sam master_key (no-op efektywnie), żadnej kolizji.
    """
    payload = request.get_json(silent=True) or {}
    password = payload.get("password") or ""

    if not password:
        return jsonify(ok=False, error="Podaj hasło."), 400

    user_id = current_user_id()
    user = User.query.get(user_id)

    try:
        master_key = cipher.try_unwrap(user.wrapped_master_key_by_password, password, user.salt_password)
    except cipher.WrongCredentialsError:
        return jsonify(ok=False, error="Nieprawidłowe hasło."), 401

    has_demo_key = ApiKeySet.query.filter_by(
        user_id=user_id, environment=signal_engine.SIGNAL_ENVIRONMENT
    ).first() is not None
    if not has_demo_key:
        return jsonify(
            ok=False,
            error=(
                "Brak zapisanego klucza API demo. Strategia sygnałowa działa wyłącznie "
                "na demo (T212 nie wspiera zleceń STOP na live) - dodaj klucz demo "
                "w Ustawienia -> Klucze API."
            ),
        ), 400

    bot_credentials.activate(user_id, master_key, current_app.instance_path)

    settings = _get_or_create_settings(user_id)
    settings.is_active = True
    db.session.commit()

    signal_engine._log(user_id, "INFO", "Strategia sygnałowa aktywowana.")
    signal_engine.reconcile(user_id)

    return jsonify(ok=True)


@signal_bp.route("/deactivate", methods=["POST"])
@login_required
def deactivate():
    """
    Wyłącza WYŁĄCZNIE tę strategię (SignalSettings.is_active=False) - czyści
    WSPÓLNE poświadczenia (bot_credentials) tylko gdy Micro-Grid I moduł EOD
    też są wyłączone (sprawdzenie krzyżowe obu), inaczej któryś z nich
    straciłby dostęp bez ostrzeżenia mimo że user go nie dotykał.
    """
    user_id = current_user_id()

    settings = _get_or_create_settings(user_id)
    settings.is_active = False
    db.session.commit()

    from ..models import EODSettings
    bot_settings = RiskSettings.query.filter_by(user_id=user_id).first()
    eod_settings = EODSettings.query.filter_by(user_id=user_id).first()
    if not (bot_settings and bot_settings.is_bot_active) and not (eod_settings and eod_settings.is_active):
        bot_credentials.deactivate(user_id, current_app.instance_path)

    return jsonify(ok=True)


@signal_bp.route("/log/clear", methods=["POST"])
@login_required
def clear_log():
    user_id = current_user_id()
    SignalAuditLog.query.filter_by(user_id=user_id).delete()
    db.session.commit()
    return jsonify(ok=True)


@signal_bp.route("/positions/<int:trade_id>/close", methods=["POST"])
@login_required
def close_position(trade_id):
    """
    Ręczne zamknięcie pozycji (siatka bezpieczeństwa podczas testów na żywo,
    Adam 2026-07-24: "koduj, będziemy sprawdzać w boju") - anuluje stop-loss
    (jeśli uzbrojony) i sprzedaje Market. Dla pozycji papierowych - zero
    requestów do T212, tylko zamknięcie lokalnego rekordu po żywej cenie.
    """
    user_id = current_user_id()
    trade = SignalTrade.query.filter_by(id=trade_id, user_id=user_id, status="OPEN").first_or_404()

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
    price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)

    if trade.is_paper:
        signal_engine._finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
        return jsonify(ok=True)

    if not trade.buy_confirmed:
        return jsonify(ok=False, error="Zlecenie kupna jeszcze nie potwierdzone - poczekaj aż się wykona."), 400

    creds = get_decrypted_credentials(user_id, current_master_key(), signal_engine.SIGNAL_ENVIRONMENT)
    if creds is None:
        return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=signal_engine.SIGNAL_ENVIRONMENT)

    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            return jsonify(ok=False, error=f"Nie udało się anulować stop-loss przed ręczną sprzedażą - {exc}"), 502

    try:
        sell_result = client.place_market_order(trade.ticker, -trade.quantity)
    except T212APIError as exc:
        return jsonify(ok=False, error=f"Sprzedaż Market nie powiodła się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA."), 502

    from .scalping import _log_order
    _log_order(
        user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
        price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
    )
    signal_engine._finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
    return jsonify(ok=True)


@signal_bp.route("/asset/adopt", methods=["POST"])
@login_required
def adopt_position():
    """
    "Przekaż botowi" dla Sygnału - ten sam pomysł co routes/bot.py::adopt_position
    (2026-07-23), dodane 2026-08-04 na życzenie Adama ("ujednolić wszystkie
    boty pod tym względem") - Sygnał wcześniej NIE MIAŁ żadnego sposobu na
    ręczne przejęcie pozycji kupionej z ręki.

    W ODRÓŻNIENIU od Micro-Gridu (który zostawia stop_target_price=None do
    czasu aż _manage_trailing_exit uzbroi go po 2 progach zysku), Sygnał
    zawsze ma AKTYWNY resting stop od chwili wejścia (patrz signal_strategy.
    compute_entry) - więc adopcja MUSI od razu wystawić prawdziwe zlecenie
    STOP na T212, inaczej pozycja byłaby NIECHRONIONA aż do ręcznej
    interwencji (nie ma tu odpowiednika "czekaj na tick" jak w Micro-Gridzie).

    JSON {"ticker": "...", "entry_amount": "100.00"} - entry_amount TYLKO
    jeśli ticker nie jest jeszcze na liście Sygnału (SignalAsset).
    """
    user_id = current_user_id()
    settings = _get_or_create_settings(user_id)
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip()

    instrument = Instrument.query.get(ticker) if ticker else None
    if instrument is None:
        return jsonify(ok=False, error=f"{ticker or '(brak)'} nie znaleziony w lokalnej bazie instrumentów."), 400

    if SignalTrade.query.filter_by(user_id=user_id, ticker=ticker, status="OPEN").first() is not None:
        return jsonify(ok=False, error=f"{ticker} jest już zarządzany przez Sygnał."), 400

    from ..services.market_hours import held_by_other_engine
    other = held_by_other_engine(user_id, ticker, "signal")
    if other is not None:
        return jsonify(ok=False, error=f"{ticker} jest już zarządzany przez {other} - zwolnij go tam najpierw."), 400

    creds = get_decrypted_credentials(user_id, current_master_key(), signal_engine.SIGNAL_ENVIRONMENT)
    if creds is None:
        return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
    client = T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"], environment=signal_engine.SIGNAL_ENVIRONMENT,
        engine="signal", user_id=user_id,
    )

    try:
        position = client.get_position(ticker)
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if position is None:
        return jsonify(ok=False, error=f"Nie posiadasz {ticker} w portfelu T212 (demo)."), 400

    try:
        quantity = Decimal(str(position["quantity"]))
        avg_price = Decimal(str(position["averagePrice"]))
    except (KeyError, InvalidOperation, TypeError):
        return jsonify(ok=False, error="Nieprawidłowe dane pozycji zwrócone przez T212."), 502
    if quantity <= 0:
        return jsonify(ok=False, error=f"{ticker}: ilość w portfelu wynosi 0."), 400

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
    candles = price_feed.get_mini_chart_ohlc(
        api_key, ticker, days=signal_engine.SIGNAL_LOOKBACK_DAYS,
        alpaca_api_key=alpaca_key, alpaca_api_secret=alpaca_secret,
    )
    atr = signal_engine._compute_atr(candles) if candles else None
    if atr is None:
        return jsonify(ok=False, error=f"Nie udało się policzyć ATR dla {ticker} (brak/za mało świec) - spróbuj ponownie za chwilę."), 502

    stop_loss_price = avg_price - atr * settings.stop_loss_atr_mult
    take_profit_price = avg_price + atr * settings.take_profit_atr_mult
    if stop_loss_price <= 0:
        return jsonify(ok=False, error="Wyliczony stop-loss <= 0 (ATR zbyt duże względem ceny)."), 400

    asset = SignalAsset.query.filter_by(user_id=user_id, ticker=ticker).first()
    if asset is None:
        try:
            entry_amount = Decimal(str(payload.get("entry_amount")))
            if entry_amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            return jsonify(
                ok=False,
                error=f"{ticker} nie jest jeszcze na liście Sygnału - podaj kwotę wejścia (entry_amount, > 0).",
            ), 400
        asset = SignalAsset(
            user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
            currency=instrument.currency_code or "USD", entry_amount=entry_amount,
        )
        db.session.add(asset)
        db.session.flush()

    try:
        stop_result = client.place_stop_order(ticker, -quantity, stop_loss_price)
    except T212APIError as exc:
        return jsonify(ok=False, error=f"Nie udało się wystawić stop-loss na T212 - {exc}. Pozycja NIE zaadoptowana."), 502

    trade = SignalTrade(
        user_id=user_id, signal_asset_id=asset.id, ticker=ticker, currency=instrument.currency_code or "USD",
        buy_order_id=f"ADOPTED-{uuid.uuid4()}", baseline_owned_quantity=Decimal("0"),
        buy_price=avg_price, quantity=quantity, allocated_value=quantity * avg_price,
        atr_at_entry=atr, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
        stop_order_id=stop_result.order_id, status="OPEN", is_paper=False, buy_confirmed=True,
    )
    db.session.add(trade)
    db.session.commit()

    signal_engine._log(
        user_id, "INFO",
        f"{ticker}: pozycja adoptowana ręcznie z portfela T212 ({quantity} @ ~{avg_price}) - "
        f"stop-loss uzbrojony od razu na {stop_loss_price:.4f}.",
    )
    return jsonify(ok=True, trade_id=trade.id)


@signal_bp.route("/positions/<int:trade_id>/release", methods=["POST"])
@login_required
def release_position(trade_id):
    """Odwrotność adopt_position() - patrz routes/bot.py::release_position, ten sam wzorzec."""
    user_id = current_user_id()
    trade = SignalTrade.query.filter_by(id=trade_id, user_id=user_id, status="OPEN").first_or_404()

    if not trade.buy_confirmed:
        return jsonify(ok=False, error="Zlecenie kupna jeszcze nie potwierdzone - poczekaj aż się wykona."), 400

    if not trade.is_paper and trade.stop_order_id:
        creds = get_decrypted_credentials(user_id, current_master_key(), signal_engine.SIGNAL_ENVIRONMENT)
        if creds is None:
            return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
        client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=signal_engine.SIGNAL_ENVIRONMENT)
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            return jsonify(ok=False, error=f"Nie udało się anulować stop-loss przed zwolnieniem - {exc}"), 502

    trade.status = "RELEASED"
    trade.closed_at = None
    db.session.commit()
    signal_engine._log(user_id, "INFO", f"{trade.ticker}: pozycja zwolniona spod zarządzania Sygnału (udziały zostają na koncie).")
    return jsonify(ok=True)


@signal_bp.route("/status", methods=["GET"])
@login_required
def status():
    user_id = current_user_id()
    settings = _get_or_create_settings(user_id)
    return jsonify(
        ok=True,
        is_active=settings.is_active,
        credentials_active=bot_credentials.is_active(user_id),
        is_paper_trading=settings.is_paper_trading,
    )
