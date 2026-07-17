"""
app/routes/scalping.py
=======================
Blueprint trybu Warp - siatka 3x3, one-click trading.

Wymaga zalogowania (@login_required) - klucz+sekret T212 brany z bazy
(ApiKeySet, zaszyfrowany master_key zalogowanego użytkownika), NIE z .env.
Ta wersja zastępuje wcześniejsze tymczasowe rozwiązanie z
T212_DEMO_API_KEY_TEMP, teraz gdy auth.py + api_keys.py istnieją.

UPROSZCZENIA POZOSTAŁE DO POPRAWY:
------------------------------------
1. [ZROBIONE] Siatka pobierana z UserSettings.warp_grid, DEFAULT_WARP_GRID to
   już tylko fallback dla nowych userów bez zapisanej siatki.
2. [ZROBIONE] Cena szacowana (estimated_price) jest teraz auto-wypełniana z
   Finnhub (warp.js::refreshTilePrices, endpoint /warp/quote) - T212 nadal
   nie daje jej w API zleceń, ale mamy niezależne źródło. User może nadal
   nadpisać ją ręcznie - patrz dataset.autoFilled w warp.js.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, jsonify, render_template, request

from ..extensions import db
from ..models import OrderLog
from ..services import logo_cache
from ..services.risk_guard import RiskGuard
from ..services.t212_client import T212APIError, T212Client
from ..utils import avatar_hue, current_master_key, current_user_id, login_required
from .api_keys import get_decrypted_credentials

scalping_bp = Blueprint("scalping", __name__, url_prefix="/warp")

# Tymczasowa, sztywna siatka - do zastąpienia UserSettings.warp_grid
# UWAGA: "META_US_EQ" nie istnieje w T212 - spółka Meta Platforms jest tam
# nadal pod starym symbolem sprzed rebrandingu z Facebooka. Zweryfikowane
# przez client.get_instruments() na koncie demo.
DEFAULT_WARP_GRID = [
    "AAPL_US_EQ", "MSFT_US_EQ", "NVDA_US_EQ",
    "TSLA_US_EQ", "AMZN_US_EQ", "GOOGL_US_EQ",
    "FB_US_EQ", "AMD_US_EQ", "NFLX_US_EQ",
]

# Per-user guardy w pamięci procesu - {user_id: RiskGuard}. Naprawione z
# pierwotnej wersji (jeden globalny _guard), bo kod bota Micro-Grid (Etap 2)
# będzie działał dla wielu użytkowników z jednego procesu - jeden wspólny
# guard oznaczałby, że cooldown/Hard Cap jednego usera wpływa na drugiego.
_guards: dict[int, RiskGuard] = {}


def _get_guard(user_id: int) -> RiskGuard:
    """Zwraca (tworząc przy pierwszym użyciu) RiskGuard danego użytkownika."""
    if user_id not in _guards:
        _guards[user_id] = RiskGuard(max_order_value=None, cooldown_ms=400)
    return _guards[user_id]


def _get_client(environment: str = "demo") -> T212Client:
    """
    Buduje klienta T212 dla ZALOGOWANEGO użytkownika, odszyfrowując jego
    klucz+sekret z bazy (ApiKeySet) przy pomocy master_key z bieżącej sesji.
    """
    creds = get_decrypted_credentials(current_user_id(), current_master_key(), environment)
    if creds is None:
        raise RuntimeError(
            f"Brak zapisanego klucza API dla środowiska '{environment}'. "
            f"Dodaj go w Ustawienia -> Klucze API."
        )
    return T212Client(
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        environment=environment,
    )


@scalping_bp.route("/", methods=["GET"])
@login_required
def warp_view():
    """
    Renderuje siatkę - z ulubionych użytkownika (UserSettings.warp_grid,
    ustawianych w /settings/watchlist), a jeśli user jeszcze nic nie wybrał -
    fallback do DEFAULT_WARP_GRID, żeby ekran nie był pusty od pierwszego
    logowania.

    Dodatkowo: ostatnie 5 zleceń do mini-widgetu "Historia" w lewym sidebarze.
    To zapytanie do WŁASNEJ bazy (OrderLog), nie do T212 - zero kosztu
    rate-limitu, można je robić przy każdym wejściu na stronę bez obaw.
    """
    import json
    from ..models import UserSettings

    settings = UserSettings.query.filter_by(user_id=current_user_id()).first()
    tickers = DEFAULT_WARP_GRID
    if settings and settings.warp_grid:
        try:
            saved = json.loads(settings.warp_grid)
            if saved:
                tickers = saved
        except (ValueError, TypeError):
            pass

    # Prawdziwe ulubione (favorites) - OSOBNE od tickers (=siatka)! To był
    # bug: sidebar "ULUBIONE" pokazywał wcześniej tickers (czyli siatkę),
    # a powinien pokazywać favorites (szerszą listę usera).
    favorite_tickers_raw = []
    if settings and settings.favorites:
        try:
            favorite_tickers_raw = json.loads(settings.favorites) or []
        except (ValueError, TypeError):
            pass

    # Automatyczne (ograniczone) dociaganie brakujacych logo dla siatki +
    # ulubionych - ten sam wzorzec co watchlist_view(), inaczej tickery
    # dodane spoza Watchlist (np. wprost do siatki) nigdy nie dostawaly
    # szansy na pobranie logo.
    logo_cache.ensure_logos_auto(
        current_app.static_folder,
        current_app.config.get("LOGO_DEV_API_KEY"),
        list(dict.fromkeys(tickers + favorite_tickers_raw)),
    )

    # Hydratacja: pełna nazwa + kolor awatara + czy aktualnie w siatce -
    # do klikalnej listy w sidebarze (klik = dodaj/usuń z siatki 3x3).
    grid_set = set(tickers)
    from ..models import Instrument
    cached_names = (
        {i.ticker: i.name for i in Instrument.query.filter(Instrument.ticker.in_(favorite_tickers_raw)).all()}
        if favorite_tickers_raw else {}
    )
    favorites = [
        {
            "ticker": t,
            "name": cached_names.get(t, ""),
            "initial": (cached_names.get(t) or t)[0].upper(),
            "hue": avatar_hue(t),
            "in_grid": t in grid_set,
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, t),
        }
        for t in favorite_tickers_raw
    ]

    # Kafelki siatki wzbogacone o logo/awatar - ten sam wzorzec co favorites
    # wyzej i watchlist.html (logo z dysku jesli jest, inaczej kolorowy
    # awatar z inicjalem).
    grid_tiles = [
        {
            "ticker": t,
            "initial": t.split("_")[0][0].upper(),
            "hue": avatar_hue(t),
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, t),
        }
        for t in tickers
    ]

    recent_orders = (
        OrderLog.query
        .filter_by(user_id=current_user_id())
        .order_by(OrderLog.created_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "warp.html", tickers=grid_tiles, recent_orders=recent_orders, favorites=favorites
    )


@scalping_bp.route("/pending", methods=["GET"])
@login_required
def pending_orders():
    """
    Lista oczekujących (niewykonanych) zleceń - do prawego panelu.
    Wywoływane RĘCZNIE przyciskiem "Pokaż" w UI (nie automatycznie przy
    ładowaniu strony) - to kolejny request do wąskiego rate limitu T212,
    więc nie chcemy go odpalać bez wyraźnej akcji użytkownika.
    """
    try:
        client = _get_client()
        orders = client.get_pending_orders()
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    return jsonify(ok=True, orders=orders)


@scalping_bp.route("/order", methods=["POST"])
@login_required
def place_order():
    """
    Przyjmuje JSON:
        {
            "ticker": "AAPL_US_EQ",
            "side": "buy" | "sell",
            "quantity": "1",              // string, zamieniane na Decimal
            "estimated_price": "190.50"   // opcjonalne, string albo brak/null
        }

    Zwraca JSON z wynikiem - kafelek w JS na tej podstawie pokazuje
    zielony/czerwony flash i gra dźwięk sukcesu/błędu.
    """
    payload = request.get_json(silent=True) or {}

    ticker = payload.get("ticker")
    side = payload.get("side")
    raw_quantity = payload.get("quantity")
    raw_estimated_price = payload.get("estimated_price")

    if not ticker or side not in ("buy", "sell") or not raw_quantity:
        return jsonify(ok=False, error="Brak wymaganych pól (ticker/side/quantity)."), 400

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

    # -- Bezpiecznik: Hard Cap + cooldown, ZANIM cokolwiek trafi do T212 -----
    guard_result = _get_guard(current_user_id()).check_before_order(ticker, quantity, estimated_price)

    if not guard_result.allowed:
        _log_order(
            user_id=current_user_id(), ticker=ticker, side=side, quantity=quantity,
            price_snapshot=estimated_price, status="blocked",
            block_reason=guard_result.decision.value,
        )
        return jsonify(
            ok=False,
            blocked=True,
            reason=guard_result.reason,
            decision=guard_result.decision.value,
        ), 200  # 200 celowo - to nie jest błąd serwera, tylko decyzja biznesowa

    # Kierunek wynika ze znaku - dodatnia = buy, ujemna = sell (konwencja T212)
    signed_quantity = quantity if side == "buy" else -quantity

    try:
        client = _get_client()
        result = client.place_market_order(ticker, signed_quantity)
    except RuntimeError as exc:
        # Brak klucza w .env - błąd konfiguracji, nie błąd tradingowy
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        _log_order(
            user_id=current_user_id(), ticker=ticker, side=side, quantity=quantity,
            price_snapshot=estimated_price, status="rejected",
            block_reason=f"T212_ERROR_{exc.status_code}",
        )
        return jsonify(ok=False, error=str(exc)), 502

    _log_order(
        user_id=current_user_id(), ticker=ticker, side=side, quantity=quantity,
        price_snapshot=estimated_price, status="sent",
        t212_order_id=result.order_id,
    )

    return jsonify(
        ok=True,
        order_id=result.order_id,
        status=result.status,
        ticker=ticker,
        side=side,
        quantity=str(quantity),
    )


@scalping_bp.route("/quote", methods=["GET"])
@login_required
def quote():
    """
    Cena live dla jednego tickera z Finnhub.
    Odpytywane przez JS co dynamicznie obliczony interwał
    (ceil(liczba_kafelkow * 1.2) sekund, patrz warp.js/focus.js).
    """
    from ..extensions import finnhub
    ticker = request.args.get("ticker", "").strip()
    if not ticker:
        return jsonify(ok=False, error="Brak tickera."), 400
    if not finnhub:
        return jsonify(ok=False, error="Finnhub nie skonfigurowany - brak FINNHUB_API_KEY w .env."), 503

    data = finnhub.get_quote(ticker)
    if not data:
        return jsonify(ok=False, error=f"Brak danych dla {ticker} (nieznany symbol lub brak połączenia)."), 404

    return jsonify(ok=True, ticker=ticker, quote=data)


@scalping_bp.route("/sparkline", methods=["GET"])
@login_required
def sparkline():
    """
    Dane historyczne (7 dni, ceny zamknięcia) do miniaturowego wykresu.
    Cache 1h po stronie serwera - dane dzienne nie zmieniają się co minutę.
    """
    from ..extensions import finnhub
    ticker = request.args.get("ticker", "").strip()
    if not ticker:
        return jsonify(ok=False, error="Brak tickera."), 400
    if not finnhub:
        return jsonify(ok=False, error="Finnhub nie skonfigurowany."), 503

    closes = finnhub.get_sparkline(ticker)
    if not closes:
        return jsonify(ok=False, error=f"Brak danych historycznych dla {ticker}."), 404

    return jsonify(ok=True, ticker=ticker, closes=closes)


@scalping_bp.route("/focus", methods=["GET"])
@login_required
def focus_view():
    """
    Focus Mode - duże kafelki, karuzela strzałkami, cena live z Finnhub.
    Liczba jednoczesnych kafelków z UserSettings.focus_tiles (domyślnie 1).
    """
    import json
    from ..models import UserSettings

    settings = UserSettings.query.filter_by(user_id=current_user_id()).first()

    tickers = DEFAULT_WARP_GRID
    if settings and settings.favorites:
        try:
            saved = json.loads(settings.favorites)
            if saved:
                tickers = saved
        except (ValueError, TypeError):
            pass

    focus_tiles = 1
    if settings and settings.focus_tiles:
        focus_tiles = min(max(int(settings.focus_tiles), 1), 9)

    # Dynamiczny interwał odświeżania żeby nie przekroczyć 60 req/min Finnhub
    import math
    refresh_interval_s = math.ceil(focus_tiles * 1.2)

    return render_template(
        "focus.html",
        tickers=tickers,
        focus_tiles=focus_tiles,
        refresh_interval_ms=refresh_interval_s * 1000,
    )


@scalping_bp.route("/account", methods=["GET"])
@login_required
def account_info():
    """
    Zwraca saldo + otwarte pozycje z ich zyskiem/stratą (ppl - profit/loss,
    pole zwracane bezpośrednio przez T212 dla /equity/portfolio).

    UWAGA: cash i portfolio są pobierane NIEZALEŻNIE. Odkryliśmy empirycznie
    (11.07.2026), że klucz API może mieć uprawnienia do /equity/portfolio
    (portfolio, P&L) a jednocześnie dostawać 403 na /equity/account/cash
    (saldo) - to dwa osobne zakresy uprawnień po stronie T212. Bez tego
    rozdzielenia jeden nieudany request (cash) blokował całą odpowiedź,
    łącznie z portfolio które akurat działało poprawnie.
    """
    try:
        client = _get_client()
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500

    cash = None
    cash_error = None
    try:
        cash = client.get_cash()
    except T212APIError as exc:
        cash_error = str(exc)

    try:
        portfolio = client.get_portfolio()
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    return jsonify(ok=True, cash=cash, cash_error=cash_error, positions=portfolio)


@scalping_bp.route("/cancel-all", methods=["POST"])
@login_required
def cancel_all():
    """
    Anuluje WSZYSTKIE oczekujące (jeszcze niewykonane) zlecenia dla podanej
    strony ("buy" albo "sell"). Kierunek rozpoznajemy po znaku quantity
    w każdym pending orderze (dodatnia = buy, ujemna = sell) - ta sama
    konwencja co przy składaniu zleceń w place_order().

    Przydatne np. gdy rynek jest zamknięty i nazbierało Ci się dużo zleceń
    Market czekających w kolejce (patrz nasza wcześniejsza rozmowa o "giełda
    śpi") - jeden klik zamiast anulować każde osobno.
    """
    payload = request.get_json(silent=True) or {}
    side = payload.get("side")

    if side not in ("buy", "sell"):
        return jsonify(ok=False, error="Podaj side: 'buy' albo 'sell'."), 400

    try:
        client = _get_client()
        pending = client.get_pending_orders()
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    def matches_side(order: dict) -> bool:
        try:
            qty = float(order.get("quantity", 0))
        except (TypeError, ValueError):
            return False
        return (qty > 0) == (side == "buy")

    to_cancel = [o for o in pending if matches_side(o)]

    results = []
    for order in to_cancel:
        order_id = str(order.get("id"))
        try:
            client.cancel_order(order_id)
            results.append({"id": order_id, "ok": True})
        except T212APIError as exc:
            results.append({"id": order_id, "ok": False, "error": str(exc)})

    cancelled_count = sum(1 for r in results if r["ok"])
    return jsonify(ok=True, cancelled=cancelled_count, total=len(to_cancel), details=results)


@scalping_bp.route("/limits", methods=["GET"])
@login_required
def limits():
    """
    Zwraca aktualny Hard Cap (max_order_value) - używane przez JS do
    policzenia czy pokazać modal potwierdzenia przed dużym zleceniem.
    None dopóki routes/settings.py nie pozwoli tego ustawić z poziomu UI -
    wtedy JS po prostu pomija sprawdzenie (nie ma z czym porównać).
    """
    value = _get_guard(current_user_id()).max_order_value
    return jsonify(ok=True, max_order_value=str(value) if value is not None else None)


@scalping_bp.route("/history", methods=["GET"])
@login_required
def history_view():
    """Prosty widok ostatnich 100 zleceń (wszystkich statusów) usera."""
    entries = (
        OrderLog.query
        .filter_by(user_id=current_user_id())
        .order_by(OrderLog.created_at.desc())
        .limit(100)
        .all()
    )
    return render_template("history.html", entries=entries)


def _log_order(
    *,
    user_id: int,
    ticker: str,
    side: str,
    quantity: Decimal,
    price_snapshot: Decimal | None,
    status: str,
    block_reason: str | None = None,
    t212_order_id: str | None = None,
    environment: str = "demo",
    pie_id: int | None = None,
) -> None:
    """
    Zapis do OrderLog - działa NIEZALEŻNIE od tego czy zlecenie poszło,
    zostało odrzucone przez T212, czy zablokowane lokalnie przez risk_guard.
    To jest ważne dla audytu: "blocked" nigdy nie dotarło do T212, ale
    chcemy wiedzieć że próba się wydarzyła.

    environment/pie_id: opcjonalne, żeby routes/pie.py (Smart Virtual Pie)
    mogło reużyć tę samą funkcję zamiast duplikować logikę zapisu - pie_id
    znaczy "to zlecenie wyszło z tego koszyka", None = Warp Mode jak dotąd.

    user_id: JAWNY parametr (nie current_user_id() wołane w środku) - Micro-Grid
    Bot (services/bot_engine.py) też reużywa tę funkcję z wątku w tle, gdzie
    current_user_id()/flask.g nie istnieją (brak aktywnego requestu). Wywołania
    z warstwy routes/* przekazują user_id=current_user_id() jawnie w miejscu
    wywołania - zachowanie identyczne jak wcześniej, tylko bez ukrytej zależności.
    """
    estimated_value = (quantity * price_snapshot) if price_snapshot else None

    entry = OrderLog(
        user_id=user_id,
        environment=environment,
        ticker=ticker,
        side=side,
        quantity=quantity,
        price_snapshot=price_snapshot,
        estimated_value=estimated_value,
        status=status,
        block_reason=block_reason,
        t212_order_id=t212_order_id,
        pie_id=pie_id,
    )
    db.session.add(entry)
    db.session.commit()
