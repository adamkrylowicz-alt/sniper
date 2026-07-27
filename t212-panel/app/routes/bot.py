"""
app/routes/bot.py
===================
Panel Micro-Grid Bota. Aktywacja bota wymaga PONOWNEGO podania hasła (nie
wystarczy aktywna sesja przeglądarki) - świadoma bramka bezpieczeństwa, bo
to uruchamia proces działający NIEZALEŻNIE od tego czy przeglądarka jest
otwarta, docelowo operujący prawdziwymi (choć na razie tylko demo) zleceniami.

Bot działa WYŁĄCZNIE na demo - T212 nie wspiera zleceń LIMIT (na których
opiera się cała strategia Cancel-Replace bota) na koncie live - patrz
services/bot_engine.py::BOT_ENVIRONMENT.

Bot ma WŁASNĄ listę aktywów (BotAsset) - CAŁKOWICIE NIEZALEŻNĄ od Smart
Virtual Pie (PieAsset). To świadoma decyzja (nie pierwotny projekt) - patrz
models.py::BotAsset po uzasadnienie.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

import pytz
from flask import Blueprint, current_app, jsonify, render_template, request

from .. import cipher
from ..extensions import db
from ..models import ActiveTrade, ApiKeySet, BotAsset, BotAuditLog, Instrument, RiskSettings, User
from ..services import bot_credentials, bot_engine, price_feed
from ..services.t212_client import T212APIError, T212Client
from ..utils import avatar_hue, current_master_key, current_user_id, friendly_name, login_required
from .api_keys import get_decrypted_credentials

bot_bp = Blueprint("bot", __name__, url_prefix="/bot")

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")


def _get_or_create_settings(user_id: int) -> RiskSettings:
    settings = RiskSettings.query.filter_by(user_id=user_id).first()
    if settings is None:
        settings = RiskSettings(user_id=user_id)
        db.session.add(settings)
        db.session.commit()
    return settings


def _get_owned_bot_asset(asset_id: int) -> BotAsset:
    return BotAsset.query.filter_by(id=asset_id, user_id=current_user_id()).first_or_404()


@bot_bp.route("/", methods=["GET"])
@login_required
def view():
    user_id = current_user_id()
    settings = _get_or_create_settings(user_id)

    # created_at w bazie jest w UTC (dt.datetime.utcnow() w bot_engine.py::_log) -
    # przeliczone tutaj na czas Amsterdamu (Adam zgłosił 2026-07-21: "wyswietla
    # 21 a jest 23", dziennik pokazywał surowe UTC bez konwersji/etykiety).
    logs_raw = (
        BotAuditLog.query
        .filter_by(user_id=user_id)
        .order_by(BotAuditLog.created_at.desc())
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

    # Hydratacja do prostych dict-ów - ten sam wzorzec co routes/pie.py::detail.
    # Pełna nazwa spółki jako główny tekst (nie sam ticker) - ten sam wzorzec
    # co Warp/Focus/Aktywa/Virtual Pie, znaleziony brakujący tutaj 2026-07-21.
    all_bot_assets = BotAsset.query.filter_by(user_id=user_id).order_by(BotAsset.created_at.desc()).all()
    open_trades = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN")
        .order_by(ActiveTrade.created_at.desc())
        .all()
    )

    tickers = list({a.ticker for a in all_bot_assets} | {t.ticker for t in open_trades})
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(tickers)).all()}
        if tickers else {}
    )
    # display_ticker (forma krotka do UI) zyje na BotAsset, nie na ActiveTrade -
    # dociagane przez bot_asset_id, z fallbackiem na surowy ticker gdyby
    # BotAsset zostal juz usuniety spod pozycji (pozycja zostaje otwarta nawet
    # po usunieciu tickera z listy bota, patrz _process_entries).
    display_ticker_by_bot_asset_id = {a.id: a.display_ticker for a in all_bot_assets}

    bot_assets = [
        {
            "id": a.id,
            "ticker": a.ticker,
            "display_ticker": a.display_ticker,
            "name": friendly_name(instruments_by_ticker[a.ticker].name) if a.ticker in instruments_by_ticker else "",
            "currency": a.currency,
            "entry_amount": str(a.entry_amount),
            "is_penny_stock": a.is_penny_stock,
            "hue": avatar_hue(a.ticker),
            "market_open": bot_engine._market_open(a.currency),
        }
        for a in all_bot_assets
    ]

    open_positions = [
        {
            "id": t.id,
            "ticker": t.ticker,
            "display_ticker": display_ticker_by_bot_asset_id.get(t.bot_asset_id, t.ticker),
            "name": friendly_name(instruments_by_ticker[t.ticker].name) if t.ticker in instruments_by_ticker else "",
            "currency": t.currency,
            "quantity": str(t.quantity),
            "average_price": str(t.average_price),
            "dca_level": t.dca_level,
            "trail_milestone_steps": t.trail_milestone_steps,
            "stop_target_price": str(t.stop_target_price) if t.stop_target_price is not None else None,
            "sell_blocked": t.sell_blocked,
            "is_paper": t.is_paper,
            "created_at_local": pytz.utc.localize(t.created_at).astimezone(_AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "hue": avatar_hue(t.ticker),
            # Gielda WLASCIWA dla waluty tej pozycji - patrz bot_engine._market_open.
            # Poza tym oknem bot nic aktywnie nie robi dla niej (ale zlecenie
            # STOP juz wystawione na T212 zostaje i wykona sie samo u brokera).
            "market_open": bot_engine._market_open(t.currency),
        }
        for t in open_trades
    ]

    return render_template(
        "bot.html",
        settings=settings,
        credentials_active=bot_credentials.is_active(user_id),
        logs=logs,
        bot_assets=bot_assets,
        open_positions=open_positions,
    )


# -- Własna lista aktywów bota (BotAsset) - NIEZALEŻNA od Smart Virtual Pie -

@bot_bp.route("/assets/add", methods=["POST"])
@login_required
def add_bot_asset():
    """
    JSON {"ticker": "...", "entry_amount": "1.00"} - w odróżnieniu od Pie,
    tutaj OBA pola podajesz naraz (patrz models.py::BotAsset - entry_amount
    nie jest nullable, bycie na tej liście = bot będzie tym handlował).
    Ticker z LOKALNEGO cache Instrument, tak jak w Pie.
    """
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

    if BotAsset.query.filter_by(user_id=user_id, ticker=ticker).first() is not None:
        return jsonify(ok=False, error=f"{ticker} jest już na liście bota."), 400

    asset = BotAsset(
        user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
        currency=instrument.currency_code or "USD", entry_amount=entry_amount,
    )
    db.session.add(asset)
    db.session.commit()
    return jsonify(ok=True, id=asset.id)


@bot_bp.route("/asset/adopt", methods=["POST"])
@login_required
def adopt_position():
    """
    "Przekaż botowi" - adoptuje pozycję kupioną RĘCZNIE (Warp/Focus/Pie) do
    zarządzania przez bota, bez przechodzenia przez _enter_position(). Patrz
    pomysł #2, docs/IDEAS_v2.md ("Ręczne adoptowanie pojedynczej pozycji",
    2026-07-23) - świadomie jedna pozycja na raz, z potwierdzeniem w UI,
    zamiast globalnego switcha "zarządzaj wszystkim" (pomysł #1, NIE
    zaimplementowany).

    JSON {"ticker": "...", "entry_amount": "1.00"} - entry_amount WYMAGANE
    tylko jeśli ticker nie jest jeszcze na liście bota (BotAsset), bo określa
    wielkość PRZYSZŁYCH poziomów DCA (patrz BotAsset.entry_amount) - nie ma
    wpływu na już istniejącą ilość, która wchodzi 1:1 z portfela T212.

    Nowy ActiveTrade dostaje ilość/średnią cenę WPROST z T212 (nie z
    lokalnych obliczeń), dca_level=0, baseline_owned_quantity=0 (CAŁA
    posiadana ilość liczy się jako "botowa" od tego momentu - w odróżnieniu
    od _enter_position(), gdzie baseline chroni PRZED-istniejące posiadanie
    przy odejmowaniu; tu adopcja to świadoma decyzja oddania całej pozycji
    botowi, więc nie ma niczego do ochrony) i buy_confirmed=True (pozycja
    jest już rozliczona na T212, nie ma na co czekać jak przy świeżym LIMIT
    BUY). Od najbliższego ticku _manage_trailing_exit ją "widzi" i zaczyna
    liczyć progi trailing stopu od tej average_price.
    """
    user_id = current_user_id()
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip()

    instrument = Instrument.query.get(ticker) if ticker else None
    if instrument is None:
        return jsonify(ok=False, error=f"{ticker or '(brak)'} nie znaleziony w lokalnej bazie instrumentów."), 400

    if ActiveTrade.query.filter_by(user_id=user_id, ticker=ticker, status="OPEN").first() is not None:
        return jsonify(ok=False, error=f"{ticker} jest już zarządzany przez bota."), 400

    creds = get_decrypted_credentials(user_id, current_master_key(), "demo")
    if creds is None:
        return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment="demo")

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

    asset = BotAsset.query.filter_by(user_id=user_id, ticker=ticker).first()
    if asset is None:
        try:
            entry_amount = Decimal(str(payload.get("entry_amount")))
            if entry_amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            return jsonify(
                ok=False,
                error=(
                    f"{ticker} nie jest jeszcze na liście bota - podaj kwotę wejścia "
                    "(entry_amount, > 0) na przyszłe poziomy DCA."
                ),
            ), 400
        asset = BotAsset(
            user_id=user_id, ticker=ticker, display_ticker=ticker.split("_")[0],
            currency=instrument.currency_code or "USD", entry_amount=entry_amount,
        )
        db.session.add(asset)
        db.session.flush()  # potrzebne asset.id do FK ActiveTrade.bot_asset_id poniżej

    position_group_id = str(uuid.uuid4())
    trade = ActiveTrade(
        user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
        ticker=ticker, currency=instrument.currency_code or "USD",
        buy_order_id=f"ADOPTED-{uuid.uuid4()}",
        buy_price=avg_price, quantity=quantity, allocated_value=quantity * avg_price,
        average_price=avg_price, dca_level=0, grid_anchor_price=avg_price,
        baseline_owned_quantity=Decimal("0"),
        status="OPEN", is_paper=False, buy_confirmed=True,
    )
    db.session.add(trade)
    db.session.commit()

    bot_engine._log(
        user_id, "INFO",
        f"{ticker}: pozycja adoptowana ręcznie z portfela T212 ({quantity} @ ~{avg_price}) - "
        "od teraz zarządzana przez trailing exit bota.",
        position_group_id,
    )

    return jsonify(ok=True, trade_id=trade.id)


@bot_bp.route("/asset/<int:asset_id>/remove", methods=["POST"])
@login_required
def remove_bot_asset(asset_id):
    asset = _get_owned_bot_asset(asset_id)
    db.session.delete(asset)
    db.session.commit()
    return jsonify(ok=True)


@bot_bp.route("/asset/<int:asset_id>/entry-amount", methods=["POST"])
@login_required
def update_entry_amount(asset_id):
    asset = _get_owned_bot_asset(asset_id)
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


@bot_bp.route("/asset/<int:asset_id>/penny-toggle", methods=["POST"])
@login_required
def toggle_penny(asset_id):
    """Ręczny przełącznik - twarda blokada wejścia bota dla tego tickera (patrz bot_engine.py::_process_entries)."""
    asset = _get_owned_bot_asset(asset_id)
    asset.is_penny_stock = not asset.is_penny_stock
    db.session.commit()
    return jsonify(ok=True, is_penny_stock=asset.is_penny_stock)


@bot_bp.route("/prices", methods=["GET"])
@login_required
def bot_asset_prices():
    """
    Cena "na żywo" (Finnhub/Yahoo, ten sam price_feed.get_live_price co bot)
    + przybliżona liczba akcji przy obecnej entry_amount, dla WSZYSTKICH
    aktywów bota naraz (batch, jedno wywołanie JS->Flask - ten sam wzorzec
    co GET /pie/<id>/charts).

    warning=True gdy entry_amount jest poniżej empirycznie zaobserwowanego
    progu (bot_engine.MIN_ORDER_VALUE_ESTIMATE) - SZACUNEK, nie gwarancja,
    patrz komentarz przy tej stałej. Realne zlecenie i tak może się nie
    udać nawet gdy warning=False (dowiadujemy się o tym wtedy z dziennika
    bota, po fakcie) - to tylko pomoc, żeby nie wysyłać "na pewno za małych"
    zleceń bez potrzeby.
    """
    user_id = current_user_id()
    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    result = {}
    for asset in BotAsset.query.filter_by(user_id=user_id).all():
        price = price_feed.get_live_price(api_key, asset.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            result[asset.ticker] = {"price": None, "implied_quantity": None, "warning": None}
            continue

        implied_quantity = asset.entry_amount / price
        result[asset.ticker] = {
            "price": str(price),
            "implied_quantity": str(implied_quantity.quantize(Decimal("0.000001"))),
            "warning": asset.entry_amount < bot_engine.MIN_ORDER_VALUE_ESTIMATE,
        }

    return jsonify(ok=True, prices=result)


@bot_bp.route("/settings", methods=["POST"])
@login_required
def update_settings():
    settings = _get_or_create_settings(current_user_id())
    payload = request.get_json(silent=True) or {}

    try:
        dca_scenario = (payload.get("dca_scenario") or settings.dca_scenario).strip()
        max_dca_levels = int(payload.get("max_dca_levels", settings.max_dca_levels))
        dca_trigger_pct = Decimal(str(payload.get("dca_trigger_pct", settings.dca_trigger_pct)))
        max_spread_pct = Decimal(str(payload.get("max_spread_pct", settings.max_spread_pct)))
        take_profit_step_pct = Decimal(str(payload.get("take_profit_step_pct", settings.take_profit_step_pct)))
        stop_loss_pct = Decimal(str(payload.get("stop_loss_pct", settings.stop_loss_pct)))
        max_daily_loss = Decimal(str(payload.get("max_daily_loss", settings.max_daily_loss)))

        if (
            max_dca_levels <= 0 or dca_trigger_pct <= 0 or max_spread_pct <= 0
            or take_profit_step_pct <= 0 or stop_loss_pct <= 0 or max_daily_loss <= 0
        ):
            raise ValueError("Wartości muszą być dodatnie.")
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Nieprawidłowe dane w ustawieniach ryzyka."), 400

    settings.dca_scenario = dca_scenario
    settings.max_dca_levels = max_dca_levels
    settings.dca_trigger_pct = dca_trigger_pct
    settings.max_spread_pct = max_spread_pct
    settings.take_profit_step_pct = take_profit_step_pct
    settings.stop_loss_pct = stop_loss_pct
    settings.max_daily_loss = max_daily_loss
    settings.is_paper_trading = bool(payload.get("is_paper_trading", settings.is_paper_trading))
    db.session.commit()

    return jsonify(ok=True)


@bot_bp.route("/activate", methods=["POST"])
@login_required
def activate():
    """
    JSON {"password": "..."} - odszyfrowuje master_key hasłem (dokładnie ten
    sam kod co auth.py::login_view), aktywuje bot_credentials, blokuje
    aktywację bez zapisanego klucza demo, i na końcu woła Reconciliation Loop.
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

    # Bot działa wyłącznie na demo (patrz docstring modułu) - bez zapisanego
    # klucza demo aktywacja i tak byłaby bezużyteczna, więc blokujemy od razu.
    has_demo_key = ApiKeySet.query.filter_by(
        user_id=user_id, environment=bot_engine.BOT_ENVIRONMENT
    ).first() is not None
    if not has_demo_key:
        return jsonify(
            ok=False,
            error=(
                "Brak zapisanego klucza API demo. Bot działa wyłącznie na demo "
                "(T212 nie wspiera zleceń LIMIT na koncie live) - dodaj klucz "
                "demo w Ustawienia -> Klucze API."
            ),
        ), 400

    bot_credentials.activate(user_id, master_key)

    settings = _get_or_create_settings(user_id)
    settings.is_bot_active = True
    db.session.commit()

    # Jawne potwierdzenie w dzienniku OD RAZU - reconcile() poniżej ma własne
    # logi, ale dla pewności (i widocznego "tak, coś się stało" dla usera)
    # nie polegamy WYŁĄCZNIE na jego wewnętrznych ścieżkach.
    bot_engine._log(user_id, "INFO", "Bot aktywowany.")

    bot_engine.reconcile(user_id)

    return jsonify(ok=True)


