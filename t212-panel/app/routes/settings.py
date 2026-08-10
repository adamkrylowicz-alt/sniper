"""
app/routes/settings.py
========================
Zarządzanie ulubionymi i siatką Warp Mode - to DWA OSOBNE pojęcia:

- favorites: szersza lista "co mnie interesuje" (watchlist), praktycznie
  bez limitu (MAX_FAVORITES_SIZE jako rozsądny sufit).
- warp_grid: PODZBIÓR favorites (max 9) - tickery faktycznie pokazywane
  jako kafelki w Warp Mode. Wybierasz je osobno spośród ulubionych.

Wyszukiwarka działa WYŁĄCZNIE po lokalnym cache'u (patrz
services/instrument_cache.py po wyjaśnienie dlaczego nie odpytujemy T212
na żywo przy każdym wyszukiwaniu).
"""

from __future__ import annotations

import json

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from ..extensions import db
from ..i18n import current_lang, t
from ..models import ApiKeySet, EODSettings, RiskSettings, SignalSettings, User, UserSettings
from ..services import instrument_cache, logo_cache
from ..services.market_hours import is_market_open as _market_open
from ..services.t212_client import T212APIError
from ..utils import avatar_hue, current_user_id, friendly_name, login_required
from .scalping import _get_client

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

MAX_GRID_SIZE = 9
MAX_FAVORITES_SIZE = 50


def _get_or_create_settings(user_id: int) -> UserSettings:
    settings = UserSettings.query.filter_by(user_id=user_id).first()
    if settings is None:
        settings = UserSettings(user_id=user_id)
        db.session.add(settings)
        db.session.commit()
    return settings


def _load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def _get_favorites(settings: UserSettings) -> list[str]:
    return _load_list(settings.favorites)


def _get_grid(settings: UserSettings) -> list[str]:
    return _load_list(settings.warp_grid)


def _save_favorites(settings: UserSettings, tickers: list[str]) -> None:
    settings.favorites = json.dumps(tickers)
    db.session.commit()


def _save_grid(settings: UserSettings, tickers: list[str]) -> None:
    settings.warp_grid = json.dumps(tickers)
    db.session.commit()


@settings_bp.route("/focus-tiles", methods=["POST"])
@login_required
def set_focus_tiles():
    """Zapisuje liczbę kafelków Focus Mode (1-9) w UserSettings."""
    try:
        n = int(request.form.get("focus_tiles", 1))
        n = min(max(n, 1), 9)
    except (TypeError, ValueError):
        n = 1

    settings = _get_or_create_settings(current_user_id())
    settings.focus_tiles = n
    db.session.commit()
    flash(f"Focus Mode: {n} {t('kafelek/kafelki jednocześnie.', current_lang())}")
    return redirect(url_for("settings.index"))


@settings_bp.route("/", methods=["GET"])
@login_required
def index():
    """Strona główna Ustawień - linki do Kluczy API i Watchlisty (kiedyś: Hard Cap itd.)."""
    current_user = User.query.get(current_user_id())
    pending_users = (
        User.query.filter_by(email_verified=True, is_active=False).order_by(User.created_at).all()
        if current_user and current_user.is_admin else []
    )
    return render_template(
        "settings_index.html",
        is_admin=bool(current_user and current_user.is_admin),
        pending_users=pending_users,
    )


@settings_bp.route("/costs", methods=["GET"])
@login_required
def costs_view():
    """
    Statyczna strona informacyjna - koszty i oplaty handlu na T212, przepisane
    z oficjalnej dokumentacji (trading212.com/terms/invest samo w sobie
    blokuje boty/scrapery Cloudflare'em, wiec zrodlem sa artykuly Help Centre
    T212, ktore NIE sa zablokowane - te same liczby, ta sama tresc).
    Zero dynamicznych danych/zapytan do T212 - to czysto referencyjny tekst,
    warty odswiezenia recznie od czasu do czasu (link do zrodla w stopce
    strony), nie warto tego automatyzowac/scrapowac przy kazdym wejsciu.
    """
    return render_template("costs.html")


def _require_admin() -> User:
    user = User.query.get(current_user_id())
    if user is None or not user.is_admin:
        abort(403)
    return user


@settings_bp.route("/pending-users/<int:user_id>/approve", methods=["POST"])
@login_required
def approve_user(user_id):
    _require_admin()
    target = User.query.get_or_404(user_id)
    target.is_active = True
    db.session.commit()
    flash(f"Zatwierdzono konto {target.username}.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/pending-users/<int:user_id>/reject", methods=["POST"])
@login_required
def reject_user(user_id):
    _require_admin()
    target = User.query.get_or_404(user_id)
    if target.is_admin:
        abort(403)  # bezpiecznik - admin nie moze przypadkiem odrzucic/skasowac siebie
    username = target.username
    db.session.delete(target)
    db.session.commit()
    flash(f"{t('Odrzucono i usunięto konto', current_lang())} {username}.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/theme", methods=["POST"])
