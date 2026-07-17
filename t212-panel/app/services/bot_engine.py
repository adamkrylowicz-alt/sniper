"""
app/services/bot_engine.py
============================
Silnik Micro-Grid Bota - w TEJ części (Etap 2, strategia wejścia):

1. tick() - wołane cyklicznie przez APScheduler (patrz app/__init__.py),
   dla każdego aktywnego bota woła _process_entries().
2. reconcile(user_id) - Reconciliation Loop (PRD sekcja 4): porównuje
   lokalny stan ActiveTrade z rzeczywistymi zleceniami na koncie T212.
   Wołane PRZY AKTYWACJI bota (routes/bot.py::activate), NIE przy starcie
   appki - po restarcie nie ma jeszcze niczyich poświadczeń w
   bot_credentials.py, więc "przy starcie" nie miałoby czego uzgadniać.
3. _process_entries()/_enter_position() - strategia wejścia (PRD sekcja
   3.1, dca_level=0): kupuje mikro-kwotę i OD RAZU wystawia LIMIT SELL na
   zysk. ŻADNA pętla DCA/Cancel-Replace/Fail-Safe jeszcze nie istnieje -
   to zadanie kolejnej części. Świadomie pominięte w tej części (patrz
   PLAN.md z sesji): Spread Guard (Finnhub free tier nie ma bid/ask) i
   proaktywny Fractional Guard (rate limit T212 uniemożliwił bezpieczną
   weryfikację pól /equity/metadata/instruments) - zamiast tego odrzucenie
   przez T212 (np. brak wsparcia ułamków) jest po prostu logowane jako
   ERROR, bot spróbuje ponownie przy kolejnym tick-u.

Bot działa WYŁĄCZNIE na demo (patrz routes/bot.py - blokada environment="live"
na poziomie aktywacji, bo T212 nie wspiera zleceń LIMIT na live) - stąd
"demo" na sztywno tutaj, nie parametr.
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


def _log(user_id: int, action_type: str, message: str, position_group_id: str | None = None) -> None:
    """Zapis do BotAuditLog + commit natychmiast (każdy wpis niezależny, ten sam styl co OrderLog)."""
    db.session.add(BotAuditLog(
        user_id=user_id, action_type=action_type, message=message,
        position_group_id=position_group_id,
    ))
    db.session.commit()


def reconcile(user_id: int) -> None:
    """
    1. Sprawdza czy jakaś lokalnie "OPEN" pozycja (ActiveTrade.sell_order_id)
       wykonała się na T212 podczas gdy bot był nieaktywny - jeśli sell_order_id
       NIE występuje już wśród pending orders, oznacza pozycję jako CLOSED.
    2. Retry LIMIT SELL dla pozycji, które mają sell_order_id=None (zakup
       poszedł, ale wystawienie LIMIT SELL zawiodło - patrz komentarz w
       _enter_position). Bez tego taka pozycja zostałaby NA ZAWSZE bez
       zlecenia sprzedaży, bo _process_entries pomija aktywa z już otwartą
       pozycją - to nie jest pełny Fail-Safe (pętla DCA, kolejna część),
       tylko minimalna łatka żeby nic nie zostawało trwale "wiszące" bez
       LIMIT SELL.
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

    # -- Retry LIMIT SELL dla pozycji bez sell_order_id (patrz docstring) ----
    missing_sell = [t for t in open_trades if t.status == "OPEN" and not t.sell_order_id]
    if missing_sell:
        settings = RiskSettings.query.filter_by(user_id=user_id).first()
        take_profit_usd = settings.take_profit_usd if settings else Decimal("0.05")

        for trade in missing_sell:
            target_price = (trade.allocated_value + take_profit_usd) / trade.quantity
            try:
                sell_result = client.place_limit_order(trade.ticker, -trade.quantity, target_price)
            except T212APIError as exc:
                _log(
                    user_id, "ERROR",
                    f"Reconciliation: retry LIMIT SELL dla {trade.ticker} znów nieudany - {exc}",
                    trade.position_group_id,
                )
                continue

            trade.sell_order_id = sell_result.order_id
            db.session.commit()
            _log(
                user_id, "INFO",
                f"Reconciliation: {trade.ticker} - LIMIT SELL wystawiony (retry), target {target_price:.4f}.",
                trade.position_group_id,
            )


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
    sell_order_id = None
    try:
        sell_result = client.place_limit_order(asset.ticker, -quantity, target_price)
        sell_order_id = sell_result.order_id
    except T212APIError as exc:
        # Zakup POSZEDŁ, LIMIT SELL nie - NIE cofamy zakupu (T212 nie ma takiej
        # operacji), zapisujemy pozycję z sell_order_id=None, żeby nie zgubić
        # śladu. Fail-Safe (kolejna część, pętla DCA) będzie umiał to naprawić/
        # ponowić - tu tylko głośny log, pozycja i tak trafia do ActiveTrade.
        _log(user_id, "ERROR", f"{asset.ticker}: LIMIT SELL nieudany po zakupie - {exc}", position_group_id)

    trade = ActiveTrade(
        user_id=user_id, bot_asset_id=asset.id, position_group_id=position_group_id,
        ticker=asset.ticker, currency=asset.currency,
        buy_order_id=buy_result.order_id, sell_order_id=sell_order_id,
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
        f"{asset.ticker}: wejście {quantity} @ ~{buy_price}, target LIMIT SELL {target_price:.4f}",
        position_group_id,
    )