@bot_bp.route("/deactivate", methods=["POST"])
@login_required
def deactivate():
    """
    Wyłącza WYŁĄCZNIE Micro-Grid (RiskSettings.is_bot_active=False) - czyści
    WSPÓLNE poświadczenia (bot_credentials) tylko gdy strategia sygnałowa I
    moduł EOD też są wyłączone (sprawdzenie krzyżowe obu), inaczej któryś z
    nich straciłby dostęp do klucza demo bez ostrzeżenia mimo że user go nie
    dotykał. Lustrzane odbicie routes/signal.py::deactivate() / routes/eod.py::deactivate().
    """
    user_id = current_user_id()

    settings = _get_or_create_settings(user_id)
    settings.is_bot_active = False
    db.session.commit()

    from ..models import EODSettings, SignalSettings
    signal_settings = SignalSettings.query.filter_by(user_id=user_id).first()
    eod_settings = EODSettings.query.filter_by(user_id=user_id).first()
    if not (signal_settings and signal_settings.is_active) and not (eod_settings and eod_settings.is_active):
        bot_credentials.deactivate(user_id)

    return jsonify(ok=True)


@bot_bp.route("/log/clear", methods=["POST"])
@login_required
def clear_log():
    """
    Czyści Dziennik bota (BotAuditLog) TYLKO dla bieżącego usera - błędy
    (ERROR) już tu nie trafiają (patrz bot_engine.py::_log, osobny plik
    instance/bot_errors.log), więc to czyści wyłącznie "pożądane" wpisy
    (BUY/INFO/WARN).
    """
    user_id = current_user_id()
    BotAuditLog.query.filter_by(user_id=user_id).delete()
    db.session.commit()
    return jsonify(ok=True)