@login_required
def toggle_theme():
    """Przełącza dark/light, zapisuje w UserSettings.dark_mode, zwraca nowy stan."""
    settings = _get_or_create_settings(current_user_id())
    settings.dark_mode = not settings.dark_mode
    db.session.commit()
    return jsonify(ok=True, dark_mode=settings.dark_mode)


@settings_bp.route("/environment", methods=["POST"])
@login_required
def toggle_environment():
    """
    Przełącza demo/live - JEDEN przełącznik dla całego konta (patrz
    utils.current_environment/UserSettings.active_environment, 2026-08-06,
    Adam: "przełącz na live... i dodaj guzik przełącznik live demo").

    Dwa bezpieczniki PRZED zapisem:
    1. Musi istnieć zapisany klucz API dla docelowego środowiska (inaczej
       każdy silnik/Warp Mode i tak zaraz dostanie "brak klucza").
    2. Żaden z 3 silników nie może być aktualnie aktywny - przełączenie
       środowiska POD aktywnym botem (który w danym ticku może być w
       trakcie odczytu poświadczeń) to dokładnie ten rodzaj wyścigu, którego
       unikaliśmy przy migracji prod->dev tego samego dnia. Wymaga
       świadomego "najpierw dezaktywuj boty".
    """
    user_id = current_user_id()
    payload = request.get_json(silent=True) or {}
    target = payload.get("environment")
    if target not in ("demo", "live"):
        return jsonify(ok=False, error="Nieprawidłowe środowisko."), 400

    has_key = ApiKeySet.query.filter_by(user_id=user_id, environment=target).first() is not None
    if not has_key:
        return jsonify(
            ok=False,
            error=f"Brak zapisanego klucza API dla środowiska '{target}' - dodaj go w Ustawienia -> Klucze API.",
        ), 400

    active_engines = []
    risk = RiskSettings.query.filter_by(user_id=user_id).first()
    if risk is not None and risk.is_bot_active:
        active_engines.append("Micro-Grid")
    signal = SignalSettings.query.filter_by(user_id=user_id).first()
    if signal is not None and signal.is_active:
        active_engines.append("Sygnał")
    eod = EODSettings.query.filter_by(user_id=user_id).first()
    if eod is not None and eod.is_active:
        active_engines.append("EOD")
    if active_engines:
        return jsonify(
            ok=False,
            error=f"Najpierw wyłącz aktywne boty ({', '.join(active_engines)}) - dopiero potem zmień środowisko.",
        ), 400

    settings = _get_or_create_settings(user_id)
    settings.active_environment = target
    db.session.commit()
    return jsonify(ok=True, active_environment=target)


@settings_bp.route("/watchlist", methods=["GET"])
@login_required
def watchlist_view():
    settings = _get_or_create_settings(current_user_id())
    favorite_tickers = _get_favorites(settings)
    grid_tickers = set(_get_grid(settings))

    # Hydratacja nazw z cache'u (jeśli dostępne) - żeby nie pokazywać samych
    # surowych tickerów bez kontekstu na liście ulubionych.
    from ..models import Instrument
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(favorite_tickers)).all()}
        if favorite_tickers else {}
    )
    cached = {t: friendly_name(i.name) for t, i in instruments_by_ticker.items()}

    # Automatyczne pobranie BRAKUJĄCYCH logo (ograniczone do kilku na wizytę,
    # patrz docstring ensure_logos_auto) - żadnego przycisku, dzieje się samo.
    logo_cache.ensure_logos_auto(
        current_app.static_folder, current_app.config.get("LOGO_DEV_API_KEY"), favorite_tickers
    )

    favorites = [
        {
            "ticker": t,
            "name": cached.get(t, ""),
            "in_grid": t in grid_tickers,
            "initial": (cached.get(t) or t)[0].upper(),
            "hue": avatar_hue(t),
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, t),
            "market_open": _market_open(instruments_by_ticker[t].currency_code) if t in instruments_by_ticker and instruments_by_ticker[t].currency_code else None,
        }
        for t in favorite_tickers
    ]

    return render_template(
        "watchlist.html",
        favorites=favorites,
        max_favorites=MAX_FAVORITES_SIZE,
        max_grid=MAX_GRID_SIZE,
        grid_count=len(grid_tickers),
        last_synced_at=instrument_cache.get_last_synced_at(),
    )


