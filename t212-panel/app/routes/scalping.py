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
1. [ZROBIONE] Siatka (warp_grid) i tickery Focus Mode (favorites) - obie
   PUSTE domyślnie dla nowych kont (na życzenie Adama, 18.07.2026 - dawniej
   obie miały fallback do sztywnej listy popularnych spółek, co myliło:
   wyglądało jakby te tickery były "zahardkodowane" na stałe).
2. [ZROBIONE] Cena szacowana (estimated_price) jest teraz auto-wypełniana z
   Finnhub (warp.js::refreshTilePrices, endpoint /warp/quote) - T212 nadal
   nie daje jej w API zleceń, ale mamy niezależne źródło. User może nadal
   nadpisać ją ręcznie - patrz dataset.autoFilled w warp.js.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, jsonify, render_template, request

from ..extensions import db
from ..models import ActiveTrade, BotAsset, EODTrade, Instrument, OrderLog, SignalTrade
from ..services import logo_cache, price_feed
from ..services.market_hours import is_market_open as _market_open
from ..services.risk_guard import RiskGuard
from ..services.t212_client import T212APIError, T212Client
from ..utils import avatar_hue, current_master_key, current_user_id, friendly_name, login_required
from .api_keys import get_decrypted_credentials

scalping_bp = Blueprint("scalping", __name__, url_prefix="/warp")


# Per-user guardy w pamięci procesu - {user_id: RiskGuard}. Naprawione z
# pierwotnej wersji (jeden globalny _guard), bo kod bota Micro-Grid (Etap 2)
# będzie działał dla wielu użytkowników z jednego procesu - jeden wspólny
# guard oznaczałby, że cooldown/Hard Cap jednego usera wpływa na drugiego.
_guards: dict[int, RiskGuard] = {}

# Cache ostatniego udanego odczytu portfela per user - {user_id: {"positions":
# [...], "total_value": Decimal, "total_ppl": Decimal}}. "Moje aktywa" pokazuje
# to NATYCHMIAST przy wejsciu (bez czekania na T212), a swiezy odczyt dociaga
# JS z opoznieniem - patrz portfolio_view()/portfolio_refresh() nizej.
_portfolio_cache: dict[int, dict] = {}


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
        engine="web",
        user_id=current_user_id(),
    )


def _bot_order_sources(user_id: int) -> dict[str, str]:
    """
    Mapa {order_id: nazwa silnika} dla WSZYSTKICH zleceń kupna śledzonych
    przez którykolwiek z 3 silników bota (dowolny ticker, dowolna otwarta
    pozycja) - czysty odczyt (SELECT), zero zapisu. Używane do etykiety
    "source" i editable=False w /warp/pending i /warp/trade_levels -
    edycja/kasowanie z poziomu Warp/wykresu celowo NIE dotyka zleceń bota
    (Adam, 2026-07-30: "to ma nie być związane z botami" - edycja zleceń
    bota wymagałaby dodatkowej synchronizacji ActiveTrade/SignalTrade/
    EODTrade po każdej ręcznej zmianie, świadomie poza zakresem).
    """
    sources: dict[str, str] = {}
    for trade in ActiveTrade.query.filter_by(user_id=user_id, status="OPEN").all():
        if trade.buy_order_id:
            sources[str(trade.buy_order_id)] = "Micro-Grid"
        if trade.dca_pending_buy_order_id:
            sources[str(trade.dca_pending_buy_order_id)] = "Micro-Grid"
    for trade in SignalTrade.query.filter_by(user_id=user_id, status="OPEN").all():
        if trade.buy_order_id:
            sources[str(trade.buy_order_id)] = "Sygnał"
    for trade in EODTrade.query.filter_by(user_id=user_id, status="OPEN").all():
        if trade.buy_order_id:
            sources[str(trade.buy_order_id)] = "EOD"
    return sources


