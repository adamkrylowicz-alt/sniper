"""
app/services/bot_engine.py
============================
Silnik Micro-Grid Bota - w TEJ części (Etap 2, strategia wejścia):

1. tick() - wołane cyklicznie przez APScheduler (patrz app/__init__.py),
   dla każdego aktywnego bota woła najpierw _retry_pending_sells() (dokończ
   zawieszone pozycje bez LIMIT SELL), potem _process_entries() (nowe wejścia).
2. reconcile(user_id) - Reconciliation Loop (PRD sekcja 4): porównuje
   lokalny stan ActiveTrade z rzeczywistymi zleceniami na koncie T212, i woła
   TĘ SAMĄ _retry_pending_sells() co tick() - żeby zachowanie było spójne
   niezależnie od tego, co je wywołało (aktywacja czy zwykły cykl).
   Wołane PRZY AKTYWACJI bota (routes/bot.py::activate), NIE przy starcie
   appki - po restarcie nie ma jeszcze niczyich poświadczeń w
   bot_credentials.py, więc "przy starcie" nie miałoby czego uzgadniać.
3. _process_entries()/_enter_position() - strategia wejścia (PRD sekcja
   3.1, dca_level=0): kupuje mikro-kwotę (Market Buy). LIMIT SELL NIE jest
   już wystawiany od razu (patrz historia tego pliku poniżej) - o to dba
   _retry_pending_sells() przy najbliższym możliwym ticku. ŻADNA pętla DCA/
   Cancel-Replace/Fail-Safe jeszcze nie istnieje - to zadanie kolejnej części.
   Świadomie pominięte w tej części (patrz PLAN.md z sesji): Spread Guard
   (Finnhub free tier nie ma bid/ask) i proaktywny Fractional Guard (rate
   limit T212 uniemożliwił bezpieczną weryfikację pól
   /equity/metadata/instruments) - zamiast tego odrzucenie przez T212 (np.
   brak wsparcia ułamków) jest po prostu logowane jako ERROR, bot spróbuje
   ponownie przy kolejnym tick-u.

Bot działa WYŁĄCZNIE na demo (patrz routes/bot.py - blokada environment="live"
na poziomie aktywacji, bo T212 nie wspiera zleceń LIMIT na live) - stąd
"demo" na sztywno tutaj, nie parametr.

Historia buga (2026-07-20, potwierdzone realnym testem na koncie demo, DWIE
niezależne przyczyny):

1. Pierwsza wersja tej części próbowała wystawić LIMIT SELL OD RAZU po
   Market Buy, z krótkim (max ~14s) blokującym time.sleep() retry tylko dla
   błędu "selling-equity-not-owned". W realnym teście zlecenie kupna miało
   status=NEW/filledQuantity=0 jeszcze 2+ minuty po złożeniu (MARKET order
   złożony przed otwarciem giełdy US czeka w kolejce) - żaden 14-sekundowy
   retry by tego nie załatwił. Fix: _enter_position() nigdy nie blokuje ani
   nie zgaduje, _retry_pending_sells() ponawia CYKLICZNIE (co tick, z
   rosnącym backoffem) aż się uda albo T212 zwróci błąd, który się sam nie
   naprawi (sell_blocked=True).

2. Pierwsza wersja fixu z punktu 1 sprawdzała "ile tickera X mam ŁĄCZNIE w
   portfolio" (T212Client.get_portfolio()) zamiast "ile pochodzi z TEGO
   konkretnego zlecenia kupna". W teście to konto demo miało z wcześniejszych
   sesji (17.07) niepowiązaną, starą pozycję 0.006 AAPL - kod zobaczył
   "owned=0.006" w portfolio i SPRZEDAŁ TĘ STARĄ pozycję, podczas gdy
   dzisiejsze zlecenie kupna (0.009) wciąż czekało niewypełnione w kolejce.
   ActiveTrade oznaczyło się jako "załatwione" (sell_order_id ustawiony),
   więc gdyby dzisiejsze zlecenie później faktycznie się wypełniło, te NOWE
   akcje nigdy nie dostałyby własnego LIMIT SELL - zostałyby trwale
   niewidoczne dla retry loop. Fix: _retry_pending_sells() sprawdza
   filledQuantity KONKRETNEGO trade.buy_order_id (get_pending_orders() dla
   zleceń wciąż w kolejce, get_order_history() dla tych co już z niej
   zniknęły - wykonane w całości albo anulowane/odrzucone), nigdy zbiorczego
   stanu portfela.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from flask import current_app

from ..extensions import db
from ..models import ActiveTrade, BotAsset, BotAuditLog, RiskSettings
from ..routes.api_keys import get_decrypted_credentials
from ..routes.scalping import _log_order
from . import bot_credentials, price_feed
from .t212_client import T212APIError, T212Client

BOT_ENVIRONMENT = "demo"

# Empirycznie zaobserwowana granica (NIE oficjalnie udokumentowana przez T212):
# próba zakupu SPCX za 1.00 USD dała błąd "min-quantity-exceeded" - "must
# trade at least 0.00874595" - przy ówczesnej cenie SPCX to odpowiada
# minimalnej WARTOŚCI zlecenia rzędu ~1.00 USD. Trzymamy tu próg z zapasem
# (1.20), żeby ostrzegać ZANIM appka wyśle zlecenie do T212, nie dopiero po
# fakcie. To SZACUNEK z jednego zaobserwowanego przypadku, nie gwarancja -
# T212 może mieć różne minima per instrument (stąd i tak realne zlecenie
# może się nie udać nawet powyżej tego progu, albo odwrotnie).
MIN_ORDER_VALUE_ESTIMATE = Decimal("1.20")

SELLING_EQUITY_NOT_OWNED_ERROR_TYPE = "/api-errors/selling-equity-not-owned"

# Backoff (minuty) między kolejnymi próbami LIMIT SELL dla pozycji, która
# jeszcze nie ma potwierdzonej ilości w portfolio T212 (patrz
# _attempt_sell_placement/_bump_retry). Indeks = min(sell_retry_count,
# len-1) - po wyczerpaniu listy odstęp zostaje na stałe 30 min. Bez sztywnego
# limitu liczby prób - pozycja może legalnie czekać godzinami na otwarcie
# giełdy, to nie jest awaria. Strop 30 min chroni ciasny rate limit demo
# (patrz t212_client.py) przed pozycją, która failuje w nieskończoność.
SELL_RETRY_BACKOFF_MINUTES = (1, 2, 5, 15, 30)


def _next_retry_delay(retry_count: int) -> dt.timedelta:
    idx = min(retry_count, len(SELL_RETRY_BACKOFF_MINUTES) - 1)
    return dt.timedelta(minutes=SELL_RETRY_BACKOFF_MINUTES[idx])


def _log(user_id: int, action_type: str, message: str, position_group_id: str | None = None) -> None:
    """Zapis do BotAuditLog + commit natychmiast (każdy wpis niezależny, ten sam styl co OrderLog)."""
    db.session.add(BotAuditLog(
        user_id=user_id, action_type=action_type, message=message,
        position_group_id=position_group_id,
    ))
    db.session.commit()


def _get_client_for_user(user_id: int, settings: RiskSettings) -> T212Client | None:
    """
    Wspólna budowa T212Client dla kroku retry w tick() - None gdy paper
    trading (nigdy nie potrzebujemy prawdziwego klienta) albo brak
    poświadczeń/klucza demo. Celowo bez logowania błędu tutaj - brakujący
    klucz i tak zostanie zgłoszony przez _process_entries() w tym samym
    ticku (dla dowolnego BotAsset czekającego na wejście), nie ma sensu
    dublować tego samego ostrzeżenia.
    """
    if settings.is_paper_trading:
        return None
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return None
    creds = get_decrypted_credentials(user_id, master_key, BOT_ENVIRONMENT)
    if creds is None:
        return None
    return T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=BOT_ENVIRONMENT)


def _bump_retry(user_id: int, trade: ActiveTrade, reason: str) -> None:
    trade.sell_retry_count += 1
    trade.next_sell_retry_at = dt.datetime.utcnow() + _next_retry_delay(trade.sell_retry_count)
    db.session.commit()
    _log(
        user_id, "INFO",
        f"{trade.ticker}: retry LIMIT SELL #{trade.sell_retry_count} odłożony - {reason}",
        trade.position_group_id,
    )


def _attempt_sell_placement(
    user_id: int, client: T212Client, trade: ActiveTrade,
    take_profit_usd: Decimal, pending_by_id: dict[str, dict],
) -> None:
    """
    Sprawdza wypełnienie KONKRETNEGO trade.buy_order_id - NIGDY zbiorczego
    stanu portfela (patrz "Historia buga" punkt 2 w docstringu modułu, gdzie
    to pomyliło się z inną, starszą pozycją tego samego tickera). Zlecenie
    wciąż w pending_by_id -> filledQuantity stamtąd. Zlecenie które z
    pending_by_id zniknęło -> stan końcowy (wykonane w całości albo
    anulowane/odrzucone), sprawdzamy get_order_history().

    Sprzedaje MIN(filled, zażądana) - przy częściowym wykonaniu koryguje
    trade.quantity/allocated_value do faktycznie wypełnionej ilości i loguje
    obie wartości.
    """
    pending_order = pending_by_id.get(trade.buy_order_id)

    if pending_order is not None:
        filled_qty = Decimal(str(pending_order.get("filledQuantity", 0)))
        if filled_qty <= 0:
            _bump_retry(
                user_id, trade,
                f"zakup jeszcze niewypełniony (status {pending_order.get('status')}), wciąż w kolejce T212.",
            )
            return
    else:
        try:
            history = client.get_order_history(limit=20)
        except T212APIError as exc:
            _bump_retry(
                user_id, trade,
                f"zlecenie kupna zniknęło z pending, a sprawdzenie historii nie powiodło się ({exc}).",
            )
            return

        item = next((i for i in history.get("items", []) if str(i.get("id")) == trade.buy_order_id), None)
        if item is None:
            _bump_retry(
                user_id, trade,
                "zlecenie kupna zniknęło z pending i nie widać go na pierwszej stronie historii.",
            )
            return

        filled_qty = Decimal(str(item.get("filledQuantity", 0)))
        if filled_qty <= 0:
            # Zlecenie zakończone (nie ma go już w pending) BEZ wykonania -
            # anulowane/odrzucone. Zero akcji kupionych, nigdy nie będzie -
            # dalsze retry byłyby stratą czasu i rate limitu.
            trade.sell_blocked = True
            db.session.commit()
            _log(
                user_id, "ERROR",
                f"{trade.ticker}: zlecenie kupna zakończone bez wykonania (status {item.get('status')}) - "
                "zero akcji kupionych, pozycja NIGDY nie dostanie LIMIT SELL, wymaga ręcznej interwencji.",
                trade.position_group_id,
            )
            return

    sell_qty = min(filled_qty, trade.quantity)
    partial = sell_qty < trade.quantity
    requested_qty = trade.quantity

    if partial:
        trade.quantity = sell_qty
        trade.allocated_value = (sell_qty * trade.buy_price).quantize(Decimal("0.01"))
        trade.average_price = trade.buy_price

    target_price = (trade.allocated_value + take_profit_usd) / trade.quantity

    try:
        sell_result = client.place_limit_order(trade.ticker, -sell_qty, target_price)
    except T212APIError as exc:
        error_type = exc.payload.get("type") if isinstance(exc.payload, dict) else None
        if error_type == SELLING_EQUITY_NOT_OWNED_ERROR_TYPE:
            _bump_retry(
                user_id, trade,
                f"zlecenie kupna pokazuje filled={filled_qty}, ale T212 wciąż zgłasza "
                f"selling-equity-not-owned (prawdopodobnie chwilowe opóźnienie księgowania) - {exc}",
            )
        else:
            # Błąd INNY niż opóźnienie księgowania nigdy się sam nie naprawi
            # (np. quantity-precision-mismatch) - dalsze automatyczne próby
            # byłyby tylko stratą ciasnego rate limitu demo.
            trade.sell_blocked = True
            db.session.commit()
            _log(
                user_id, "ERROR",
                f"{trade.ticker}: LIMIT SELL trwale odrzucony ({exc}) - NIE będzie już ponawiany "
                "automatycznie, wymaga ręcznej interwencji.",
                trade.position_group_id,
            )
        return

    trade.sell_order_id = sell_result.order_id
    trade.sell_retry_count = 0
    trade.next_sell_retry_at = None
    db.session.commit()

    if partial:
        _log(
            user_id, "WARN",
            f"{trade.ticker}: LIMIT SELL wystawiony na {sell_qty} (CZĘŚCIOWE WYKONANIE zlecenia kupna - "
            f"zażądano {requested_qty}, faktycznie wypełnione {filled_qty}), target {target_price:.4f}.",
            trade.position_group_id,
        )
    else:
        _log(
            user_id, "INFO",
            f"{trade.ticker}: LIMIT SELL wystawiony, target {target_price:.4f}.",
            trade.position_group_id,
        )


def _retry_pending_sells(
    user_id: int, client: T212Client, settings: RiskSettings, pending: list[dict] | None = None,
) -> None:
    """
    Znajduje pozycje OPEN bez sell_order_id (zakup poszedł, LIMIT SELL jeszcze
    nie), których backoff (next_sell_retry_at) już minął, i próbuje ponownie -
    patrz _attempt_sell_placement. Wołane z KAŻDEGO tick() (co 60s) ORAZ z
    reconcile() (przy aktywacji bota) - bez tego pozycja zostałaby trwale
    zawieszona aż do ręcznej dezaktywacji/reaktywacji bota.

    `pending`: opcjonalna, już pobrana lista z get_pending_orders() - reconcile()
    ją i tak potrzebuje dla własnej logiki CLOSED-detection, więc przekazuje
    tutaj zamiast dublować to samo zapytanie (rate limit demo jest ciasny).
    """
    now = dt.datetime.utcnow()
    candidates = (
        ActiveTrade.query
        .filter_by(user_id=user_id, status="OPEN", is_paper=False, sell_order_id=None, sell_blocked=False)
        .filter(db.or_(ActiveTrade.next_sell_retry_at.is_(None), ActiveTrade.next_sell_retry_at <= now))
        .all()
    )
    if not candidates:
        return

    if pending is None:
        try:
            pending = client.get_pending_orders()
        except T212APIError as exc:
            _log(user_id, "ERROR", f"Retry LIMIT SELL: błąd pobierania pending orders - {exc}")
            return
    pending_by_id = {str(o.get("id")): o for o in pending}

    for trade in candidates:
        _attempt_sell_placement(user_id, client, trade, settings.take_profit_usd, pending_by_id)


def reconcile(user_id: int) -> None:
    """
    1. Sprawdza czy jakaś lokalnie "OPEN" pozycja (ActiveTrade.sell_order_id)
       wykonała się na T212 podczas gdy bot był nieaktywny - jeśli sell_order_id
       NIE występuje już wśród pending orders, oznacza pozycję jako CLOSED.
    2. Woła _retry_pending_sells() - ten sam mechanizm co cykliczny tick(),
       więc zachowanie jest identyczne niezależnie od tego, co je wywołało.
    """
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        # Nie powinno się zdarzyć - routes/bot.py::activate() woła to TUŻ PO
        # bot_credentials.activate(). Jeśli mimo to tu trafiamy, to realny
        # problem (nie cichy no-op) - ma być widoczny w dzienniku, nie milczeć.
        _log(user_id, "ERROR", "Reconciliation: brak poświadczeń w bot_credentials mimo aktywacji - zgłoś to.")
        return

    creds = get_decrypted_credentials(user_id, master_key, BOT_ENVIRONMENT)
    if creds is None:
        _log(user_id, "ERROR", "Reconciliation: brak zapisanego klucza API demo, pomijam.")
        return

    settings = RiskSettings.query.filter_by(user_id=user_id).first()

    # is_paper=False - pozycje papierowe nigdy nie trafily do T212, wiec nie
    # ma czego z nim uzgadniac (patrz models.py::ActiveTrade.is_paper).
    open_trades = ActiveTrade.query.filter_by(user_id=user_id, status="OPEN", is_paper=False).all()
    if not open_trades:
        _log(user_id, "INFO", "Reconciliation: brak otwartych pozycji (realnych) do sprawdzenia.")
        return

    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=BOT_ENVIRONMENT)
    try:
        pending = client.get_pending_orders()
    except T212APIError as exc:
        _log(user_id, "ERROR", f"Reconciliation: błąd T212 - {exc}")
        return

    pending_ids = {str(o.get("id")) for o in pending}
    closed_count = 0

    for trade in open_trades:
        if trade.sell_order_id and trade.sell_order_id not in pending_ids:
            trade.status = "CLOSED"
            trade.closed_at = dt.datetime.utcnow()
            closed_count += 1
            _log(
                user_id, "INFO",
                f"Reconciliation: {trade.ticker} (grupa {trade.position_group_id}) "
                f"wykonane podczas nieaktywności bota - oznaczone jako CLOSED.",
                position_group_id=trade.position_group_id,
            )

    db.session.commit()
    if closed_count == 0:
        _log(user_id, "INFO", f"Reconciliation: {len(open_trades)} pozycji sprawdzonych, wszystkie nadal aktualne.")

    if settings is not None:
        _retry_pending_sells(user_id, client, settings, pending=pending)


def tick(app) -> None:
    """
    Wołane cyklicznie przez APScheduler (patrz app/__init__.py). `app` musi
    być prawdziwym obiektem Flask, nie proxy current_app - ten sam wzorzec
    co services/logo_cache.py::start_bulk_fetch (wątek/job w tle potrzebuje
    własnego app_context()).
    """
    with app.app_context():
        for user_id in bot_credentials.active_user_ids():
            settings = RiskSettings.query.filter_by(user_id=user_id).first()
            if not settings or not settings.is_bot_active:
                continue

            client = _get_client_for_user(user_id, settings)
            if client is not None:
                _retry_pending_sells(user_id, client, settings)

            _process_entries(user_id, settings)


def _process_entries(user_id: int, settings: RiskSettings) -> None:
    """
    Strategia wejścia (dca_level=0) - PRD sekcja 3.1. Dla każdego BotAsset
    usera (WŁASNA lista bota, patrz models.py::BotAsset - niezależna od
    Smart Virtual Pie) z is_penny_stock=False - jeśli nie ma już otwartej
    pozycji na tym aktywie, otwiera nową.
    """
    assets = BotAsset.query.filter_by(user_id=user_id, is_penny_stock=False).all()
    for asset in assets:
        already_open = ActiveTrade.query.filter_by(bot_asset_id=asset.id, status="OPEN").first()
        if already_open:
            continue
        _enter_position(user_id, asset, settings)


def _enter_position(user_id: int, asset: BotAsset, settings: RiskSettings) -> None:
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return  # nie powinno się zdarzyć - user_id pochodzi z bot_credentials.active_user_ids()

    creds = get_decrypted_credentials(user_id, master_key, BOT_ENVIRONMENT)
    if creds is None:
        _log(user_id, "ERROR", f"{asset.ticker}: brak zapisanego klucza API demo.")
        return

    price = price_feed.get_live_price(current_app.config.get("FINNHUB_API_KEY"), asset.ticker)
    if price is None or price <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: brak ceny (Finnhub i Yahoo zawiodły), pomijam ten tick.")
        return

    quantity = (asset.entry_amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        _log(user_id, "ERROR", f"{asset.ticker}: wyliczona ilość <= 0 (kwota {asset.entry_amount} / cena {price}).")
        return

    buy_price = price
    allocated_value = quantity * buy_price
    target_price = (allocated_value + settings.take_profit_usd) / quantity
    position_group_id = str(uuid.uuid4())

    if settings.is_paper_trading:
        # Symulacja - ZERO requestow do T212, tylko zapis do ActiveTrade z
        # syntetycznymi ID zleceń. Reconcile() musi pomijac is_paper=True
        # (nie ma czego uzgadniac - zadne zlecenie nigdzie nie poszlo).
        trade = ActiveTrade(
            user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
            ticker=asset.ticker, currency=asset.currency,
            buy_order_id=f"PAPER-{uuid.uuid4()}", sell_order_id=f"PAPER-{uuid.uuid4()}",
            buy_price=buy_price, quantity=quantity, allocated_value=allocated_value,
            average_price=buy_price, dca_level=0, status="OPEN", is_paper=True,
        )
        db.session.add(trade)
        db.session.commit()
        _log(
            user_id, "BUY",
            f"[PAPER] {asset.ticker}: symulowane wejście {quantity} @ ~{buy_price}, "
            f"symulowany target {target_price:.4f} - ŻADNE zlecenie nie poszło do T212.",
            position_group_id,
        )
        return

    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=BOT_ENVIRONMENT)

    try:
        buy_result = client.place_market_order(asset.ticker, quantity)
    except T212APIError as exc:
        _log(user_id, "ERROR", f"{asset.ticker}: zakup nieudany - {exc}")
        return

    # T212Client.place_market_order() dziś nie ekstrahuje realnej ceny wykonania
    # z odpowiedzi - używamy ceny SPRZED zlecenia jako przybliżenia. Źródło
    # niewielkiej nieścisłości w target_price przy realnym poślizgu (slippage) -
    # akceptowalne przy mikro-kwotach z PRD, ale świadomie odnotowane.
    #
    # LIMIT SELL NIE jest wystawiany tutaj (patrz historia buga w docstringu
    # modułu) - zakup może wciąż być w kolejce (rynek zamknięty, kolejka T212)
    # znacznie dłużej niż sensowny blokujący retry, a nawet po wykonaniu
    # faktycznie kupiona ilość może różnić się od zażądanej (częściowe
    # wykonanie). _retry_pending_sells() (wołane z tick()) sprawdzi FAKTYCZNIE
    # posiadaną ilość w portfolio T212 i wystawi LIMIT SELL przy najbliższym
    # możliwym cyklu.
    trade = ActiveTrade(
        user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
        ticker=asset.ticker, currency=asset.currency,
        buy_order_id=buy_result.order_id, sell_order_id=None,
        buy_price=buy_price, quantity=quantity, allocated_value=allocated_value,
        average_price=buy_price, dca_level=0, status="OPEN", is_paper=False,
    )
    db.session.add(trade)
    db.session.commit()

    # pie_id=None - BotAsset jest niezależne od Pie, więc te zlecenia nie
    # są przypisane do żadnego koszyka (patrz models.py::BotAsset).
    _log_order(
        user_id=user_id, ticker=asset.ticker, side="buy", quantity=quantity,
        price_snapshot=buy_price, status="sent", t212_order_id=buy_result.order_id,
    )
    _log(
        user_id, "BUY",
        f"{asset.ticker}: wejście {quantity} @ ~{buy_price} - LIMIT SELL (target ~{target_price:.4f}) "
        "zostanie wystawiony przy najbliższym możliwym ticku.",
        position_group_id,
    )