@settings_bp.route("/watchlist/search", methods=["GET"])
@login_required
def watchlist_search():
    """
    AJAX - JSON, czyta WYŁĄCZNIE lokalny cache, zero requestów do T212.
    Pusty ?q= + wybrana ?category= = przeglądanie tej zakładki alfabetycznie
    (panel "Wszystkie instrumenty"). Bez paginacji - limit ustawiony powyżej
    najliczniejszej kategorii (STOCK ~9.3k), więc jedno wywołanie zwraca
    komplet od razu (patrz watchlist.js - świadomie zrezygnowano z "Załaduj
    więcej" na rzecz pełnej listy).
    ?category= jedna z instrument_cache.CATEGORIES (stock_usd/stock_eur/other) -
    nierozpoznana/brakująca wartość = bez filtra kategorii.

    Dokłada is_favorite/is_grid/logo_filename do każdego wyniku, żeby JS mógł
    pokazać właściwy stan przycisków i logo bez dodatkowego requestu.
    """
    query = request.args.get("q", "")
    category = request.args.get("category") or None
    results = instrument_cache.search_instruments(query, limit=10000, category=category)

    settings = _get_or_create_settings(current_user_id())
    favorites = set(_get_favorites(settings))
    grid = set(_get_grid(settings))

    return jsonify(ok=True, results=[
        {
            "ticker": r.ticker,
            "name": friendly_name(r.name),
            "type": r.instrument_type,
            "currency": r.currency_code,
            "is_leveraged": r.is_leveraged,
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, r.ticker),
            "is_favorite": r.ticker in favorites,
            "is_grid": r.ticker in grid,
        }
        for r in results
    ])


@settings_bp.route("/watchlist/category-counts", methods=["GET"])
@login_required
def watchlist_category_counts():
    """
    Liczby instrumentów per zakładka (Akcje/ETF-y/ETP-y z dźwignią/Warranty) -
    do plakietek przy zakładkach. Reużywane też przez pie_detail.html
    (wyszukiwarka dodawania aktywów w Smart Virtual Pie korzysta z tego
    samego lokalnego cache instrumentów).
    """
    return jsonify(ok=True, counts=instrument_cache.get_category_counts())


# -- Ulubione (favorites) - szersza lista, bez wpływu na siatkę wprost -----

@settings_bp.route("/watchlist/add", methods=["POST"])
@login_required
def watchlist_add():
    ticker = (request.form.get("ticker") or "").strip()
    settings = _get_or_create_settings(current_user_id())
    tickers = _get_favorites(settings)

    if not ticker:
        flash(t("Brak tickera.", current_lang()))
    elif ticker in tickers:
        flash(f"{ticker} {t('jest już w ulubionych.', current_lang())}")
    elif len(tickers) >= MAX_FAVORITES_SIZE:
        flash(f"{t('Lista ulubionych jest pełna (max', current_lang())} {MAX_FAVORITES_SIZE}).")
    else:
        tickers.append(ticker)
        _save_favorites(settings, tickers)
        flash(f"{t('Dodano', current_lang())} {ticker} {t('do ulubionych.', current_lang())}")

    return redirect(url_for("settings.watchlist_view"))


@settings_bp.route("/watchlist/remove", methods=["POST"])
@login_required
def watchlist_remove():
    """Usuwa z ulubionych CAŁKOWICIE - jeśli ticker był też w siatce, znika i stamtąd."""
    ticker = (request.form.get("ticker") or "").strip()
    settings = _get_or_create_settings(current_user_id())

    favorites = _get_favorites(settings)
    if ticker in favorites:
        favorites.remove(ticker)
        _save_favorites(settings, favorites)

    grid = _get_grid(settings)
    if ticker in grid:
        grid.remove(ticker)
        _save_grid(settings, grid)

    flash(f"{t('Usunięto', current_lang())} {ticker} {t('z ulubionych.', current_lang())}")
    return redirect(url_for("settings.watchlist_view"))


# -- Siatka Warp Mode (warp_grid) - podzbiór ulubionych, max 9 ------------

@settings_bp.route("/grid/add", methods=["POST"])
@login_required
def grid_add():
    """
    Dodaje ticker DO SIATKI, max 9 w siatce naraz. Jeśli tickera jeszcze nie
    ma w ulubionych - dodaje go tam RÓWNIEŻ automatycznie (żeby dało się
    kliknąć "+ Siatka" wprost z panelu "Wszystkie instrumenty", bez
    wymuszania dwóch osobnych kroków).
    """
    ticker = (request.form.get("ticker") or "").strip()
    settings = _get_or_create_settings(current_user_id())

    favorites = _get_favorites(settings)
    grid = _get_grid(settings)

    if ticker in grid:
        flash(f"{ticker} {t('jest już w siatce.', current_lang())}")
        return redirect(url_for("settings.watchlist_view"))

    if len(grid) >= MAX_GRID_SIZE:
        flash(f"{t('Siatka jest pełna (max', current_lang())} {MAX_GRID_SIZE} {t('kafelków).', current_lang())}")
        return redirect(url_for("settings.watchlist_view"))

    if ticker not in favorites:
        if len(favorites) >= MAX_FAVORITES_SIZE:
            flash(f"{t('Lista ulubionych jest pełna (max', current_lang())} {MAX_FAVORITES_SIZE}) {t('- nie można dodać.', current_lang())}")
            return redirect(url_for("settings.watchlist_view"))
        favorites.append(ticker)
        _save_favorites(settings, favorites)

    grid.append(ticker)
    _save_grid(settings, grid)
    flash(f"{t('Dodano', current_lang())} {ticker} {t('do siatki.', current_lang())}")

    return redirect(url_for("settings.watchlist_view"))


