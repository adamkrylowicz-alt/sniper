"""
app/routes/pie.py
===================
Blueprint Smart Virtual Pie - prywatne "wirtualne ETF-y" (Etap 1 z PRD
"Smart Virtual Pie & Micro-Grid Bot"). Pozwala dokupywać akcje "luzem" bez
opłaty FX 0.15%, z proporcjonalnymi wagami zamiast sztywnych %.

Reużywa infrastrukturę z scalping.py (Warp Mode) zamiast duplikować ją tu -
_get_client (odszyfrowanie kluczy T212 z sesji), _get_guard (Hard Cap +
cooldown, per-user, WSPÓLNY z Warp dla danego usera, żeby limity się nie
rozjeżdżały między trybami) i _log_order (zapis do OrderLog, teraz z
opcjonalnym pie_id).

UWAGA: Micro-Grid Bot (Moduł B) NIE korzysta z PieAsset - ma własną,
niezależną listę (BotAsset, patrz routes/bot.py i models.py). Ten plik to
WYŁĄCZNIE Smart Virtual Pie (ręczne dokupywanie), zero związku z botem.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from ..extensions import db
from ..i18n import current_lang, t
from ..models import Instrument, Pie, PieAsset
from ..services import price_feed
from ..services.market_data_keys import get_decrypted_market_data_keys
from ..services.market_hours import is_market_open as _market_open
from ..services.t212_client import T212APIError
from ..utils import avatar_hue, current_environment, current_master_key, current_user_id, friendly_name, login_required, ticker_display_name
from .scalping import _get_client, _get_guard, _log_order

pie_bp = Blueprint("pie", __name__, url_prefix="/pie")


def _get_owned_pie(pie_id: int) -> Pie:
    """404 zamiast 403/500 jeśli koszyk nie istnieje, należy do kogoś innego, albo należy do INNEGO środowiska (demo/live)."""
    return Pie.query.filter_by(
        id=pie_id, user_id=current_user_id(), environment=current_environment(current_user_id()),
    ).first_or_404()


def _get_owned_asset(asset_id: int) -> PieAsset:
    return PieAsset.query.filter_by(id=asset_id, user_id=current_user_id()).first_or_404()


# -- Koszyki (Pie) ----------------------------------------------------------

@pie_bp.route("/", methods=["GET"])
@login_required
def list_view():
    pies = (
        Pie.query
        .filter_by(user_id=current_user_id(), environment=current_environment(current_user_id()))
        .order_by(Pie.created_at.desc())
        .all()
    )
    return render_template("pie_list.html", pies=pies)


@pie_bp.route("/create", methods=["POST"])
@login_required
def create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash(t("Podaj nazwę koszyka.", current_lang()))
        return redirect(url_for("pie.list_view"))

    pie = Pie(user_id=current_user_id(), name=name, environment=current_environment(current_user_id()))
    db.session.add(pie)
    db.session.commit()
    return redirect(url_for("pie.detail", pie_id=pie.id))


@pie_bp.route("/<int:pie_id>", methods=["GET"])
@login_required
def detail(pie_id):
    pie = _get_owned_pie(pie_id)
    # Hydratacja do prostych dict-ów - szablon i JS nie muszą znać ORM-a,
    # a hue liczymy raz tutaj zamiast w Jinja (ten sam avatar_hue co
    # Warp/Watchlist, żeby kolory tickerów były spójne w całej appce).
    # Pełna nazwa spółki pod tickerem - sam skrót (np. "SPCX") nic nie mówi,
    # ten sam wzorzec co tile_data w scalping.py::focus_view().
    tickers = [a.ticker for a in pie.assets]
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(tickers)).all()}
        if tickers else {}
    )
    assets = [
        {
            "id": a.id,
            "ticker": a.ticker,
            "display_ticker": a.display_ticker,
            "name": friendly_name(instruments_by_ticker[a.ticker].name) if a.ticker in instruments_by_ticker else "",
            "currency": a.currency,
            "target_weight": str(a.target_weight),
            "hue": avatar_hue(a.ticker),
            "market_open": _market_open(a.currency),
        }
        for a in pie.assets
    ]
    return render_template("pie_detail.html", pie=pie, assets=assets)


@pie_bp.route("/<int:pie_id>/rename", methods=["POST"])
@login_required
def rename(pie_id):
    pie = _get_owned_pie(pie_id)
    name = (request.form.get("name") or "").strip()
    if name:
        pie.name = name
        db.session.commit()
    return redirect(url_for("pie.detail", pie_id=pie.id))


@pie_bp.route("/<int:pie_id>/delete", methods=["POST"])
@login_required
def delete(pie_id):
    pie = _get_owned_pie(pie_id)
    name = pie.name
    db.session.delete(pie)  # cascade="all, delete-orphan" na Pie.assets kasuje PieAsset-y
    db.session.commit()
    flash(f"{t('Usunięto koszyk', current_lang())} {name}.")
    return redirect(url_for("pie.list_view"))


# -- Aktywa w koszyku (PieAsset) ---------------------------------------------

@pie_bp.route("/<int:pie_id>/assets/add", methods=["POST"])
@login_required
def add_asset(pie_id):
    """
    Dodaje aktywo z LOKALNEGO cache Instrument (nigdy na żywo z T212 - patrz
    services/instrument_cache.py po wyjaśnienie dlaczego). Jeśli tickera nie
    ma w cache, użytkownik musi go najpierw odświeżyć w Ustawieniach.
    """
    pie = _get_owned_pie(pie_id)
    ticker = (request.form.get("ticker") or "").strip()

    if not ticker:
        flash(t("Brak tickera.", current_lang()))
        return redirect(url_for("pie.detail", pie_id=pie.id))

    instrument = Instrument.query.get(ticker)
    if instrument is None:
        flash(f"{ticker} {t('nie znaleziony w lokalnej bazie instrumentów - odśwież ją w Ustawieniach.', current_lang())}")
        return redirect(url_for("pie.detail", pie_id=pie.id))

    if PieAsset.query.filter_by(pie_id=pie.id, ticker=ticker).first() is not None:
        flash(f"{ticker_display_name(ticker)} {t('jest już w tym koszyku.', current_lang())}")
        return redirect(url_for("pie.detail", pie_id=pie.id))

    asset = PieAsset(
        user_id=current_user_id(),
        pie_id=pie.id,
        ticker=ticker,
        display_ticker=ticker.split("_")[0],
        currency=instrument.currency_code or "USD",
        target_weight=Decimal("1"),
    )
    db.session.add(asset)
    db.session.commit()
    flash(f"Dodano {ticker_display_name(ticker)} do koszyka.")
    return redirect(url_for("pie.detail", pie_id=pie.id))


@pie_bp.route("/asset/<int:asset_id>/remove", methods=["POST"])
@login_required
def remove_asset(asset_id):
    asset = _get_owned_asset(asset_id)
    pie_id = asset.pie_id
    db.session.delete(asset)
    db.session.commit()
    return redirect(url_for("pie.detail", pie_id=pie_id))


@pie_bp.route("/asset/<int:asset_id>/weight", methods=["POST"])
@login_required
def update_weight(asset_id):
    """
    JSON {"target_weight": "1.5"} - wołane z JS PO PUSZCZENIU suwaka wagi
    (debounced, nie na każdy 'input'), patrz static/js/pie.js.
    """
    asset = _get_owned_asset(asset_id)
    payload = request.get_json(silent=True) or {}

    try:
        weight = Decimal(str(payload.get("target_weight")))
        if weight <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        return jsonify(ok=False, error="Nieprawidłowa waga (musi być > 0)."), 400

    asset.target_weight = weight
    db.session.commit()
    return jsonify(ok=True, target_weight=str(asset.target_weight))


# -- Kupno ---------------------------------------------------------------

@pie_bp.route("/asset/<int:asset_id>/buy", methods=["POST"])
@login_required
def buy_asset(asset_id):
    """
    JSON {"quantity": "...", "estimated_price": "..."} - ten sam pipeline
    guard -> T212 -> OrderLog co /warp/order (scalping.place_order), tylko
    BEZ side (Smart Virtual Pie to "dokupywanie", zawsze buy) i z pie_id
    dopisanym do loga.
    """
    asset = _get_owned_asset(asset_id)
    payload = request.get_json(silent=True) or {}

    raw_quantity = payload.get("quantity")
    raw_estimated_price = payload.get("estimated_price")

    if not raw_quantity:
        return jsonify(ok=False, error="Podaj ilość."), 400

    try:
        quantity = abs(Decimal(str(raw_quantity)))
    except (InvalidOperation, ValueError):
        return jsonify(ok=False, error="Nieprawidłowa ilość."), 400

    estimated_price = None
    if raw_estimated_price not in (None, ""):
        try:
            estimated_price = Decimal(str(raw_estimated_price))
        except (InvalidOperation, ValueError):
            return jsonify(ok=False, error="Nieprawidłowa szacowana cena."), 400

    guard_result = _get_guard(current_user_id()).check_before_order(asset.ticker, quantity, estimated_price)
    if not guard_result.allowed:
        _log_order(
            user_id=current_user_id(), ticker=asset.ticker, side="buy", quantity=quantity,
            price_snapshot=estimated_price, status="blocked",
            block_reason=guard_result.decision.value, pie_id=asset.pie_id,
        )
        return jsonify(
            ok=False, blocked=True,
            reason=guard_result.reason, decision=guard_result.decision.value,
        ), 200  # 200 celowo - decyzja biznesowa, nie błąd serwera (patrz scalping.py)

    try:
        client = _get_client()
        result = client.place_market_order(asset.ticker, quantity)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        _log_order(
            user_id=current_user_id(), ticker=asset.ticker, side="buy", quantity=quantity,
            price_snapshot=estimated_price, status="rejected",
            block_reason=f"T212_ERROR_{exc.status_code}", pie_id=asset.pie_id,
        )
        return jsonify(ok=False, error=str(exc)), 502

    _log_order(
        user_id=current_user_id(), ticker=asset.ticker, side="buy", quantity=quantity,
        price_snapshot=estimated_price, status="sent",
        t212_order_id=result.order_id, pie_id=asset.pie_id,
    )

    return jsonify(
        ok=True, order_id=result.order_id, status=result.status,
        ticker=asset.ticker, quantity=str(quantity),
    )


# -- Mini-wykresy + Smart FX Guard -------------------------------------------

@pie_bp.route("/<int:pie_id>/charts", methods=["GET"])
@login_required
def charts(pie_id):
    """
    Batch - jedno wywołanie JS->Flask dla WSZYSTKICH aktywów koszyka naraz,
    zamiast N osobnych requestów. Dane z Finnhub (services/price_feed.py),
    kompletnie niezależne od T212 - zero ryzyka zjedzenia jego rate limitu.
    Pełne OHLC (nie same zamknięcia) - pie.js rysuje świece, nie linię.
    """
    pie = _get_owned_pie(pie_id)
    tickers = [a.ticker for a in pie.assets]
    market_keys = get_decrypted_market_data_keys(current_user_id(), current_master_key())
    api_key = market_keys.get("finnhub_api_key")
    data = price_feed.get_mini_charts_ohlc(
        api_key, tickers,
        alpaca_api_key=market_keys.get("alpaca_api_key"),
        alpaca_api_secret=market_keys.get("alpaca_api_secret"),
    )
    return jsonify(ok=True, charts=data, has_api_key=bool(api_key))


@pie_bp.route("/<int:pie_id>/fx-check", methods=["GET"])
@login_required
def fx_check(pie_id):
    """
    RĘCZNY (przycisk), NIGDY pollowany - to wywołanie T212 (/equity/account/summary),
    a jego rate limit jest wąski (patrz t212_client.py/instrument_cache.py).

    ?total=<szacowana wartość zakupu w walucie koszyka> - opcjonalne, liczone
    po stronie JS z aktualnych pól ilość/cena na stronie.

    T212 API nie ujawnia salda PER WALUTA - dla koszyka wielowalutowego dajemy
    tylko miękkie ostrzeżenie zamiast twardej blokady (patrz plan/PRD).
    """
    pie = _get_owned_pie(pie_id)
    currencies = {a.currency for a in pie.assets}
    mixed = len(currencies) > 1

    try:
        client = _get_client()
        cash = client.get_cash()
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    free = cash.get("free")
    warning = None

    if mixed:
        warning = (
            "Koszyk miesza waluty - T212 API nie ujawnia salda per waluta, "
            "nie da się dokładnie zweryfikować. Może wystąpić opłata FX."
        )
    else:
        raw_total = request.args.get("total")
        if raw_total and free is not None:
            try:
                total = Decimal(str(raw_total))
                free_decimal = Decimal(str(free))
                if total > free_decimal:
                    warning = (
                        f"Szacowana wartość zakupu ({total:.2f}) przekracza "
                        f"dostępne środki ({free_decimal:.2f})."
                    )
            except (InvalidOperation, ValueError):
                pass

    return jsonify(
        ok=True,
        free=str(free) if free is not None else None,
        mixed_currencies=mixed,
        currencies=sorted(currencies),
        warning=warning,
    )