@bot_bp.route("/positions/<int:trade_id>/unblock", methods=["POST"])
@login_required
def unblock_position(trade_id):
    """
    Reczne odblokowanie pozycji po sell_blocked=True (blad T212 inny niz
    "selling-equity-not-owned" przy wystawianiu/przesuwaniu trailing STOP-a -
    patrz bot_engine.py::_bump_retry - taki blad sam sie nigdy nie naprawi,
    bo retry z backoffem byłby tylko powtarzaniem tego samego błędu w kółko).
    Nie probuje nic naprawic samo z siebie - tylko zdejmuje blokade, zeby
    _manage_trailing_exit sprobowal ponownie na najblizszym ticku (np. po
    tym jak Adam recznie poprawil przyczyne na koncie T212).
    """
    user_id = current_user_id()
    trade = ActiveTrade.query.filter_by(id=trade_id, user_id=user_id, status="OPEN").first_or_404()
    trade.sell_blocked = False
    trade.sell_retry_count = 0
    trade.next_sell_retry_at = None
    db.session.commit()
    return jsonify(ok=True)


@bot_bp.route("/positions/<int:trade_id>/release", methods=["POST"])
@login_required
def release_position(trade_id):
    """
    Odwrotność adopt_position() - "Cofnij przekazanie": zwalnia pozycję spod
    zarządzania bota BEZ sprzedawania czegokolwiek (udziały zostają dokładnie
    tam gdzie są na T212 - to tylko bot przestaje ich pilnować). Działa na
    KAŻDEJ otwartej pozycji, nie tylko adoptowanych ręcznie - to ogólny
    "wyłącznik" jednej pozycji, symetryczny do adopt_position().

    Zanim zwolnimy, anulujemy WSZYSTKIE żywe zlecenia bota powiązane z tą
    pozycją (trailing STOP, zaległa noga DCA) - inaczej po zwolnieniu
    zostałby na koncie T212 "osierocony" resting order bota, który mógłby
    sprzedać akcje mimo że user właśnie poprosił o odzyskanie ręcznej
    kontroli (dokładnie odwrotność tego czego chciał).

    Status ustawiany na "RELEASED" (NIE "CLOSED" - CLOSED oznacza w całej
    reszcie kodu realną sprzedaż z close_price, patrz _finalize_closed_trade;
    tu żadna sprzedaż się nie odbyła). Wszystkie zapytania bota filtrują
    WYŁĄCZNIE status="OPEN", więc RELEASED znika z jego zarządzania od razu,
    bez żadnych dalszych zmian w bot_engine.py - i pozwala ponownie
    "przekazać botowi" tę samą pozycję później (adopt_position widzi wtedy
    brak wiersza OPEN dla tego tickera).
    """
    user_id = current_user_id()
    trade = ActiveTrade.query.filter_by(id=trade_id, user_id=user_id, status="OPEN").first_or_404()

    if not trade.buy_confirmed:
        return jsonify(ok=False, error="Zlecenie kupna jeszcze nie potwierdzone - poczekaj aż się wykona."), 400

    if not trade.is_paper:
        creds = get_decrypted_credentials(user_id, current_master_key(), "demo")
        if creds is None:
            return jsonify(ok=False, error="Brak zapisanego klucza API demo (Ustawienia -> Klucze API)."), 400
        client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment="demo")

        for order_id in (trade.stop_order_id, trade.sell_order_id, trade.dca_pending_buy_order_id):
            if not order_id:
                continue
            try:
                client.cancel_order(order_id)
            except T212APIError as exc:
                bot_engine._log(
                    user_id, "INFO",
                    f"{trade.ticker}: anulowanie zlecenia bota ({order_id}) przy zwalnianiu pozycji "
                    f"nie powiodło się (mogło się już wykonać/zniknąć) - {exc}",
                    trade.position_group_id,
                )

    trade.stop_order_id = None
    trade.sell_order_id = None
    trade.dca_pending_buy_order_id = None
    trade.dca_pending_quantity = None
    trade.dca_pending_price = None
    trade.dca_pending_baseline_quantity = None
    trade.status = "RELEASED"
    db.session.commit()

    bot_engine._log(
        user_id, "INFO",
        f"{trade.ticker}: pozycja zwolniona spod zarządzania bota na żądanie użytkownika "
        "(udziały zostają na koncie, bot już ich nie pilnuje).",
        trade.position_group_id,
    )

    return jsonify(ok=True)


@bot_bp.route("/status", methods=["GET"])
@login_required
def status():
    user_id = current_user_id()
    settings = _get_or_create_settings(user_id)
    return jsonify(
        ok=True,
        is_bot_active=settings.is_bot_active,
        credentials_active=bot_credentials.is_active(user_id),
        is_paper_trading=settings.is_paper_trading,
    )