@settings_bp.route("/grid/remove", methods=["POST"])
@login_required
def grid_remove():
    """Usuwa ticker Z SIATKI (zostaje nadal w ulubionych)."""
    ticker = (request.form.get("ticker") or "").strip()
    settings = _get_or_create_settings(current_user_id())

    grid = _get_grid(settings)
    if ticker in grid:
        grid.remove(ticker)
        _save_grid(settings, grid)
        flash(f"{t('Usunięto', current_lang())} {ticker} {t('z siatki.', current_lang())}")

    return redirect(url_for("settings.watchlist_view"))


@settings_bp.route("/logos/fetch", methods=["POST"])
@login_required
def fetch_logos():
    """
    Pobiera (RAZ, na dysk) logotypy dla WSZYSTKICH obecnych ulubionych,
    bez limitu (w przeciwieństwie do automatycznego ensure_logos_auto przy
    każdym wejściu na stronę, które robi tylko kilka na raz) - to jawna,
    świadoma akcja usera, więc może potrwać dłużej.
    """
    settings = _get_or_create_settings(current_user_id())
    tickers = _get_favorites(settings)

    if not tickers:
        flash(t("Brak ulubionych - najpierw dodaj jakieś instrumenty.", current_lang()))
        return redirect(url_for("settings.watchlist_view"))

    results = logo_cache.fetch_missing_logos(
        current_app.static_folder, current_app.config.get("LOGO_DEV_API_KEY"), tickers
    )
    fetched = sum(1 for ok in results.values() if ok)

    flash(f"{t('Pobrano/zaktualizowano', current_lang())} {fetched}/{len(tickers)} {t('logotypów.', current_lang())}")
    return redirect(url_for("settings.watchlist_view"))


@settings_bp.route("/logos/bulk-fetch", methods=["POST"])
@login_required
def bulk_fetch_logos():
    """
    Pobiera logo dla WSZYSTKICH zsynchronizowanych instrumentów (nie tylko
    ulubionych) - w tle, w osobnym wątku (patrz logo_cache.start_bulk_fetch),
    bo przy ~15800 instrumentach to dziesiątki minut, za długo na jeden
    request/response. AJAX - JS odpytuje potem /logos/bulk-fetch/status.

    Bezpieczne do wielokrotnego klikania: już pobrane logo są pomijane
    (fetch_and_cache_logo sam to sprawdza), a drugi bulk-fetch nie odpali
    się, jeśli poprzedni jeszcze trwa.
    """
    app = current_app._get_current_object()
    started = logo_cache.start_bulk_fetch(
        app, current_app.static_folder, current_app.config.get("LOGO_DEV_API_KEY")
    )
    if not started:
        return jsonify(ok=False, error="Pobieranie już trwa - sprawdź postęp poniżej.")
    return jsonify(ok=True)


@settings_bp.route("/logos/bulk-fetch/status", methods=["GET"])
@login_required
def bulk_fetch_logos_status():
    """Do pollowania z JS co kilka sekund, dopóki running=True."""
    return jsonify(ok=True, **logo_cache.bulk_fetch_status())


@settings_bp.route("/watchlist/refresh-cache", methods=["POST"])
@login_required
def refresh_cache():
    """
    Odświeża lokalny cache instrumentów - RĘCZNA akcja, z wymuszonym
    minimalnym odstępem czasu (patrz instrument_cache.MIN_REFRESH_INTERVAL),
    żeby nie zjeść wąskiego rate limitu T212 przez przypadkowe wielokrotne
    kliknięcie.
    """
    try:
        client = _get_client()
        count = instrument_cache.refresh_instrument_cache(client)
        flash(f"{t('Zsynchronizowano', current_lang())} {count} {t('instrumentów.', current_lang())}")
    except instrument_cache.RefreshTooSoonError as exc:
        minutes = int(exc.retry_after.total_seconds() // 60) + 1
        flash(f"{t('Za wcześnie - spróbuj ponownie za ~', current_lang())}{minutes} {t('min.', current_lang())}")
    except RuntimeError as exc:
        flash(str(exc))
    except T212APIError as exc:
        flash(f"{t('Błąd T212:', current_lang())} {exc}")

    return redirect(url_for("settings.watchlist_view"))
