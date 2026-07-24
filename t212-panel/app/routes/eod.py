"""
app/routes/eod.py
====================
Panel modułu EOD (End of Day, services/eod_engine.py) - TRZECI, osobny
silnik obok Micro-Grid (routes/bot.py) i strategii sygnałowej
(routes/signal.py). Ten sam wzorzec aktywacji hasłem + WSPÓLNY magazyn
poświadczeń (bot_credentials) co pozostałe dwa.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pytz
from flask import Blueprint, current_app, jsonify, render_template, request

from .. import cipher
from ..extensions import db
from ..models import ApiKeySet, EODAsset, EODAuditLog, EODSettings, EODTrade, Instrument, RiskSettings, SignalSettings, User
from ..services import bot_credentials, eod_engine, price_feed
from ..services.t212_client import T212APIError, T212Client
from ..utils import avatar_hue, current_master_key, current_user_id, friendly_name, login_required
from .api_keys import get_decrypted_credentials

eod_bp = Blueprint("eod", __name__, url_prefix="/eod")

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")


def _get_or_create_settings(user_id: int) -> EODSettings:
    settings = EODSettings.query.filter_by(user_id=user_id).first()
    if settings is None:
        settings = EODSettings(user_id=user_id)
        db.session.add(settings)
        db.session.commit()
    return settings


def _get_owned_asset(asset_id: int) -> EODAsset:
    return EODAsset.query.filter_by(id=asset_id, user_id=current_user_id()).first_or_404()


@eod_bp.route("/", methods=["GET"])
@login_required
def view():
    user_id = current_user_id()
    settings = _get_or_create_settings(user_id)

    logs_raw = (
        EODAuditLog.query
        .filter_by(user_id=user_id)
        .order_by(EODAuditLog.created_at.desc())
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

    all_assets = EODAsset.query.filter_by(user_id=user_id).order_by(EODAsset.created_at.desc()).all()
    open_trades = (
        EODTrade.query
        .filter_by(user_id=user_id, status="OPEN")
        .order_by(EODTrade.created_at.desc())
        .all()
    )

    tickers = list({a.ticker for a in all_assets} | {t.ticker for t in open_trades})
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(tickers)).all()}
        if tickers else {}
    )
    display_ticker_by_asset_id = {a.id: a.display_ticker for a in all_assets}

    from ..services.market_hours import is_market_open

    eod_assets = [
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
            "display_ticker": display_ticker_by_asset_id.get(t.eod_asset_id, t.ticker),
            "name": friendly_name(instruments_by_ticker[t.ticker].name) if t.ticker in instruments_by_ticker else "",
            "currency": t.currency,
            "quantity": str(t.quantity),
            "buy_price": str(t.buy_price),
            "stop_loss_price": str(t.stop_loss_price),
            "take_profit_price": str(t.take_profit_price),
            "drop_pct_at_entry": str(t.drop_pct_at_entry),
            "size_multiplier": str(t.size_multiplier),
            "buy_confirmed": t.buy_confirmed,
            "is_paper": t.is_paper,
            "created_at_local": pytz.utc.localize(t.created_at).astimezone(_AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "hue": avatar_hue(t.ticker),
            "market_open": is_market_open(t.currency),
        }
        for t in open_trades
    ]

    return render_template(
        "eod.html",
        settings=settings,
        credentials_active=bot_credentials.is_active(user_id),
        logs=logs,
        eod_assets=eod_assets,
        open_positions=open_positions,
    )


@eod_bp.route("/assets/add", methods=["POST"])
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

    if EODAsset.query.filter_by(user_id=user_id, ticker=ticker).first() is not None:
        return jsonify(ok=False, error=f"{ticker} jest już na liście modułu EOD."), 400

    asset = EODAsset(
        user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
        currency=instrument.currency_code or "EUR", entry_amount=entry_amount,
    )
    db.session.add(asset)
    db.session.commit()
    return jsonify(ok=True, id=asset.id)


@eod_bp.route("/asset/<int:asset_id>/remove", methods=["POST"])
@login_required
def remove_asset(asset_id):
    asset = _get_owned_asset(asset_id)
    db.session.delete(asset)
    db.session.commit()
    return jsonify(ok=True)


@eod_bp.route("/asset/<int:asset_id>/entry-amount", methods=["POST"])
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


@eod_bp.route("/prices", methods=["GET"])
@login_required
def asset_prices():
    user_id = current_user_id()
    api_key = current_app.config.get("FINNHUB_API_KEY")

    result = {}
    for asset in EODAsset.query.filter_by(user_id=user_id).all():
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


@eod_bp.route("/settings", methods=["POST"])
@login_required
def update_settings():
    settings = _get_or_create_settings(current_user_id())
    payload = request.get_json(silent=True) or {}

    try:
        stop_loss_pct = Decimal(str(payload.get("stop_loss_pct", settings.stop_loss_pct)))
        take_profit_pct = Decimal(str(payload.get("take_profit_pct", settings.take_profit_pct)))
        if stop_loss_pct <= 0 or take_profit_pct <= 0:
            raise ValueError("Wartości muszą być dodatnie.")
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Nieprawidłowe dane w ustawieniach ryzyka."), 400

    settings.stop_loss_pct = stop_loss_pct
    settings.take_profit_pct = take_profit_pct
    settings.is_paper_trading = bool(payload.get("is_paper_trading", settings.is_paper_trading))
    settings.force_close_enabled = bool(payload.get("force_close_enabled", settings.force_close_enabled))
    db.session.commit()

    return jsonify(ok=True)


@eod_bp.route("/activate", methods=["POST"])
@login_required
def activate():
    """JSON {"password": "..."} - ten sam wzorzec co routes/bot.py::activate / routes/signal.py::activate."""
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
        user_id=user_id, environment=eod_engine.EOD_ENVIRONMENT
    ).first() is not None
    if not has_demo_key:
        return jsonify(
            ok=False,
            error=(
                "Brak zapisanego klucza API demo. Moduł EOD działa wyłącznie na demo "
                "(T212 nie wspiera zleceń STOP na live) - dodaj klucz demo w Ustawienia -> Klucze API."
            ),
        ), 400

    bot_credentials.activate(user_id, master_key)

    settings = _get_or_create_settings(user_id)
    settings.is_active = True
    db.session.commit()

    eod_engine._log(user_id, "INFO", "Moduł EOD aktywowany.")
    eod_engine.reconcile(user_id)

    return jsonify(ok=True)


@eod_bp.route("/deactivate", methods=["POST"])
@login_required
def deactivate():
    """
    Wyłącza WYŁĄCZNIE moduł EOD - czyści WSPÓLNE poświadczenia tylko gdy
    Micro-Grid I Sygnał też są wyłączone (sprawdzenie krzyżowe obu), inaczej
    te dwa straciłyby dostęp bez ostrzeżenia.
    """
    user_id = current_user_id()

    settings = _get_or_create_settings(user_id)
    settings.is_active = False
    db.session.commit()

    bot_settings = RiskSettings.query.filter_by(user_id=user_id).first()
    signal_settings = SignalSettings.query.filter_by(user_id=user_id).first()
    if not (bot_settings and bot_settings.is_bot_active) and not (signal_settings and signal_settings.is_active):
        bot_credentials.deactivate(user_id)

    return jsonify(ok=True)


@eod_bp.route("/log/clear", methods=["POST"])
@login_required
def clear_log():
    user_id = current_user_id()
    EODAuditLog.query.filter_by(user_id=user_id).delete()
    db.session.commit()
    return jsonify(ok=True)


@eod_bp.route("/positions/<int:trade_id>/close", methods=["POST"])
@login_required
def close_position(trade_id):
    """Ręczne zamknięcie pozycji (siatka bezpieczeństwa na czas testów na żywo) - ten sam wzorzec co routes/signal.py::close_position."""
    user_id = current_user_id()
    trade = EODTrade.query.filter_by(id=trade_id, user_id=user_id, status="OPEN").first_or_404()

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
    price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)

    if trade.is_paper:
        eod_engine._finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
        return jsonify(ok=True)

    if not trade.buy_confirmed:
        return jsonify(ok=False, error="Zlecenie kupna jeszcze nie potwierdzone - poczekaj aż się wykona."), 400

    creds = get_decrypted_credentials(user_id, current_master_key(), eod_engine.EOD_ENVIRONMENT)
    if creds is None:
        return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=eod_engine.EOD_ENVIRONMENT)

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
    eod_engine._finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
    return jsonify(ok=True)


@eod_bp.route("/status", methods=["GET"])
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
