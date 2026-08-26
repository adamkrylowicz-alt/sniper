"""
app/routes/eod.py
====================
Panel modułu EOD (End of Day, services/eod_engine.py) - TRZECI, osobny
silnik obok Micro-Grid (routes/bot.py) i strategii sygnałowej
(routes/signal.py). Ten sam wzorzec aktywacji hasłem + WSPÓLNY magazyn
poświadczeń (bot_credentials) co pozostałe dwa.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

import pytz
from flask import Blueprint, current_app, jsonify, render_template, request

from .. import cipher
from ..extensions import db
from ..models import ApiKeySet, EODAsset, EODAuditLog, EODSettings, EODTrade, Instrument, RiskSettings, SignalSettings, User
from ..services import bot_credentials, eod_engine, price_feed
from ..services.market_data_keys import get_decrypted_market_data_keys
from ..services.t212_client import T212APIError, T212Client
from ..utils import avatar_hue, current_environment, current_master_key, current_user_id, friendly_name, login_required, ticker_display_name
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
    env = current_environment(user_id)

    logs_raw = (
        EODAuditLog.query
        .filter_by(user_id=user_id, environment=env)
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

    all_assets = EODAsset.query.filter_by(user_id=user_id, environment=env).order_by(EODAsset.created_at.desc()).all()
    open_trades = (
        EODTrade.query
        .filter_by(user_id=user_id, status="OPEN", environment=env)
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

    env = current_environment(user_id)
    if EODAsset.query.filter_by(user_id=user_id, ticker=ticker, environment=env).first() is not None:
        return jsonify(ok=False, error=f"{ticker_display_name(ticker)} jest już na liście modułu EOD."), 400

    asset = EODAsset(
        user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
        currency=instrument.currency_code or "EUR", entry_amount=entry_amount,
        environment=env,
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
    market_keys = get_decrypted_market_data_keys(user_id, current_master_key())
    api_key = market_keys.get("finnhub_api_key")
    alpaca_key = market_keys.get("alpaca_api_key")
    alpaca_secret = market_keys.get("alpaca_api_secret")

    result = {}
    for asset in EODAsset.query.filter_by(user_id=user_id, environment=current_environment(user_id)).all():
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


@eod_bp.route("/settings", methods=["POST"])
@login_required
def update_settings():
    settings = _get_or_create_settings(current_user_id())
    payload = request.get_json(silent=True) or {}

    try:
        stop_loss_pct = Decimal(str(payload.get("stop_loss_pct", settings.stop_loss_pct)))
        take_profit_pct = Decimal(str(payload.get("take_profit_pct", settings.take_profit_pct)))
        max_concurrent_positions = int(payload.get("max_concurrent_positions", settings.max_concurrent_positions))
        fx_fee_pct = Decimal(str(payload.get("fx_fee_pct", settings.fx_fee_pct)))
        if stop_loss_pct <= 0 or take_profit_pct <= 0 or max_concurrent_positions <= 0 or fx_fee_pct < 0:
            raise ValueError("Wartości muszą być dodatnie.")
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Nieprawidłowe dane w ustawieniach ryzyka."), 400

    settings.stop_loss_pct = stop_loss_pct
    settings.take_profit_pct = take_profit_pct
    settings.max_concurrent_positions = max_concurrent_positions
    settings.is_paper_trading = bool(payload.get("is_paper_trading", settings.is_paper_trading))
    settings.force_close_enabled = bool(payload.get("force_close_enabled", settings.force_close_enabled))
    settings.fx_cost_adjustment_enabled = bool(payload.get("fx_cost_adjustment_enabled", settings.fx_cost_adjustment_enabled))
    settings.fx_fee_pct = fx_fee_pct
    settings.stop_loss_only_mode = bool(payload.get("stop_loss_only_mode", settings.stop_loss_only_mode))

    # Money management √equity - ten sam wzorzec auto-capture co routes/bot.py
    # (2026-07-31), dodany tutaj 2026-08-03 (Adam: "dodaj do obu").
    equity_sizing_enabled = bool(payload.get("equity_sizing_enabled", settings.equity_sizing_enabled))
    equity_sizing_turned_on = equity_sizing_enabled and not settings.equity_sizing_enabled
    equity_sizing_baseline = settings.equity_sizing_baseline

    if equity_sizing_turned_on:
        creds = get_decrypted_credentials(current_user_id(), current_master_key(), current_environment(current_user_id()))
        if creds is None:
            return jsonify(
                ok=False,
                error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API) - potrzebny do "
                      "odczytania bieżącego equity jako punktu odniesienia dla skalowania.",
            ), 400
        client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(current_user_id()),
            engine="eod", user_id=current_user_id(),
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

    env = current_environment(user_id)
    has_key = ApiKeySet.query.filter_by(user_id=user_id, environment=env).first() is not None
    if not has_key:
        return jsonify(
            ok=False,
            error=(
                f"Brak zapisanego klucza API dla środowiska '{env}' - dodaj go w Ustawienia -> Klucze API."
            ),
        ), 400

    bot_credentials.activate(user_id, master_key, current_app.instance_path)

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
        bot_credentials.deactivate(user_id, current_app.instance_path)

    return jsonify(ok=True)


@eod_bp.route("/log/clear", methods=["POST"])
@login_required
def clear_log():
    user_id = current_user_id()
    EODAuditLog.query.filter_by(user_id=user_id, environment=current_environment(user_id)).delete()
    db.session.commit()
    return jsonify(ok=True)


@eod_bp.route("/positions/<int:trade_id>/close", methods=["POST"])
@login_required
def close_position(trade_id):
    """
    Ręczne zamknięcie pozycji (siatka bezpieczeństwa na czas testów na żywo).
    Logika przeniesiona do eod_engine.close_trade_manual (2026-08-09) żeby
    Telegram `/close` mogło jej użyć bez duplikowania.
    """
    user_id = current_user_id()
    trade = EODTrade.query.filter_by(
        id=trade_id, user_id=user_id, status="OPEN", environment=current_environment(user_id),
    ).first_or_404()

    ok, message = eod_engine.close_trade_manual(user_id, trade)
    if not ok:
        return jsonify(ok=False, error=message), 400
    return jsonify(ok=True)


@eod_bp.route("/asset/adopt", methods=["POST"])
@login_required
def adopt_position():
    """
    "Przekaż botowi" dla EOD - ten sam pomysł co routes/bot.py::adopt_position
    (2026-07-23), dodane 2026-08-04 ("ujednolić wszystkie boty pod tym
    względem"). Ten sam powód co Sygnał (routes/signal.py::adopt_position) -
    EOD zawsze ma AKTYWNY resting stop od wejścia, więc adopcja od razu
    wystawia prawdziwe zlecenie STOP na T212.

    Adoptowana pozycja NIE ma prawdziwego "spadku wyzwalającego" (nie weszła
    przez _worst_recent_drop) - drop_pct_at_entry=0/size_multiplier=1 jako
    formalne defaulty (kolumny NOT NULL), take_profit_price liczony sztywnym
    % (fallback z eod_strategy.compute_entry, bo nie ma "reference_price"
    sprzed spadku do czego wracać).

    JSON {"ticker": "...", "entry_amount": "100.00"}.
    """
    user_id = current_user_id()
    settings = _get_or_create_settings(user_id)
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip()

    instrument = Instrument.query.get(ticker) if ticker else None
    if instrument is None:
        return jsonify(ok=False, error=f"{ticker or '(brak)'} nie znaleziony w lokalnej bazie instrumentów."), 400

    env = current_environment(user_id)
    if EODTrade.query.filter_by(user_id=user_id, ticker=ticker, status="OPEN", environment=env).first() is not None:
        return jsonify(ok=False, error=f"{ticker_display_name(ticker)} jest już zarządzany przez EOD."), 400

    from ..services.market_hours import held_by_other_engine
    other = held_by_other_engine(user_id, ticker, "eod", include_reservation=False)
    if other is not None:
        return jsonify(ok=False, error=f"{ticker_display_name(ticker)} jest już zarządzany przez {other} - zwolnij go tam najpierw."), 400

    creds = get_decrypted_credentials(user_id, current_master_key(), current_environment(user_id))
    if creds is None:
        return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
    client = T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
        engine="eod", user_id=user_id,
    )

    try:
        position = client.get_position(ticker)
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if position is None:
        return jsonify(ok=False, error=f"Nie posiadasz {ticker_display_name(ticker)} w portfelu T212 (demo)."), 400

    try:
        quantity = Decimal(str(position["quantity"]))
        avg_price = Decimal(str(position["averagePrice"]))
    except (KeyError, InvalidOperation, TypeError):
        return jsonify(ok=False, error="Nieprawidłowe dane pozycji zwrócone przez T212."), 502
    if quantity <= 0:
        return jsonify(ok=False, error=f"{ticker_display_name(ticker)}: ilość w portfelu wynosi 0."), 400

    stop_loss_price = avg_price * (1 - settings.stop_loss_pct)
    take_profit_price = avg_price * (1 + settings.take_profit_pct)

    asset = EODAsset.query.filter_by(user_id=user_id, ticker=ticker, environment=env).first()
    if asset is None:
        try:
            entry_amount = Decimal(str(payload.get("entry_amount")))
            if entry_amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            return jsonify(
                ok=False,
                error=f"{ticker_display_name(ticker)} nie jest jeszcze na liście EOD - podaj kwotę wejścia (entry_amount, > 0).",
            ), 400
        asset = EODAsset(
            user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
            currency=instrument.currency_code or "USD", entry_amount=entry_amount,
            environment=env,
        )
        db.session.add(asset)
        db.session.flush()

    try:
        stop_result = client.place_stop_order(ticker, -quantity, stop_loss_price)
    except T212APIError as exc:
        return jsonify(ok=False, error=f"Nie udało się wystawić stop-loss na T212 - {exc}. Pozycja NIE zaadoptowana."), 502

    trade = EODTrade(
        user_id=user_id, eod_asset_id=asset.id, ticker=ticker, currency=instrument.currency_code or "USD",
        buy_order_id=f"ADOPTED-{uuid.uuid4()}", baseline_owned_quantity=Decimal("0"),
        buy_price=avg_price, quantity=quantity, allocated_value=quantity * avg_price,
        drop_pct_at_entry=Decimal("0"), size_multiplier=Decimal("1"),
        stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
        stop_order_id=stop_result.order_id, status="OPEN", is_paper=False, buy_confirmed=True,
        environment=env,
    )
    db.session.add(trade)
    db.session.commit()

    eod_engine._log(
        user_id, "INFO",
        f"{ticker_display_name(ticker)}: pozycja adoptowana ręcznie z portfela T212 ({quantity} @ ~{avg_price}) - "
        f"stop-loss uzbrojony od razu na {stop_loss_price:.4f}.",
    )
    return jsonify(ok=True, trade_id=trade.id)


@eod_bp.route("/positions/<int:trade_id>/release", methods=["POST"])
@login_required
def release_position(trade_id):
    """Odwrotność adopt_position() - patrz routes/bot.py::release_position, ten sam wzorzec."""
    user_id = current_user_id()
    trade = EODTrade.query.filter_by(
        id=trade_id, user_id=user_id, status="OPEN", environment=current_environment(user_id),
    ).first_or_404()

    if not trade.buy_confirmed:
        return jsonify(ok=False, error="Zlecenie kupna jeszcze nie potwierdzone - poczekaj aż się wykona."), 400

    if not trade.is_paper and trade.stop_order_id:
        creds = get_decrypted_credentials(user_id, current_master_key(), current_environment(user_id))
        if creds is None:
            return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
        client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id))
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            return jsonify(ok=False, error=f"Nie udało się anulować stop-loss przed zwolnieniem - {exc}"), 502

    trade.status = "RELEASED"
    trade.closed_at = None
    db.session.commit()
    eod_engine._log(user_id, "INFO", f"{ticker_display_name(trade.ticker)}: pozycja zwolniona spod zarządzania EOD (udziały zostają na koncie).")
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
