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

    result = {}
    for asset in SignalAsset.query.filter_by(user_id=user_id).all():
        price = price_feed.get_live_price(api_key, asset.ticker)
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

        if rsi_threshold <= 0 or rsi_threshold >= 100 or stop_loss_atr_mult <= 0 or take_profit_atr_mult <= 0:
            raise ValueError("Wartości poza dozwolonym zakresem.")
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Nieprawidłowe dane w ustawieniach ryzyka."), 400

    settings.rsi_threshold = rsi_threshold
    settings.stop_loss_atr_mult = stop_loss_atr_mult
    settings.take_profit_atr_mult = take_profit_atr_mult
    settings.is_paper_trading = bool(payload.get("is_paper_trading", settings.is_paper_trading))
    db.session.commit()

    return jsonify(ok=True)


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

    bot_credentials.activate(user_id, master_key)

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
    WSPÓLNE poświadczenia (bot_credentials) tylko gdy Micro-Grid (RiskSettings.
    is_bot_active) też jest wyłączony, inaczej ten drugi bot straciłby dostęp
    bez ostrzeżenia mimo że user go nie dotykał.
    """
    user_id = current_user_id()

    settings = _get_or_create_settings(user_id)
    settings.is_active = False
    db.session.commit()

    bot_settings = RiskSettings.query.filter_by(user_id=user_id).first()
    if not (bot_settings and bot_settings.is_bot_active):
        bot_credentials.deactivate(user_id)

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
