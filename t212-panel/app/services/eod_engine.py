"""
app/services/eod_engine.py
=============================
Moduł EOD (End of Day) - TRZECI, znowu CAŁKOWICIE OSOBNY silnik obok
Micro-Grid (bot_engine.py) i strategii sygnałowej (signal_engine.py). Patrz
docs/IDEAS_v2.md ("Specjalny moduł EOD") i models.py (komentarz nad
EODAsset) po pełne uzasadnienie niezależności.

Działa 9:00-22:00 czasu Amsterdamu (EOD_WINDOW niżej) - pierwotnie tylko
koniec sesji EUR (16:00-17:30), rozszerzone 2026-07-24 (Adam: "zmień EOD na
USA także") na 16:00-22:00, i dalej rozszerzone 2026-07-27 (Adam: "niech
działa cały czas od 9 do 22") na CAŁY dzień handlowy obu sesji - okno
obejmuje otwarcie/całą sesję EUR (Euronext/Xetra ~9:00-17:30 CEST) i całą
sesję USA (NASDAQ/NYSE ~15:35-21:55 CEST, patrz market_hours.py) jednym
zakresem czasowym, per-tickerowa bramka `market_hours.is_market_open()` i
tak filtruje czy WŁAŚCIWA dla waluty danego tickera giełda jest akurat
otwarta. Cel: szybka reakcja na NAGŁE,
OSTRE spadki w krótkim czasie (1-5 minut), nie na powolne pełzanie w dół (to
już robi Micro-Grid/Sygnał na świecach dziennych). Dane: świece 1-MINUTOWE
dzisiejszej sesji (price_feed.get_eod_intraday_1m, Yahoo nieoficjalne -
patrz docs/IDEAS_v2.md pkt 3, decyzja 2026-07-24: żaden tani dostawca nie
dawał prawdziwego 1-min dla Europy, docelowo IBKR API gdy dostępne).

Trigger: dla każdego tickera na liście EODAsset, licz % zmiany od ceny sprzed
1/2/3/4/5 minut do teraz - bierz NAJGORSZY (najbardziej ujemny) z tych 5
okien jako "ostry ruch". Mapuj go na tier wielkości pozycji (PRD):
    -2.0% do -3.49%  -> 0.6x entry_amount
    -3.5% do -5.49%  -> 1.0x entry_amount
    -5.5% i więcej   -> 1.5x entry_amount
Poniżej -2.0% - brak triggera.

Wyjście - TA SAMA mechanika co signal_engine.py (T212 nie pozwala na dwa
resting ordery na te same udziały, docs/IDEAS_v2.md pkt 4): stop-loss to
PRAWDZIWY resting STOP na T212 (chroni nawet offline). Wymuszone zamknięcie
przed końcem sesji (PRD: "nie przenoszą się na kolejny dzień") jest
OPCJONALNE - `EODSettings.force_close_enabled`, domyślnie WYŁĄCZONE (Adam
2026-07-24: najpierw zaimplementowane jako sztywne zachowanie, potem
odrzucone jako "kto ci kazał nie trzymać ich na drugi dzień" - zamiast
usuwać funkcję, przerobione na przełącznik w Ustawieniach ryzyka, wyłączony
domyślnie).

TAKE-PROFIT (zmiana 2026-07-28) - już NIE sztywny `price*(1+take_profit_pct)`
(0.6%), tylko `reference_price` z `_worst_recent_drop()` - cena SPRZED
SPADKU, czyli poziom do którego bot próbuje złapać odbicie. Adam: "jebło w
dół np 3-5% w ciągu 1-2min kupuje i liczę na szybkie odbicie w okolice
wcześniejszego poziomu... nawet nie musi być idealnie w punkt ale w
okolice" - cel skaluje się teraz z wielkością spadku, sztywny % zostaje
WYŁĄCZNIE jako fallback gdyby reference_price wypadł ≤ ceny wejścia.

TRAILING STOP-LOSS (dodane 2026-07-28, ten sam dzień, patrz
[[feedback_snajper_profit_protection_priority]] w pamięci Claude - "ochrona
zysku" to zasada dla WSZYSTKICH botów) - Adam po pierwszym wdrożeniu
dynamicznego TP zapytał wprost: "stoploss chyba powinien iść w górę jeśli
pozycja się nie zamknie... conajmniej jakiś zysk żeby złapać??" - słusznie:
bez tego pozycja, która ruszyła w dobrą stronę ale nie dotknęła jeszcze
`reference_price`, wciąż ryzykowała PEŁNY zjazd z powrotem do sztywnego SL
sprzed wejścia. `_trail_stop_loss()`/`_trail_stop_loss_paper()` przesuwają
stop W GÓRĘ (nigdy w dół) o TĘ SAMĄ odległość co przy wejściu
(`stop_loss_pct`, licząc od BIEŻĄCEJ ceny) - naturalnie aktywuje się dopiero
gdy pozycja jest na plusie. Świadomie PROSTSZE niż trailing w Sygnale/
Micro-Grid (jeden stały dystans, bez ATR) - EOD to krótki scalp na odbicie
z ciasnymi, procentowymi progami z samego początku, nie potrzebuje ich
komplikować.

Poświadczenia WSPÓLNE z Micro-Grid/Sygnał (services/bot_credentials.py) -
ten sam demo klucz T212, jedno hasło odblokowuje wszystkie trzy silniki.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation

import pytz
from flask import current_app

from ..extensions import db
from ..models import EODAsset, EODAuditLog, EODSettings, EODTrade
from ..routes.api_keys import get_decrypted_credentials
from ..routes.scalping import _log_order
from ..utils import current_environment, humanize_ticker_prefix, telegram_env_tag, ticker_display_name
from . import bot_credentials, diagnostics, market_hours, position_alerts, price_feed, price_watchdog, sector_diversity, telegram_notify
from .bot_engine import _FILLED_ORDER_STATUS, _lookup_recent_order, _next_retry_delay, _place_buy_with_precision_fallback
from .strategy import eod_strategy
from .strategy.microgrid_strategy import compute_equity_scaled_amount
from .t212_client import T212APIError, T212Client

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

# EOD_ENVIRONMENT jako stała USUNIĘTA 2026-08-06 - patrz identyczny komentarz
# w bot_engine.py przy `utils.current_environment`. Stara teza "T212 nie
# wspiera LIMIT/STOP na live" OBALONA empirycznie 2026-08-06/07 - LIMIT i
# STOP-LIMIT ręcznie potwierdzone działające na live.

# PRD (pierwotnie): "Działa tylko pod koniec sesji (od ok. 16:00)". Adam
# 2026-07-24 rozszerzył o sesję USA (16:00-22:00), a 2026-07-27 poprosił o
# CAŁY dzień handlowy: "niech działa cały czas od 9 do 22" - okno teraz
# obejmuje otwarcie EUR (Euronext/Xetra ~9:00) przez koniec sesji EUR
# (~17:25/17:30) i całą popołudniową sesję USA (NASDAQ/NYSE do ~21:55/22:00
# CEST) w jednym zakresie. `market_hours.is_market_open()` per-ticker nadal
# filtruje czy WŁAŚCIWA giełda dla waluty danego tickera jest akurat otwarta
# - to okno tylko ogranicza KIEDY bot w ogóle SPRAWDZA, nie zastępuje tej bramki.
EOD_WINDOW = (dt.time(9, 0), dt.time(22, 0))
# Tuż przed zamknięciem NASDAQ/NYSE (późniejsza z dwóch sesji) - używane
# WYŁĄCZNIE gdy EODSettings.force_close_enabled=True (opcjonalny
# przełącznik, domyślnie wyłączony, patrz docstring modułu).
FORCE_CLOSE_TIME = dt.time(21, 55)

DROP_LOOKBACK_MINUTES = range(1, 6)  # 1..5 minut wstecz

# Tiery wielkości pozycji (PRD, tabela "Przykładowa siatka dokupywania") -
# uproszczone do STAŁYCH mnożników zamiast zakresu % kapitału (spójne z
# entry_amount-per-ticker już używanym w BotAsset/SignalAsset, unika
# konieczności odpytywania całkowitej wartości konta na każdy tick).
SIZE_TIERS = (
    (Decimal("0.055"), Decimal("1.5")),   # -5.5% i więcej
    (Decimal("0.035"), Decimal("1.0")),   # -3.5% do -5.49%
    (Decimal("0.02"), Decimal("0.6")),    # -2.0% do -3.49%
)

# Backoff dla tick()::get_pending_orders po błędzie T212API (dodane 2026-07-28:
# znalezione na żywo - Micro-Grid już się wycofywał po serii 429, ale Sygnał i
# EOD dalej dobijały się o get_pending_orders CO 60s BEZ PRZERWY, bo żaden z
# nich nie miał własnego backoffu - non-stop bombardowanie WSPÓLNEGO dla
# wszystkich trzech silników, ciasnego limitu T212 demo nie dawało kontu
# żadnej szansy się zresetować, 429 ciągnęło się 12+h zamiast typowych paru
# minut) PRZENIESIONY do t212_client.py::get_pending_orders_for_tick()
# (2026-07-29 - był tu WŁASNY licznik/zegar, niezależny od tego samego w
# bot_engine.py/signal_engine.py, mimo że wszystkie trzy dobijają się o TEN
# SAM limit - Adam: "boty nie widza o sobie i napierdlaja w ten sam czas",
# złapane na żywo: Sygnał złapał 5 kolejnych 429 mimo że Micro-Grid/EOD w tym
# samym czasie ticowały bez błędu). _manage_exits (trailing stop-loss +
# take-profit, ochrona zysku) CELOWO nie jest tu blokowany, patrz tick()
# niżej i [[feedback_snajper_profit_protection_priority]] - backoff dotyczy
# WYŁĄCZNIE potwierdzania nowych wejść/detekcji wykonania stopa.


# Backoff (minuty) dla DRUGIEGO calla w _confirm_pending_entries -
# get_position() per pozycja, wołane ZARAZ PO udanym get_pending_orders()
# (patrz tick() wyżej) - na bardzo ciasnym limicie demo (x-ratelimit-limit
# widziane jako 0/1 pozostało) dwa udane calle T212 pod rząd w tym samym
# ticku to loteria, więc bez backoffu ten drugi call próbowałby ZNOWU na
# każdym kolejnym udanym ticku, mimo że przed chwilą zawiódł - dokładając
# kolejne zapytanie do tego samego, już wyczerpanego budżetu. Ten sam
# wzorzec co _entry_fail_backoff w bot_engine.py (w pamięci procesu, per
# (user_id, ticker), NIE per-trade w bazie - prostsze niż kolumny
# sell_retry_count/next_sell_retry_at w ActiveTrade, bo EODTrade nie ma
# ich odpowiednika i nie ma potrzeby przeżywać restartu appki).
CONFIRM_FAIL_BACKOFF_MINUTES = (1, 2, 5, 15, 30)
_confirm_fail_backoff: dict[tuple[int, str], tuple[int, dt.datetime]] = {}


def _next_confirm_fail_delay(consecutive_fails: int) -> dt.timedelta:
    idx = min(consecutive_fails - 1, len(CONFIRM_FAIL_BACKOFF_MINUTES) - 1)
    return dt.timedelta(minutes=CONFIRM_FAIL_BACKOFF_MINUTES[idx])


def _in_eod_window() -> bool:
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:
        return False
    return EOD_WINDOW[0] <= now_local.time() <= EOD_WINDOW[1]


def _should_force_close(settings: EODSettings) -> bool:
    """Tylko gdy settings.force_close_enabled=True (opcjonalny przełącznik, patrz docstring modułu)."""
    if not settings.force_close_enabled:
        return False
    now_local = dt.datetime.now(_AMSTERDAM_TZ)
    if now_local.weekday() >= 5:
        return False
    return now_local.time() >= FORCE_CLOSE_TIME


def _log_block_reason_throttled(user_id: int, key: str, message: str) -> None:
    """Dedupe (30 min/klucz) dla raportu /why (telegram_commands.py,
    2026-08-09) - patrz diagnostics.should_log_throttled, scalone tam z 3
    identycznych kopii. Prefiks "eod:" żeby nie kolidować z tymi samymi
    kluczami w bot_engine.py/signal_engine.py na dzielonym słowniku."""
    if diagnostics.should_log_throttled(user_id, f"eod:{key}"):
        _log(user_id, "INFO", message)


def _log(user_id: int, action_type: str, message: str) -> None:
    # Dodane 2026-07-30: kopia KAŻDEGO wpisu do ukrytego logu diagnostycznego
    # (diagnostics.py) - nie zmienia nic z poniższego (EODAuditLog/Dziennik
    # zostają jak były, ERROR nadal też leci do current_app.logger.error).
    diagnostics.log_diag(user_id, "eod", f"[{action_type}] {message}")
    if action_type == "ERROR":
        current_app.logger.error("[eod user=%s] %s", user_id, message)
        telegram_notify.send_telegram_message(
            current_app.config.get("TELEGRAM_BOT_TOKEN"), current_app.config.get("TELEGRAM_CHAT_ID"),
            f"🔴 [{telegram_env_tag(user_id)}] EOD ERROR (user {user_id}): {humanize_ticker_prefix(message)}",
        )
    entry = EODAuditLog(
        user_id=user_id, action_type=action_type, message=message,
        environment=current_environment(user_id),
    )
    db.session.add(entry)
    db.session.commit()


def _worst_recent_drop(candles: list[dict] | None) -> tuple[Decimal, Decimal] | None:
    """
    Najbardziej ujemna zmiana % między ceną sprzed N minut (N=1..5) a
    ostatnią świecą - "ostry ruch" niezależnie od DOKŁADNEGO okna czasowego
    w którym się wydarzył (PRD: "reaguje na ostry spadek w krótkim czasie
    (1-5 minut)"). None gdy za mało świec (dopiero co otworzyła się sesja).

    Zwraca (drop_pct, reference_price) - reference_price to CENA SPRZED TYLU
    MINUT ILE DAŁ NAJGORSZY SPADEK, czyli poziom "sprzed spadku" (dodane
    2026-07-28, wcześniej funkcja zwracała tylko drop_pct pod nazwą
    _worst_recent_drop_pct - zmieniona nazwa, bo teraz zwraca więcej niż
    sam procent). Adam: "jebło w dół np 3-5% w ciągu 1-2min, kupuje i liczę
    na szybkie odbicie w okolice wcześniejszego poziomu np 3-4min wcześniej"
    - ten reference_price staje się celem take-profit w _enter_position,
    zamiast dawnego sztywnego price*(1+take_profit_pct) oderwanego od
    wielkości spadku.
    """
    if not candles or len(candles) < 2:
        return None
    latest_close = Decimal(str(candles[-1]["c"]))
    worst = None
    for n in DROP_LOOKBACK_MINUTES:
        idx = len(candles) - 1 - n
        if idx < 0:
            continue
        reference = Decimal(str(candles[idx]["c"]))
        if reference <= 0:
            continue
        pct = (latest_close - reference) / reference
        if worst is None or pct < worst[0]:
            worst = (pct, reference)
    return worst


def _size_multiplier_for_drop(drop_pct: Decimal | None) -> Decimal | None:
    """Zwraca mnożnik entry_amount dla danego spadku (ujemny %), albo None gdy poniżej progu triggera (-2.0%)."""
    if drop_pct is None or drop_pct >= 0:
        return None
    drop_abs = abs(drop_pct)
    for threshold, multiplier in SIZE_TIERS:
        if drop_abs >= threshold:
            return multiplier
    return None


def _finalize_closed_trade(user_id: int, trade: EODTrade, via: str, fill_price: Decimal) -> None:
    trade.close_price = fill_price
    trade.status = "CLOSED"
    trade.closed_via = via
    trade.closed_at = dt.datetime.utcnow()
    db.session.commit()
    pnl = (fill_price - trade.buy_price) * trade.quantity
    _log(
        user_id, "INFO",
        f"{trade.ticker}: pozycja EOD zamknięta ({via}) @ ~{fill_price}, "
        f"P/L ~{pnl:.2f} {trade.currency}.",
    )


def close_trade_manual(user_id: int, trade: EODTrade) -> tuple[bool, str]:
    """
    Ręczne zamknięcie pozycji - wydzielone z routes/eod.py::close_position
    (Adam, 2026-08-09: dodanie `/close` na Telegramie), ten sam wzorzec co
    signal_engine.py::close_trade_manual (identyczny kształt, osobna kopia
    bo osobny silnik/model - patrz komentarz tam).
    """
    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
    price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
    name = ticker_display_name(trade.ticker)

    if trade.is_paper:
        _finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
        return True, f"{name}: zamknięte (paper) @ ~{price or trade.buy_price}."

    if not trade.buy_confirmed:
        return False, f"{name}: zlecenie kupna jeszcze nie potwierdzone - poczekaj aż się wykona."

    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return False, "EOD nie ma aktywnych poświadczeń (aktywuj go w appce)."
    env = current_environment(user_id)
    creds = get_decrypted_credentials(user_id, master_key, env)
    if creds is None:
        return False, "Brak zapisanego klucza API."
    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=env, engine="eod", user_id=user_id)

    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            return False, f"{name}: nie udało się anulować stop-loss przed ręczną sprzedażą - {exc}"

    try:
        sell_result = client.place_market_order(trade.ticker, -trade.quantity)
    except T212APIError as exc:
        return False, f"{name}: sprzedaż Market nie powiodła się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA."

    _log_order(
        user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
        price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
    )
    _finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
    return True, f"{name}: zamknięte @ ~{price or trade.buy_price}."


def _get_current_equity(
    user_id: int, settings: EODSettings, client: T212Client | None = None,
) -> Decimal | None:
    """Ten sam wzorzec co signal_engine.py::_get_current_equity, tag "eod" w diagnostics.log_diag."""
    if client is None:
        master_key = bot_credentials.get_master_key(user_id)
        if master_key is None:
            return None
        creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
        if creds is None:
            return None
        client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
            engine="eod", user_id=user_id,
        )
    try:
        raw = client.get_cash()
        return Decimal(str(raw["total"]))
    except (T212APIError, InvalidOperation, TypeError, KeyError) as exc:
        diagnostics.log_diag(
            user_id, "eod", f"equity sizing: nie udało się pobrać equity ({exc}) - baza bez skalowania.",
        )
        return None


def _enter_position(
    user_id: int, client: T212Client | None, asset: EODAsset, settings: EODSettings,
    price: Decimal, drop_pct: Decimal, multiplier: Decimal, reference_price: Decimal,
    current_equity: Decimal | None = None,
) -> bool:
    """
    Zwraca True gdy pozycja faktycznie została otwarta (paper LUB realne
    zlecenie złożone), False gdy odrzucona - patrz _process_entries niżej
    (ranking kandydatów po głębokości spadku zamiast "pierwszy pasujący"),
    ten sam wzorzec co signal_engine.py/bot_engine.py.
    """
    # Matematyka (sizing, TP=reference_price z fallbackiem na sztywny %)
    # wyciągnięta 2026-07-28 do eod_strategy.compute_entry() - PEŁNE
    # uzasadnienie (Adam: "liczę na szybkie odbicie w okolice wcześniejszego
    # poziomu... nawet nie musi być idealnie w punkt ale w okolice") zostaje
    # w docstringu tego modułu i eod_strategy.py - tu tylko wołanie już
    # zweryfikowanej formuły (sprawdzone 1:1 na 3 realnych transakcjach z bazy).
    #
    # Money management √equity (2026-08-03) - skalujemy BAZOWĄ kwotę PRZED
    # mnożnikiem tieru spadku (`multiplier`), nie po - equity_sizing dotyczy
    # Twojego kapitału bazowego, tier dotyczy agresywności KONKRETNEGO
    # sygnału, oba mnożą się niezależnie.
    effective_entry_amount = asset.entry_amount
    if settings.equity_sizing_enabled and current_equity is not None:
        effective_entry_amount = compute_equity_scaled_amount(
            asset.entry_amount, current_equity, settings.equity_sizing_baseline or Decimal("0"),
        )
    # Koszt przewalutowania (FX) dla tickerów USD na koncie EUR (patrz
    # EODSettings.fx_cost_adjustment_enabled) - PODBIJA próg stop-loss,
    # NIE dotyka ceny użytej do wyliczenia quantity.
    fx_reference_price = None
    if asset.currency == "USD" and settings.fx_cost_adjustment_enabled:
        fx_reference_price = price * (1 + settings.fx_fee_pct * 2)

    try:
        decision = eod_strategy.compute_entry(
            effective_entry_amount, price, multiplier, reference_price,
            settings.stop_loss_pct, settings.take_profit_pct,
            fx_reference_price=fx_reference_price,
        )
    except eod_strategy.EntryValidationError as exc:
        _log(user_id, "ERROR", f"{asset.ticker}: {exc}")
        return False
    quantity = decision.quantity
    stop_loss_price = decision.stop_loss_price
    take_profit_price = decision.take_profit_price

    if settings.is_paper_trading:
        trade = EODTrade(
            user_id=user_id, eod_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
            buy_order_id=f"PAPER-{uuid.uuid4()}",
            buy_price=price, quantity=quantity, allocated_value=quantity * price,
            drop_pct_at_entry=drop_pct, size_multiplier=multiplier,
            stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            status="OPEN", is_paper=True, buy_confirmed=True,
            environment=current_environment(user_id),
        )
        db.session.add(trade)
        db.session.commit()
        _log(
            user_id, "BUY",
            f"[PAPER] {asset.ticker}: ostry spadek {drop_pct*100:.2f}% (tier x{multiplier}), "
            f"{quantity} @ ~{price} - SL {stop_loss_price:.4f} / TP {take_profit_price:.4f}.",
        )
        return True

    try:
        existing_position = client.get_position(asset.ticker)
    except T212APIError:
        existing_position = None
    baseline = Decimal(str(existing_position["quantity"])) if existing_position else Decimal("0")

    try:
        buy_result, quantity = _place_buy_with_precision_fallback(client, asset.ticker, quantity, price)
    except T212APIError as exc:
        _log(user_id, "ERROR", f"{asset.ticker}: błąd składania zlecenia kupna EOD - {exc}")
        return False

    trade = EODTrade(
        user_id=user_id, eod_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
        buy_order_id=buy_result.order_id, baseline_owned_quantity=baseline,
        buy_price=price, quantity=quantity, allocated_value=quantity * price,
        drop_pct_at_entry=drop_pct, size_multiplier=multiplier,
        stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
        status="OPEN", is_paper=False, buy_confirmed=False,
        environment=current_environment(user_id),
    )
    db.session.add(trade)
    db.session.commit()

    _log_order(
        user_id=user_id, ticker=asset.ticker, side="buy", quantity=quantity,
        price_snapshot=price, status="sent", t212_order_id=buy_result.order_id,
    )
    _log(
        user_id, "BUY",
        f"{asset.ticker}: ostry spadek {drop_pct*100:.2f}% (tier x{multiplier}), "
        f"{quantity} @ ~{price} - SL {stop_loss_price:.4f} / TP {take_profit_price:.4f}. "
        "Czeka na potwierdzenie kupna, dopiero potem uzbroi stop-loss.",
    )
    return True


def _process_entries(
    user_id: int, client: T212Client | None, settings: EODSettings, current_equity: Decimal | None = None,
) -> None:
    env = current_environment(user_id)
    assets = EODAsset.query.filter_by(user_id=user_id, environment=env).all()
    if not assets:
        return

    open_tickers = {t.ticker for t in EODTrade.query.filter_by(user_id=user_id, status="OPEN", environment=env).all()}
    # DODANE 2026-08-03 - do tej pory EOD nie miało ŻADNEGO limitu jednoczesnych
    # pozycji (ta sama luka jaką miał Sygnał do wieczora tego samego dnia,
    # patrz signal_engine.py::MAX_CONCURRENT_POSITIONS) - lista High Conviction
    # mogłaby teoretycznie otworzyć pozycję na KAŻDYM tickerze naraz przy
    # szerokim spadku rynku. EODSettings.max_concurrent_positions - edytowalne
    # w UI, domyślnie 2 (patrz migrate_add_max_concurrent_positions.py).
    if len(open_tickers) >= settings.max_concurrent_positions:
        _log_block_reason_throttled(
            user_id, "max_concurrent",
            f"Wejścia: limit pozycji osiągnięty ({len(open_tickers)}/{settings.max_concurrent_positions}) - nic nowego dziś.",
        )
        return  # limit otwartych pozycji osiągnięty - nic nowego dziś

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    # ZMIANA 2026-08-05 (Adam: "napraw to", po ustaleniu że ta pętla wchodziła
    # w pierwszego pasującego zamiast oceniać wszystkich - patrz identyczna
    # zmiana w signal_engine.py::_process_entries, ten sam wzorzec) - EOD to
    # teraz silnik PRIORYTETOWY ("to jest tzw strzal, jak on cos zacznie
    # reszta ma czekac i nie przeszkadzac", patrz market_hours.py - CAŁA
    # lista EOD jest zarezerwowana od Micro-Gridu/Sygnału), więc tym bardziej
    # ma sens wybierać NAJLEPSZEGO kandydata z tego co faktycznie mu wolno
    # dotknąć, a nie pierwszego z brzegu. Faza 1 (ta pętla) zbiera
    # WSZYSTKICH kandydatów u których w ogóle wystrzelił trigger (ostry
    # spadek), faza 2 (niżej) sortuje po GŁĘBOKOŚCI spadku i próbuje wejść
    # od najgłębszego - to dokładnie ta sama logika co już istniejący system
    # tierów wielkości pozycji (SIZE_TIERS: głębszy spadek = większa
    # przekonanie = większa pozycja), tylko rozszerzona na WYBÓR tickera,
    # nie tylko wielkość zlecenia.
    candidates: list[tuple] = []  # (asset, drop, reference_price, multiplier, price)

    for asset in assets:
        if asset.ticker in open_tickers:
            continue
        other = market_hours.held_by_other_engine(user_id, asset.ticker, "eod")
        if other is not None:
            # Cudza pozycja (Micro-Grid/Sygnał) na tym samym tickerze -
            # pomijamy, zeby nie powtorzyc kolizji SUp_EQ (patrz
            # market_hours.py::held_by_other_engine).
            diagnostics.log_diag(
                user_id, "eod",
                f"{asset.ticker}: pominięte wejście - już otwarte w {other}.",
            )
            continue
        sector_collision = sector_diversity.held_sector_ticker(user_id, env, asset.ticker)
        if sector_collision is not None:
            # Koncentracja sektorowa (2026-08-10) - patrz docstring
            # sector_diversity.py, znalezione na żywo (Sygnał, 4/6 pozycji w
            # tym samym sektorze). Skanuje wszystkie 3 silniki.
            diagnostics.log_diag(
                user_id, "eod",
                f"{asset.ticker}: pominięte wejście - już otwarta pozycja w tym samym sektorze ({sector_collision}).",
            )
            continue
        if not market_hours.is_market_open(asset.currency):
            continue

        candles = price_feed.get_eod_intraday_1m(asset.ticker, alpaca_key, alpaca_secret)
        drop_result = _worst_recent_drop(candles)
        if drop_result is None:
            continue
        drop, reference_price = drop_result
        multiplier = _size_multiplier_for_drop(drop)
        if multiplier is None:
            continue

        price = price_feed.get_live_price(api_key, asset.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            continue

        candidates.append((asset, drop, reference_price, multiplier, price))

    if not candidates:
        return

    candidates.sort(key=lambda c: c[1])  # drop jest UJEMNY - najgłębszy (najbardziej ujemny) pierwszy
    _log(
        user_id, "INFO",
        f"Wejścia EOD: {len(candidates)} kandydat(ów) z triggerem. Najgłębszy: "
        f"{candidates[0][0].ticker} ({candidates[0][1]*100:.2f}%).",
    )
    for asset, drop, reference_price, multiplier, price in candidates:
        if _enter_position(user_id, client, asset, settings, price, drop, multiplier, reference_price, current_equity):
            return  # NAJWYŻEJ JEDNO faktyczne wejście na tick - ten sam powód
            # rate-limitowy co zawsze (patrz docstring MAX_CONCURRENT_POSITIONS
            # w innych silnikach) - kolejny kandydat dostanie szansę w
            # następnym ticku, chyba że zwycięzca akurat zawiódł (wtedy
            # próbujemy od razu kolejnego w tym samym ticku, patrz fallback
            # bot_engine.py::_process_entries).


def _confirm_pending_entries(user_id: int, client: T212Client, settings: EODSettings, pending_order_ids: set[str]) -> None:
    """
    `pending_order_ids` pobierane RAZ w tick() i dzielone z _manage_exits -
    unika dublowania get_pending_orders w tym samym ticku (patrz
    TICK_ERROR_BACKOFF_MINUTES wyżej, ten sam powód co w bot_engine.py).
    """
    pending_trades = EODTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False,
        environment=current_environment(user_id),
    ).all()
    if not pending_trades:
        return

    now = dt.datetime.utcnow()
    for trade in pending_trades:
        if trade.buy_order_id in pending_order_ids:
            continue

        backoff_key = (user_id, trade.ticker)
        backoff = _confirm_fail_backoff.get(backoff_key)
        if backoff is not None and now < backoff[1]:
            continue  # w backoffie po poprzednich błędach get_position, patrz CONFIRM_FAIL_BACKOFF_MINUTES

        try:
            position = client.get_position(trade.ticker)
        except T212APIError as exc:
            consecutive = (backoff[0] if backoff else 0) + 1
            delay = _next_confirm_fail_delay(consecutive)
            _confirm_fail_backoff[backoff_key] = (consecutive, now + delay)
            _log(
                user_id, "ERROR",
                f"{trade.ticker}: błąd sprawdzenia portfela po zakupie EOD #{consecutive} z rzędu - {exc} - "
                f"kolejna próba za {int(delay.total_seconds() // 60)} min.",
            )
            continue
        _confirm_fail_backoff.pop(backoff_key, None)

        current_owned = Decimal(str(position["quantity"])) if position else Decimal("0")
        filled = current_owned - trade.baseline_owned_quantity
        if filled <= 0:
            trade.status = "CLOSED"
            trade.closed_via = "never-filled"
            trade.closed_at = dt.datetime.utcnow()
            db.session.commit()
            _log(user_id, "ERROR", f"{trade.ticker}: zlecenie kupna EOD zniknęło z kolejki bez wypełnienia.")
            continue

        trade.quantity = filled
        trade.allocated_value = filled * trade.buy_price
        trade.buy_confirmed = True
        db.session.commit()

        try:
            stop_result = client.place_stop_order(trade.ticker, -filled, trade.stop_loss_price)
            trade.stop_order_id = stop_result.order_id
            db.session.commit()
            # KRYTYCZNE: dopisz nowo uzbrojony stop do TEGO SAMEGO pending_order_ids,
            # ktorego zaraz potem uzyje _manage_exits w tym samym ticku (patrz tick()/
            # reconcile() - dzielony snapshot, TICK_ERROR_BACKOFF_MINUTES wyzej). Bez
            # tego _manage_exits widzialby swiezo zlozony stop jako "nieobecny w
            # pending" (bo snapshot pobrano PRZED tym place'em) i falszywie uznawal
            # pozycje za zamknieta w tym samym ticku, w ktorym dopiero co ja otworzyl -
            # ten sam zywy bug znaleziony 2026-07-28 na IFXd_EQ w signal_engine.py
            # (identyczny wzorzec kodu tutaj, wiec identyczne ryzyko).
            pending_order_ids.add(stop_result.order_id)
            _log(user_id, "INFO", f"{trade.ticker}: kupno EOD potwierdzone ({filled} szt.), stop-loss uzbrojony na {trade.stop_loss_price:.4f}.")
        except T212APIError as exc:
            _log(
                user_id, "ERROR",
                f"{trade.ticker}: kupno EOD potwierdzone, ale NIE udało się uzbroić stop-loss - {exc}. "
                "Pozycja NIECHRONIONA, sprawdź ręcznie.",
            )


def _bump_buy_retry(user_id: int, trade: EODTrade, reason: str) -> None:
    trade.buy_retry_count += 1
    trade.next_buy_retry_at = dt.datetime.utcnow() + _next_retry_delay(trade.buy_retry_count)
    db.session.commit()
    _log(user_id, "INFO", f"{trade.ticker}: sprawdzenie LIMIT BUY #{trade.buy_retry_count} - {reason}")


def _retry_pending_buys(
    user_id: int, client: T212Client, settings: EODSettings, pending: list[dict],
) -> None:
    """
    "Goni" cenę LIMIT BUY, który jeszcze się nie wypełnił i przy obecnej
    cenie rynkowej JUŻ SIĘ NIE MOŻE wypełnić - anuluje stare zlecenie i
    wystawia nowe po aktualnej cenie. Dokładny port `bot_engine.py::
    _retry_pending_buys` (Micro-Grid), identyczny jak `signal_engine.py::
    _retry_pending_buys` - dodane 2026-07-30, ten sam powód (IFXd_EQ utknięte
    9+ godzin w Sygnale, EOD miał identyczną dziurę, patrz komentarz przy
    `_confirm_pending_entries` wyżej - "identyczny wzorzec kodu, identyczne
    ryzyko").

    Przeliczanie ilości od DOCELOWEJ kwoty alokacji (EODAsset.entry_amount),
    NIE od starej `trade.quantity` - ten sam fix co w bot_engine.py 2026-07-22
    (Meta/FB, patrz pełne uzasadnienie w signal_engine.py::_retry_pending_buys).

    `pending`: już pobrana lista z get_pending_orders[_for_tick]() - dzielona
    z `_confirm_pending_entries` w tym samym cyklu (rate limit demo ciasny).

    stop_loss_only_mode (2026-08-06) - patrz identyczny guard i uzasadnienie
    w bot_engine.py::_retry_pending_buys (znalezione na żywo na Micro-Gridzie,
    ten sam mechanizm/ryzyko tutaj).
    """
    if settings.stop_loss_only_mode:
        return
    now = dt.datetime.utcnow()
    candidates = (
        EODTrade.query
        .filter_by(
            user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False,
            environment=current_environment(user_id),
        )
        .filter(db.or_(EODTrade.next_buy_retry_at.is_(None), EODTrade.next_buy_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if market_hours.is_position_management_hours(t.currency)]
    if not candidates:
        return

    pending_by_id = {str(o.get("id")): o for o in pending}

    for trade in candidates:
        pending_order = pending_by_id.get(trade.buy_order_id)
        if pending_order is None:
            continue  # zniknęło z pending - zajmie się tym _confirm_pending_entries

        if Decimal(str(pending_order.get("filledQuantity", 0))) > 0:
            continue  # częściowo już wypełnione - nie anulujemy w połowie, niech dokończy

        current_price = price_feed.get_live_price(
            current_app.config.get("FINNHUB_API_KEY"), trade.ticker,
            current_app.config.get("ALPACA_API_KEY"), current_app.config.get("ALPACA_API_SECRET"),
        )
        if current_price is None or current_price <= 0:
            _bump_buy_retry(user_id, trade, "brak aktualnej ceny do porównania z limitem, spróbuję ponownie.")
            continue

        if current_price <= trade.buy_price:
            _bump_buy_retry(
                user_id, trade,
                f"cena ({current_price}) wciąż <= limitu ({trade.buy_price}), czekam na wypełnienie.",
            )
            continue

        asset = EODAsset.query.get(trade.eod_asset_id)
        target_amount = asset.entry_amount if asset is not None else (trade.quantity * trade.buy_price)
        new_quantity = (target_amount / current_price).quantize(Decimal("0.0001"))
        if new_quantity <= 0:
            _bump_buy_retry(
                user_id, trade,
                f"cena odjechała ({current_price} > limit {trade.buy_price}), ale przeliczona ilość <= 0 "
                f"(kwota {target_amount} / cena {current_price}), pomijam ten tick.",
            )
            continue

        try:
            client.cancel_order(trade.buy_order_id)
        except T212APIError as exc:
            _bump_buy_retry(
                user_id, trade,
                f"cena odjechała ({current_price} > limit {trade.buy_price}), ale anulowanie starego "
                f"zlecenia nie powiodło się - {exc}",
            )
            continue

        try:
            new_result, new_quantity = _place_buy_with_precision_fallback(
                client, trade.ticker, new_quantity, current_price,
            )
        except T212APIError as exc:
            _bump_buy_retry(
                user_id, trade,
                f"stare LIMIT BUY anulowane, ale nowe po {current_price} nie powiodło się - {exc}",
            )
            continue

        old_price = trade.buy_price
        old_quantity = trade.quantity
        trade.buy_order_id = new_result.order_id
        trade.buy_price = current_price
        trade.quantity = new_quantity
        trade.allocated_value = (new_quantity * current_price).quantize(Decimal("0.01"))
        trade.buy_retry_count = 0
        trade.next_buy_retry_at = None
        db.session.commit()
        _log(
            user_id, "INFO",
            f"{trade.ticker}: cena odjechała ({old_price} -> {current_price}) - LIMIT BUY ponowiony po nowej "
            f"cenie, ilość przeliczona ({old_quantity} -> {new_quantity}) żeby zachować alokację ~{target_amount}.",
        )


def _force_close_real(user_id: int, client: T212Client, trade: EODTrade) -> None:
    """Wymuszone zamknięcie - TYLKO gdy EODSettings.force_close_enabled=True (patrz _should_force_close). Anuluje STOP, Market Sell."""
    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            _log(user_id, "INFO", f"{trade.ticker}: anulowanie stop-loss przy wymuszonym zamknięciu EOD nie powiodło się (mógł się właśnie wykonać) - {exc}")
            return  # kolejny tick wykryje ewentualne wykonanie STOP-a (zniknie z pending)

    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
    price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)

    try:
        sell_result = client.place_market_order(trade.ticker, -trade.quantity)
    except T212APIError as exc:
        _log(user_id, "ERROR", f"{trade.ticker}: wymuszone zamknięcie EOD (koniec sesji) nie powiodło się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA, sprawdź ręcznie.")
        return

    _log_order(
        user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
        price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
    )
    _finalize_closed_trade(user_id, trade, "eod-forced", fill_price=price if price is not None else trade.buy_price)


# Ulamek dystansu stop_loss_pct - jak duza musi byc poprawa zanim oplaca sie
# placic Cancel-Replace z ciasnego rate limitu demo T212 (ten sam powod co
# MIN_TRAIL_REQUOTE_ATR_FRACTION w signal_engine.py).
MIN_TRAIL_REQUOTE_EOD_FRACTION = Decimal("0.1")


def _trail_stop_loss(
    user_id: int, client: T212Client, trade: EODTrade, settings: EODSettings, price: Decimal,
) -> None:
    """
    Przesuwa stop-loss W GÓRĘ (nigdy w dół) - dodane 2026-07-28, patrz
    uzasadnienie w docstringu modułu ("TRAILING STOP-LOSS"). Ten sam dystans
    procentowy co przy wejściu (`stop_loss_pct`), liczony od BIEŻĄCEJ ceny -
    naturalnie aktywuje się dopiero gdy pozycja jest na plusie, więc chroni
    WYŁĄCZNIE już zarobiony zysk. Wołane z `_manage_exits()` gdy
    `price < take_profit_price` (jeśli TP już osiągnięty, pozycja i tak
    zaraz się zamyka - nie ma sensu przesuwać stopu tuż przed sprzedażą).
    """
    candidate_stop = eod_strategy.compute_trailing_stop(
        trade.stop_loss_price, price, trade.buy_price, settings.stop_loss_pct, MIN_TRAIL_REQUOTE_EOD_FRACTION,
    )
    if candidate_stop is None:
        return  # nic do poprawy - juz na tym poziomie/wyzej, albo poprawa za mala na Cancel-Replace

    # Cudzy (nie-botowy) SELL na tickerze - patrz identyczny komentarz i
    # incydent w bot_engine.py::_manage_trailing_exit (2026-08-10, Adam po
    # TotalEnergies/SUp_EQ). get_pending_orders() dzieli 50s cache z resztą
    # ticku, więc to zwykle cache hit, nie dodatkowy request.
    try:
        live_orders = client.get_pending_orders()
    except T212APIError:
        live_orders = []
    foreign_order = next(
        (o for o in live_orders if o.get("ticker") == trade.ticker and o.get("side") == "SELL"), None,
    )
    if foreign_order is not None and str(foreign_order.get("id")) != trade.stop_order_id:
        trade.stop_order_id = None
        trade.status = "RELEASED"
        db.session.commit()
        _log(
            user_id, "WARN",
            f"{trade.ticker}: wykryto na T212 zlecenie SELL ({foreign_order.get('id')}) spoza bota "
            f"(initiatedFrom={foreign_order.get('initiatedFrom')}) - pozycja zwolniona spod zarządzania "
            "automatycznie, żeby bot nie dobijał się o nią co tick. Udziały zostają na koncie.",
        )
        return

    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            _log(user_id, "INFO", f"{trade.ticker}: przesunięcie trailing stop-loss EOD - anulowanie starego ({trade.stop_order_id}) nie powiodło się (mógł się już wykonać) - {exc}")
            return

    try:
        stop_result = client.place_stop_order(trade.ticker, -trade.quantity, candidate_stop)
    except T212APIError as exc:
        trade.stop_order_id = None
        db.session.commit()
        _log(user_id, "ERROR", f"{trade.ticker}: uzbrojenie przesuniętego stop-loss EOD (target {candidate_stop}) nie powiodło się - {exc}. Pozycja NIECHRONIONA do następnego ticku.")
        return

    old_stop = trade.stop_loss_price
    trade.stop_loss_price = candidate_stop
    trade.stop_order_id = stop_result.order_id
    db.session.commit()
    _log(user_id, "INFO", f"{trade.ticker}: trailing stop-loss EOD przesunięty z {old_stop:.4f} na {candidate_stop:.4f} (cena teraz {price:.4f}).")


def _trail_stop_loss_paper(trade: EODTrade, settings: EODSettings, price: Decimal) -> None:
    """Jak _trail_stop_loss(), ale bez T212 (pozycja papierowa - czysty zapis do bazy, zero zlecen/rate limitu)."""
    candidate_stop = (price * (1 - settings.stop_loss_pct)).quantize(Decimal("0.0001"))
    if candidate_stop <= trade.stop_loss_price:
        return
    trade.stop_loss_price = candidate_stop
    db.session.commit()


def _manage_exits(
    user_id: int, client: T212Client, settings: EODSettings,
    pending_order_ids: set[str], pending_fetch_ok: bool,
) -> None:
    open_trades = EODTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True,
        sl_suspended_for_weekend=False, environment=current_environment(user_id),
    ).all()
    if not open_trades:
        return

    force_close = _should_force_close(settings)

    # `pending_order_ids`/`pending_fetch_ok` pobierane RAZ w tick() (dzielone
    # z _confirm_pending_entries, patrz TICK_ERROR_BACKOFF_MINUTES wyżej) -
    # nie moga juz blokowac trailing stop-loss (ochrona zysku ma
    # pierwszenstwo, patrz [[feedback_snajper_profit_protection_priority]] i
    # ten sam fix w signal_engine.py tego samego dnia) - fetch_ok=False
    # wylacza WYLACZNIE detekcje "czy stop juz sam sie wykonal", reszta
    # petli leci dalej (nie potrzebuje pending, tylko ceny).
    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for trade in open_trades:
        # Stop-loss zniknal z pending - MOZE oznaczac wykonanie, ale samo
        # zniknięcie tego NIE dowodzi (mogło też zostać anulowane/odrzucone
        # przez T212) - ten sam bug i fix co bot_engine.py::_resolve_vanished_leg
        # (znaleziony tam 2026-07-23), nigdy nie przeniesiony do EOD.
        # Sprawdzamy realny status w historii T212 PRZED uznaniem za zamknięte.
        if pending_fetch_ok and trade.stop_order_id and trade.stop_order_id not in pending_order_ids:
            item = _lookup_recent_order(client, trade.stop_order_id)
            if item is None:
                continue
            status = (item.get("order") or {}).get("status")
            if status == _FILLED_ORDER_STATUS:
                fill_price_raw = (item.get("fill") or {}).get("price")
                fill_price = Decimal(str(fill_price_raw)) if fill_price_raw is not None else trade.stop_loss_price
                _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=fill_price)
            else:
                _log(
                    user_id, "WARN",
                    f"{trade.ticker}: zlecenie stop-loss {trade.stop_order_id} zniknęło z pending, ale "
                    f"historia T212 pokazuje status {status} (NIE {_FILLED_ORDER_STATUS}) - NIE zamykam "
                    "pozycji EOD, zlecenie zostanie wystawione ponownie.",
                )
                trade.stop_order_id = None
                db.session.commit()
            continue

        if force_close:
            _force_close_real(user_id, client, trade)
            continue

        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            # Watchdog - patrz identyczny komentarz w bot_engine.py::_manage_trailing_exit.
            if price_watchdog.note_price_result("eod", trade.ticker, False):
                _log(
                    user_id, "ERROR",
                    f"{trade.ticker}: brak ceny przez {price_watchdog.ALERT_THRESHOLD} ticków z rzędu - "
                    "trailing stop NIE działa dla tej pozycji! Sprawdź TICKER_MAP/mapowanie Yahoo "
                    "(finnhub_client.py) albo pokrycie instrumentu.",
                )
            continue
        price_watchdog.note_price_result("eod", trade.ticker, True)
        position_alerts.check_move_alert(user_id, "eod", trade.ticker, trade.buy_price, price, trade.currency)
        if price < trade.take_profit_price:
            _trail_stop_loss(user_id, client, trade, settings, price)
            continue

        if trade.stop_order_id:
            try:
                client.cancel_order(trade.stop_order_id)
            except T212APIError as exc:
                _log(user_id, "INFO", f"{trade.ticker}: anulowanie stop-loss przed take-profit EOD nie powiodło się (mógł się właśnie wykonać) - {exc}")
                continue

        try:
            sell_result = client.place_market_order(trade.ticker, -trade.quantity)
        except T212APIError as exc:
            _log(user_id, "ERROR", f"{trade.ticker}: take-profit EOD osiągnięty, ale sprzedaż Market nie powiodła się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA.")
            continue

        _log_order(
            user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
            price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
        )
        _finalize_closed_trade(user_id, trade, "take-profit", fill_price=price)


def _manage_paper_exits(user_id: int, settings: EODSettings) -> None:
    open_trades = EODTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=True, environment=current_environment(user_id),
    ).all()
    if not open_trades:
        return

    force_close = _should_force_close(settings)
    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")

    for trade in open_trades:
        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
        if price is None or price <= 0:
            if force_close:
                _finalize_closed_trade(user_id, trade, "eod-forced", fill_price=trade.buy_price)
            continue
        if price <= trade.stop_loss_price:
            _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=price)
        elif price >= trade.take_profit_price:
            _finalize_closed_trade(user_id, trade, "take-profit", fill_price=price)
        elif force_close:
            _finalize_closed_trade(user_id, trade, "eod-forced", fill_price=price)
        else:
            _trail_stop_loss_paper(trade, settings, price)


def reconcile(user_id: int) -> None:
    """Wołane raz zaraz po aktywacji (routes/eod.py::activate) - nadgania stan bez czekania na najbliższy tick."""
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        _log(user_id, "ERROR", "Reconciliation: brak poświadczeń w bot_credentials mimo aktywacji.")
        return
    creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
    if creds is None:
        _log(user_id, "ERROR", "Reconciliation: brak zapisanego klucza API demo, pomijam.")
        return
    settings = EODSettings.query.filter_by(user_id=user_id).first()
    if not settings or not settings.is_active:
        # Symetrycznie do tick() - patrz identyczny fix w bot_engine.py
        # (2026-08-09, IDEAS_v2.md TODO z 2026-08-06).
        return

    _manage_paper_exits(user_id, settings)
    if not settings.is_paper_trading:
        client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
            engine="eod", user_id=user_id,
        )
        # get_pending_orders_for_tick() zamiast get_pending_orders() - reconcile()
        # (start appki + ręczna aktywacja) wcześniej omijało WSPÓLNY backoff/cache
        # tick()/reconcile() między Micro-Grid/Sygnał/EOD (patrz t212_client.py),
        # więc restart appki albo ręczne "Aktywuj" mogło strzelić realny request
        # do T212 W TRAKCIE aktywnego backoffu ustawionego przez inny silnik, a
        # własny 429 stąd nie zasilał eskalacji - złapane na żywo 2026-08-04
        # (EOD dostał 429 mimo że tick() był już w backoffie).
        try:
            pending = client.get_pending_orders_for_tick()
        except T212APIError as exc:
            consecutive, delay_seconds = client.tick_backoff_status() or (1, 60.0)
            _log(
                user_id, "ERROR",
                f"Reconciliation: błąd pobierania pending orders #{consecutive} z rzędu ({exc}) - "
                f"kolejna próba za {max(1, round(delay_seconds / 60))} min.",
            )
            pending_ids = set()
            pending_fetch_ok = False
        else:
            if pending is None:
                _log(user_id, "INFO", "Reconciliation: pomijam pending orders (wspólny backoff po wcześniejszych błędach T212).")
                pending_ids = set()
                pending_fetch_ok = False
            else:
                pending_ids = {str(o.get("id")) for o in pending}
                pending_fetch_ok = True
        if pending_fetch_ok:
            _confirm_pending_entries(user_id, client, settings, pending_ids)
            _retry_pending_buys(user_id, client, settings, pending=pending)
        _manage_exits(user_id, client, settings, pending_order_ids=pending_ids, pending_fetch_ok=pending_fetch_ok)


def tick(app) -> None:
    """Wołane cyklicznie przez APScheduler co 60s (PRD: 'co 1 minutę sprawdza świece 1-minutowe') - patrz app/__init__.py."""
    with app.app_context():
        for user_id in bot_credentials.active_user_ids():
            settings = EODSettings.query.filter_by(user_id=user_id).first()
            if not settings or not settings.is_active:
                continue

            diagnostics.log_diag(user_id, "eod", "tick start")

            _manage_paper_exits(user_id, settings)

            if settings.is_paper_trading:
                if _in_eod_window() and not settings.stop_loss_only_mode:
                    current_equity = _get_current_equity(user_id, settings) if settings.equity_sizing_enabled else None
                    _process_entries(user_id, None, settings, current_equity)
                continue

            master_key = bot_credentials.get_master_key(user_id)
            if master_key is None:
                continue
            creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
            if creds is None:
                _log(user_id, "ERROR", "Brak zapisanego klucza API demo, pomijam tick.")
                continue
            client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
            engine="eod", user_id=user_id,
        )

            # Kupno nie ma priorytetu (Adam, 2026-07-27, patrz ten sam komentarz
            # w bot_engine.py::tick) - w backoffie po błędach get_pending_orders
            # pomijamy TYLKO nowe wejścia i potwierdzanie/detekcję przez pending,
            # _manage_exits (trailing stop-loss + take-profit) leci zawsze.
            skip_new_entries = False
            # get_pending_orders_for_tick() = get_pending_orders() + backoff
            # WSPÓLNY między Micro-Grid/Sygnał/EOD, patrz t212_client.py - None
            # = wciąż w backoffie po poprzednich błędach (JAKIEGOKOLWIEK z
            # trzech silników).
            try:
                pending = client.get_pending_orders_for_tick()
            except T212APIError as exc:
                consecutive, delay_seconds = client.tick_backoff_status() or (1, 60.0)
                _log(
                    user_id, "ERROR",
                    f"Tick: błąd pobierania pending orders #{consecutive} z rzędu ({exc}) - "
                    f"kolejna próba za {max(1, round(delay_seconds / 60))} min zamiast za 60s.",
                )
                skip_new_entries = True
                _manage_exits(user_id, client, settings, pending_order_ids=set(), pending_fetch_ok=False)
            else:
                if pending is None:
                    skip_new_entries = True
                    _manage_exits(user_id, client, settings, pending_order_ids=set(), pending_fetch_ok=False)
                else:
                    pending_ids = {str(o.get("id")) for o in pending}
                    _confirm_pending_entries(user_id, client, settings, pending_ids)
                    _retry_pending_buys(user_id, client, settings, pending=pending)
                    _manage_exits(user_id, client, settings, pending_order_ids=pending_ids, pending_fetch_ok=True)

            if skip_new_entries or settings.stop_loss_only_mode:
                continue
            if _in_eod_window():
                current_equity = _get_current_equity(user_id, settings, client) if settings.equity_sizing_enabled else None
                _process_entries(user_id, client, settings, current_equity)