@scalping_bp.route("/", methods=["GET"])
@login_required
def warp_view():
    """
    Renderuje siatkę - z ulubionych użytkownika (UserSettings.warp_grid,
    ustawianych w /settings/watchlist). Jeśli user jeszcze nic nie wybrał,
    siatka jest PUSTA (celowo, bez zahardkodowanego fallbacku) - dopiero
    zaznaczenie tickerów w widgecie ULUBIONE dodaje je do siatki. Pusty stan
    ma czytelny komunikat w warp.html.

    Dodatkowo: ostatnie 5 zleceń do mini-widgetu "Historia" w lewym sidebarze.
    To zapytanie do WŁASNEJ bazy (OrderLog), nie do T212 - zero kosztu
    rate-limitu, można je robić przy każdym wejściu na stronę bez obaw.
    """
    import json
    from ..models import UserSettings

    settings = UserSettings.query.filter_by(user_id=current_user_id()).first()
    tickers = []
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
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(favorite_tickers_raw)).all()}
        if favorite_tickers_raw else {}
    )
    cached_names = {t: friendly_name(i.name) for t, i in instruments_by_ticker.items()}

    def _tile_market_open(ticker):
        instrument = instruments_by_ticker.get(ticker)
        return _market_open(instrument.currency_code) if instrument and instrument.currency_code else None

    favorites = [
        {
            "ticker": t,
            "name": cached_names.get(t, ""),
            "initial": (cached_names.get(t) or t)[0].upper(),
            "hue": avatar_hue(t),
            "in_grid": t in grid_set,
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, t),
            "market_open": _tile_market_open(t),
        }
        for t in favorite_tickers_raw
    ]

    # Kafelki siatki wzbogacone o logo/awatar - ten sam wzorzec co favorites
    # wyzej i watchlist.html (logo z dysku jesli jest, inaczej kolorowy
    # awatar z inicjalem). Nazwa spolki (nie tylko ticker) na kafelku - warp_grid
    # jest zawsze podzbiorem favorites, wiec cached_names juz ja ma, bez
    # dodatkowego zapytania.
    grid_tiles = [
        {
            "ticker": t,
            "name": cached_names.get(t, ""),
            "initial": t.split("_")[0][0].upper(),
            "hue": avatar_hue(t),
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, t),
            "market_open": _tile_market_open(t),
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
    Wywoływane automatycznie przy wejściu na /warp (patrz warp.js -
    loadPendingOrders() z opóźnieniem 1.5s), nie na żądanie przyciskiem.

    Dodane 2026-07-30: "editable" per zlecenie (True dla LIMIT BUY NIE
    śledzonych przez żaden silnik bota, patrz _bot_order_sources) - panel
    dorysowuje przyciski +/-/Zatwierdź/Skasuj WYŁĄCZNIE dla editable=True
    (Adam: "to ma nie być związane z botami"). "limitPrice" jest już w
    surowym dict z T212, tu tylko jawnie wykorzystane.
    """
    try:
        client = _get_client()
        # force_refresh=True - wolane tylko przy wejsciu na strone + po akcji
        # usera (nie na interwale, patrz warp.js), wiec bezpieczne; bez tego
        # swiezo zlozone/anulowane/edytowane zlecenie potrafilo "nie istniec"
        # jeszcze do 50s (ten sam bug co w cancel_one()/reprice_order()).
        orders = client.get_pending_orders(force_refresh=True)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    # Doklejenie pełnej nazwy spółki - ten sam wzorzec co Warp/Focus/Aktywa/
    # Virtual Pie/Bot (friendly_name() + Instrument.name), brakowało tutaj
    # (zgłoszone przez Adama 2026-07-21) - bez tego widget pokazywał sam
    # ticker (np. "SPCX_US_EQ"), nieczytelny na pierwszy rzut oka.
    tickers = [o.get("ticker") for o in orders if o.get("ticker")]
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(tickers)).all()}
        if tickers else {}
    )
    bot_sources = _bot_order_sources(current_user_id())
    for order in orders:
        instrument = instruments_by_ticker.get(order.get("ticker"))
        order["name"] = friendly_name(instrument.name) if instrument else None
        is_limit_buy = order.get("type") == "LIMIT" and order.get("side") == "BUY"
        order["editable"] = is_limit_buy and str(order.get("id")) not in bot_sources

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


@scalping_bp.route("/order/limit", methods=["POST"])
@login_required
def place_limit_order_route():
    """
    Skladanie NOWEGO zlecenia LIMIT (buy/sell), w odroznieniu od place_order()
    wyzej (zawsze MARKET). Dodane 2026-07-30 (Adam: "jak wystawić order limit
    np na cocacole?? jak kurwa??") - do tej pory jedynym sposobem bylo zlozyc
    je recznie w prawdziwej apce T212 albo poprosic o ad-hoc skrypt. Celowo
    "wszedzie" (Warp/Focus/instrument) - patrz odpowiednie *.js.

    NIE dotyczy zadnego z 3 silnikow bota - to zwykle, bezposrednie zlecenie
    na koncie, tak samo jak place_order(), wiec bez ograniczen typu
    _bot_order_sources (te dotycza tylko EDYCJI/KASOWANIA istniejacych
    zlecen, patrz reprice_order()/cancel_one() wyzej).

    Przyjmuje JSON: {"ticker": ..., "side": "buy"|"sell", "quantity": "...",
    "price": "..."} - wszystko wymagane, price to faktyczny limit (nie
    szacunek jak w place_order()).
    """
    payload = request.get_json(silent=True) or {}

    ticker = payload.get("ticker")
    side = payload.get("side")
    raw_quantity = payload.get("quantity")
    raw_price = payload.get("price")

    if not ticker or side not in ("buy", "sell") or not raw_quantity or not raw_price:
        return jsonify(ok=False, error="Brak wymaganych pól (ticker/side/quantity/price)."), 400

    try:
        quantity = abs(Decimal(str(raw_quantity)))
    except (InvalidOperation, ValueError):
        return jsonify(ok=False, error="Nieprawidłowa ilość."), 400
    if quantity <= 0:
        return jsonify(ok=False, error="Ilość musi być dodatnia."), 400

    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError):
        return jsonify(ok=False, error="Nieprawidłowa cena."), 400
    if price <= 0:
        return jsonify(ok=False, error="Cena musi być dodatnia."), 400

    guard_result = _get_guard(current_user_id()).check_before_order(ticker, quantity, price)
    if not guard_result.allowed:
        _log_order(
            user_id=current_user_id(), ticker=ticker, side=side, quantity=quantity,
            price_snapshot=price, status="blocked", block_reason=guard_result.decision.value,
        )
        return jsonify(
            ok=False, blocked=True, reason=guard_result.reason, decision=guard_result.decision.value,
        ), 200  # 200 celowo - decyzja biznesowa, nie błąd serwera (ten sam wzorzec co place_order())

    try:
        client = _get_client()
        if side == "buy":
            # _place_buy_with_precision_fallback (nie surowe place_limit_order)
            # - ten sam powod co w reprice_order() nizej: zle zaokraglona
            # ilosc dostaje 400 z T212 (quantity-precision-mismatch), fallback
            # przelicza i ponawia RAZ zamiast zostawic usera z bledem bez
            # zadnego zlecenia zlozonego.
            from ..services.bot_engine import _place_buy_with_precision_fallback
            result, quantity = _place_buy_with_precision_fallback(client, ticker, quantity, price)
        else:
            result = client.place_limit_order(ticker, -quantity, price)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        _log_order(
            user_id=current_user_id(), ticker=ticker, side=side, quantity=quantity,
            price_snapshot=price, status="rejected", block_reason=f"T212_ERROR_{exc.status_code}",
        )
        return jsonify(ok=False, error=str(exc)), 502

    _log_order(
        user_id=current_user_id(), ticker=ticker, side=side, quantity=quantity,
        price_snapshot=price, status="sent", t212_order_id=result.order_id,
    )

    return jsonify(
        ok=True, order_id=result.order_id, ticker=ticker, side=side,
        quantity=str(quantity), price=str(price),
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


@scalping_bp.route("/stats", methods=["GET"])
@login_required
def stats():
    """
    Statystyki fundamentalne dla strony szczegolow instrumentu (jak
    "Statystyki" w apce T212) - kapitalizacja rynkowa (profile2) + 52-tyg.
    zakres/P-E/dywidenda/wolumen (metric=all). Cache 24h po stronie
    finnhub_client.py - te liczby nie zmieniaja sie w ciagu dnia, jeden
    request na wejscie na strone (nie pollowane).
    """
    from ..extensions import finnhub
    ticker = request.args.get("ticker", "").strip()
    if not ticker:
        return jsonify(ok=False, error="Brak tickera."), 400
    if not finnhub:
        return jsonify(ok=False, error="Finnhub nie skonfigurowany."), 503

    financials = finnhub.get_basic_financials(ticker)
    profile = finnhub.get_profile(ticker)

    if not financials and not profile:
        return jsonify(ok=False, error=f"Brak statystyk dla {ticker}."), 404

    return jsonify(
        ok=True,
        ticker=ticker,
        market_cap=(profile or {}).get("marketCapitalization"),
        **(financials or {}),
    )


@scalping_bp.route("/candles", methods=["GET"])
@login_required
def candles():
    """
    Dane OHLC (open/high/low/close) do swiecowego wykresu w Focus Mode i na
    stronie szczegolow instrumentu - patrz finnhub_client.py::get_candles.
    Osobny endpoint od /sparkline (tamten zwraca same zamkniecia, wciaz
    uzywany np. przez mini-wykresy Smart Virtual Pie) - tu chcemy pelnego
    OHLC do swiec.

    ?days= opcjonalne (domyslnie 30) - przelacznik zakresu 1T/1M/3M/1R/MAX
    na stronie instrumentu (patrz instrument.js). Ograniczone do rozsadnego
    zakresu, zeby ktos przez pomylke/manipulacje URL-em nie zazadal np.
    100 lat danych.

    ?interval= opcjonalne, jedno z 1m/5m/15m/1h (dodane 2026-07-27, Adam:
    "świeczki 1min na wykresach, tam gdzie się da poki co czyli usa z
    alpaca", potem rozszerzone o kolejne interwały: "dodaj tez inne
    timestampy oprocz tych co juz sa"; rozszerzone o EU przez IBKR
    2026-07-28, patrz price_feed.IBKR_TICKER_MAP) - zakładki śróddzienne na
    stronie instrumentu. Pokrycie zależy od tickera (US zawsze przez
    Alpaca, EU tylko te z IBKR_TICKER_MAP) - `price_feed.get_intraday_chart`
    samo zwraca `None` dla tickerów bez pokrycia, stąd tu już nie ma
    twardej blokady na `_US_EQ`. `days` jest wtedy ignorowane (lookback
    zależny od interwału, patrz _INTRADAY_INTERVALS).
    """
    ticker = request.args.get("ticker", "").strip()
    if not ticker:
        return jsonify(ok=False, error="Brak tickera."), 400

    interval = request.args.get("interval", default="", type=str)
    if interval:
        candles = price_feed.get_intraday_chart(
            ticker, interval,
            current_app.config.get("ALPACA_API_KEY"),
            current_app.config.get("ALPACA_API_SECRET"),
        )
        if not candles:
            return jsonify(ok=False, error=f"Brak danych ({interval}) dla {ticker} (poza sesją albo źródło niedostępne)."), 404
        return jsonify(ok=True, ticker=ticker, candles=candles)

    from ..extensions import finnhub
    if not finnhub:
        return jsonify(ok=False, error="Finnhub nie skonfigurowany."), 503

    days = request.args.get("days", default=30, type=int) or 30
    days = min(max(days, 1), 1825)  # 1 dzien .. 5 lat

    data = finnhub.get_candles(ticker, days=days)
    if not data:
        return jsonify(ok=False, error=f"Brak danych świecowych dla {ticker}."), 404

    return jsonify(ok=True, ticker=ticker, candles=data)


@scalping_bp.route("/trade_levels", methods=["GET"])
@login_required
def trade_levels():
    """
    Poziomy stop-loss/take-profit dla otwartej pozycji BOTA (dowolnego z
    trzech silnikow) na danym tickerze - do linii na wykresie strony
    instrumentu (instrument.js). Adam, 2026-07-28: "pokazuj tez linie stop
    loss oraz tp o ile sa".

    Micro-Grid (ActiveTrade) ma TYLKO stop_target_price - trailing, RUCHOMY
    stop, celowo BEZ stalego take-profit (patrz bot_engine.py::
    _manage_trailing_exit - to caly sens strategii, goni cene w gore zamiast
    wyjsc na sztywnym progu). Sygnal (SignalTrade) i EOD (EODTrade) maja OBA,
    STALE: stop_loss_price/take_profit_price (patrz modele w models.py).

    Zwraca liste - moze byc pusta (brak otwartej pozycji bota na tym
    tickerze, np. akcja trzymana wylacznie recznie poza botem), a teoretycznie
    moze miec wiecej niz jeden wpis jesli ten sam ticker jest OTWARTY w wiecej
    niz jednym silniku naraz (rzadkie, kazdy silnik dziala niezaleznie,
    wlasna lista aktywow).

    Dodane 2026-07-30: TAKZE oczekujace zlecenia LIMIT BUY na tym tickerze
    (type="pending_buy") - realne zapytanie do T212 (get_pending_orders),
    nie tylko lokalne tabele bota. Kazdy wpis ma "editable" (False dla
    zlecen sledzonych przez bota, patrz _bot_order_sources) - instrument.js
    rysuje ich linie zawsze, ale pozwala przeciagac/edytowac WYLACZNIE
    editable=True (Adam: "to ma nie byc zwiazane z botami").
    """
    ticker = request.args.get("ticker", "").strip()
    if not ticker:
        return jsonify(ok=False, error="Brak tickera."), 400

    user_id = current_user_id()
    levels = []

    bot_trade = ActiveTrade.query.filter_by(user_id=user_id, ticker=ticker, status="OPEN").first()
    if bot_trade and bot_trade.stop_target_price is not None:
        levels.append({"type": "stop_loss", "price": float(bot_trade.stop_target_price), "source": "Micro-Grid"})

    signal_trade = SignalTrade.query.filter_by(user_id=user_id, ticker=ticker, status="OPEN").first()
    if signal_trade:
        # BEZ take_profit - usuniety 2026-07-28 (Adam: "usun sztywny
        # take-profit, zrob pelny trailing"), signal_trade.take_profit_price
        # w bazie zostaje wylacznie jako orientacyjny zapis z chwili wejscia,
        # nie wyzwala juz zadnej sprzedazy - pokazywanie go jako linii na
        # wykresie tylko by mylilo (wygladaloby na aktywny poziom wyjscia).
        levels.append({"type": "stop_loss", "price": float(signal_trade.stop_loss_price), "source": "Sygnał"})

    eod_trade = EODTrade.query.filter_by(user_id=user_id, ticker=ticker, status="OPEN").first()
    if eod_trade:
        levels.append({"type": "stop_loss", "price": float(eod_trade.stop_loss_price), "source": "EOD"})
        levels.append({"type": "take_profit", "price": float(eod_trade.take_profit_price), "source": "EOD"})

    # Oczekujące LIMIT BUY na tym tickerze (dodane 2026-07-30, Adam: "chce
    # zeby tez byla taka kreska jak odpale np buy limit order recznie") -
    # editable=False dla zleceń śledzonych przez bota (patrz
    # _bot_order_sources) - te zostają WYŁĄCZNIE do podglądu, edycja/
    # kasowanie z tego widoku celowo ich nie dotyka.
    # Prawdziwy bug (2026-07-30, Adam: "NIE MA ŻADNEGO" mimo realnie
    # istniejącego zlecenia na T212) - przy 429/timeout z T212 (rzadki, ale
    # realny - ciasny limit demo, cache błędu WSPÓLNY dla botów+strony na
    # 50s, patrz _cached_request_ex) tu było "except: pending = []", co
    # WYGLĄDAŁO jak "brak zleceń" (ok=True, pusta lista) zamiast "nie
    # udało się sprawdzić". Skutek: linia "Kupno LIMIT" na wykresie znikała
    # losowo przy odświeżeniu, mimo że zlecenie cały czas istniało - front
    # (instrument.js) teraz odróżnia te dwa stany dzięki pending_unavailable.
    pending_unavailable = False
    try:
        client = _get_client()
        # force_refresh=True - dodane po tym jak swiezo zlozony LIMIT (przez
        # nowy /warp/order/limit) potrafil nie pojawic sie na wykresie od
        # razu (ten sam 50s-cache bug co w cancel_one()/reprice_order()).
        # loadTradeLevels() jest wolane tylko przy wejsciu na strone + po
        # akcji usera (patrz instrument.js), nigdy na interwale - bezpieczne.
        pending = client.get_pending_orders(force_refresh=True)
    except (RuntimeError, T212APIError):
        pending = []
        pending_unavailable = True

    bot_sources = _bot_order_sources(user_id)
    for order in pending:
        if order.get("ticker") != ticker or order.get("type") != "LIMIT" or order.get("side") != "BUY":
            continue
        limit_price = order.get("limitPrice")
        if limit_price is None:
            continue
        order_id = str(order.get("id"))
        bot_source = bot_sources.get(order_id)
        levels.append({
            "type": "pending_buy",
            "price": float(limit_price),
            "source": bot_source or "Ręcznie",
            "order_id": order_id,
            "editable": bot_source is None,
        })

    return jsonify(ok=True, ticker=ticker, levels=levels, pending_unavailable=pending_unavailable)


@scalping_bp.route("/focus", methods=["GET"])
@login_required
def focus_view():
    """
    Focus Mode - duże kafelki, karuzela strzałkami, cena live z Finnhub.
    Liczba jednoczesnych kafelków z UserSettings.focus_tiles (domyślnie 1).
    Tickery z UserSettings.favorites (ta sama lista co ULUBIONE w Warp Mode) -
    PUSTE dla nowych kont, tak samo jak siatka Warp (na życzenie Adama,
    18.07.2026 - wcześniej fallback do sztywnej listy popularnych spółek
    wyglądał jak zahardkodowane tickery, ta sama myląca sytuacja co w Warp).
    """
    import json
    from ..models import UserSettings

    settings = UserSettings.query.filter_by(user_id=current_user_id()).first()

    tickers = []
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

    # Pełna nazwa spółki + waluta notowania pod tickerem (na życzenie Adama,
    # 18.07.2026 - sam skrót typu "IPOE" nic nie mówi kto to). Ten sam
    # wzorzec co grid_tiles w warp_view() wyżej.
    from ..models import Instrument
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(tickers)).all()}
        if tickers else {}
    )
    # Logo/awatar na kafelku (brakowalo od zawsze - Focus Mode nigdy nie mial
    # tego dociagniete, w odroznieniu od Warp/Watchlist - zob. pliki/to do.txt).
    # ensure_logos_auto ten sam ograniczony wzorzec co warp_view()/watchlist_view().
    logo_cache.ensure_logos_auto(
        current_app.static_folder,
        current_app.config.get("LOGO_DEV_API_KEY"),
        tickers,
    )
    tile_data = [
        {
            "ticker": t,
            "name": friendly_name(instruments_by_ticker[t].name) if t in instruments_by_ticker else "",
            "currency": instruments_by_ticker[t].currency_code if t in instruments_by_ticker else "",
            "market_open": _market_open(instruments_by_ticker[t].currency_code) if t in instruments_by_ticker and instruments_by_ticker[t].currency_code else None,
            "hue": avatar_hue(t),
            "initial": t.split("_")[0][0].upper(),
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, t),
        }
        for t in tickers
    ]

    return render_template(
        "focus.html",
        tickers=tickers,
        tile_data=tile_data,
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


@scalping_bp.route("/order/<order_id>/cancel", methods=["POST"])
@login_required
def cancel_one(order_id: str):
    """
    Anuluje POJEDYNCZE oczekujące zlecenie LIMIT BUY po ID - dodane 2026-07-30
    razem z reprice_order() niżej (Adam: "chcę żeby można je było edytować").
    Celowo TYLKO dla zleceń NIE śledzonych przez bota (patrz
    _bot_order_sources) - pełne uzasadnienie w docstringu reprice_order().
    """
    user_id = current_user_id()
    try:
        client = _get_client()
        # force_refresh=True - patrz docstring T212Client.get_pending_orders,
        # bez tego pomijalismy realnie istniejace, swiezo utworzone zlecenie
        # przez stary wpis w 50s cache'u wspoldzielonym z botami (404 mimo
        # ze zlecenie tam bylo, Adam: "nie da sie skasowac orderu").
        pending = client.get_pending_orders(force_refresh=True)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    order = next((o for o in pending if str(o.get("id")) == str(order_id)), None)
    if order is None:
        return jsonify(ok=False, error="Zlecenie nie istnieje albo już zostało wykonane/anulowane."), 404
    if order.get("type") != "LIMIT" or order.get("side") != "BUY":
        return jsonify(ok=False, error="Kasowanie z tego miejsca dostępne tylko dla zleceń LIMIT BUY."), 400

    bot_sources = _bot_order_sources(user_id)
    if str(order_id) in bot_sources:
        return jsonify(
            ok=False, error=f"To zlecenie należy do bota ({bot_sources[str(order_id)]}) - nie można go tu skasować.",
        ), 403

    try:
        client.cancel_order(order_id)
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    _log_order(
        user_id=user_id, ticker=order.get("ticker", ""), side="buy",
        quantity=Decimal(str(order.get("quantity", 0))), price_snapshot=None, status="cancelled",
        t212_order_id=str(order_id),
    )
    return jsonify(ok=True, order_id=order_id)


@scalping_bp.route("/order/<order_id>/reprice", methods=["POST"])
@login_required
def reprice_order(order_id: str):
    """
    "Edycja" zlecenia LIMIT BUY - T212 nie ma natywnego modify/PATCH (patrz
    T212Client - tylko place_*/cancel_order), więc to zawsze anuluj-i-złóż-
    nowe pod maską, dokładnie ten sam wzorzec co bot stosuje przy gonieniu
    ceny (bot_engine.py::_retry_pending_buys). Dodane 2026-07-30 (Adam: "chcę
    żeby można było edytować cyfrowo podciągając bądź obniżając cenę lub
    łapiąc za kreskę i przesuwając ją po wykresie").

    Celowo TYLKO dla zleceń NIE śledzonych przez żaden z 3 silników bota
    (_bot_order_sources) - reprice zlecenia bota podmieniłby jego order_id
    pod silnikiem bez jego wiedzy (silnik szukałby starego ID w pending,
    zobaczyłby "zniknęło", wpadłby w tę samą niejednoznaczną pętlę co przy
    prawdziwym DTEd_EQ 2026-07-30) - świadomie poza zakresem tej zmiany,
    wymagałoby osobnej synchronizacji ActiveTrade/SignalTrade/EODTrade.

    Body: {"new_price": "...", "new_quantity": "..."} - new_quantity OPCJONALNE
    (dodane 2026-07-30, Adam: "zmiany ilości nie zaimplementowałeś a powinna
    być") - gdy brak/puste, zostaje ilość z ISTNIEJĄCEGO zlecenia (get_pending_
    orders). Ticker ZAWSZE z istniejącego zlecenia, nigdy z requestu - żeby
    nie dało się podmienić niczego poza ceną/ilością.
    """
    user_id = current_user_id()
    payload = request.get_json(silent=True) or {}
    raw_price = payload.get("new_price")
    if not raw_price:
        return jsonify(ok=False, error="Brak new_price."), 400
    try:
        new_price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError):
        return jsonify(ok=False, error="Nieprawidłowa cena."), 400
    if new_price <= 0:
        return jsonify(ok=False, error="Cena musi być dodatnia."), 400

    raw_quantity = payload.get("new_quantity")
    new_quantity: Decimal | None = None
    if raw_quantity not in (None, ""):
        try:
            new_quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, ValueError):
            return jsonify(ok=False, error="Nieprawidłowa ilość."), 400
        if new_quantity <= 0:
            return jsonify(ok=False, error="Ilość musi być dodatnia."), 400

    try:
        client = _get_client()
        # force_refresh=True - patrz komentarz w cancel_one() wyzej, ten sam
        # powod (walidacja KONKRETNEGO, mozliwe ze przed chwila utworzonego
        # zlecenia nie moze polegac na do 50s starym wspoldzielonym cache).
        pending = client.get_pending_orders(force_refresh=True)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except T212APIError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    order = next((o for o in pending if str(o.get("id")) == str(order_id)), None)
    if order is None:
        return jsonify(ok=False, error="Zlecenie nie istnieje albo już zostało wykonane/anulowane."), 404
    if order.get("type") != "LIMIT" or order.get("side") != "BUY":
        return jsonify(ok=False, error="Edycja dostępna tylko dla zleceń LIMIT BUY."), 400

    bot_sources = _bot_order_sources(user_id)
    if str(order_id) in bot_sources:
        return jsonify(
            ok=False, error=f"To zlecenie należy do bota ({bot_sources[str(order_id)]}) - nie można go tu edytować.",
        ), 403

    ticker = order.get("ticker")
    try:
        existing_quantity = Decimal(str(order.get("quantity", 0)))
    except (InvalidOperation, ValueError):
        return jsonify(ok=False, error="Nieprawidłowa ilość w istniejącym zleceniu."), 500
    if existing_quantity <= 0:
        return jsonify(ok=False, error="Nieprawidłowa ilość w istniejącym zleceniu."), 500
    quantity = new_quantity if new_quantity is not None else existing_quantity

    # Ten sam bezpiecznik co zwykłe /warp/order - reprice to efektywnie nowe
    # zlecenie, więc Hard Cap/cooldown musi je widzieć tak samo.
    guard_result = _get_guard(user_id).check_before_order(ticker, quantity, new_price)
    if not guard_result.allowed:
        _log_order(
            user_id=user_id, ticker=ticker, side="buy", quantity=quantity,
            price_snapshot=new_price, status="blocked", block_reason=guard_result.decision.value,
        )
        return jsonify(
            ok=False, blocked=True, reason=guard_result.reason, decision=guard_result.decision.value,
        ), 200  # 200 celowo - decyzja biznesowa, nie błąd serwera (ten sam wzorzec co place_order())

    try:
        client.cancel_order(order_id)
    except T212APIError as exc:
        return jsonify(ok=False, error=f"Anulowanie starego zlecenia nie powiodło się: {exc}"), 502

    try:
        # _place_buy_with_precision_fallback (nie surowe place_limit_order) -
        # dodane 2026-07-30 po realnym błędzie na żywo: reprice KO_US_EQ na
        # 0.11 szt. dostał 400 z T212, ten sam typ ryzyka co historyczny bug
        # DTEd_EQ (_retry_pending_buys pomijało ten fallback). Stare zlecenie
        # jest już anulowane w tym momencie, więc brak tego zabezpieczenia
        # oznaczałby utratę zlecenia bez zamiennika przy złej precyzji ilości.
        # Import LENIWY (nie na poziomie modułu) - bot_engine.py importuje
        # _log_order STĄD (routes/scalping.py), więc import na górze pliku
        # zapętliłby się (ten sam powód co market_hours.py::
        # held_by_other_engine, patrz jego docstring).
        from ..services.bot_engine import _place_buy_with_precision_fallback
        result, quantity = _place_buy_with_precision_fallback(client, ticker, quantity, new_price)
    except T212APIError as exc:
        _log_order(
            user_id=user_id, ticker=ticker, side="buy", quantity=quantity,
            price_snapshot=new_price, status="rejected", block_reason=f"T212_ERROR_{exc.status_code}",
        )
        return jsonify(ok=False, error=f"Stare zlecenie anulowane, ale nowe nie powiodło się: {exc}"), 502

    _log_order(
        user_id=user_id, ticker=ticker, side="buy", quantity=quantity,
        price_snapshot=new_price, status="sent", t212_order_id=result.order_id,
    )
    return jsonify(ok=True, order_id=result.order_id, price=str(new_price), ticker=ticker, quantity=str(quantity))


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


def _annotate_bot_state(user_id: int, positions: list[dict]) -> list[dict]:
    """
    Dolacza do kazdej pozycji flagi o stanie bota (BotAsset/ActiveTrade) - do
    przycisku "Przekaz botowi" w portfolio.html (patrz routes/bot.py::adopt_position,
    pomysl #2 z docs/IDEAS_v2.md, 2026-07-23). Zapytania WYLACZNIE do lokalnej
    bazy (tanie, zero rate limitu T212) - liczone na swiezo przy KAZDYM
    renderze/refreshu, NIGDY nie wchodza do _portfolio_cache razem z reszta
    portfela, inaczej adopcja pozycji nie odswiezylaby przycisku na "juz
    zarzadzane" bez pelnego odswiezenia z T212.
    """
    if not positions:
        return positions
    tickers = [p["ticker"] for p in positions]
    bot_assets_by_ticker = {
        a.ticker: a for a in
        BotAsset.query.filter_by(user_id=user_id).filter(BotAsset.ticker.in_(tickers)).all()
    }
    open_trades_by_ticker = {
        t.ticker: t for t in
        ActiveTrade.query.filter_by(user_id=user_id, status="OPEN").filter(ActiveTrade.ticker.in_(tickers)).all()
    }
    for p in positions:
        asset = bot_assets_by_ticker.get(p["ticker"])
        trade = open_trades_by_ticker.get(p["ticker"])
        p["bot_managed"] = trade is not None
        p["bot_trade_id"] = trade.id if trade is not None else None
        p["on_bot_list"] = asset is not None
        p["bot_entry_amount"] = str(asset.entry_amount) if asset else None
    return positions


def _serialize_portfolio(positions: list[dict], total_value, total_ppl, total_ppl_pct) -> dict:
    """
    Forma JSON-owalna (Decimal -> float) dzielona przez initial_data (portfolio.html,
    embedowane do natychmiastowego re-renderu z zapamietanym sortem - patrz
    portfolio.js::SORT_STORAGE_KEY) i portfolio_refresh() - zeby oba mialy
    IDENTYCZNY ksztalt danych, ktory renderPortfolio() w JS umie skonsumowac.
    """
    return {
        "positions": [
            {
                "ticker": p["ticker"],
                "display_ticker": p["display_ticker"],
                "name": p["name"],
                "currency": p["currency"],
                "market_open": p["market_open"],
                "quantity": float(p["quantity"]),
                "avg_price": float(p["avg_price"]),
                "current_price": float(p["current_price"]),
                "value": float(p["value"]),
                "ppl": float(p["ppl"]),
                "ppl_pct": float(p["ppl_pct"]),
                "hue": p["hue"],
                "initial": p["initial"],
                "logo_filename": p["logo_filename"],
                "bot_managed": p["bot_managed"],
                "bot_trade_id": p["bot_trade_id"],
                "on_bot_list": p["on_bot_list"],
                "bot_entry_amount": p["bot_entry_amount"],
            }
            for p in positions
        ],
        "total_value": float(total_value),
        "total_ppl": float(total_ppl),
        "total_ppl_pct": float(total_ppl_pct),
    }


@scalping_bp.route("/portfolio", methods=["GET"])
@login_required
def portfolio_view():
    """
    "Moje aktywa" - WSZYSTKIE otwarte pozycje z T212 (nie tylko te w
    ulubionych/siatce jak dotad wszedzie indziej), z iloscia/za ile/ile teraz
    warte/zysk-strata.

    Strona NIE odpytuje T212 na zywo przy renderowaniu - pokazuje od razu to,
    co jest w cache z poprzedniej wizyty (_portfolio_cache, ten sam wzorzec
    co _guards/session_store - w pamieci procesu), a swiezy odczyt dociaga
    dopiero JS z malym opoznieniem (portfolio.js, ten sam powod co opoznione
    "Otwarte zlecenia" w Warp: zeby nie strzelac zapytaniem do T212 zaraz po
    zaladowaniu strony, w waski rate limit demo). Pierwsza wizyta (brak
    cache) pokazuje czytelny stan "ladowanie" zamiast probowac na sztywno
    i czesto trafiac w blad.

    initial_data (JSON embedowany w portfolio.html) pozwala portfolio.js
    naniesc zapamietany sort NATYCHMIAST (bez czekania na siec) - bez tego
    tabela chwile stala w kolejnosci backendu (wartosc malejaco), zanim po
    ~1.5s odswiezenie na zywo przestawialo ja na sort usera, co Adam zglosil
    27.07.2026 jako widoczny "skok"/rozjazd tabeli tuz po zaladowaniu.
    """
    user_id = current_user_id()
    cached = _portfolio_cache.get(user_id)
    if cached:
        positions = _annotate_bot_state(user_id, cached["positions"])
        initial_data = _serialize_portfolio(
            positions, cached["total_value"], cached["total_ppl"], cached["total_ppl_pct"]
        )
        return render_template(
            "portfolio.html", positions=positions,
            total_value=cached["total_value"], total_ppl=cached["total_ppl"],
            total_ppl_pct=cached["total_ppl_pct"],
            error=None, has_cache=True, initial_data=initial_data,
        )
    return render_template(
        "portfolio.html", positions=[], total_value=None, total_ppl=None,
        total_ppl_pct=None, error=None, has_cache=False, initial_data=None,
    )


def _fetch_portfolio_live(user_id: int) -> dict:
    """
    Prawdziwe zapytanie do T212 (+wzbogacenie o nazwe/logo/hue) - rzuca
    RuntimeError/T212APIError przy niepowodzeniu, wywolujacy ma to zlapac.
    Aktualizuje _portfolio_cache przy sukcesie.
    """
    from ..models import Instrument

    client = _get_client()
    raw_positions = client.get_portfolio()

    tickers = [p.get("ticker") for p in raw_positions if p.get("ticker")]
    instruments_by_ticker = (
        {i.ticker: i for i in Instrument.query.filter(Instrument.ticker.in_(tickers)).all()}
        if tickers else {}
    )

    positions = []
    total_value = Decimal("0")
    total_ppl = Decimal("0")
    total_cost_basis = Decimal("0")
    for p in raw_positions:
        ticker = p.get("ticker")
        if not ticker:
            continue
        try:
            quantity = Decimal(str(p.get("quantity", 0)))
            avg_price = Decimal(str(p.get("averagePrice", 0)))
            current_price = Decimal(str(p.get("currentPrice", 0)))
            ppl = Decimal(str(p.get("ppl", 0)))
        except InvalidOperation:
            continue

        instrument = instruments_by_ticker.get(ticker)
        name = friendly_name(instrument.name) if instrument else ""
        cost_basis = quantity * avg_price
        value = quantity * current_price

        currency = instrument.currency_code if instrument else ""
        positions.append({
            "ticker": ticker,
            "display_ticker": ticker.split("_")[0],
            "name": name,
            "currency": currency,
            "market_open": _market_open(currency) if currency else None,
            "quantity": quantity,
            "avg_price": avg_price,
            "current_price": current_price,
            "value": value,
            "ppl": ppl,
            "ppl_pct": (ppl / cost_basis * 100) if cost_basis else Decimal("0"),
            "hue": avatar_hue(ticker),
            "initial": (name or ticker.split("_")[0])[0].upper(),
            "logo_filename": logo_cache.get_cached_logo_filename(current_app.static_folder, ticker),
        })
        total_value += value
        total_ppl += ppl
        total_cost_basis += cost_basis

    positions.sort(key=lambda x: x["value"], reverse=True)

    total_ppl_pct = (total_ppl / total_cost_basis * 100) if total_cost_basis else Decimal("0")
    result = {
        "positions": positions, "total_value": total_value, "total_ppl": total_ppl,
        "total_ppl_pct": total_ppl_pct,
    }
    _portfolio_cache[user_id] = result
    return result


@scalping_bp.route("/portfolio/refresh", methods=["GET"])
@login_required
def portfolio_refresh():
    """
    JSON - wolane przez portfolio.js z opoznieniem po zaladowaniu strony,
    zeby odswiezyc dane na zywo bez blokowania pierwszego renderu (patrz
    portfolio_view). Decimal -> float, bo to tylko do wyswietlenia w JS,
    nie do dalszych precyzyjnych obliczen.
    """
    user_id = current_user_id()
    try:
        result = _fetch_portfolio_live(user_id)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc), rate_limited=False), 500
    except T212APIError as exc:
        # rate_limited osobno, zeby JS mogl pokazac spokojny komunikat zamiast
        # surowego zrzutu wyjatku - 429 na demo T212 jest CZESTY i oczekiwany
        # (bardzo waski limit), nie realny blad wart alarmowania.
        return jsonify(ok=False, error=str(exc), rate_limited=(exc.status_code == 429)), 502

    _annotate_bot_state(user_id, result["positions"])

    return jsonify(
        ok=True,
        **_serialize_portfolio(result["positions"], result["total_value"], result["total_ppl"], result["total_ppl_pct"]),
    )


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
